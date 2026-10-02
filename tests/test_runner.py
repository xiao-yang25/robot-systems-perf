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
        for name in ('smoke', 'baseline'):
            config = json.loads((Path(__file__).resolve().parents[1] / f'configs/{name}.json').read_text())
            validate(config)

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
