import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from perfkit.ros_evidence import run_ros_evidence
from perfkit.ros_graph import collect_graph
from tests.test_ros_graph import envelope
from tests.device_regression import wheel_identity, run


class RosEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fake_python(self, rmw='rmw_test'):
        value = envelope(); value['source']['rmw'] = rmw
        p = self.root/'fake python'
        p.write_text('#!'+sys.executable+'\nimport os,json,sys\n'
                     'print('+repr(json.dumps(value))+')\n')
        p.chmod(0o755); return str(p)

    def test_preflight_needs_no_business_source_and_cannot_evaluate_business(self):
        value = envelope(); value['source']['rmw'] = 'rmw_test'
        with patch('perfkit.ros_evidence.load_monitor', side_effect=AssertionError('must not read')), \
             patch('perfkit.ros_evidence.collect_graph', return_value=value) as query:
            result = run_ros_evidence(None, self.root/'ready', preflight=True, domain_id=23)
        self.assertTrue(result['ready']); self.assertEqual(result['business_acceptance'], 'not_evaluated')
        self.assertFalse((self.root/'ready/ros-relations.json').exists())
        self.assertEqual(json.loads((self.root/'ready/ros-status.json').read_text())['status'], 'complete')
        with self.assertRaises(FileExistsError):
            run_ros_evidence(None, self.root/'ready', preflight=True, domain_id=23)

    def test_invalid_preflight_or_sdk_rejects_before_source_output_and_launch(self):
        fifo = self.root/'setup.bash'; os.mkfifo(fifo)
        cases = [dict(monitor_run=self.root/'missing'), dict(trace_metadata=self.root/'missing'),
                 dict(domain_id=None), dict(rmw='x;cmd'), dict(sdk_prefix=fifo),
                 dict(sdk_prefix=Path('relative')), dict(sdk_prefix=self.root/"missing")]
        with patch('perfkit.ros_evidence.load_monitor') as load, patch('perfkit.ros_graph.subprocess.Popen') as launch:
            for i, override in enumerate(cases):
                args = dict(monitor_run=None, output=self.root/str(i), preflight=True, domain_id=23)
                args.update(override)
                with self.subTest(override=override), self.assertRaises((ValueError, OSError)):
                    run_ros_evidence(**args)
                self.assertFalse(args['output'].exists())
            load.assert_not_called(); launch.assert_not_called()

    def test_missing_ros_preflight_is_not_ready_and_failed_with_evidence(self):
        value = envelope(); value.update(status='unavailable', reason='rclpy unavailable')
        with patch('perfkit.ros_evidence.collect_graph', return_value=value), self.assertRaises(RuntimeError):
            run_ros_evidence(None, self.root/'missing', preflight=True, domain_id=23)
        result = json.loads((self.root/'missing/ros-preflight.json').read_text())
        self.assertFalse(result['ready'])
        self.assertEqual(json.loads((self.root/'missing/ros-status.json').read_text())['status'], 'failed')

    def test_requested_rmw_mismatch_cannot_pass(self):
        out = self.root/'mismatch'; out.mkdir()
        result = collect_graph(out, 23, self.fake_python(), 0, 5, rmw='rmw_other')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('does not match', result['reason'])

    def test_sdk_prefix_checks_actual_module_without_loading_shell_code(self):
        sdk = self.root/'sdk prefix $(not-executed)'; sdk.mkdir()
        (sdk/'setup.bash').write_text('exit 4\n')
        module = sdk/'python/rclpy/__init__.py'
        value = envelope(); value['source'].update(rmw='rmw_test', rclpy_module=str(module))
        python = self.root/'selected-python'
        python.write_text('#!'+sys.executable+'\nprint('+repr(json.dumps(value))+')\n'); python.chmod(0o755)
        out=self.root/'selected'; out.mkdir()
        result=collect_graph(out,23,str(python),0,5,sdk_prefix=sdk,rmw='rmw_test')
        self.assertEqual(result['status'],'empty',result)
        self.assertEqual((out/'ros-graph-query/stderr.bin').read_bytes(),b'')
        self.assertEqual(result['environment_request']['sdk_prefix'],str(sdk))

    def test_sdk_module_outside_prefix_missing_or_symlinked_outside_fails(self):
        sdk=self.root/'sdk'; sdk.mkdir()
        elsewhere=self.root/'elsewhere'; elsewhere.mkdir()
        (sdk/'linked').symlink_to(elsewhere,target_is_directory=True)
        for index, module in enumerate((None,str(elsewhere/'rclpy.py'),str(sdk/'linked/rclpy.py'))):
            value=envelope(); value['source'].update(rmw='rmw_test',rclpy_module=module)
            python=self.root/('probe'+str(index)); python.write_text('#!'+sys.executable+'\nprint('+repr(json.dumps(value))+')\n'); python.chmod(0o755)
            out=self.root/str(index); out.mkdir()
            result=collect_graph(out,23,str(python),0,5,sdk_prefix=sdk)
            self.assertEqual(result['status'],'failed')
            self.assertIn('SDK prefix',result['reason'])

    def test_preflight_final_write_failure_keeps_failed_status(self):
        from perfkit import ros_evidence as module
        write = module._json
        fired = []
        def fail_once(path, value):
            write(path, value)
            if path.name == 'ros-status.json' and value['status'] == 'complete' and not fired:
                fired.append(1); raise OSError('post-save')
        with patch.object(module, 'collect_graph', return_value=envelope()), \
             patch.object(module, '_json', side_effect=fail_once), self.assertRaises(OSError):
            run_ros_evidence(None, self.root/'fail', preflight=True, domain_id=23)
        self.assertEqual(json.loads((self.root/'fail/ros-status.json').read_text())['status'], 'failed')
