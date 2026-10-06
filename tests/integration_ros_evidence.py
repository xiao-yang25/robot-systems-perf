"""Installed M2b against test-owned ROS nodes and a real component container.

Requires an existing ROS SDK and the local tests/fixtures/ros_components build.
LoadNode is used only here, against this test's newly created container.
"""
import argparse
from email.parser import Parser
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import venv
import uuid
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.comparison_common import launch
from tests.process_helpers import OwnedProcesses
from perfkit.lifecycle import defer_interrupts


NODE_PROGRAM = '''import rclpy,sys
rclpy.init(args=[])
node=rclpy.create_node(sys.argv[1],namespace=sys.argv[2])
try: rclpy.spin(node)
finally: node.destroy_node(); rclpy.shutdown()
'''


def _verify_qos(qos, role):
    assert qos['reliability']['name'] == 'RELIABLE', (role, qos)
    assert type(qos['reported_depth']) is int and qos['reported_depth'] >= 0
    if qos['history']['name'] == 'UNKNOWN':
        assert qos['depth'] is None
        assert qos['depth_reason'] == 'RMW graph does not expose queue depth'
    else:
        assert qos['history']['name'] == 'KEEP_LAST', 'fixture configures KEEP_LAST'
        assert type(qos['depth']) is int and qos['depth'] == qos['reported_depth']
        assert qos['depth_reason'] is None
        assert qos['depth'] == 3, 'fixture configures KEEP_LAST depth 3'


def verify_endpoint_qos(topic):
    """Fixture reliability is known; graph queue-depth availability is RMW-specific."""
    for role in ('publishers', 'subscriptions'):
        assert len(topic[role]) == 2, role
        for endpoint in topic[role]:
            _verify_qos(endpoint['qos'], role)


def fixture_graph_diagnostics(snapshot, namespace, manager, *, duplicate=False):
    """One snapshot must satisfy the contract; never combine partial snapshots."""
    expected_names = {namespace+'/'+name: (2 if duplicate and name == 'first' else 1)
                      for name in ('first', 'second', 'standalone')}
    names = [node['full_name'] for node in snapshot['nodes']]
    data_topics = [topic for topic in snapshot['topics'] if topic['name'] == namespace+'/data']
    components = [row for row in snapshot['components'] if row['manager'] == manager]
    expected_components = sorted([namespace+'/first', namespace+'/second'])
    actual_components = sorted(node['full_name'] for row in components for node in (row['nodes'] or []))
    incomplete, invalid = [], []
    if snapshot['status'] not in ('observed', 'empty'):
        invalid.append('query status: '+snapshot['status']+': '+str(snapshot.get('reason')))
    for name, count in expected_names.items():
        observed = names.count(name)
        if observed < count: incomplete.append('missing node occurrences: '+name)
        if observed > count: invalid.append('unexpected duplicate node: '+name)
    if not components:
        incomplete.append('component manager missing')
    elif len(components) != 1 or components[0]['status'] != 'observed':
        invalid.append('component query not observed exactly once')
    elif actual_components != expected_components:
        if len(actual_components) == len(set(actual_components)) and set(actual_components) < set(expected_components):
            incomplete.append('component list incomplete')
        else:
            invalid.append('unexpected component list')
    if not data_topics:
        incomplete.append('data topic missing')
    elif len(data_topics) != 1:
        invalid.append('duplicate data topic')
    else:
        topic = data_topics[0]
        for role in ('publishers', 'subscriptions'):
            endpoints = topic[role]
            if endpoints is None:
                invalid.append('endpoint query unavailable: '+role)
                continue
            endpoint_names = sorted(endpoint['full_name'] for endpoint in endpoints)
            for endpoint in endpoints:
                try:
                    _verify_qos(endpoint['qos'], role)
                except AssertionError as error:
                    invalid.append('QoS mismatch ('+role+'): '+str(error))
            if endpoint_names != expected_components:
                if len(endpoint_names) == len(set(endpoint_names)) and set(endpoint_names) < set(expected_components):
                    incomplete.append('endpoints incomplete: '+role)
                else:
                    invalid.append('unexpected endpoints: '+role)
    return {'ready': not incomplete and not invalid, 'incomplete': incomplete, 'invalid': invalid,
            'expected': {'node_occurrences': expected_names, 'component_nodes': expected_components,
                         'data_topic': namespace+'/data', 'endpoint_nodes_per_role': expected_components,
                         'qos': {'reliability': 'RELIABLE', 'known_history': 'KEEP_LAST', 'known_depth': 3}},
            'actual': {'status': snapshot['status'], 'reason': snapshot.get('reason'),
                       'node_names': names, 'components': snapshot['components'], 'data_topics': data_topics},
            'query_window': snapshot['query_window'],
            'host_window': snapshot.get('query_evidence', {}).get('host_window')}


def wait_for_fixture_graph(query, output, namespace, manager, *, name='graph', duplicate=False,
                           max_attempts=3, timeout_seconds=20, clock=time.monotonic):
    """Retry only successful-but-incomplete fixture observations within one budget.

    query(folder, budget) must bound and reap its child. The caller supplies the
    installed CLI; its separate exit/cleanup grace is not a readiness extension.
    """
    if (type(max_attempts) is not int or not 1 <= max_attempts <= 3 or
            type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or
            not 2 < timeout_seconds <= 20):
        raise ValueError('fixture requires 1..3 attempts and a finite total budget in (2,20] seconds')
    started = clock(); deadline = started + timeout_seconds
    report = output / (name+'-readiness.json')
    state = {'status': 'running', 'max_attempts': max_attempts, 'budget_seconds': timeout_seconds,
             'started_monotonic': started, 'attempts': [], 'selected_output': None}
    with report.open('x') as stream:
        json.dump(state, stream, indent=2); stream.write('\n')
    def save():
        with defer_interrupts():
            report.write_text(json.dumps(state, indent=2)+'\n')
    try:
        for index in range(1, max_attempts+1):
            remaining = deadline-clock()
            if remaining <= 2:
                raise AssertionError('fixture readiness budget has insufficient time for graph-wait=2')
            folder = output / (name if index == 1 else name+'-'+str(index).zfill(3))
            attempt = {'index': index, 'output': folder.name, 'query_budget_seconds': min(10, remaining),
                       'status': 'querying'}
            state['attempts'].append(attempt); save()
            # Record I/O consumes the same phase budget; do not launch using
            # the stale allowance calculated before saving the attempt.
            remaining = deadline-clock()
            if remaining <= 2:
                attempt['status'] = 'not_started'
                raise AssertionError('fixture readiness budget exhausted before query launch')
            attempt['query_budget_seconds'] = min(10, remaining)
            snapshot = query(folder, attempt['query_budget_seconds'])
            diagnostic = fixture_graph_diagnostics(snapshot, namespace, manager, duplicate=duplicate)
            attempt.update(status='ready' if diagnostic['ready'] else 'incomplete', diagnostic=diagnostic)
            if diagnostic['invalid']:
                attempt['status'] = 'invalid'
                raise AssertionError('invalid fixture graph: '+json.dumps(diagnostic, sort_keys=True))
            attempt['evaluated_monotonic'] = clock()
            if attempt['evaluated_monotonic'] >= deadline:
                attempt['status'] = 'late'
                raise AssertionError('fixture readiness deadline exceeded: '+json.dumps(diagnostic, sort_keys=True))
            save()
            if diagnostic['ready']:
                state.update(status='ready', selected_output=folder.name, finished_monotonic=clock(),
                             recovered_incomplete=index > 1)
                save()
                return folder, snapshot
        raise AssertionError('fixture graph incomplete after bounded attempts: '+
                             json.dumps(state['attempts'][-1]['diagnostic'], sort_keys=True))
    except BaseException as error:
        state.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                     finished_monotonic=clock(), error_type=type(error).__name__, error=str(error))
        if state['attempts'] and state['attempts'][-1]['status'] == 'querying':
            state['attempts'][-1].update(status='query_failed', error_type=type(error).__name__, error=str(error))
        try:
            save()
        except BaseException as cleanup:
            if hasattr(error, 'add_note'):
                error.add_note('fixture readiness record write failed: '+repr(cleanup))
        raise


def load_components(manager, namespace):
    import rclpy
    from composition_interfaces.srv import LoadNode
    rclpy.init(args=[])
    node = rclpy.create_node('perfkit_test_loader', namespace=namespace, enable_rosout=False)
    client = node.create_client(LoadNode, manager + '/_container/load_node')
    try:
        assert client.wait_for_service(timeout_sec=10)
        for name in ('first', 'second'):
            request = LoadNode.Request()
            request.package_name = 'perfkit_test_components'
            request.plugin_name = 'perfkit_test::FixtureNode'
            request.node_name = name
            request.node_namespace = namespace
            future = client.call_async(request)
            rclpy.spin_until_future_complete(node, future, timeout_sec=10)
            assert future.done() and future.result().success, future.result()
    finally:
        node.destroy_node()
        rclpy.shutdown()


def verify(wheel, output, component_prefix, container_binary, ros_python, *, sdk_prefix=None, skip_temperature=False):
    owner = OwnedProcesses()
    with tempfile.TemporaryDirectory(prefix='m2b-install-') as temporary:
        root = Path(temporary); prefix = root/'venv'; cwd = root/'unrelated'; cwd.mkdir()
        venv.EnvBuilder(with_pip=False).create(prefix)
        pip_spec = importlib.util.find_spec('pip')
        if pip_spec is None:
            raise RuntimeError('prepared offline pip bootstrap required')
        env = dict(os.environ)
        sdk_pythonpath = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = str(Path(pip_spec.origin).parent.parent)
        install = subprocess.run([str(prefix/'bin/python'), '-m', 'pip', '--isolated', 'install', '--no-index',
            '--disable-pip-version-check', str(wheel.resolve())], env=env, capture_output=True, text=True, timeout=30)
        (output/'install.log').write_text(install.stdout + install.stderr)
        assert install.returncode == 0
        # Retain SDK paths needed by the separate ROS interpreter, never the project root.
        source_root = Path(__file__).resolve().parents[1]
        env['PYTHONPATH'] = ':'.join(item for item in sdk_pythonpath.split(':') if item and
            Path(item).resolve() != source_root and not item.endswith('pip.zip'))
        env.pop('EP_SOURCE_REVISION', None)
        env['ROS_DOMAIN_ID'] = '77'
        env['AMENT_PREFIX_PATH'] = str(component_prefix.resolve()) + ':' + env.get('AMENT_PREFIX_PATH', '')
        os.environ['ROS_DOMAIN_ID'] = env['ROS_DOMAIN_ID']  # Test-only loader, same isolated domain.
        os.environ['AMENT_PREFIX_PATH'] = env['AMENT_PREFIX_PATH']
        logs = []
        def start(command, name):
            log = (output/(name+'.log')).open('x'); logs.append(log)
            return launch(owner, command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        def run(command, name, expected=0, *, timeout_seconds=20):
            process = start(command, name)
            actual = process.wait(timeout=timeout_seconds)
            assert actual == expected, {'stage': name, 'expected_exit': expected,
                                        'actual_exit': actual, 'log': name+'.log'}
            assert not Path('/proc', str(process.pid)).exists()
            return process
        namespace = '/perfkit_fixture_' + uuid.uuid4().hex
        manager = namespace + '/container'
        monitor_cli, ros_cli = str(prefix/'bin/robot-perf-monitor'), str(prefix/'bin/robot-perf-ros')
        try:
            container = start([container_binary, '--ros-args', '-r', '__node:=container', '-r',
                               '__ns:='+namespace], 'container')
            standalone = start([ros_python, '-c', NODE_PROGRAM, 'standalone', namespace], 'standalone')
            load_components(manager, namespace)
            workload = root/'workload.json'
            workload.write_text(json.dumps({'format_version': 1, 'workload_id': 'fixture', 'ros_domain_id': 77,
                'functions': [{'id': name, 'process_selector': {'pids': [pid]},
                               'ros_nodes': [namespace+'/'+name]}
                              for name, pid in [('first', container.pid), ('second', container.pid),
                                                ('standalone', standalone.pid)]]}))
            capture = output/'monitor'
            monitor_command = [monitor_cli, '--workload', str(workload), '--profile', 'light', '--seconds', '3',
                 '--interval', '.25', '--discovery-interval', '.5', '--output', str(capture)]
            if skip_temperature:
                from tests.temperature_guard import guarded_command, assert_temperature_guard
                evidence = output/'monitor-temperature-guard.json'
                monitor_command = guarded_command([*monitor_command, '--skip-temperatures'],
                                                   prefix/'bin/python', evidence)
            run(monitor_command, 'monitor')
            if skip_temperature:
                assert_temperature_guard(evidence)
            source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in capture.iterdir() if p.is_file()}
            command = [ros_cli, '--monitor-run', str(capture), '--graph', '--domain-id', '77', '--ros-python', ros_python,
                       '--component-manager', manager, '--graph-wait', '2']
            def query_fixture(folder, budget):
                run([*command, '--query-timeout', str(budget), '--output', str(folder)], folder.name,
                    timeout_seconds=budget+2)
                return json.loads((folder/'graph-query.json').read_text())
            graph, snapshot = wait_for_fixture_graph(query_fixture, output, namespace, manager)
            topic = next(t for t in snapshot['topics'] if t['name'] == namespace+'/data')
            assert len(topic['publishers']) == len(topic['subscriptions']) == 2
            verify_endpoint_qos(topic)
            preflight = output/'preflight'
            preflight_command = [ros_cli, '--preflight', '--domain-id', '77',
                                 '--ros-python', ros_python, '--graph-wait', '0', '--query-timeout', '10']
            if sdk_prefix is not None:
                preflight_command += ['--sdk-prefix', str(sdk_prefix)]
            run([*preflight_command, '--rmw', snapshot['source']['rmw'], '--output', str(preflight)], 'preflight')
            readiness = json.loads((preflight/'ros-preflight.json').read_text())
            assert readiness['ready'] and readiness['source']['rmw'] == snapshot['source']['rmw']
            assert readiness['source']['python_executable'] and readiness['source']['rclpy_module']
            assert not (preflight/'ros-relations.json').exists()
            run([*preflight_command, '--rmw', 'rmw_perfkit_missing', '--output', str(output/'bad-rmw')],
                'bad-rmw', expected=1)
            assert json.loads((output/'bad-rmw/ros-status.json').read_text())['status'] == 'failed'
            relations = json.loads((graph/'ros-relations.json').read_text())
            assert all(role['nodes'][0]['graph']['status'] == 'present' and
                       role['nodes'][0]['process_relation']['status'] == 'unresolved' for role in relations['functions'])
            status = json.loads((capture/'monitor-status.json').read_text())
            assert snapshot['query_window']['start_ns'] > status['window_end_ns'], 'separate windows must not be conflated'
            summary = json.loads((capture/'monitor-summary.json').read_text())
            first, second, single = summary['workload']['functions']
            assert first['resource_refs'] == second['resource_refs'] and first['resource_refs'] != single['resource_refs']
            duplicate = start([ros_python, '-c', NODE_PROGRAM, 'first', namespace], 'duplicate-node')
            duplicate_graph, _ = wait_for_fixture_graph(query_fixture, output, namespace, manager,
                                                       name='duplicate', duplicate=True)
            duplicate_relations = json.loads((duplicate_graph/'ros-relations.json').read_text())
            assert duplicate_relations['functions'][0]['nodes'][0]['graph']['status'] == 'ambiguous'
            # Synthetic normalized metadata tests installed import boundaries,
            # and is explicitly not a CTF/tracetools execution claim.
            context = json.loads((capture/'environment.json').read_text())['observation_context']
            events = []
            for index, role in enumerate(summary['workload']['functions'], 1):
                ref = role['resource_refs'][0]; entity = summary['resources']['registered_entities'][ref]
                window = role['reference_observations'][ref]
                events.append({'event': 'ros2:rcl_node_init', 'monotonic_ns': (window['first_observed_ns'] + window['last_observed_ns'])//2,
                    'pid': entity['pid'], 'starttime_ticks': entity['starttime_ticks'], 'node_handle': index,
                    'node_name': role['function_id'], 'namespace': namespace})
            trace = root/'trace.json'
            trace.write_text(json.dumps({'format_version': 1, 'kind': 'ros2_node_init_metadata',
                'source': {'adapter': 'ros2_tracing_normalized_v1', 'tool_version': 'synthetic-fixture-not-real-trace', 'raw_sha256': '0'*64},
                'context': {key: context[key] for key in ('boot_id', 'pid_namespace', 'clock')} | {'ros_domain_id': 77},
                'events': events}))
            imported = output/'trace-import'
            run([ros_cli, '--monitor-run', str(capture), '--trace-metadata', str(trace), '--output', str(imported)], 'trace-import')
            result = json.loads((imported/'ros-relations.json').read_text())
            assert all(role['nodes'][0]['process_relation']['status'] == 'imported_trace_identity_consistent' for role in result['functions'])
            assert result['functions'][0]['nodes'][0]['process_relation']['resource_refs'] == result['functions'][1]['nodes'][0]['process_relation']['resource_refs']
            assert 'cpu_percent_one_core' not in json.dumps(result) and 'rss_peak_bytes' not in json.dumps(result)
            assert result['business_acceptance'] == 'not_evaluated'
            final_fault = output/'final-status-fault'
            fault_code = """import sys
from perfkit import ros_evidence as evidence
original=evidence._json
sent=[]
def write(path,value):
 original(path,value)
 if path.name=='ros-status.json' and value['status']=='complete' and not sent:
  sent.append(True)
  raise OSError('controlled post-complete status failure')
evidence._json=write
sys.argv=['robot-perf-ros',*sys.argv[1:]]
raise SystemExit(evidence.main())
"""
            run([str(prefix/'bin/python'), '-c', fault_code, '--monitor-run', str(capture),
                 '--trace-metadata', str(trace), '--output', str(final_fault)], 'final-status-fault', 1)
            fault_status = json.loads((final_fault/'ros-status.json').read_text())
            assert fault_status['status'] == 'failed' and fault_status['error_type'] == 'OSError'
            assert fault_status['error'] == 'controlled post-complete status failure'
            before = {p.name: p.read_bytes() for p in imported.iterdir()}
            run([ros_cli, '--monitor-run', str(capture), '--trace-metadata', str(trace), '--output', str(imported)], 'no-overwrite', 1)
            assert before == {p.name: p.read_bytes() for p in imported.iterdir()}
            missing = output/'missing-ros'
            no_ros_python = root/'no-ros-python'
            no_ros_python.write_text('#!/bin/sh\nexec "'+str(prefix/'bin/python')+'" -I "$@"\n')
            no_ros_python.chmod(0o755)
            run([ros_cli, '--monitor-run', str(capture), '--graph', '--domain-id', '77', '--ros-python', str(no_ros_python),
                 '--graph-wait', '0', '--query-timeout', '2', '--output', str(missing)], 'missing-ros', 1)
            assert json.loads((missing/'ros-status.json').read_text())['status'] == 'failed'
            assert json.loads((missing/'graph-query.json').read_text())['status'] == 'unavailable'
            fake = root/'hanging-python'
            fake.write_text('#!'+ros_python+'\nimport os,time,ctypes\nctypes.CDLL(None).prctl(15,b"m2b_query",0,0,0)\nprint(os.getpid(),flush=True)\ntime.sleep(60)\n')
            fake.chmod(0o755)
            canceled = output/'canceled'
            process = start([ros_cli, '--monitor-run', str(capture), '--graph', '--domain-id', '77', '--ros-python', str(fake),
                             '--graph-wait', '0', '--query-timeout', '20', '--output', str(canceled)], 'canceled')
            raw = canceled/'ros-graph-query/stdout.bin'
            deadline = time.monotonic()+8
            # Read a verified descendant identity; raw buffered output may not
            # flush before cancellation, so it is not the lifecycle authority.
            children = []
            while not children and time.monotonic() < deadline:
                children = owner.discover({'m2b_query': 'query-child'})
                time.sleep(.03)
            assert len(children) == 1
            query_identity = children[0].identity
            owner.signal(process, signal.SIGTERM)
            assert process.wait(timeout=5) == 130
            assert json.loads((canceled/'ros-status.json').read_text())['status'] == 'interrupted'
            assert not Path('/proc', str(query_identity.pid)).exists()
            assert all(proc.poll() is None for proc in (container, standalone, duplicate))
            assert source_hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in capture.iterdir() if p.is_file()}
            with zipfile.ZipFile(wheel) as archive:
                expected_version = Parser().parsestr(archive.read(next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))).decode())['Version']
                hashes = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist() if name.startswith('perfkit/') and name.endswith('.py')}
            code = '''import hashlib,json,pathlib,perfkit; from importlib import metadata
root=pathlib.Path(perfkit.__file__).parent
print(json.dumps({'version':metadata.version('robot-systems-perf'),'hashes':{'perfkit/'+p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py')}}))'''
            modules = subprocess.run([str(prefix/'bin/python'), '-c', code], cwd=cwd, env=env, capture_output=True, text=True, timeout=5)
            actual = json.loads(modules.stdout)
            assert actual['version'] == expected_version and actual['hashes'] == hashes
            (output/'verification.json').write_text(json.dumps({'installed_version': expected_version,
                'skip_temperature_requested': skip_temperature,
                'installed_module_hashes_match_wheel': True, 'real_rclcpp_component_container': True,
                'installed_preflight_ready': True, 'explicit_missing_rmw_failed': True,
                'fixture_readiness': json.loads((output/'graph-readiness.json').read_text()),
                'duplicate_readiness': json.loads((output/'duplicate-readiness.json').read_text()),
                'two_shared_components_and_independent_node': True, 'endpoint_qos': True,
                'runtime': snapshot['source'],
                'endpoint_history': {role: [p['qos']['history']['name'] for p in topic[role]]
                                     for role in ('publishers', 'subscriptions')},
                'duplicate_nodes_retained': True, 'graph_not_pid_attribution': True,
                'query_and_monitor_windows_separate': True, 'synthetic_trace_import_only': True,
                'source_monitor_unchanged': True, 'unavailable_ros_failed': True,
                'final_status_failure_consistent': True,
                'sigterm_exitcode': 130, 'owned_query_reaped': True, 'external_fixtures_alive': True,
                'no_target_device_claim': True}, indent=2)+'\n')
        finally:
            owner.cleanup()
            for log in logs: log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--component-prefix', type=Path, required=True)
    parser.add_argument('--container-binary', default='/opt/ros/humble/lib/rclcpp_components/component_container')
    parser.add_argument('--ros-python', default=sys.executable)
    parser.add_argument('--sdk-prefix', type=Path)
    parser.add_argument('--skip-temperature', action='store_true')
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=False)
    verify(args.wheel, args.output.resolve(), args.component_prefix, args.container_binary, args.ros_python,
           sdk_prefix=args.sdk_prefix, skip_temperature=args.skip_temperature)


if __name__ == '__main__': main()
