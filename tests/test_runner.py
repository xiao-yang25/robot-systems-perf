import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from perfkit.runner import validate, start, stop
from perfkit import runner


class RunnerContractTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((Path(__file__).resolve().parents[1] / 'configs/smoke.json').read_text())

    def test_both_checked_in_configs_are_supported(self):
        for name in ('smoke', 'baseline', 's01-only'):
            config = json.loads((Path(__file__).resolve().parents[1] / f'configs/{name}.json').read_text())
            validate(config)

    def test_optional_type_error_degrades_and_direct_environment_records_view(self):
        with patch.object(Path, 'read_text', side_effect=TypeError('synthetic optional interface')):
            self.assertIsNone(runner.read_optional('/sys/optional'))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(runner.os.environ, {}, clear=True), \
                patch.object(runner, 'read_optional', return_value=None), \
                patch.object(runner, 'command_optional', return_value=None), \
                patch('perfkit.platform_probe.collect_profile', return_value={'format_version': 1}) as profile:
            recorded = runner.environment(Path(directory))
        self.assertEqual(recorded['host_profile_source'], 'local_process_view_at_startup')
        self.assertEqual(recorded['host_profile'], {'format_version': 1})
        profile.assert_called_once_with(include_kernel_command_line=False)

    def test_standalone_s01_requires_only_periodic_binary_and_records_hash(self):
        config = copy.deepcopy(self.config)
        config['scenarios'] = [config['scenarios'][1]]
        config['sampling_mode'] = 'minimal'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            (root / 'build/periodic_bench').write_bytes(b'independent known binary bytes')
            output = root / 'output'
            with patch.object(runner, 'environment', return_value={}), \
                    patch.object(runner, 'run_one', return_value={'measurement_window': {'start_ns': 1, 'end_ns': 2}}), \
                    patch.object(runner, 'measurement_quality', return_value={}), \
                    patch('perfkit.analysis.write_report'):
                runner.run_experiment(config, output, root)
            recorded = json.loads((output / 'environment.json').read_text())
            self.assertEqual(recorded['required_binaries'], ['periodic_bench'])
            import hashlib
            self.assertEqual(recorded['binary_sha256'], {'periodic_bench': hashlib.sha256(b'independent known binary bytes').hexdigest()})
            with self.assertRaisesRegex(RuntimeError, 'binary missing'):
                runner.run_experiment(self.config, root / 'missing-ros', root)
            self.assertFalse((root / 'missing-ros').exists())

    def test_rejects_invalid_or_unbounded_workloads(self):
        cases = [('measurement_seconds', float('nan')), ('repetitions', True),
                 ('repetitions', 0), ('drain_seconds', -1), ('resource_sampling_seconds', 0)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                config = copy.deepcopy(self.config); config[field] = value
                with self.assertRaises(ValueError): validate(config)
        config = copy.deepcopy(self.config)
        config['scenarios'][0]['frequency_hz'] = 20000
        with self.assertRaises(ValueError): validate(config)
        config = copy.deepcopy(self.config)
        config['scenarios'][0]['payload_bytes'] = 1.5
        with self.assertRaises(ValueError): validate(config)

    def test_duplicate_scenario_is_rejected_before_overwriting_raw_files(self):
        self.config['scenarios'].append(copy.deepcopy(self.config['scenarios'][0]))
        with self.assertRaises(ValueError): validate(self.config)

    def test_process_group_cleanup_reaps_real_child(self):
        with tempfile.TemporaryDirectory() as directory:
            child = start([sys.executable, '-c', 'import time; time.sleep(60)'], Path(directory) / 'child.log')
            stop(child)
            self.assertIsNotNone(child.poll())
            stop(child)

    def test_environment_capture_interruption_preserves_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            for name in ('ros_bench', 'periodic_bench'):
                (root / 'build' / name).touch()
            config = root / 'config.json'; config.write_text(json.dumps(self.config))
            output = root / 'output'
            with patch.object(runner, '__file__', str(root / 'perfkit/runner.py')), \
                 patch.object(runner, 'environment', side_effect=KeyboardInterrupt('capture interrupted')), \
                 patch.object(sys, 'argv', ['runner', '--config', str(config), '--output', str(output)]):
                with self.assertRaises(KeyboardInterrupt): runner.main()
            status = json.loads((output / 'run-status.json').read_text())
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(status['completed_runs'], 0)
            self.assertEqual(status['error_type'], 'KeyboardInterrupt')


if __name__ == '__main__':
    unittest.main()
