"""Finite offline installed-wheel regression; fixture tests own only their objects."""
import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from perfkit.lifecycle import defer_interrupts
from perfkit.ros_evidence import run_ros_evidence
from perfkit.ros_graph import validate_graph_request, validate_ros_environment
from tests.integration_intake import verify as verify_intake
from tests.integration_workload import verify as verify_workload
from tests.integration_ros_evidence import verify as verify_ros


def wheel_identity(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError('one wheel metadata record required')
        metadata = Parser().parsestr(archive.read(names[0]).decode())
        if metadata['Name'] != 'robot-systems-perf' or not metadata['Version']:
            raise ValueError('robot-systems-perf wheel required')
        # The preflight API and regression scripts belong to this source checkout.
        # Refuse a different wheel rather than testing source and labelling it installed.
        modules = {name: archive.read(name) for name in archive.namelist()
                   if name.startswith('perfkit/') and name.endswith('.py')}
        source = Path(__file__).resolve().parents[1]
        expected = {'perfkit/' + p.name for p in (source/'perfkit').glob('*.py')}
        if set(modules) != expected or any((source/name).read_bytes() != value for name, value in modules.items()):
            raise ValueError('wheel modules must match this regression source checkout')
    return {'version': metadata['Version'], 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
            'preflight_source_matches_wheel': True}


def run(args):
    if args.ros_fixture and (not args.preflight or args.component_prefix is None or args.container_binary is None):
        raise ValueError('ROS fixture requires preflight, prepared component prefix and container binary')
    if not args.preflight and (args.sdk_prefix is not None or args.rmw is not None or args.component_manager):
        raise ValueError('SDK/RMW/component selection requires preflight')
    if args.preflight:
        validate_graph_request(args.domain_id, args.ros_python, 0, args.query_timeout, args.component_manager)
        validate_ros_environment(args.rmw, args.sdk_prefix)
    identity = wheel_identity(args.wheel)
    args.output.mkdir(parents=True, exist_ok=False)
    state = dict(identity, status='running', started_ns=time.monotonic_ns(), stages={},
                 ros_fixture='not_requested', business_acceptance='not_evaluated')
    def save():
        (args.output/'device-regression-status.json').write_text(json.dumps(state, indent=2)+'\n')
    primary = None
    try:
        save()
        for name, check in (('intake', verify_intake), ('workload', verify_workload)):
            stage = args.output/name; stage.mkdir()
            state['stages'][name] = 'running'; save()
            check(args.wheel, stage)
            state['stages'][name] = 'passed'; save()
        if args.preflight:
            state['stages']['ros_preflight'] = 'running'; save()
            result = run_ros_evidence(None, args.output/'ros-preflight', preflight=True,
                domain_id=args.domain_id, ros_python=args.ros_python, wait_seconds=0,
                timeout_seconds=args.query_timeout, component_managers=args.component_manager,
                rmw=args.rmw, sdk_prefix=args.sdk_prefix)
            state['stages']['ros_preflight'] = 'passed'; state['ros_runtime'] = result['source']; save()
        if args.ros_fixture:
            # The caller preloaded the SDK; the wrapper selects the same interpreter.
            if args.rmw is not None:
                os.environ['RMW_IMPLEMENTATION'] = args.rmw
            stage = args.output/'ros-fixture'; stage.mkdir()
            state['stages']['ros_fixture'] = 'running'; save()
            verify_ros(args.wheel, stage, args.component_prefix, args.container_binary, args.ros_python, sdk_prefix=args.sdk_prefix)
            state['stages']['ros_fixture'] = 'passed'; state['ros_fixture'] = 'passed'
        state['status'] = 'complete'
    except BaseException as error:
        primary = error
        state.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                     error_type=type(error).__name__, error=str(error))
        raise
    finally:
        state['finished_ns'] = time.monotonic_ns()
        try:
            with defer_interrupts():
                save()
        except BaseException as cleanup:
            unhandled = primary is None
            if unhandled:
                state.update(status='interrupted' if isinstance(cleanup, KeyboardInterrupt) else 'failed',
                             error_type=type(cleanup).__name__, error=str(cleanup))
            else:
                state.setdefault('cleanup_errors', []).append(
                    {'stage': 'final_status_write', 'error_type': type(cleanup).__name__, 'error': str(cleanup)})
            try:
                with defer_interrupts():
                    save()
            except BaseException as retry:
                if hasattr(cleanup, 'add_note'):
                    cleanup.add_note('regression status retry failed: ' + repr(retry))
            if unhandled:
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--sdk-prefix', type=Path)
    parser.add_argument('--ros-python', default=sys.executable)
    parser.add_argument('--rmw')
    parser.add_argument('--domain-id', type=int, default=0)
    parser.add_argument('--component-manager', action='append', default=[])
    parser.add_argument('--query-timeout', type=float, default=10)
    parser.add_argument('--ros-fixture', action='store_true')
    parser.add_argument('--component-prefix', type=Path)
    parser.add_argument('--container-binary')
    args = parser.parse_args(); args.wheel = args.wheel.resolve(); args.output = args.output.resolve()
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise KeyboardInterrupt('device regression interrupted by SIGTERM')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, AssertionError) as error:
        print('Device regression failed:', error, file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    print('Finite device regression passed:', args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
