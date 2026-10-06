"""Graph-query protocol, read-only adapter and direct-child cleanup regressions."""
import json
from enum import IntEnum
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from perfkit.ros_graph import collect_graph, MAX_OUTPUT_BYTES, _validate_snapshot
from perfkit import ros_graph_adapter as adapter


ROOT = Path(__file__).resolve().parents[1]


def envelope(status='empty'):
    return {'format_version': 1, 'kind': 'ros_graph_snapshot', 'status': status,
        'reason': None, 'domain_id': 23, 'query_window': {'start_ns': 1, 'end_ns': 2},
        'source': {'adapter': 'rclpy', 'ros_distro': 'test', 'rmw': 'test', 'python_version': '3'},
        'nodes': [], 'topics': [], 'components': [], 'limitations': ['point query']}


class GraphProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def executable(self, code):
        path = self.root / 'fake-python'
        path.write_text('#!' + sys.executable + '\n' + code)
        path.chmod(0o755)
        return str(path)

    def query(self, code, **options):
        output = self.root / 'output'
        output.mkdir()
        return collect_graph(output, 23, self.executable(code), wait_seconds=0,
                             timeout_seconds=options.pop('timeout_seconds', 5), **options)

    def test_explicit_script_no_shell_environment_preserved_and_raw_evidence(self):
        arguments = self.root / 'arguments.json'
        code = ('import json,os,sys\nfrom pathlib import Path\n'
                'Path(' + repr(str(arguments)) + ').write_text(json.dumps({"argv":sys.argv, '
                '"domain":os.environ["ROS_DOMAIN_ID"],"rmw":os.environ["RMW_IMPLEMENTATION"]}))\n'
                'sys.stderr.write("diagnostic")\nprint(' + repr(json.dumps(envelope())) + ')\n')
        with patch.dict(os.environ, {'ROS_DOMAIN_ID': '99', 'RMW_IMPLEMENTATION': 'selected_rmw'}):
            result = self.query(code)
        self.assertEqual(result['status'], 'empty', result)
        argv = json.loads(arguments.read_text())
        self.assertEqual(argv['domain'], '23')
        self.assertEqual(argv['rmw'], 'selected_rmw')
        self.assertTrue(argv['argv'][1].endswith('/perfkit/ros_graph_adapter.py'))
        self.assertNotIn('-m', argv['argv'])
        self.assertEqual((self.root / 'output/ros-graph-query/stderr.bin').read_bytes(), b'diagnostic')

    def test_invalid_inputs_never_launch_or_create_evidence(self):
        output = self.root / 'output'; output.mkdir()
        invalid = [dict(domain_id=-1), dict(domain_id=233), dict(domain_id=True),
                   dict(wait_seconds=-.1), dict(wait_seconds=31), dict(wait_seconds=float('nan')),
                   dict(timeout_seconds=0), dict(timeout_seconds=61),
                   dict(python_executable=''), dict(component_managers=['relative']),
                   dict(component_managers=['/node;bad']), dict(component_managers=['/n'] * 17)]
        with patch('perfkit.ros_graph.subprocess.Popen') as launch:
            for override in invalid:
                args = dict(output=output, domain_id=23, python_executable=sys.executable,
                            wait_seconds=0, timeout_seconds=1)
                args.update(override)
                with self.subTest(override=override), self.assertRaises(ValueError):
                    collect_graph(**args)
            launch.assert_not_called()
        self.assertFalse((output / 'ros-graph-query').exists())

    def test_protocol_failures_never_observed(self):
        cases = [('empty', ''), ('non-json', 'print("not JSON")'),
                 ('unsupported', 'print(' + repr(json.dumps(dict(envelope(), format_version=2))) + ')'),
                 ('false-observed', 'print(' + repr(json.dumps(envelope('observed'))) + ')'),
                 ('incomplete', 'print("{}")')]
        for label, code in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                result = self.query(code)
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(result['reason'])

    def test_real_nonzero_exit_overrides_valid_empty_snapshot(self):
        result = self.query('print(' + repr(json.dumps(envelope())) + ')\nraise SystemExit(4)')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['query_evidence']['returncode'], 4)
        self.assertEqual(result['reason'], 'adapter exited with code 4')

    def test_ignored_and_custom_sigchld_refuse_before_launch_or_evidence(self):
        output = self.root / 'output'
        output.mkdir()
        saved = signal.getsignal(signal.SIGCHLD)
        def custom_handler(signum, frame):
            pass
        try:
            for handler in (signal.SIG_IGN, custom_handler):
                with self.subTest(handler=handler), patch('perfkit.ros_graph.subprocess.Popen') as launch:
                    signal.signal(signal.SIGCHLD, handler)
                    with self.assertRaisesRegex(ValueError, 'default SIGCHLD'):
                        collect_graph(output, 23, sys.executable, 0, 1)
                    self.assertIs(signal.getsignal(signal.SIGCHLD), handler)
                    launch.assert_not_called()
                    self.assertFalse((output / 'ros-graph-query').exists())
        finally:
            signal.signal(signal.SIGCHLD, saved)

    def test_missing_rclpy_is_explicitly_unavailable(self):
        code = '''import importlib.abc,runpy,sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname == 'rclpy' or fullname.startswith('rclpy.'):
            raise ModuleNotFoundError('controlled missing rclpy')
sys.meta_path.insert(0,Block())
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
        result = self.query(code, component_managers=['/container'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('rclpy unavailable', result['reason'])
        self.assertIsNone(result['components'][0]['nodes'])

    def test_real_timeout_kills_ignoring_child_and_reaps(self):
        pidfile = self.root / 'pid'
        code = ('import os,signal,time\nfrom pathlib import Path\n'
                'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                'Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()))\n'
                'while True: time.sleep(.01)\n')
        start = time.monotonic()
        result = self.query(code, timeout_seconds=1.5)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('timeout', result['reason'])
        self.assertLess(time.monotonic() - start, 4)
        with self.assertRaises(ChildProcessError):
            os.waitpid(int(pidfile.read_text()), os.WNOHANG)

    def test_output_limit_is_total_and_child_is_reaped(self):
        pidfile = self.root / 'pid'
        code = ('import os,time\nfrom pathlib import Path\n'
                'Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()))\n'
                'while True:\n os.write(1,b"a"*65536)\n os.write(2,b"b"*65536)\n')
        result = self.query(code)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('4 MiB', result['reason'])
        total = sum(path.stat().st_size for path in (self.root / 'output/ros-graph-query').iterdir())
        self.assertEqual(total, MAX_OUTPUT_BYTES)
        with self.assertRaises(ChildProcessError):
            os.waitpid(int(pidfile.read_text()), os.WNOHANG)

    def test_interruption_and_read_oserror_preserve_original_and_reap(self):
        for error in (KeyboardInterrupt('original cancellation'), OSError('original I/O')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                output = self.root / 'output'; output.mkdir()
                actual_popen = subprocess.Popen
                children = []
                def launch(*args, **kwargs):
                    child = actual_popen(*args, **kwargs)
                    children.append(child)
                    return child
                def interrupt(*args):
                    raise error
                with patch('perfkit.ros_graph.subprocess.Popen', side_effect=launch), \
                        patch('perfkit.ros_graph._capture', side_effect=interrupt):
                    with self.assertRaises(type(error)) as raised:
                        collect_graph(output, 23, self.executable('import time\ntime.sleep(10)'), 0, 1)
                self.assertIs(raised.exception, error)
                self.assertIsNotNone(children[0].returncode)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)

    def test_real_signal_during_cleanup_preserves_cancellation_and_reaps(self):
        output = self.root / 'output'; output.mkdir()
        marker = self.root / 'term'
        childpid = self.root / 'pid'
        fake = self.executable('import os,signal,time\nfrom pathlib import Path\n'
            'def term(*args): Path(' + repr(str(marker)) + ').write_text("cleanup")\n'
            'signal.signal(signal.SIGTERM,term)\nPath(' + repr(str(childpid)) + ').write_text(str(os.getpid()))\n'
            'while True: time.sleep(.01)\n')
        code = ('import sys\nfrom pathlib import Path\nfrom perfkit.ros_graph import collect_graph\n'
                'collect_graph(Path(sys.argv[1]),23,sys.argv[2],0,1.5)\n')
        process = subprocess.Popen([sys.executable, '-c', code, str(output), fake], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            until = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < until:
                self.assertIsNone(process.poll())
                time.sleep(.01)
            self.assertTrue(marker.exists())
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b'KeyboardInterrupt', stderr)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(childpid.read_text()), 0)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=2)
            process.stdout.close(); process.stderr.close()


class GraphAdapterTests(unittest.TestCase):
    def fake_modules(self, node):
        ros = types.ModuleType('rclpy')
        ros.init = lambda **kwargs: self.assertEqual(kwargs, {'args': []})
        ros.shutdown = lambda: None
        def create(name, **kwargs):
            self.assertEqual(kwargs, dict(namespace='/', enable_rosout=False,
                            start_parameter_services=False, use_global_arguments=False))
            node.observer = name
            return node
        ros.create_node = create
        ros.spin_once = lambda *args, **kwargs: None
        utilities = types.ModuleType('rclpy.utilities')
        utilities.get_rmw_implementation_identifier = lambda: 'test_rmw'
        return {'rclpy': ros, 'rclpy.utilities': utilities}

    def node(self):
        node = types.SimpleNamespace()
        node.destroy_node = lambda: None
        node.get_node_names_and_namespaces = lambda: [(node.observer, '/'), ('same', '/ns'), ('same', '/ns')]
        node.get_topic_names_and_types = lambda: [('/topic', ['pkg/msg/T'])]
        node.get_publishers_info_by_topic = lambda name: []
        node.get_subscriptions_info_by_topic = lambda name: []
        return node

    def test_duplicates_retained_and_observer_excluded(self):
        node = self.node()
        with patch.dict(sys.modules, self.fake_modules(node)):
            result = adapter.snapshot(23, 0, 1, [])
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['nodes'], [{'name': 'same', 'namespace': '/ns', 'full_name': '/ns/same'}] * 2)

    def test_missing_endpoint_read_is_partial_failure(self):
        node = self.node()
        def fail(name):
            raise RuntimeError('endpoint unavailable')
        node.get_publishers_info_by_topic = fail
        with patch.dict(sys.modules, self.fake_modules(node)):
            result = adapter.snapshot(23, 0, 1, [])
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['partial'])
        self.assertIsNone(result['topics'][0]['publishers'])
        self.assertIn('endpoint unavailable', result['reason'])

    def test_missing_node_read_is_partial_failure(self):
        node = self.node()
        def fail():
            raise RuntimeError('node query unavailable')
        node.get_node_names_and_namespaces = fail
        with patch.dict(sys.modules, self.fake_modules(node)):
            result = adapter.snapshot(23, 0, 1, [])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['nodes'], [])
        self.assertIn('node query unavailable', result['reason'])

    def test_missing_component_interface_is_explicit_and_not_success(self):
        node = self.node()
        modules = self.fake_modules(node)
        modules['composition_interfaces'] = None
        modules['composition_interfaces.srv'] = None
        with patch.dict(sys.modules, modules):
            result = adapter.snapshot(23, 0, 1, ['/a', '/ns/b'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([row['manager'] for row in result['components']], ['/a', '/ns/b'])
        for row in result['components']:
            self.assertEqual(row['status'], 'unavailable')
            self.assertIsNone(row['nodes'])
            self.assertIn('composition_interfaces unavailable', row['reason'])

    def test_observer_only_graph_is_empty(self):
        node = self.node()
        node.get_node_names_and_namespaces = lambda: [(node.observer, '/')]
        node.get_topic_names_and_types = lambda: []
        with patch.dict(sys.modules, self.fake_modules(node)):
            result = adapter.snapshot(23, 0, 1, [])
        self.assertEqual(result['status'], 'empty')

    def test_observer_only_endpoint_topic_is_excluded(self):
        node = self.node()
        node.get_node_names_and_namespaces = lambda: [(node.observer, '/')]
        node.get_publishers_info_by_topic = lambda name: [types.SimpleNamespace(
            node_name=node.observer, node_namespace='/')]
        with patch.dict(sys.modules, self.fake_modules(node)):
            result = adapter.snapshot(23, 0, 1, [])
        self.assertEqual(result['status'], 'empty')
        self.assertEqual(result['topics'], [])

    def test_qos_and_gid_serialization(self):
        qos = types.SimpleNamespace(history=1, reliability=2, durability=3, liveliness=4,
            depth=7, deadline=types.SimpleNamespace(nanoseconds=11),
            lifespan=types.SimpleNamespace(nanoseconds=12),
            liveliness_lease_duration=types.SimpleNamespace(nanoseconds=13),
            avoid_ros_namespace_conventions=False)
        info = types.SimpleNamespace(node_name='n', node_namespace='/ns',
                                     endpoint_gid=[1, 2, 255], qos_profile=qos, topic_type='pkg/msg/T')
        row = adapter._endpoint(info)
        self.assertEqual(row['full_name'], '/ns/n')
        self.assertEqual(row['endpoint_gid'], '0102ff')
        self.assertEqual(row['qos']['deadline_ns'], 11)
        self.assertEqual(row['qos']['reliability']['value'], 2)
        self.assertEqual(row['qos']['reported_depth'], 7)
        self.assertEqual(row['qos']['depth'], 7)
        self.assertIsNone(row['qos']['depth_reason'])

    def test_unknown_history_preserves_reported_zero_and_has_null_depth(self):
        class History(IntEnum):
            UNKNOWN = 3
        qos = types.SimpleNamespace(history=History.UNKNOWN, reliability=2,
            durability=3, liveliness=4, depth=0,
            deadline=types.SimpleNamespace(nanoseconds=11),
            lifespan=types.SimpleNamespace(nanoseconds=12),
            liveliness_lease_duration=types.SimpleNamespace(nanoseconds=13),
            avoid_ros_namespace_conventions=False)
        info = types.SimpleNamespace(node_name='n', node_namespace='/ns',
                                     endpoint_gid=[1, 2, 255], qos_profile=qos, topic_type='pkg/msg/T')
        row = adapter._endpoint(info)
        self.assertEqual(row['qos']['reported_depth'], 0)
        self.assertIsNone(row['qos']['depth'])
        self.assertEqual(row['qos']['depth_reason'], 'RMW graph does not expose queue depth')
        snapshot = envelope('observed')
        snapshot['topics'] = [{'name': '/topic', 'types': ['pkg/msg/T'],
                               'publishers': [row], 'subscriptions': []}]
        self.assertIs(_validate_snapshot(snapshot, 23, []), snapshot)
        row['qos']['depth'] = 0
        with self.assertRaisesRegex(ValueError, 'invalid adapter endpoint'):
            _validate_snapshot(snapshot, 23, [])

    def test_component_timeout_missing_and_error_have_null_nodes(self):
        for mode in ('missing', 'timeout', 'error', 'success'):
            with self.subTest(mode=mode):
                node = self.node()
                client = types.SimpleNamespace(wait_for_service=lambda **kwargs: mode != 'missing')
                future = types.SimpleNamespace(done=lambda: mode != 'timeout', cancel=lambda: None,
                    result=lambda: types.SimpleNamespace(full_node_names=['/composed'], unique_ids=[17]))
                def call(request):
                    if mode == 'error':
                        raise RuntimeError('controlled RPC error')
                    return future
                client.call_async = call
                names = []
                node.create_client = lambda service, name: (names.append(name) or client)
                node.destroy_client = lambda value: self.assertIs(value, client)
                ros = types.SimpleNamespace(spin_until_future_complete=lambda *args, **kwargs: None)
                service = types.SimpleNamespace(Request=lambda: object())
                result = adapter._component(node, ros, '/container', service, time.monotonic() + .1)
                self.assertEqual(names, ['/container/_container/list_nodes'])
                if mode == 'success':
                    self.assertEqual(result['nodes'], [{'full_name': '/composed', 'unique_id': 17}])
                else:
                    self.assertIsNone(result['nodes'])
                    self.assertTrue(result['reason'])


if __name__ == '__main__':
    unittest.main()
