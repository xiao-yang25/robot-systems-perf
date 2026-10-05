"""Comparison identity, bounded lifecycle and incomplete evidence contracts."""
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import comparison_common as common
from scripts import compare_tools as compare
from tests.process_helpers import OwnedProcesses

BABELTRACE_HELP = (Path(__file__).parent / 'fixtures/babeltrace-1.5.8-help.txt').read_text()
CYCLICTEST_HELP = (Path(__file__).parent / 'fixtures/cyclictest-2.5-help.txt').read_text()


class ComparisonEvidenceTests(unittest.TestCase):
    def test_tool_probe_requires_clean_recognized_version_and_help(self):
        compare.validate_tool_probe('pidstat', 'sysstat version 12.8.1\n')
        compare.validate_tool_probe('babeltrace', BABELTRACE_HELP)
        compare.validate_tool_probe('cyclictest', CYCLICTEST_HELP)
        for tool, text in [('pidstat', ''), ('pidstat', 'unrecognized banner'),
                           ('babeltrace', 'BabelTrace Trace Viewer and Converter 1.5.8\n'),
                           ('babeltrace', 'BabelTrace Trace Viewer and Converter 1.5.8\nusage : babeltrace [OPTIONS]\n'),
                           ('babeltrace', BABELTRACE_HELP + 'Error parsing options.\n'),
                           ('pidstat', 'sysstat version 12.8.1\nError while loading shared libraries'),
                           ('pidstat', 'sysstat version 12.8.1\nTraceback (most recent call last):')]:
            with self.subTest(tool=tool, text=text), self.assertRaises(RuntimeError):
                compare.validate_tool_probe(tool, text)

    def test_required_help_options_reject_prefixes_and_truncation(self):
        for tool, original, flags in [('cyclictest', CYCLICTEST_HELP, ['clock', 'affinity', 'mainaffinity']),
                                      ('babeltrace', BABELTRACE_HELP, ['fields', 'input-format', 'clock-cycles'])]:
            for flag in flags:
                with self.subTest(tool=tool, flag=flag), self.assertRaises(RuntimeError):
                    compare.validate_tool_probe(tool, original.replace('--' + flag, '--' + flag + '-extra'))
            with self.subTest(tool=tool, truncated=True), self.assertRaises(RuntimeError):
                compare.validate_tool_probe(tool, '\n'.join(original.splitlines()[:3]))

    def test_pid_reuse_and_reset_cannot_be_compared(self):
        start = {'starttime_ticks': 10, 'utime_ticks': 20, 'stime_ticks': 3,
                 'rss_pages': 1, 'num_threads': 2}
        with patch.object(compare.os, 'sysconf', return_value=100):
            for end in (dict(start, starttime_ticks=11), dict(start, utime_ticks=19)):
                with self.assertRaises(ValueError):
                    compare.target_delta({'42': start}, {'42': end}, 1)
            result = compare.target_delta({'42': start}, {'42': dict(start, utime_ticks=45)}, 2)
        self.assertEqual(result['42']['cpu_percent_one_core'], 12.5)

    def test_missing_tool_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(compare.shutil, 'which', return_value=None):
            result = compare.preflight('resource', Path(directory))
        self.assertFalse(result['ready'])
        self.assertEqual(next(check for check in result['checks'] if check['tool'] == 'pidstat')['reason'],
                         'executable missing')

    def test_trace_requires_events_for_each_exact_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'decode.log'
            log.write_text('[1] ros2:rclcpp_publish: { vpid = 42 }, {}\n'
                           '[2] ros2:callback_start: { vpid = 43 }, {}\n'
                           '[3] ros2:callback_start: { vpid = 142 }, {}\n')
            self.assertEqual(compare.trace_event_counts(log, [42, 43]),
                             {'42': {'ros2:rclcpp_publish': 1}, '43': {'ros2:callback_start': 1}})
            with self.assertRaisesRegex(RuntimeError, 'lacks ROS events'):
                compare.trace_event_counts(log, [42, 44])
            log.write_text('metadata and empty stream headers only\n')
            with self.assertRaises(RuntimeError):
                compare.trace_event_counts(log, [42, 43])

    def test_partial_trace_setup_destroys_only_own_session_and_stops(self):
        calls = []
        def fail(command, folder, timeout, **kwargs):
            calls.append(command)
            if 'enable-channel' in command:
                raise RuntimeError('channel failure')
            if '-m' in command:
                benchmark = folder.parent / 'benchmark'
                benchmark.mkdir()
                (benchmark / 'summary.json').write_text(json.dumps({'results': [{'quality': {}, 'acceptance': {}}]}))
            return {'returncode': 0}
        with tempfile.TemporaryDirectory() as directory, patch.object(compare, 'execute', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'channel failure'):
                compare.trace_runs(Path(directory), 2, 3)
        sessions = [command[3] for command in calls if command[:3] == ['lttng', '--no-sessiond', 'create']]
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].startswith('rsp-'))
        self.assertEqual(calls[-3:], [['lttng', '--no-sessiond', 'stop', sessions[0]],
                                    ['lttng', '--no-sessiond', 'list', sessions[0]],
                                    ['lttng', '--no-sessiond', 'destroy', sessions[0]]])
        self.assertTrue(all('--all' not in command for command in calls))

    def test_cancel_inside_nested_cleanup_does_not_skip_destroy(self):
        calls = []
        def cancel(command, folder, timeout, **kwargs):
            calls.append(command)
            if '-m' in command:
                benchmark = folder.parent / 'benchmark'
                benchmark.mkdir()
                (benchmark / 'summary.json').write_text(json.dumps({'results': [{'quality': {}, 'acceptance': {}}]}))
            if 'enable-channel' in command:
                raise RuntimeError('channel failure')
            if 'stop' in command:
                with common.defer_interrupts():
                    signal.raise_signal(signal.SIGTERM)
            return {'returncode': 0}
        with tempfile.TemporaryDirectory() as directory, patch.object(compare, 'execute', side_effect=cancel):
            with self.assertRaises(KeyboardInterrupt):
                compare.trace_runs(Path(directory), 2, 1)
        self.assertEqual([command[2] for command in calls[-3:]], ['stop', 'list', 'destroy'])


@unittest.skipUnless(platform.system() == 'Linux', 'real pidfd lifecycle requires Linux')
class ComparisonLifecycleTests(unittest.TestCase):
    def test_observation_failure_reaps_owned_command_and_preserves_external_target(self):
        owner = OwnedProcesses()
        try:
            external = common.launch(owner, [sys.executable, '-c', 'import time; time.sleep(30)'])
            def reject(handle):
                self.assertTrue(handle.alive())
                raise RuntimeError('actual worker conditions differ')
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / 'observer-failed'
                with self.assertRaisesRegex(RuntimeError, 'actual worker conditions differ'):
                    common.execute([sys.executable, '-c', 'import time; time.sleep(30)'], folder, 5, observe=reject)
                record = json.loads((folder / 'result.json').read_text())
                self.assertFalse(Path('/proc', str(record['pid'])).exists())
                self.assertTrue((folder / 'command.log').exists())
                self.assertIsNone(external.poll())
        finally:
            owner.cleanup()

    def test_fast_command_and_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'fast'
            result = common.execute([sys.executable, '-c', "print('done')"], folder, 5)
            self.assertEqual(result['returncode'], 0)
            self.assertEqual((folder / 'command.log').read_text(), 'done\n')
            self.assertFalse(Path('/proc', str(result['pid'])).exists())
            original = (folder / 'command.log').read_bytes()
            with self.assertRaises(FileExistsError):
                common.execute([sys.executable, '-c', "print('overwritten')"], folder, 5)
            self.assertEqual((folder / 'command.log').read_bytes(), original)

    def test_timeout_reaps_own_child_and_external_target_survives(self):
        owner = OwnedProcesses()
        try:
            external = common.launch(owner, [sys.executable, '-c', 'import time; time.sleep(30)'])
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / 'timeout'
                with self.assertRaises(TimeoutError):
                    common.execute([sys.executable, '-c', 'import time; time.sleep(30)'], folder, .1)
                result = json.loads((folder / 'result.json').read_text())
                self.assertIn('TimeoutError', result['error'])
                self.assertFalse(Path('/proc', str(result['pid'])).exists())
                self.assertIsNone(external.poll())
        finally:
            owner.cleanup()

    def test_cancel_reaps_owned_command_and_retains_interrupted_evidence(self):
        owner = OwnedProcesses()
        try:
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / 'cancel'
                code = ("from scripts.comparison_common import execute; "
                        "from perfkit.runner import install_signal_handler; "
                        "import sys; install_signal_handler(); "
                        "execute([sys.executable,'-c','import time; time.sleep(30)'],sys.argv[1],40)")
                driver = common.launch(owner, [sys.executable, '-c', code, str(folder)], cwd=common.ROOT,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                end = time.monotonic() + 5
                while not (folder / 'command.log').exists():
                    self.assertIsNone(driver.poll())
                    if time.monotonic() > end:
                        self.fail('command did not start')
                    time.sleep(.01)
                owner.signal(driver, signal.SIGTERM)
                self.assertNotEqual(driver.wait(timeout=10), 0)
                result = json.loads((folder / 'result.json').read_text())
                self.assertIn('KeyboardInterrupt', result['error'])
                if 'pid' in result:
                    self.assertFalse(Path('/proc', str(result['pid'])).exists())
        finally:
            owner.cleanup()


if __name__ == '__main__':
    unittest.main()
