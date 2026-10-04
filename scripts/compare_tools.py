#!/usr/bin/env python3
"""Finite resource, wakeup and ROS trace comparisons on the target Linux host."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET

try:
    from .comparison_common import ROOT, defer_interrupts, execute, fingerprint, launch, write_json
except ImportError:
    from comparison_common import ROOT, defer_interrupts, execute, fingerprint, launch, write_json
from perfkit.resources import parse_task_stat
from perfkit.runner import environment, install_signal_handler
from tests.integration_resource_profiles import PROGRAM
from tests.process_helpers import OwnedProcesses


def preflight(mode, output, cpu=None, pids=()):
    checks = []
    try:
        OwnedProcesses()  # No launch; validates creator cleanup capabilities.
        checks.append({'tool': 'safe-process-cleanup', 'available': True, 'reason': None})
    except RuntimeError as error:
        checks.append({'tool': 'safe-process-cleanup', 'available': False, 'reason': str(error)})
    if mode == 'wakeup':
        try:
            from scripts.wakeup_compare import _runtime_settings
            settings, _ = _runtime_settings(cpu)
            checks.append({'tool': 'wakeup-settings', 'available': True, 'reason': None, 'settings': settings})
        except (RuntimeError, ValueError, OSError) as error:
            checks.append({'tool': 'wakeup-settings', 'available': False, 'reason': str(error)})
    if pids:
        try:
            checks.append({'tool': 'selected-targets', 'available': True, 'reason': None,
                           'initial_state': target_state(pids)})
        except (ValueError, OSError) as error:
            checks.append({'tool': 'selected-targets', 'available': False, 'reason': str(error)})
    names = {'resource': ['pidstat'], 'wakeup': ['cyclictest'], 'trace': ['ros2', 'lttng', 'babeltrace']}[mode]
    if mode in ('wakeup', 'trace'):
        names += [str(ROOT / 'build' / ('periodic_bench' if mode == 'wakeup' else 'ros_bench'))]
    for index, name in enumerate(names):
        path = shutil.which(name)
        check = {'tool': name, 'available': bool(path), 'reason': None if path else 'executable missing'}
        if path:
            check.update(fingerprint(path))
        checks.append(check)
    # Executable existence is insufficient to establish tracing capability.
    if mode == 'trace' and all(check['available'] for check in checks):
        probe = "import ctypes,sys; lib=ctypes.CDLL('libtracetools.so'); f=lib.ros_trace_compile_status; f.restype=ctypes.c_bool; enabled=f(); print('Tracing enabled' if enabled else 'Tracing disabled'); sys.exit(0 if enabled else 1)"
        for label, command in [('tracetools', [sys.executable, '-c', probe]),
                               ('session-daemon', ['lttng', '--no-sessiond', '--mi=xml', 'list'])]:
            try:
                result = execute(command, output / label, 10)
                if label == 'session-daemon':
                    document = ET.fromstring((output / label / 'command.log').read_text())
                    active = document.findall('.//{*}session/{*}enabled')
                    if any(item.text == 'true' for item in active):
                        raise RuntimeError('other active trace sessions would confound the off/on comparison')
                checks.append({'tool': label, 'available': True, 'reason': None, 'result': result})
            except (RuntimeError, OSError, TimeoutError, ET.ParseError) as error:
                checks.append({'tool': label, 'available': False, 'reason': str(error)})
    for index, check in enumerate(list(checks)):
        if check.get('available') and check['tool'] in ('pidstat', 'cyclictest', 'lttng', 'babeltrace'):
            flag = '-V' if check['tool'] == 'pidstat' else '--help' if check['tool'] == 'cyclictest' else '--version'
            command = [check['path'], flag]
            # Version probe is evidence; some cyclictest versions return nonzero.
            try:
                execute(command, output / ('version-%d' % index), 10)
            except RuntimeError as error:
                check['version_probe_reason'] = str(error)
    if mode == 'trace':
        probe = "import ctypes,sys; lib=ctypes.CDLL('lib'+sys.argv[1]+'.so'); f=lib.rmw_get_implementation_identifier; f.restype=ctypes.c_char_p; actual=f(); print(actual.decode()); sys.exit(0 if actual==sys.argv[1].encode() else 1)"
        for rmw in ('rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp'):
            try:
                result = execute([sys.executable, '-c', probe, rmw], output / rmw, 10)
                checks.append({'tool': rmw, 'available': True, 'reason': None, 'result': result})
            except (RuntimeError, OSError, TimeoutError) as error:
                checks.append({'tool': rmw, 'available': False, 'reason': str(error)})
    return {'mode': mode, 'ready': all(check['available'] for check in checks), 'checks': checks}


def trace_event_counts(log, expected_pids):
    counts = {str(pid): {} for pid in expected_pids}
    for line in Path(log).read_text().splitlines():
        event = re.search(r'\b(ros2:[a-zA-Z0-9_]+):', line)
        pid = re.search(r'\bvpid\s*=\s*(\d+)\b', line)
        if event and pid and pid.group(1) in counts:
            events = counts[pid.group(1)]
            events[event.group(1)] = events.get(event.group(1), 0) + 1
    if not counts or any(not events for events in counts.values()):
        raise RuntimeError('decoded trace lacks ROS events for one or more benchmark PIDs')
    return counts


def target_state(pids):
    result = {}
    for pid in pids:
        row = parse_task_stat(Path('/proc', str(pid), 'stat').read_text())
        if row['pid'] != pid or row['state'] == 'Z':
            raise ValueError('target exited or identity unavailable')
        result[str(pid)] = row
    return result


def target_delta(before, after, elapsed):
    result = {}
    ticks = os.sysconf('SC_CLK_TCK')
    for pid, start in before.items():
        end = after[pid]
        if start['starttime_ticks'] != end['starttime_ticks']:
            raise ValueError('target PID reused: ' + pid)
        delta = end['utime_ticks'] + end['stime_ticks'] - start['utime_ticks'] - start['stime_ticks']
        if delta < 0:
            raise ValueError('target CPU counter reset')
        result[pid] = {'cpu_percent_one_core': 100 * delta / ticks / elapsed,
                       'rss_bytes_end_point': end['rss_pages'] * os.sysconf('SC_PAGE_SIZE'),
                       'threads_start': start['num_threads'], 'threads_end': end['num_threads']}
    return result


def resource_runs(output, seconds, repetitions, pids):
    owner = OwnedProcesses()
    records = []
    try:
        if not pids:
            child = launch(owner, [sys.executable, '-c', PROGRAM], stdout=None, stderr=None)
            pids = [child.pid]
            deadline = time.monotonic() + 5
            while target_state(pids)[str(child.pid)]['num_threads'] != 49:
                if time.monotonic() > deadline:
                    raise TimeoutError('49-thread owned fixture did not become ready')
                time.sleep(.05)
        identity = target_state(pids)
        for repeat in range(repetitions):
            order = ['none', 'monitor', 'pidstat']
            if repeat % 2:
                order.reverse()
            for position, tool in enumerate(order):
                folder = output / ('resource-%d-%s' % (repeat + 1, tool))
                before = target_state(pids)
                target_delta(identity, before, 1)  # Reject reuse between windows too.
                begin = time.monotonic_ns()
                if tool == 'none':
                    folder.mkdir()
                    time.sleep(seconds)
                    measured = {'cpu_seconds': None, 'cpu_percent_one_core': None,
                                'reason': 'no collector; parent wall sleep only'}
                elif tool == 'pidstat':
                    measured = execute(['pidstat', '-t', '-u', '-r', '-w', '-p',
                                        ','.join(map(str, pids)), '1', str(seconds)], folder, seconds + 20,
                                       env=dict(os.environ, LC_ALL='C'))
                else:
                    command = [sys.executable, '-m', 'perfkit.monitor', '--profile', 'full',
                               '--seconds', str(seconds), '--interval', '1', '--system-interval', '1',
                               '--process-interval', '1', '--thread-interval', '1', '--discovery-interval', '1',
                               '--active-cpu-percent', '0', '--max-targets', str(len(pids)),
                               '--skip-temperatures', '--all-users', '--output', str(folder / 'monitor')]
                    for pid in pids:
                        command += ['--pid', str(pid)]
                    measured = execute(command, folder, seconds + 30)
                after = target_state(pids)
                elapsed = (time.monotonic_ns() - begin) / 1e9
                row = dict(tool=tool, repetition=repeat + 1, order_position=position,
                           run_dir=str(folder), elapsed_seconds=elapsed,
                           target_endpoint_observation=target_delta(before, after, elapsed), command=measured)
                if tool == 'monitor':
                    row['monitor_summary'] = str(folder / 'monitor' / 'monitor-summary.json')
                    summary = json.loads(Path(row['monitor_summary']).read_text())
                    row.update(monitor_quality=summary['quality'], monitor_acceptance=summary['acceptance'],
                               monitor_observer=summary['resources']['observer'],
                               source_coverage=summary['resources']['source_coverage'],
                               collection_cost=summary['resources']['collection_cost'])
                    if summary['quality']['peak_registered_targets'] != len(pids):
                        raise RuntimeError('monitor did not register all comparison targets')
                records.append(row)
                write_json(output / 'runs.json', records)
        return records
    finally:
        with defer_interrupts():
            owner.cleanup()


def trace_runs(output, seconds, repetitions):
    records = []
    for repeat in range(repetitions):
        rmws = ['rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp']
        if repeat % 2:
            rmws.reverse()
        for rmw in rmws:
            order = [False, True] if repeat % 2 == 0 else [True, False]
            for enabled in order:
                folder = output / ('trace-%d-%s-%s' % (repeat + 1, rmw, 'on' if enabled else 'off'))
                folder.mkdir()
                session = 'rsp-' + uuid.uuid4().hex
                config = json.loads((ROOT / 'configs/tail-diagnostic.json').read_text())
                config.update(measurement_seconds=seconds, repetitions=1, sampling_mode='minimal')
                write_json(folder / 'config.json', config)
                created = False
                try:
                    if enabled:
                        created = True  # Also attempt own-name cleanup on partial creation.
                        commands = [
                            ['create', session, '--output', str(folder / 'ctf')],
                            ['enable-channel', '--userspace', '--session', session, 'ros'],
                            ['enable-event', '--userspace', '--session', session, '--channel', 'ros', 'ros2:*',
                             '--filter', '$ctx.procname == "ros_bench"'],
                            ['add-context', '--userspace', '--session', session, '--channel', 'ros',
                             '--type', 'vpid', '--type', 'vtid'],
                            ['start', session]]
                        for index, command in enumerate(commands):
                            execute(['lttng', '--no-sessiond', *command], folder / ('setup-%d' % index), 10)
                    result = execute([sys.executable, '-m', 'perfkit.runner', '--config', str(folder / 'config.json'),
                                      '--output', str(folder / 'benchmark')], folder / 'run', seconds + 60,
                                     env=dict(os.environ, RMW_IMPLEMENTATION=rmw), discover=True)
                    if enabled:
                        execute(['lttng', '--no-sessiond', 'list', session], folder / 'trace-status', 10)
                finally:
                    if created:
                        with defer_interrupts():
                            failures = []
                            for action in ('stop', 'list', 'destroy'):
                                try:
                                    execute(['lttng', '--no-sessiond', action, session], folder / action, 10)
                                except (RuntimeError, OSError, TimeoutError) as error:
                                    failures.append(str(error))
                            if failures:
                                raise RuntimeError('owned trace session cleanup failed: ' + '; '.join(failures))
                files = [path for path in (folder / 'ctf').rglob('*') if path.is_file()] if enabled else []
                if enabled and not any(path.name != 'metadata' and path.stat().st_size for path in files):
                    raise RuntimeError('tracing produced no event stream; capture unavailable')
                counts = None
                if enabled:
                    execute(['babeltrace', str(folder / 'ctf')], folder / 'decode', 60)
                    runtime = folder / 'benchmark' / 'C01' / '001'
                    pids = [json.loads((runtime / (role + '-runtime-start.json')).read_text())['pid']
                            for role in ('publisher', 'subscriber')]
                    counts = trace_event_counts(folder / 'decode' / 'command.log', pids)
                benchmark = json.loads((folder / 'benchmark' / 'summary.json').read_text())['results'][0]
                row = {'rmw': rmw, 'repetition': repeat + 1, 'own_trace_enabled': enabled,
                       'run_dir': str(folder), 'command': result,
                       'benchmark_summary': str(folder / 'benchmark' / 'summary.json'),
                       'benchmark_quality': benchmark['quality'], 'benchmark_acceptance': benchmark['acceptance'],
                       'trace_event_counts_by_benchmark_pid': counts, 'trace_lost_events': None,
                       'reason': 'ROS events validated by benchmark PID; loss and DDS joins require offline analysis'}
                records.append(row)
                write_json(output / 'runs.json', records)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('resource', 'wakeup', 'trace'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--pid', type=int, action='append', default=[])
    parser.add_argument('--cpu', type=int)
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('requires native target Linux; host/container scope must be recorded')
    if not 2 <= args.seconds <= 120 or not 1 <= args.repetitions <= 3:
        parser.error('seconds must be 2..120, repetitions 1..3; no automatic extensions')
    if len(set(args.pid)) != len(args.pid) or len(args.pid) > 256 or any(pid <= 0 for pid in args.pid):
        parser.error('at most 256 distinct positive PIDs')
    if (args.pid and args.mode != 'resource') or (args.cpu is not None and args.mode != 'wakeup'):
        parser.error('--pid applies to resource; --cpu applies to wakeup')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    install_signal_handler()
    status = {'status': 'running', 'mode': args.mode, 'seconds': args.seconds, 'repetitions': args.repetitions,
              'acceptance': None, 'reason': 'descriptive comparison; no configured business SLA/budget'}
    try:
        write_json(args.output / 'environment.json', environment(ROOT))
        write_json(args.output / 'source.json', [fingerprint(path) for directory in ('scripts', 'perfkit', 'tests')
                   for path in sorted((ROOT / directory).glob('*.py'))])
        checks = preflight(args.mode, args.output, args.cpu, args.pid)
        write_json(args.output / 'preflight.json', checks)
        if not checks['ready']:
            raise RuntimeError('tools/capability unavailable; see preflight.json and command.log')
        if not args.preflight:
            if args.mode == 'resource':
                resource_runs(args.output, args.seconds, args.repetitions, args.pid)
            elif args.mode == 'wakeup':
                try:
                    from .wakeup_compare import wakeup_runs
                except ImportError:
                    from wakeup_compare import wakeup_runs
                runs = wakeup_runs(args.output, args.seconds, args.repetitions, execute, args.cpu)
                write_json(args.output / 'runs.json', runs)
            else:
                trace_runs(args.output, args.seconds, args.repetitions)
        status['status'] = 'preflight_ready' if args.preflight else 'execution_completed'
    except BaseException as error:
        status.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        write_json(args.output / 'status.json', status)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, OSError, ValueError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
