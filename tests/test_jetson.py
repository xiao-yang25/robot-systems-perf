"""Known tegrastats formats and real owned-child lifecycle regressions."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from perfkit.jetson import TegrastatsCollector, parse_tegrastats


ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError('fake tegrastats did not reach expected state')


def executable(root, body):
    path = root / 'fake-tegrastats'
    path.write_text(f'#!{sys.executable}\n' + body)
    path.chmod(0o755)
    return str(path)


class ParserTests(unittest.TestCase):
    def test_documented_multi_gpc_and_scalar(self):
        values = parse_tegrastats('RAM 3/4MB GR3D_FREQ 99%@[1098,1098,1098] EMC_FREQ 95%@1600 CPU [1%@100]')
        self.assertEqual(values['gpu_utilization_percent'], 99)
        self.assertEqual(values['gpu_frequency_mhz'], [1098, 1098, 1098])
        self.assertEqual(values['emc_activity_percent'], 95)
        self.assertEqual(values['emc_frequency_mhz'], 1600)
        self.assertTrue(all(reason is None for reason in values['availability'].values()))
        self.assertEqual(parse_tegrastats('GR3D_FREQ 77%@918')['gpu_frequency_mhz'], [918])

    def test_percentage_only_and_frequency_only_do_not_infer_each_other(self):
        for text, key, expected, missing in (
                ('GR3D_FREQ 0%', 'gpu_utilization_percent', 0, 'gpu_frequency_mhz'),
                ('GR3D_FREQ @[1098, 1098]', 'gpu_frequency_mhz', [1098, 1098], 'gpu_utilization_percent'),
                ('GR3D_FREQ @0', 'gpu_frequency_mhz', [0], 'gpu_utilization_percent'),
                ('EMC_FREQ 0%', 'emc_activity_percent', 0, 'emc_frequency_mhz'),
                ('EMC_FREQ @1600', 'emc_frequency_mhz', 1600, 'emc_activity_percent')):
            with self.subTest(text=text):
                values = parse_tegrastats(text)
                self.assertEqual(values[key], expected)
                self.assertIsNone(values['availability'][key])
                self.assertIsNone(values[missing])
                self.assertIsNotNone(values['availability'][missing])

    def test_invalid_component_preserves_other_valid_component(self):
        values = parse_tegrastats('GR3D_FREQ 101%@1098 EMC_FREQ 20%@-1')
        self.assertIsNone(values['gpu_utilization_percent'])
        self.assertEqual(values['gpu_frequency_mhz'], [1098])
        self.assertEqual(values['emc_activity_percent'], 20)
        self.assertIsNone(values['emc_frequency_mhz'])
        self.assertIn('outside', values['availability']['gpu_utilization_percent'])
        self.assertIn('invalid', values['availability']['emc_frequency_mhz'])

    def test_missing_malformed_and_wrong_emc_array_are_not_zero(self):
        for text in ('RAM 0/1MB', 'GR3D_FREQ unavailable', 'GR3D_FREQ 50%@[]',
                     'GR3D_FREQ 50%@[,]', 'GR3D_FREQ nan%@nan'):
            with self.subTest(text=text):
                values = parse_tegrastats(text)
                self.assertIsNone(values['gpu_frequency_mhz'])
                self.assertIsNotNone(values['availability']['gpu_frequency_mhz'])
        values = parse_tegrastats('EMC_FREQ 0%@[100,100]')
        self.assertEqual(values['emc_activity_percent'], 0)
        self.assertIsNone(values['emc_frequency_mhz'])


class CollectorTests(unittest.TestCase):
    def test_disabled_missing_and_permission_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'raw.jsonl'
            with TegrastatsCollector(output, 0.1) as collector:
                self.assertEqual(collector.snapshot()['reason'], 'disabled')
            self.assertFalse(output.exists())
            with patch('perfkit.jetson.shutil.which', return_value=None):
                with TegrastatsCollector(output, 0.1, True) as collector:
                    self.assertEqual(collector.snapshot()['reason'], 'executable_not_found')
            denied = root / 'denied'
            denied.write_text('no executable permission')
            with TegrastatsCollector(output, 0.1, True, str(denied)) as collector:
                snap = collector.snapshot()
                self.assertEqual(snap['reason'], 'permission_denied')
                self.assertIsNone(snap['values']['gpu_utilization_percent'])

    def test_receipt_raw_zero_duplicate_stale_and_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = root / 'arguments'
            program = executable(root, f'''import sys,time
from pathlib import Path
Path({str(arguments)!r}).write_text(' '.join(sys.argv[1:]))
print('GR3D_FREQ 0%@0 EMC_FREQ 0%@0', flush=True)
print('diagnostic', file=sys.stderr, flush=True)
while True: time.sleep(0.1)
''')
            output = root / 'raw.jsonl'
            collector = TegrastatsCollector(output, 0.01, True, program)
            with collector:
                wait_for(lambda: collector.snapshot()['available'])
                wait_for(lambda: collector.stderr_path.read_text() == 'diagnostic\n')
                snap = collector.snapshot()
                self.assertEqual(arguments.read_text(), '--interval 10')
                self.assertEqual(snap['values']['gpu_utilization_percent'], 0)
                self.assertEqual(snap['values']['gpu_frequency_mhz'], [0])
                self.assertEqual(snap['values']['emc_activity_percent'], 0)
                self.assertEqual(snap['values']['emc_frequency_mhz'], 0)
                second = collector.snapshot()
                self.assertEqual(snap['sample_id'], second['sample_id'])
                second['values']['gpu_frequency_mhz'].append(123)
                second['values']['availability']['gpu_utilization_percent'] = 'changed by caller'
                self.assertEqual(collector.snapshot()['values']['gpu_frequency_mhz'], [0])
                self.assertIsNone(collector.snapshot()['values']['availability']['gpu_utilization_percent'])
                received = snap['received_monotonic_ns']
                self.assertTrue(collector.snapshot(received + 1_000_000_000)['available'])
                stale = collector.snapshot(received + 1_000_000_001)
                self.assertEqual(stale['reason'], 'stale_sample')
                self.assertIsNone(stale['values']['gpu_utilization_percent'])
                self.assertEqual(stale['age_ns'], 1_000_000_001)
                self.assertEqual(collector.snapshot(received - 1)['reason'], 'receipt_time_in_future')
                record = json.loads(output.read_text())
                self.assertEqual(record['sample_id'], snap['sample_id'])
                self.assertEqual(record['received_monotonic_ns'], received)
                self.assertEqual(record['raw'], 'GR3D_FREQ 0%@0 EMC_FREQ 0%@0')
                pid = collector._process.pid
            self.assertFalse(collector._thread.is_alive())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertEqual(collector.snapshot()['reason'], 'closed')
            collector.close()
            with self.assertRaises(RuntimeError):
                collector.__enter__()

    def test_interval_validation(self):
        for interval in (0, -1, float('inf'), float('nan'), 3_000_000, 1e308):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                TegrastatsCollector(Path('unused'), interval)

    def test_partial_initialization_reaps_child_and_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = executable(root, 'import time\ntime.sleep(30)\n')
            output = root / 'raw.jsonl'
            collector = TegrastatsCollector(output, 0.1, True, program)
            with patch('perfkit.jetson.threading.Thread.start', side_effect=RuntimeError('cannot start')):
                with self.assertRaisesRegex(RuntimeError, 'cannot start'):
                    collector.__enter__()
            self.assertIsNotNone(collector._process.poll())
            self.assertTrue(collector._raw_stream.closed)
            self.assertTrue(collector._stderr_stream.closed)
            output.write_text('existing evidence')
            with self.assertRaises(FileExistsError):
                with TegrastatsCollector(output, 0.1, True, program):
                    pass
            self.assertEqual(output.read_text(), 'existing evidence')

    def test_final_partial_line_and_stale_three_interval_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = executable(root, '''import sys,time
sys.stdout.write('GR3D_FREQ 25%@100')
sys.stdout.flush()
time.sleep(0.2)
''')
            collector = TegrastatsCollector(root / 'raw.jsonl', 0.4, True, program)
            with collector:
                wait_for(lambda: collector.snapshot()['reason'] == 'exited')
            record = json.loads(collector.raw_output.read_text())
            self.assertEqual(record['raw'], 'GR3D_FREQ 25%@100')
            self.assertEqual(collector.snapshot()['sample_id'], 1)
            # Use a long-lived child for synthetic receipt-age checks.
            program = executable(root, "import time\nprint('EMC_FREQ 10%@100',flush=True)\ntime.sleep(30)\n")
            with TegrastatsCollector(root / 'second.jsonl', 0.4, True, program) as second:
                snap = wait_for(lambda: second.snapshot() if second.snapshot()['available'] else None)
                received = snap['received_monotonic_ns']
                self.assertTrue(second.snapshot(received + 1_200_000_000)['available'])
                self.assertEqual(second.snapshot(received + 1_200_000_001)['reason'], 'stale_sample')

    def test_early_exit_diagnostic_and_no_fake_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = executable(root, "import sys\nprint('permission denied', file=sys.stderr)\nsys.exit(13)\n")
            collector = TegrastatsCollector(root / 'raw.jsonl', 0.1, True, program)
            with collector:
                snap = wait_for(lambda: collector.snapshot() if collector.snapshot()['reason'] == 'exited' else None)
                self.assertFalse(snap['available'])
                self.assertEqual(snap['status']['returncode'], 13)
                self.assertIsNone(snap['values']['gpu_utilization_percent'])
                wait_for(lambda: collector.stderr_path.read_text() == 'permission denied\n')
            self.assertFalse(collector._thread.is_alive())

    def test_long_line_and_unterminated_long_line_are_counted_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = executable(root, '''import sys,time
sys.stdout.write('x' * 200000 + '\\nGR3D_FREQ 10%@100\\n')
sys.stdout.flush()
sys.stdout.write('z' * 200000)
sys.stdout.flush()
while True: time.sleep(0.1)
''')
            collector = TegrastatsCollector(root / 'raw.jsonl', 0.01, True, program)
            with collector:
                wait_for(lambda: collector.snapshot()['status']['dropped_stdout_lines'] == 2)
                snap = collector.snapshot()
                self.assertTrue(snap['available'])
                self.assertEqual(snap['sample_id'], 1)
                self.assertEqual(snap['values']['gpu_utilization_percent'], 10)
            records = [json.loads(line) for line in collector.raw_output.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['raw'], 'GR3D_FREQ 10%@100')
            self.assertFalse(collector._thread.is_alive())

    def test_kill_fallback_does_not_signal_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = executable(root, '''import signal,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('GR3D_FREQ 50%', flush=True)
while True: time.sleep(0.1)
''')
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            try:
                collector = TegrastatsCollector(root / 'raw.jsonl', 0.01, True, program)
                collector.TERMINATE_TIMEOUT_SECONDS = 0.05
                with collector:
                    wait_for(lambda: collector.snapshot()['available'])
                    pid = collector._process.pid
                self.assertEqual(collector.snapshot()['status']['returncode'], -signal.SIGKILL)
                self.assertIsNone(unrelated.poll())
                self.assertFalse(collector._thread.is_alive())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_inherited_pipe_owner_does_not_keep_reader_alive_or_receive_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / 'descendant'
            program = executable(root, f'''import subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
Path({str(pidfile)!r}).write_text(str(p.pid))
print('GR3D_FREQ 50%', flush=True)
while True: time.sleep(0.1)
''')
            descendant = None
            collector = TegrastatsCollector(root / 'raw.jsonl', 0.01, True, program)
            try:
                with collector:
                    wait_for(lambda: collector.snapshot()['available'])
                    descendant = int(pidfile.read_text())
                self.assertFalse(collector._thread.is_alive())
                os.kill(descendant, 0)
            finally:
                collector.close()
                if descendant is not None:
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_sigint_and_sigterm_during_cleanup_are_deferred_until_reaped(self):
        for cancellation in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=cancellation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker = root / 'term'
                pidfile = root / 'pid'
                program = executable(root, f'''import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *args: Path({str(marker)!r}).touch())
Path({str(pidfile)!r}).write_text(str(os.getpid()))
print('GR3D_FREQ 50%', flush=True)
while True: time.sleep(0.01)
''')
                code = '''import sys,time
from pathlib import Path
from perfkit.jetson import TegrastatsCollector
c=TegrastatsCollector(Path(sys.argv[1]),0.01,True,sys.argv[2])
with c:
    while not c.snapshot()['available']: time.sleep(0.01)
'''
                with (root / 'owner.log').open('w') as log:
                    owner = subprocess.Popen([sys.executable, '-c', code, str(root / 'raw.jsonl'), program],
                                             cwd=ROOT, stdout=log, stderr=log)
                    try:
                        wait_for(marker.exists)
                        pid = int(pidfile.read_text())
                        owner.send_signal(cancellation)
                        self.assertNotEqual(owner.wait(timeout=10), 0)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                        self.assertIn('KeyboardInterrupt', (root / 'owner.log').read_text())
                    finally:
                        if owner.poll() is None:
                            owner.kill()
                            owner.wait()
                        if pidfile.exists():
                            try:
                                os.kill(int(pidfile.read_text()), signal.SIGKILL)
                            except ProcessLookupError:
                                pass


if __name__ == '__main__':
    unittest.main()
