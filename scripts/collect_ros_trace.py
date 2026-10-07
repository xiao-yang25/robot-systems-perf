#!/usr/bin/env python3
"""One bounded, PID-filtered ROS userspace trace of two test-owned C01 nodes.

Source-checkout tool, not a wheel entry point or a business E2E analyzer.
Requires an already running LTTng session daemon; never starts/stops that daemon.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

try:
    from .comparison_common import ROOT, defer_interrupts, launch
except ImportError:
    from comparison_common import ROOT, defer_interrupts, launch
from perfkit.ros_graph import _capture, validate_ros_environment
from perfkit.runner import install_signal_handler
from tests.process_helpers import OwnedProcesses

MAX_CTF_BYTES = 64 * 1024 * 1024
FIXTURE_GATE = """import os, sys, time
from pathlib import Path
end = time.monotonic() + 120
while not Path(sys.argv[1]).exists():
    if time.monotonic() > end:
        raise TimeoutError('release gate timed out')
    time.sleep(.01)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""
PROBE = '''import ctypes,json,os,sys
print(json.dumps({'python':sys.executable,'ros_distro':os.getenv('ROS_DISTRO')}),flush=True)
l=ctypes.CDLL('libtracetools.so'); l.ros_trace_compile_status.restype=ctypes.c_bool
r=ctypes.CDLL('lib'+sys.argv[1]+'.so'); r.rmw_get_implementation_identifier.restype=ctypes.c_char_p
paths=sorted(set(line.split()[-1] for line in open('/proc/self/maps') if 'libtracetools.so' in line))
print(json.dumps({'tracing_compiled':bool(l.ros_trace_compile_status()),'rmw':r.rmw_get_implementation_identifier().decode(),'tracetools_paths':paths}),flush=True)
'''


def save(path, value):
    """Atomic replacement inside our exclusively created result directory."""
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
    os.replace(temporary, path)


def digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def command(argv, folder, env, timeout=10):
    """Bound both execution time and combined raw output; record failures too."""
    folder.mkdir()
    owner = OwnedProcesses()
    record = {'argv': list(map(str, argv)), 'start_ns': time.monotonic_ns(),
              'returncode': None, 'error': None, 'identity': None, 'max_output_bytes': 4 * 1024 * 1024}
    save(folder/'command.json', record['argv'])
    primary = None
    try:
        with (folder/'stdout.bin').open('xb') as out, (folder/'stderr.bin').open('xb') as err:
            process = launch(owner, argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
            handle = owner.handle(process)
            record['identity'] = {'pid': handle.pid, 'starttime_ticks': handle.identity.starttime}
            try:
                failure, size = _capture(process, out, err, time.monotonic()+timeout)
                record.update(captured_bytes=size, returncode=process.returncode)
                if failure:
                    raise RuntimeError(failure)
                if process.returncode != 0:
                    raise RuntimeError('command exited '+str(process.returncode)+': '+str(folder/'stderr.bin'))
            finally:
                process.stdout.close(); process.stderr.close()
    except BaseException as error:
        primary = error
        record['error'] = type(error).__name__+': '+str(error)
        raise
    finally:
        with defer_interrupts():
            secondary = None
            try:
                owner.cleanup(timeout=1)
            except BaseException as error:
                record['cleanup_error'] = repr(error)
                secondary = error
            if record['identity'] is not None:
                record['returncode'] = process.returncode
            record['end_ns'] = time.monotonic_ns()
            try:
                save(folder/'result.json', record)
            except OSError as error:
                if primary is None and secondary is None:
                    raise
                print('command evidence save failed: '+str(error), file=sys.stderr)
            if secondary is not None and primary is None:
                raise secondary
    return (folder/'stdout.bin').read_text()


def preflight(args, output, env):
    result = {'ready': False, 'checks': [], 'tracing_compiled': None,
              'session_active': False, 'events_observed': False}
    def check(name, action):
        try:
            value = action()
            result['checks'].append({'name': name, 'available': True, 'value': value, 'reason': None})
            return value
        except (OSError, ValueError, RuntimeError, UnicodeError) as error:
            result['checks'].append({'name': name, 'available': False, 'reason': str(error)})
            return None
    for name in ('lttng', 'babeltrace'):
        def probe(name=name):
            path = shutil.which(name, path=env.get('PATH'))
            if not path:
                raise RuntimeError(name+' executable missing')
            text = command([path, '--version'], output/('probe-'+name), env)
            if name == 'lttng':
                valid = re.search(r'\(LTTng Trace Control\) 2\.', text)
            else:
                valid = re.search(r'(?:BabelTrace Trace Viewer and Converter|babeltrace(?:2)?)\s+(?:1|2)\.', text, re.I)
            if not valid:
                raise RuntimeError('unrecognized '+name+' version output')
            return {'path': str(Path(path).resolve()), 'sha256': digest(Path(path)), 'version_output': text}
        check(name, probe)
    def binary():
        path = args.ros_bench.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError('ros_bench executable missing or not executable')
        return {'path': str(path), 'sha256': digest(path)}
    check('ros_bench', binary)
    def runtime():
        text = command([args.ros_python, '-c', PROBE, args.rmw], output/'probe-sdk', env)
        rows = [json.loads(line) for line in text.splitlines()]
        if len(rows) != 2:
            raise ValueError('invalid SDK probe output')
        result['tracing_compiled'] = rows[1].get('tracing_compiled')
        save(output/'sdk-probe.json', rows)
        if rows[1].get('tracing_compiled') is not True:
            raise RuntimeError('selected SDK tracetools was compiled without tracing')
        if rows[1].get('rmw') != args.rmw:
            raise RuntimeError('actual RMW differs from requested RMW')
        paths = rows[1].get('tracetools_paths', [])
        if not paths:
            raise RuntimeError('loaded tracetools source path unavailable')
        if args.sdk_prefix:
            for path in paths:
                Path(path).resolve().relative_to(args.sdk_prefix.resolve())
        rows[1]['tracetools_files'] = [{'path': path, 'sha256': digest(Path(path))} for path in paths]
        save(output/'sdk-probe.json', rows)
        return rows
    check('sdk-runtime', runtime)
    if any(row['name'] == 'lttng' and row['available'] for row in result['checks']):
        check('existing-session-daemon', lambda: command(
            ['lttng', '--no-sessiond', 'list'], output/'probe-session-daemon', env))
    result['ready'] = all(row['available'] for row in result['checks'])
    save(output/'preflight.json', result)
    return result


def inventory(root):
    files = []
    if not root.exists():
        return files
    total = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('CTF evidence contains a symlink')
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_CTF_BYTES:
            raise ValueError('CTF evidence exceeds 64 MiB limit')
        files.append({'path': str(path.relative_to(root)), 'bytes': size, 'sha256': digest(path)})
    return files


def validate_events(text, identities, procname):
    counts = {str(row['pid']): {} for row in identities.values()}
    for line in text.splitlines():
        event = re.search(r'\b(ros2:[A-Za-z0-9_]+):', line)
        if not event:
            continue
        pid = re.search(r'\bvpid\s*=\s*(\d+)\b', line)
        if not pid or pid[1] not in counts:
            raise ValueError('ROS event missing owned vpid or outside selected PID filter')
        ns = re.search(r'\bpid_ns\s*=\s*(\d+)\b', line)
        name = re.search(r'\bprocname\s*=\s*"([^"]+)"', line)
        expected = next(row for row in identities.values() if str(row['pid']) == pid[1])
        if not ns or int(ns[1]) != expected['pid_namespace_inode'] or not name or name[1] != procname:
            raise ValueError('ROS event namespace/procname differs from selected fixture')
        bucket = counts[pid[1]]
        bucket[event[1]] = bucket.get(event[1], 0)+1
    for role, row in identities.items():
        expected = {'ros2:rcl_node_init'}
        expected |= {'ros2:rclcpp_publish'} if role == 'publisher' else {'ros2:callback_start', 'ros2:callback_end'}
        missing = expected - counts[str(row['pid'])].keys()
        if missing:
            raise ValueError(role+' lacks required real events: '+', '.join(sorted(missing)))
    return counts


def capture(args, output, env, state):
    owner = OwnedProcesses()
    processes, logs = {}, []
    session = 'rsp-'+uuid.uuid4().hex
    procname = 'rsp-'+uuid.uuid4().hex[:11]  # Linux comm limit: 15 visible bytes.
    binary = output/procname
    shutil.copyfile(args.ros_bench.resolve(), binary)
    binary.chmod(0o500)
    original = next(row['value'] for row in json.loads((output/'preflight.json').read_text())['checks'] if row['name']=='ros_bench')
    if digest(binary) != original['sha256']:
        raise ValueError('fixture binary changed since preflight')
    state['fixture_binary'] = {'path': str(binary), 'sha256': original['sha256'], 'procname_guard': procname}

    state['session_name'] = session
    attempted = False
    primary = None
    steps = 0
    def control(*argv):
        nonlocal steps
        steps += 1
        return command(['lttng', '--no-sessiond', *argv], output/('control-%02d'%steps), env)
    try:
        identities = {}
        for role in ('subscriber', 'publisher'):
            log = (output/(role+'.log')).open('xb'); logs.append(log)
            argv = [str(binary), '--role', role, '--count', str(args.seconds*100),
                    '--warmup', '0', '--payload-bytes', '64', '--depth', '100', '--reliability', 'reliable',
                    '--topic', '/rsp_trace_'+uuid.uuid4().hex, '--output', str(output/(role+'.csv')),
                    '--runtime-evidence', str(output/role), '--period-ns', '10000000',
                    '--callback-delay-ns', '0', '--ready-file', str(output/'subscriber.ready')]
            if role == 'subscriber':
                topic = argv[argv.index('--topic')+1]
            else:
                argv[argv.index('--topic')+1] = topic
            save(output/(role+'-command.json'), argv)
            # Extra gate keeps the owned PID alive before any ROS initialization.
            gate = [sys.executable, '-c', FIXTURE_GATE, str(output/(role+'.release')), *argv]
            processes[role] = launch(owner, gate, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
            handle = owner.handle(processes[role])
            identities[role] = {'pid': handle.pid, 'starttime_ticks': handle.identity.starttime,
                'observed_ns': time.monotonic_ns(), 'pid_namespace': os.readlink('/proc/'+str(handle.pid)+'/ns/pid'),
                'pid_namespace_inode': os.stat('/proc/'+str(handle.pid)+'/ns/pid').st_ino}
        save(output/'identities.json', identities)
        namespaces = {row['pid_namespace_inode'] for row in identities.values()}
        if len(namespaces) != 1:
            raise ValueError('fixtures do not share one verified PID namespace')
        expression = '('+' || '.join('$ctx.vpid == '+str(row['pid']) for row in identities.values())+')'
        expression += ' && $ctx.pid_ns == '+str(next(iter(namespaces)))+' && $ctx.procname == "'+procname+'"'
        state['event_filter'] = expression
        # Unique private session name; attempt cleanup even if create times out.
        attempted = True
        control('create', session, '--output', str(output/'ctf'))
        control('enable-channel', '--userspace', '--session', session, 'ros', '--subbuf-size', '262144', '--num-subbuf', '4')
        control('add-context', '--userspace', '--session', session, '--channel', 'ros', '--type', 'vpid', '--type', 'vtid', '--type', 'pid_ns', '--type', 'procname')
        control('enable-event', '--userspace', '--session', session, '--channel', 'ros', 'ros2:*', '--filter', expression)
        state['session_active'] = None  # Start may succeed server-side even if its CLI times out.
        control('start', session)
        state.update(session_active=True, capture_start_ns=time.monotonic_ns())
        save(output/'trace-status.json', state)
        for role in ('subscriber', 'publisher'):
            (output/(role+'.release')).write_text('release\n')
        deadline = time.monotonic()+args.seconds+20
        stopped_subscriber = False
        while True:
            # pidfd readiness does not reap; keep IDs reserved until trace stops.
            pub_done = not owner.handle(processes['publisher']).running()
            sub_done = not owner.handle(processes['subscriber']).running()
            if sub_done and not stopped_subscriber:
                raise RuntimeError('subscriber exited before controlled shutdown')
            if pub_done and not stopped_subscriber:
                time.sleep(.2)
                owner.signal(processes['subscriber'], signal.SIGINT)
                stopped_subscriber = True
            if pub_done and sub_done:
                break
            if time.monotonic() > deadline:
                raise TimeoutError('fixture exceeded bounded capture window')
            if (output/'ctf').exists() and sum(p.stat().st_size for p in (output/'ctf').rglob('*') if p.is_file()) > MAX_CTF_BYTES:
                raise RuntimeError('CTF exceeded 64 MiB during capture')
            time.sleep(.05)
    except BaseException as error:
        primary = error
        state['primary_error'] = type(error).__name__+': '+str(error)
    finally:
        with defer_interrupts():
            if attempted:
                for action in ('stop', 'list', 'destroy'):
                    try:
                        control(action, session)
                        if action in ('stop', 'destroy'):
                            state['session_active'] = False
                        if action == 'destroy':
                            state['session_destroyed'] = True
                    except BaseException as error:
                        state['cleanup_errors'].append(action+': '+repr(error))
            state['session_may_remain'] = bool(attempted and not state.get('session_destroyed'))
            if state['session_active'] is not False:
                state['session_active'] = None
                state['cleanup_errors'].append('session activity unconfirmed; manually inspect/clean only session '+session)
            state['capture_end_ns'] = time.monotonic_ns()
            try:
                for role, process in processes.items():
                    if process.returncode is None and not owner.handle(process).running():
                        process.wait(timeout=1)
                state['fixture_returncodes'] = {role: process.returncode for role, process in processes.items()}
                owner.cleanup(timeout=1)
            except BaseException as error:
                state['cleanup_errors'].append('fixture cleanup: '+repr(error))
            finally:
                for log in logs:
                    log.close()
    if primary:
        raise primary
    if state['cleanup_errors']:
        raise RuntimeError('cleanup failed; see trace-status.json')
    if set(state['fixture_returncodes']) != {'publisher', 'subscriber'} or any(state['fixture_returncodes'].values()):
        raise RuntimeError('fixture failed; see raw logs and returncodes')
    files = inventory(output/'ctf')
    save(output/'ctf-manifest.json', {'files': files, 'max_bytes': MAX_CTF_BYTES})
    if not any(Path(row['path']).name == 'metadata' and row['bytes'] for row in files) or not any(
            Path(row['path']).name != 'metadata' and row['bytes'] for row in files):
        raise RuntimeError('CTF metadata/event stream missing')
    decoded = command(['babeltrace', str(output/'ctf')], output/'decode', env, timeout=20)
    counts = validate_events(decoded, identities, procname)
    for role in identities:
        runtime = json.loads((output/(role+'-start.json')).read_text())
        if type(runtime.get('pid')) is not int or runtime['pid'] != identities[role]['pid'] or runtime.get('rmw_identifier', {}).get('value') != args.rmw:
            raise ValueError('fixture actual PID/RMW differs from requested environment')
        probe = json.loads((output/'sdk-probe.json').read_text())[1]
        maps = (output/(role+'-start.maps')).read_text()
        actual = {line.split()[-1] for line in maps.splitlines() if 'libtracetools.so' in line}
        expected = set(probe['tracetools_paths'])
        if actual != expected:
            raise ValueError('fixture loaded tracetools differs from probed SDK')
        if any(digest(Path(row['path'])) != row['sha256'] for row in probe['tracetools_files']):
            raise ValueError('tracetools file changed since preflight')
    state.update(events_observed=True, event_counts_by_pid=counts,
                 trace_lost_events=None, trace_lost_events_reason='not yet decoded from raw diagnostics/CTF packet context',
                 callback_latency=None, business_acceptance='not_evaluated')


def run(args):
    validate_ros_environment(args.rmw, str(args.sdk_prefix) if args.sdk_prefix else None)
    if not isinstance(args.ros_python, str) or not args.ros_python.strip() or len(args.ros_python)>4096 or '\x00' in args.ros_python:
        raise ValueError('ros_python must be an explicit nonempty interpreter path or name')
    if type(args.seconds) is not int or not 2 <= args.seconds <= 10:
        raise ValueError('seconds must be 2..10')
    if type(args.domain_id) is not int or not 0 <= args.domain_id <= 232:
        raise ValueError('domain_id must be 0..232')
    output = args.output.resolve()
    owned = False
    state = {'format_version': 1, 'kind': 'ros_trace_capture', 'status': 'running',
             'started_ns': time.monotonic_ns(), 'ended_ns': None, 'cleanup_errors': [],
             'tracing_compiled': None, 'session_active': False, 'events_observed': False,
             'error': None, 'primary_error': None, 'interruption_error': None, 'business_acceptance': 'not_evaluated', 'synthetic': True}
    code = 0
    try:
        with defer_interrupts():
            output.mkdir()  # Parent exists; never writes into a pre-existing directory.
            owned = True
            save(output/'trace-status.json', state)
        if platform.system() != 'Linux':
            raise RuntimeError('requires native Linux; Docker scope must be declared')
        env = dict(os.environ, ROS_DOMAIN_ID=str(args.domain_id), RMW_IMPLEMENTATION=args.rmw)
        save(output/'request.json', {'domain_id': args.domain_id, 'rmw': args.rmw, 'ros_python': args.ros_python,
             'sdk_prefix': str(args.sdk_prefix) if args.sdk_prefix else None, 'seconds': args.seconds,
             'view': args.view, 'temperature': {'status': 'skipped', 'reason': 'this tracing tool does not read thermal interfaces'}})
        save(output/'host.json', {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
             'pid_namespace': os.readlink('/proc/self/ns/pid'), 'clock': 'linux_monotonic',
             'ctf_clock_mapping': 'unconfirmed; preserve metadata, do not directly subtract CTF and host clocks',
             'kernel': platform.release(), 'machine': platform.machine(), 'view': args.view,
             'controller_python': sys.executable, 'script_sha256': digest(Path(__file__))})
        checks = preflight(args, output, env)
        state['tracing_compiled'] = checks['tracing_compiled']
        if not checks['ready']:
            raise RuntimeError('tracing preflight unavailable; see preflight.json and raw probe output')
        if args.preflight:
            state['status'] = 'preflight_ready'
        else:
            capture(args, output, env, state)
            state['status'] = 'events_observed'
    except BaseException as error:
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
        if state['primary_error'] is None:
            state['primary_error'] = type(error).__name__+': '+str(error)
        if code == 130:
            state['interruption_error'] = repr(error)
        state.update(status='interrupted' if code == 130 else 'failed', error=type(error).__name__+': '+str(error))
    finally:
        if owned:
            def persist():
                try:
                    save(output/'trace-status.json', state)
                    return True
                except OSError as error:
                    state['cleanup_errors'].append('final state save: '+repr(error))
                    print('final state save failed: '+str(error), file=sys.stderr)
                    return False
            try:
                with defer_interrupts():
                    if (output/'ctf').exists():
                        try:
                            save(output/'ctf-manifest.json', {'files': inventory(output/'ctf'), 'max_bytes': MAX_CTF_BYTES})
                        except (OSError, ValueError) as error:
                            state['cleanup_errors'].append('CTF manifest: '+repr(error))
                            if not code:
                                code = 1
                                state.update(status='failed', error='CTF manifest failed')
                    state['ended_ns'] = time.monotonic_ns()
                    if not persist():
                        if not code:
                            code = 1
                            state.update(status='failed', error='final state save failed')
                        persist()  # One independent terminal-save attempt; never regenerate reports.
            except KeyboardInterrupt as error:
                code = 130
                state.update(status='interrupted', error=repr(error), interruption_error=repr(error), ended_ns=time.monotonic_ns())
                # Save interrupted independently of any derived-output failures.
                with defer_interrupts():
                    persist()
    if state['error']:
        print(state['error'], file=sys.stderr)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ros-python', required=True)
    parser.add_argument('--rmw', required=True)
    parser.add_argument('--sdk-prefix', type=Path)
    parser.add_argument('--domain-id', type=int, required=True)
    parser.add_argument('--ros-bench', type=Path, default=ROOT/'build/ros_bench')
    parser.add_argument('--seconds', type=int, default=5)
    parser.add_argument('--view', choices=('host', 'container'), required=True)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    install_signal_handler()
    return run(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
