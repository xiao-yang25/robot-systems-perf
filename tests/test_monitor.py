import json
import os
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit import monitor as m


class IdleSampler:
    instances = []
    def __init__(self, path, interval, window_source):
        self.path, self.error, self.closed = path, None, False
        self.instances.append(self)
    def set_window(self, *args):
        pass
    def __enter__(self):
        self.path.write_text('')
        return self
    def __exit__(self, *args):
        self.closed = True
    def register(self, *args, **kwargs):
        raise AssertionError('empty inventory must not register')


class MonitorTests(unittest.TestCase):
    def test_installed_source_records_version_and_hashes_without_parent_git_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root/'perfkit'
            package.mkdir()
            source = package/'monitor.py'
            source.write_text('fixture source\n')
            with patch.object(m, '__file__', str(source)), \
                 patch.object(m.metadata, 'version', return_value='0.3.0'), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(m.subprocess, 'run') as git:
                record = m._source_record()
            git.assert_not_called()
            self.assertIsNone(record['git_revision'])
            self.assertEqual(record['package_version'], '0.3.0')
            import hashlib
            self.assertEqual(record['sha256']['perfkit/monitor.py'], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_scope_defaults_to_current_uid_and_cli_patterns_are_validated(self):
        self.assertEqual(m.validate_config({})['uids'], [os.getuid()])
        self.assertIsNone(m.validate_config({'uids': None})['uids'])
        for config in ({'duration_seconds': float('nan')}, {'duration_seconds': 0},
                       {'max_targets': True}, {'max_targets': 1.5}, {'uids': [True]},
                       {'include_names': ['[']}, {'include_names': 'python'},
                       {'unknown': 1}, {'require_jetson': 'yes'}, {'format_version': True},
                       {'active_cpu_percent': 0}):
            with self.subTest(config=config), self.assertRaises((ValueError, TypeError)):
                m.validate_config(config)
        self.assertEqual(m.validate_config({'active_cpu_percent': 0, 'include_names': ['worker']})['active_cpu_percent'], 0)

    def test_empty_capture_is_review_required_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'capture'
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {'skipped_count': 0}}):
                summary = m.run_monitor({'duration_seconds': 1, 'discovery_interval_seconds': .1}, output)
            self.assertEqual(summary['status'], 'complete')
            self.assertEqual(summary['quality']['status'], 'review_required')
            self.assertEqual(summary['quality']['registrations'], 0)
            self.assertTrue(IdleSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'complete')
            with self.assertRaises(FileExistsError):
                m.run_monitor({}, output)

    def test_discovery_failure_closes_sampler_and_records_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', side_effect=PermissionError('proc root denied')):
                with self.assertRaises(PermissionError):
                    m.run_monitor({}, output)
            self.assertTrue(IdleSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'failed')
            self.assertFalse((output/'monitor-summary.json').exists())

    def test_interrupt_keeps_partial_report_and_closes_sampler(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', side_effect=KeyboardInterrupt('fixture interrupt')):
                with self.assertRaises(KeyboardInterrupt):
                    m.run_monitor({}, output)
            self.assertTrue(IdleSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'interrupted')
            self.assertEqual(json.loads((output/'monitor-summary.json').read_text())['status'], 'interrupted')

    def test_require_jetson_fails_before_observing_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'collect_profile', return_value={'checks': {'jetson_detected': False}}), \
                 patch.object(m, 'scan_processes') as scan:
                with self.assertRaisesRegex(RuntimeError, 'Jetson'):
                    m.run_monitor({'require_jetson': True}, output)
            scan.assert_not_called()
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'failed')

    def test_late_signal_during_summary_or_report_keeps_consistent_interrupted_evidence(self):
        for phase in ('summarize_resources', 'write_monitor_report'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'capture'
                original = getattr(m, phase)
                sent = []
                def late_signal(*args, **kwargs):
                    value = original(*args, **kwargs)
                    if not sent:
                        sent.append(True)
                        os.kill(os.getpid(), signal.SIGINT)
                    return value
                with patch.object(m, 'ResourceSampler', IdleSampler), \
                     patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {'skipped_count': 0}}), \
                     patch.object(m, phase, side_effect=late_signal):
                    with self.assertRaises(KeyboardInterrupt):
                        m.run_monitor({'duration_seconds': 1}, output)
                self.assertEqual(json.loads((output/'monitor-summary.json').read_text())['status'], 'interrupted')
                self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'interrupted')
                self.assertIn('采集状态：interrupted', (output/'MONITOR_REPORT.md').read_text())

    def test_sampler_failure_is_non_success_and_closes_owned_sampler(self):
        class FailedSampler(IdleSampler):
            def __enter__(self):
                super().__enter__()
                self.error = 'fixture sampler failure'
                return self
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'ResourceSampler', FailedSampler):
                with self.assertRaisesRegex(RuntimeError, 'sampling failed'):
                    m.run_monitor({}, output)
            self.assertTrue(FailedSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'failed')
            self.assertFalse((output/'monitor-summary.json').exists())

    def test_signal_during_final_status_write_reconciles_all_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            original = m._json
            writes = []
            def final_signal(path, value):
                if path.name == 'monitor-status.json':
                    writes.append(True)
                    if len(writes) == 2:
                        os.kill(os.getpid(), signal.SIGINT)
                return original(path, value)
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {'skipped_count': 0}}), \
                 patch.object(m, '_json', side_effect=final_signal):
                with self.assertRaises(KeyboardInterrupt):
                    m.run_monitor({'duration_seconds': 1}, output)
            self.assertEqual(json.loads((output/'monitor-summary.json').read_text())['status'], 'interrupted')
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'interrupted')
            self.assertIn('采集状态：interrupted', (output/'MONITOR_REPORT.md').read_text())


if __name__ == '__main__':
    unittest.main()
