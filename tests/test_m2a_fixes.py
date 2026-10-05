"""Read ordering, scoped discovery and failure evidence regression boundaries."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from perfkit import intake, monitor
from perfkit.discovery import scan_processes
from perfkit.workload import BusinessRelations, WorkloadSelector, validate_workload
from tests.test_discovery import fixture, task_record
from tests.test_monitor import FakeClock, IdleSampler, resource_summary


def declared(*functions):
    return validate_workload({'format_version': 1, 'workload_id': 'bounded-demo', 'functions': list(functions)})


def pid_function(name, pids, **filters):
    return {'id': name, 'process_selector': dict(pids=pids, **filters)}


class IntakeOrderingTests(unittest.TestCase):
    def test_cli_conflict_does_not_read_missing_or_unreadable_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / 'metadata.json'
            metadata.write_text('{"format_version": 1}')
            for unreadable in (False, True):
                if not unreadable:
                    metadata.unlink()
                else:
                    metadata.write_text('{"format_version": 1}')
                reads = []
                original = Path.read_text
                def read(path, *args, **kwargs):
                    if path == metadata:
                        reads.append(path)
                        raise PermissionError('controlled unreadable metadata')
                    return original(path, *args, **kwargs)
                output = root / ('output-' + str(unreadable))
                with patch('sys.argv', ['intake', '--metadata', str(metadata), '--output', str(output),
                           '--skip-temperature', '--require-capability', 'thermal']), \
                     patch.object(Path, 'read_text', read), patch.object(intake, 'collect_profile') as collect, \
                     redirect_stdout(io.StringIO()) as printed:
                    self.assertEqual(intake.main(), 1)
                self.assertEqual(reads, [])
                collect.assert_not_called()
                self.assertIn('conflicts', printed.getvalue())
                self.assertFalse(output.exists())

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO required')
    def test_actual_cli_conflict_never_opens_fifo_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo, output = root / 'metadata.fifo', root / 'output'
            os.mkfifo(fifo)
            completed = subprocess.run([sys.executable, '-m', 'perfkit.intake', '--metadata', str(fifo),
                '--output', str(output), '--skip-temperature', '--require-capability', 'thermal'],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=2)
            self.assertEqual(completed.returncode, 1)
            self.assertIn('conflicts', completed.stdout)
            self.assertFalse(output.exists())

    def test_api_checks_shared_options_before_metadata_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(intake, 'validate_metadata') as metadata:
            output = Path(directory) / 'output'
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                intake.run_intake(output, metadata={'format_version': 1}, skip_temperature=True,
                                  required_capabilities=['thermal'])
            metadata.assert_not_called()
            self.assertFalse(output.exists())


class PIDScopeTests(unittest.TestCase):
    def test_pure_pid_scan_reads_only_union_and_counts_before_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid in (1, 2, 3, 4):
                fixture(root, pid=pid, uid=os.getuid())
            workload = declared(pid_function('a', [1, 2]), pid_function('b', [2]))
            selector = WorkloadSelector(monitor.validate_config({'max_targets': 1}), workload, 100)
            read, link = Path.read_text, os.readlink
            def guard(path, *args, **kwargs):
                self.assertIn(Path(path).parent.name, ('1', '2'), 'unrequested PID detail read')
                return read(path, *args, **kwargs)
            def guard_link(path, *args, **kwargs):
                self.assertIn(Path(path).parent.name, ('1', '2'), 'unrequested PID exe read')
                return link(path, *args, **kwargs)
            with patch.object(Path, 'read_text', guard), patch('os.readlink', guard_link):
                inventory = scan_processes(root, _scope=selector)
            self.assertEqual(inventory['scan']['mode'], 'explicit_pids')
            self.assertEqual([item['pid'] for item in inventory['processes']], [1, 2])
            decision = selector.update(inventory['processes'], 100)
            relation = BusinessRelations(workload, None).observe(decision, {}, inventory, 99, 100)
            self.assertEqual(relation['functions'][0]['status'], 'ambiguous')
            self.assertEqual(relation['functions'][0]['matching_count'], 2)
            self.assertEqual(relation['functions'][1]['matching_count'], 1)
            self.assertEqual(relation['functions'][1]['omitted_by_target_cap'], 1)

    def test_mixed_names_keep_full_discovery_and_function_hard_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, pid=1, uid=os.getuid())
            fixture(root, pid=2, uid=os.getuid(), comm='named')
            fixture(root, pid=3, uid=os.getuid())
            workload = declared(pid_function('a', [1], cgroup_patterns=['^/robot.slice/']),
                                {'id': 'b', 'process_selector': {'include_names': ['^named$']}})
            selector = WorkloadSelector(monitor.validate_config({}), workload, 100)
            inventory = scan_processes(root, _scope=selector)
            self.assertEqual(inventory['scan']['mode'], 'scoped_discovery')
            self.assertEqual(inventory['scan']['process_count'], 3)
            decision = selector.update(inventory['processes'], 100)
            self.assertEqual([item['pid'] for item in decision['targets']], [1, 2])
            self.assertEqual([item['pid'] for item in decision['workload_matches']['a']], [1])

    def test_pure_pid_hard_filters_identity_recheck_and_missing_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid in range(1, 7):
                fixture(root, pid=pid, uid=os.getuid(), comm='excluded' if pid == 2 else 'worker')
            (root / '3/status').write_text('Uid:\t{}\t0\t0\t0\n'.format(os.getuid()+1))
            (root / '4/cgroup').write_text('0::/other\n')
            (root / '6/status').unlink()
            workload = declared(pid_function('a', list(range(1, 7)), exclude_names=['^excluded$'],
                                          cgroup_patterns=['^/robot.slice/']))
            selector = WorkloadSelector(monitor.validate_config({}), workload, 100)
            original, counts = Path.read_text, []
            def changed(path, *args, **kwargs):
                if path == root / '5/stat':
                    counts.append(True)
                    if len(counts) == 2:
                        return task_record(pid=5, start=501)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', changed):
                inventory = scan_processes(root, _scope=selector)
            self.assertEqual(inventory['scan']['mode'], 'explicit_pids')
            decision = selector.update(inventory['processes'], 100)
            self.assertEqual([item['pid'] for item in decision['targets']], [1])
            self.assertEqual(inventory['scan']['skipped_by_reason'],
                             {'identity_changed': 1, 'status_missing': 1})
            relation = BusinessRelations(workload, None).observe(decision, {}, inventory, 99, 100)
            self.assertFalse(relation['functions'][0]['scope_complete'])


class FailureClosureTests(unittest.TestCase):
    def capture_failure(self, directory, phase=None):
        output = Path(directory) / 'capture'
        primary = RuntimeError('controlled discovery failure')
        original_open, original_json = Path.open, monitor._json
        class Sampler(IdleSampler):
            def __exit__(self, *args):
                super().__exit__(*args)
                if phase == 'sampler_close': raise OSError('controlled sampler close failure')
        class BrokenStream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): self.stream.__enter__(); return self
            def __exit__(self, *args):
                result = self.stream.__exit__(*args)
                if phase == 'stream_close': raise OSError('controlled stream close failure')
                return result
            def write(self, value):
                if phase == 'terminal_write' and 'observation_ended' in value:
                    raise OSError('controlled terminal write failure')
                return self.stream.write(value)
            def flush(self): return self.stream.flush()
        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return BrokenStream(stream) if path.name == 'business-relations.jsonl' else stream
        def status_write(path, value):
            if phase == 'status_write' and path.name == 'monitor-status.json' and value['status'] == 'failed':
                raise OSError('controlled status write failure')
            return original_json(path, value)
        with patch.object(monitor, 'time', FakeClock()), patch.object(monitor, 'ResourceSampler', Sampler), \
             patch.object(monitor, 'collect_profile', return_value={}), \
             patch.object(monitor, '_source_record', return_value={}), patch.object(Path, 'open', opened), \
             patch.object(monitor, '_json', side_effect=status_write), \
             patch.object(monitor, 'scan_processes', side_effect=[{'processes': [], 'scan': {}}, primary]):
            with self.assertRaises(RuntimeError) as caught:
                monitor.run_monitor({'duration_seconds': 1, 'discovery_interval_seconds': .5}, output,
                                    workload=declared(pid_function('a', [42])))
        self.assertIs(caught.exception, primary)
        self.assertTrue(IdleSampler.instances[-1].closed)
        rows = [json.loads(line) for line in (output / 'business-relations.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0]['event'], 'business_relation_scan')
        return output, rows

    def test_failure_records_end_without_fabricating_metric_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            output, rows = self.capture_failure(directory)
            self.assertEqual(rows[-1]['event'], 'observation_ended')
            self.assertEqual(rows[-1]['outcome'], 'failed')
            self.assertIn('RuntimeError', rows[-1]['reason'])
            status = json.loads((output / 'monitor-status.json').read_text())
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(status['error'], 'controlled discovery failure')
            self.assertIsNotNone(status['window_end_ns'])
            self.assertEqual(status['window_end_ns'], rows[-1]['monotonic_ns'])
            self.assertFalse((output / 'monitor-summary.json').exists())

    def test_cleanup_errors_never_replace_primary_error_or_prior_rows(self):
        for phase in ('terminal_write', 'stream_close', 'status_write', 'sampler_close'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                output, rows = self.capture_failure(directory, phase)
                if phase != 'status_write':
                    status = json.loads((output / 'monitor-status.json').read_text())
                    self.assertEqual(status['status'], 'failed')
                    self.assertEqual(status['error_type'], 'RuntimeError')
                    self.assertTrue(status['cleanup_errors'])
                if phase != 'terminal_write':
                    self.assertEqual(rows[-1]['event'], 'observation_ended')

    def test_saved_interrupt_survives_summary_read_write_and_report_failures(self):
        for phase in ('summary_read', 'summary_write', 'report_write'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'capture'
                primary = KeyboardInterrupt('controlled primary interruption')
                original_json = monitor._json
                def write(path, value):
                    if phase == 'summary_write' and path.name == 'monitor-summary.json':
                        raise OSError('controlled summary_write failure')
                    return original_json(path, value)
                def summarize(*args):
                    if phase == 'summary_read': raise OSError('controlled summary_read failure')
                    return resource_summary()
                def report(*args):
                    if phase == 'report_write': raise OSError('controlled report_write failure')
                with patch.object(monitor, 'time', FakeClock()), \
                     patch.object(monitor, 'ResourceSampler', IdleSampler), \
                     patch.object(monitor, 'collect_profile', return_value={}), \
                     patch.object(monitor, '_source_record', return_value={}), \
                     patch.object(monitor, 'scan_processes', side_effect=primary), \
                     patch.object(monitor, 'summarize_resources', side_effect=summarize), \
                     patch.object(monitor, '_json', side_effect=write), \
                     patch.object(monitor, 'write_monitor_report', side_effect=report):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        monitor.run_monitor({}, output, workload=declared(pid_function('a', [42])))
                self.assertIs(caught.exception, primary)
                self.assertTrue(IdleSampler.instances[-1].closed)
                status = json.loads((output / 'monitor-status.json').read_text())
                self.assertEqual(status['status'], 'interrupted')
                self.assertEqual(status['error_type'], 'KeyboardInterrupt')
                self.assertEqual(status['error'], 'controlled primary interruption')
                self.assertTrue(any(phase in row['error'] for row in status['cleanup_errors']))
                terminal = json.loads((output / 'business-relations.jsonl').read_text().splitlines()[-1])
                self.assertEqual(terminal['outcome'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
