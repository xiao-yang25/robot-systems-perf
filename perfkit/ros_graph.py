"""Bounded, explicitly requested ROS graph query; this module needs only stdlib.

The caller owns run status. This helper owns only its direct adapter child, and
requires that no other thread or SIGCHLD handler reaps that child.
The caller must also leave native SIGCHLD auto-reap flags (SA_NOCLDWAIT) unset.
Python's getsignal guard cannot inspect native flags or detect external reapers.
"""
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from .lifecycle import defer_interrupts


MAX_OUTPUT_BYTES = 4 * 1024 * 1024
LIMITATIONS = [
    'Visible ROS graph entities may belong to remote hosts.',
    'ROS graph names and endpoint GIDs do not establish a local PID mapping.',
    'This is a point query; graph reads are not atomic and discovery may be incomplete.',
    'An empty observation does not establish that no business workload exists.',
]


def validate_graph_request(domain_id, python_executable, wait_seconds, timeout_seconds,
                           component_managers=()):
    if type(domain_id) is not int or not 0 <= domain_id <= 232:
        raise ValueError('domain_id must be an integer in 0..232')
    for name, value in (('wait_seconds', wait_seconds), ('timeout_seconds', timeout_seconds)):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(name + ' must be a finite number')
    if not 0 <= wait_seconds <= 30:
        raise ValueError('wait_seconds must be in 0..30')
    if not wait_seconds < timeout_seconds <= 60:
        raise ValueError('timeout_seconds must exceed wait_seconds and be at most 60')
    if (not isinstance(python_executable, str) or not python_executable.strip() or
            len(python_executable) > 4096 or '\x00' in python_executable):
        raise ValueError('python_executable must be an explicit nonempty executable path or name')
    if not isinstance(component_managers, (tuple, list)) or len(component_managers) > 16:
        raise ValueError('component_managers must be a list or tuple of at most 16 names')
    for manager in component_managers:
        if (not isinstance(manager, str) or len(manager) > 512 or not re.fullmatch(
                r'/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*', manager)):
            raise ValueError('component_managers must contain supported absolute ROS node names')
    if len(set(component_managers)) != len(component_managers):
        raise ValueError('component_managers must not contain duplicates')


def validate_ros_environment(rmw=None, sdk_prefix=None):
    if rmw is not None and (not isinstance(rmw, str) or len(rmw) > 200 or not re.fullmatch(r'rmw_[A-Za-z0-9_]+', rmw)):
        raise ValueError('rmw must be an explicit rmw implementation identifier')
    if sdk_prefix is not None:
        path = Path(sdk_prefix)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('sdk_prefix must be an absolute existing SDK directory')


def _failure(domain_id, start_ns, reason):
    return {'format_version': 1, 'kind': 'ros_graph_snapshot', 'status': 'failed',
            'reason': reason, 'domain_id': domain_id,
            'query_window': {'start_ns': start_ns, 'end_ns': time.monotonic_ns()},
            'source': {'adapter': 'rclpy', 'ros_distro': None, 'rmw': None,
                       'python_version': None}, 'nodes': [], 'topics': [], 'components': [],
            'limitations': list(LIMITATIONS)}


def _stop_owned(process):
    # Direct child, no other reaper: Popen's unreaped-child ownership is the
    # authority to signal. Never signal any graph/discovered numeric PID.
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _capture(process, stdout_file, stderr_file, deadline):
    size = 0
    reason = None
    with selectors.DefaultSelector() as selector:
        for pipe, target in ((process.stdout, stdout_file), (process.stderr, stderr_file)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, target)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = 'adapter exceeded global timeout'
                break
            for key, _ in selector.select(min(remaining, .1)):
                chunk = os.read(key.fileobj.fileno(), min(65536, MAX_OUTPUT_BYTES - size + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = MAX_OUTPUT_BYTES - size
                key.data.write(chunk[:available])
                size += len(chunk[:available])
                if len(chunk) > available:
                    return 'adapter output exceeded 4 MiB total stdout/stderr limit', size
        if reason is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = 'adapter exceeded global timeout'
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    reason = 'adapter exceeded global timeout'
    return reason, size


def _validate_snapshot(value, domain_id, managers):
    if not isinstance(value, dict) or type(value.get('format_version')) is not int or value['format_version'] != 1:
        raise ValueError('unsupported adapter format_version')
    if value.get('kind') != 'ros_graph_snapshot' or value.get('domain_id') != domain_id:
        raise ValueError('adapter kind/domain mismatch')
    if value.get('status') not in ('observed', 'empty', 'unavailable', 'failed'):
        raise ValueError('invalid adapter status')
    if 'reason' not in value or value['reason'] is not None and not isinstance(value['reason'], str):
        raise ValueError('invalid adapter reason')
    window = value.get('query_window')
    if (not isinstance(window, dict) or type(window.get('start_ns')) is not int or
            type(window.get('end_ns')) is not int or not 0 <= window['start_ns'] <= window['end_ns']):
        raise ValueError('invalid adapter query_window')
    source = value.get('source')
    if not isinstance(source, dict) or source.get('adapter') != 'rclpy' or any(
            key not in source or source[key] is not None and not isinstance(source[key], str)
            for key in ('ros_distro', 'rmw', 'python_version')):
        raise ValueError('invalid adapter source')
    if not isinstance(value.get('limitations'), list) or not all(
            isinstance(item, str) for item in value['limitations']):
        raise ValueError('invalid adapter limitations')
    def node(row, name_key='name', namespace_key='namespace'):
        return (isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key]
                for key in (name_key, namespace_key, 'full_name')) and
                row['full_name'] == row[namespace_key].rstrip('/') + '/' + row[name_key])
    def endpoint(row):
        if not node(row, 'node_name', 'node_namespace') or not isinstance(row.get('qos'), dict):
            return False
        gid = row.get('endpoint_gid')
        if not isinstance(gid, str) or not re.fullmatch(r'(?:[0-9a-f]{2}){1,128}', gid):
            return False
        qos = row['qos']
        for key in ('history', 'reliability', 'durability', 'liveliness'):
            policy = qos.get(key)
            if not isinstance(policy, dict) or not isinstance(policy.get('name'), str) or type(policy.get('value')) is not int:
                return False
        if type(qos.get('reported_depth')) is not int or qos['reported_depth'] < 0:
            return False
        if qos['history']['name'] == 'UNKNOWN':
            if qos.get('depth') is not None or qos.get('depth_reason') != 'RMW graph does not expose queue depth':
                return False
        elif (type(qos.get('depth')) is not int or qos['depth'] < 0 or
              qos['depth'] != qos['reported_depth'] or qos.get('depth_reason') is not None):
            return False
        return (isinstance(qos.get('scope'), str) and bool(qos['scope']) and
                all(type(qos.get(key)) is int for key in
                    ('deadline_ns', 'lifespan_ns', 'liveliness_lease_duration_ns')) and
                type(qos.get('avoid_ros_namespace_conventions')) is bool)
    if not isinstance(value.get('nodes'), list) or not all(node(row) for row in value['nodes']):
        raise ValueError('invalid adapter nodes')
    if not isinstance(value.get('topics'), list):
        raise ValueError('invalid adapter topics')
    for topic in value['topics']:
        if (not isinstance(topic, dict) or not isinstance(topic.get('name'), str) or
                not isinstance(topic.get('types'), list) or not all(
                    isinstance(item, str) for item in topic['types'])):
            raise ValueError('invalid adapter topic')
        for role in ('publishers', 'subscriptions'):
            endpoints = topic.get(role)
            if endpoints is None and value['status'] == 'failed':
                continue
            if not isinstance(endpoints, list) or not all(endpoint(row) for row in endpoints):
                raise ValueError('invalid adapter endpoint')
    components = value.get('components')
    if not isinstance(components, list) or [row.get('manager') if isinstance(row, dict) else None
                                          for row in components] != list(managers):
        raise ValueError('invalid adapter component manager results')
    for row in components:
        if row.get('status') not in ('observed', 'unavailable', 'failed') or 'reason' not in row:
            raise ValueError('invalid adapter component status')
        if row.get('nodes') is None and row['status'] != 'observed':
            continue
        if not isinstance(row.get('nodes'), list) or not all(isinstance(item, dict) and
                isinstance(item.get('full_name'), str) and type(item.get('unique_id')) is int
                for item in row['nodes']):
            raise ValueError('invalid adapter component nodes')
    if value['status'] in ('observed', 'empty'):
        if any(not source[key] for key in ('rmw', 'python_version')):
            raise ValueError('successful adapter query lacks actual runtime source')
        if any(row['status'] != 'observed' for row in components):
            raise ValueError('adapter reported success with incomplete component queries')
        seen = bool(value['nodes'] or value['topics'] or any(row['nodes'] for row in components))
        if (value['status'] == 'observed') != seen:
            raise ValueError('adapter status does not match graph observation')
    return value


def collect_graph(output: Path, domain_id: int, python_executable: str,
                  wait_seconds: float = 2, timeout_seconds: float = 10,
                  component_managers=(), *, rmw=None, sdk_prefix=None) -> dict:
    """Query once; output must exist. Invalid requests launch no process.

    Exceptions from host I/O or cancellation propagate after bounded cleanup.
    Protocol, timeout and adapter failures return evidence instead of success.
    """
    validate_graph_request(domain_id, python_executable, wait_seconds, timeout_seconds,
                           component_managers)
    validate_ros_environment(rmw, sdk_prefix)
    # Ignored/custom SIGCHLD can reap the child before Popen observes its exit,
    # causing Popen to substitute returncode=0. Refuse that state before writing
    # query evidence or launching anything; never replace global handlers here.
    if not hasattr(signal, 'SIGCHLD') or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise ValueError('graph query requires default SIGCHLD and exclusive child reaping')
    output = Path(output)
    if not output.is_dir():
        raise ValueError('output must be an existing directory')
    evidence = output / 'ros-graph-query'
    evidence.mkdir()
    start_ns = time.monotonic_ns()
    command = [python_executable, str(Path(__file__).with_name('ros_graph_adapter.py').resolve()),
               '--domain-id', str(domain_id), '--wait-seconds', str(wait_seconds),
               '--timeout-seconds', str(timeout_seconds)]
    for manager in component_managers:
        command += ['--component-manager', manager]
    if rmw is not None:
        command += ['--rmw', rmw]
    environment = dict(os.environ, ROS_DOMAIN_ID=str(domain_id))
    process = None
    original = None
    failure = None
    captured_bytes = 0
    # Opening raw files precedes spawn so an output failure never leaks a child.
    with (evidence / 'stdout.bin').open('xb') as stdout_file, (evidence / 'stderr.bin').open('xb') as stderr_file:
        try:
            deadline = time.monotonic() + timeout_seconds
            # The child shares this host's monotonic clock. An absolute budget
            # includes interpreter/import time, rather than restarting timeout
            # inside the adapter after process startup.
            command += ['--deadline-ns', str(int(deadline * 1000000000))]
            with defer_interrupts():
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, env=environment, shell=False)
            failure, captured_bytes = _capture(process, stdout_file, stderr_file, deadline)
        except BaseException as error:
            original = error
            raise
        finally:
            if process is not None:
                try:
                    with defer_interrupts():
                        _stop_owned(process)
                except BaseException as cleanup_error:
                    if original is None:
                        raise
                    if hasattr(original, 'add_note'):
                        original.add_note('adapter cleanup error: ' + repr(cleanup_error))
                finally:
                    for pipe in (process.stdout, process.stderr):
                        pipe.close()
    if failure is None and process.returncode != 0:
        failure = 'adapter exited with code ' + str(process.returncode)
    if failure is None:
        try:
            def reject_constant(constant):
                raise ValueError('nonfinite JSON constant: ' + constant)
            value = json.loads((evidence / 'stdout.bin').read_bytes(), parse_constant=reject_constant)
            value = _validate_snapshot(value, domain_id, component_managers)
        except (ValueError, UnicodeError) as error:
            failure = 'invalid adapter JSON: ' + str(error)
    result = _failure(domain_id, start_ns, failure) if failure else value
    if failure:
        result['components'] = [{'manager': manager, 'status': 'failed',
                                 'reason': failure, 'nodes': None} for manager in component_managers]
    if not failure and rmw is not None and result['source']['rmw'] != rmw:
        result.update(status='failed', reason='actual RMW does not match explicitly requested RMW')
    if not failure and sdk_prefix is not None:
        module = result['source'].get('rclpy_module')
        try:
            if not isinstance(module, str) or not Path(module).is_absolute():
                raise ValueError('actual rclpy module path unavailable')
            Path(module).resolve().relative_to(Path(sdk_prefix).resolve())
        except ValueError:
            result.update(status='failed', reason='actual rclpy module is not within requested SDK prefix')
    result['environment_request'] = {'rmw': rmw, 'sdk_prefix': str(sdk_prefix) if sdk_prefix else None}
    result['limitations'] = list(dict.fromkeys(result['limitations'] + LIMITATIONS))
    result['query_evidence'] = {'stdout': 'ros-graph-query/stdout.bin',
        'stderr': 'ros-graph-query/stderr.bin', 'captured_bytes': captured_bytes,
        'max_output_bytes': MAX_OUTPUT_BYTES, 'returncode': process.returncode,
        'host_window': {'start_ns': start_ns, 'end_ns': time.monotonic_ns()},
        'timeout_seconds': timeout_seconds, 'wait_seconds': wait_seconds}
    return result
