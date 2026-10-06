from argparse import Namespace
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from tests import device_regression as driver


class DeviceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.wheel=self.root/'bundle.whl'
        self.source=Path(driver.__file__).resolve().parents[1]
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            archive.writestr('robot_systems_perf-9.8.7.dist-info/METADATA', 'Name: robot-systems-perf\nVersion: 9.8.7\n')
            for path in (self.source/'perfkit').glob('*.py'):
                archive.writestr('perfkit/'+path.name, path.read_bytes())
        self.args=Namespace(wheel=self.wheel, output=self.root/'output', preflight=False,
            ros_fixture=False, component_prefix=None, container_binary=None, sdk_prefix=None,
            rmw=None, component_manager=[], domain_id=23, ros_python='python3', query_timeout=10,
            skip_temperature=False)

    def test_version_is_read_from_wheel_and_module_mismatch_is_rejected(self):
        self.assertEqual(driver.wheel_identity(self.wheel)['version'], '9.8.7')
        bad=self.root/'bad.whl'
        with zipfile.ZipFile(self.wheel) as source, zipfile.ZipFile(bad,'w') as target:
            for name in source.namelist():
                target.writestr(name, b'changed' if name=='perfkit/ros_graph.py' else source.read(name))
        with self.assertRaises(ValueError): driver.wheel_identity(bad)

    def test_core_stages_are_finite_and_ros_is_not_claimed(self):
        with patch.object(driver,'verify_intake') as intake, patch.object(driver,'verify_workload') as workload:
            driver.run(self.args)
            intake.assert_called_once(); workload.assert_called_once()
        value=json.loads((self.args.output/'device-regression-status.json').read_text())
        self.assertEqual(value['status'], 'complete')
        self.assertEqual(value['version'], '9.8.7')
        self.assertEqual(value['ros_fixture'], 'not_requested')
        self.assertEqual(value['business_acceptance'], 'not_evaluated')
        with self.assertRaises(FileExistsError): driver.run(self.args)

    def test_missing_preflight_dependency_preserves_core_results(self):
        self.args.preflight=True
        with patch.object(driver,'verify_intake'), patch.object(driver,'verify_workload'), \
             patch.object(driver,'run_ros_evidence', side_effect=RuntimeError('SDK unavailable')), self.assertRaises(RuntimeError):
            driver.run(self.args)
        value=json.loads((self.args.output/'device-regression-status.json').read_text())
        self.assertEqual(value['status'],'failed')
        self.assertEqual(value['stages']['workload'],'passed')
        self.assertEqual(value['stages']['ros_preflight'],'running')
        self.assertIn('SDK unavailable',value['error'])

    def test_temperature_request_reaches_every_installed_stage(self):
        self.args.skip_temperature=True
        self.args.preflight=self.args.ros_fixture=True
        self.args.component_prefix=self.root
        self.args.container_binary='controlled-container'
        with patch.object(driver,'verify_intake') as intake, patch.object(driver,'verify_workload') as workload, \
             patch.object(driver,'verify_ros') as ros, patch.object(driver,'run_ros_evidence',return_value={'source':{}}):
            driver.run(self.args)
        intake.assert_called_once_with(self.wheel,self.args.output/'intake',skip_temperature=True)
        workload.assert_called_once_with(self.wheel,self.args.output/'workload',skip_temperature=True)
        self.assertTrue(ros.call_args.kwargs['skip_temperature'])
        self.assertTrue(json.loads((self.args.output/'device-regression-status.json').read_text())['skip_temperature'])

    def test_final_post_save_error_downgrades_status_and_preserves_error(self):
        write=Path.write_text; fired=[]; error=OSError('post-save')
        def fail_once(path,value,*args,**kwargs):
            result=write(path,value,*args,**kwargs)
            if path.name=='device-regression-status.json' and json.loads(value)['status']=='complete' and not fired:
                fired.append(1); raise error
            return result
        with patch.object(driver,'verify_intake'), patch.object(driver,'verify_workload'), \
             patch.object(Path,'write_text',fail_once), self.assertRaises(OSError) as caught:
            driver.run(self.args)
        self.assertIs(caught.exception,error)
        self.assertEqual(json.loads((self.args.output/'device-regression-status.json').read_text())['status'],'failed')

    def test_interruption_and_secondary_status_error_preserve_original(self):
        write=Path.write_text; fired=[]; error=KeyboardInterrupt('cancel')
        def fail_once(path,value,*args,**kwargs):
            if path.name=='device-regression-status.json' and json.loads(value)['status']=='interrupted' and not fired:
                fired.append(1); raise OSError('secondary')
            return write(path,value,*args,**kwargs)
        with patch.object(driver,'verify_intake',side_effect=error), patch.object(Path,'write_text',fail_once), \
             self.assertRaises(KeyboardInterrupt) as caught:
            driver.run(self.args)
        self.assertIs(caught.exception,error)
        state=json.loads((self.args.output/'device-regression-status.json').read_text())
        self.assertEqual(state['status'],'interrupted')
        self.assertEqual(state['cleanup_errors'][0]['error_type'],'OSError')

    def test_invalid_fixture_selection_does_not_create_output(self):
        self.args.ros_fixture=True
        with self.assertRaises(ValueError): driver.run(self.args)
        self.assertFalse(self.args.output.exists())
