"""Literal contracts for a finite S01 / rt-tests comparison adapter."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import wakeup_compare as wakeup


def histogram(bins=100000, overflow=0):
    return ('T: 0 (123) P: 0 I:1000 C: %d Min: 0 Act: 1 Avg: 1 Max: 2\n# Histogram\n' % (6 + overflow)
            + ''.join('%06d %06d\n' % (i, 1 if i == 0 else 2 if i == 1 else 3 if i == 2 else 0) for i in range(bins))
            + '# Total: 6\n# Min Latencies: 0\n# Avg Latencies: 1\n# Max Latencies: 2\n'
            + '# Histogram Overflows: %d\n# Histogram Overflow at cycle number:\n# Thread 0:%s\n'
            % (overflow, ''.join(' %d' % i for i in range(overflow))))


class WakeupComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "build").mkdir()
        self.s01 = self.root / "build" / "periodic_bench"
        self.s01.write_bytes(b"literal s01 binary")
        self.s01.chmod(0o755)
        self.cyclictest = self.root / "cyclictest"
        self.cyclictest.write_bytes(b"literal rt-tests binary")
        self.cyclictest.chmod(0o755)
        self.calls = []
        for patcher in (
            patch.object(wakeup, "ROOT", self.root),
            patch.object(wakeup.os, "SCHED_OTHER", 0, create=True),
            patch.object(wakeup.os, "sched_getscheduler", return_value=0, create=True),
            patch.object(wakeup.os, "sched_getparam", return_value=type("Param", (), {"sched_priority": 0})(), create=True),
            patch.object(wakeup.os, "sched_getaffinity", return_value={2, 5}, create=True),
            patch.object(wakeup.shutil, "which", side_effect=lambda name: str(self.cyclictest) if name == "cyclictest" else "/usr/bin/taskset"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, command, folder, timeout, env=None, discover=False):
        self.calls.append((command, folder, timeout, env, discover))
        if "--period-ns" in command:
            count = int(command[command.index('--count') + 1])
            rows = ['seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured']
            for i in range(count):
                scheduled = (i + 1) * 1000000
                lateness = 10 if i == 0 else 50 if i == 2 else 30
                rows.append(f'{i},{scheduled},{scheduled+lateness},{scheduled+90},5,1')
            (folder / "samples.csv").write_text('\n'.join(rows) + '\n', encoding="utf-8")
        else:
            (folder / "command.log").write_text(histogram(), encoding="utf-8")
        return {"returncode": 0, "command": command, "elapsed_ns": 2000000000,
                "cpu_seconds": 0.01, "cpu_percent_one_core": 0.5}

    def test_literal_commands_order_metrics_and_evidence(self):
        records = wakeup.wakeup_runs(self.root / "output", 2, 3, self.execute)
        self.assertEqual([record["tool"] for record in records],
                         ["s01", "cyclictest", "cyclictest", "s01", "s01", "cyclictest"])
        self.assertEqual([record["repetition"] for record in records], [1, 1, 2, 2, 3, 3])
        first = self.calls[0]
        self.assertEqual(first[0], [str(self.s01), "--period-ns", "1000000", "--work-ns", "0",
                                   "--count", "2000", "--warmup", "0", "--output",
                                   str(first[1] / "samples.csv")])
        self.assertEqual(self.calls[1][0], [str(self.cyclictest), "--priority=0", "--policy=other",
                                           "--default-system", "--threads=1", "--clock=0",
                                           "--interval=1000", "--duration=2", "--quiet", "--histogram=100000"])
        self.assertTrue(all(call[2:] == (32, None, False) for call in self.calls))
        metrics = records[0]["metrics"]
        self.assertEqual(metrics["start_deviation_ns"],
                         {"n": 2000, "min": 10, "mean": 30, "p50": 30, "p95": 30, "p99": 30, "max": 50})
        self.assertIsNone(metrics["response_time"]["distribution"])
        self.assertIn("finish_ns", metrics["response_time"]["boundary"])
        settings = json.loads((first[1] / "settings.json").read_text())
        self.assertEqual(settings["effective_requested_affinity"], [2, 5])
        self.assertEqual(settings["tool_evidence"]["sha256"], hashlib.sha256(b"literal s01 binary").hexdigest())
        self.assertIsNone(records[1]["metrics"]["start_deviation_ns"])
        self.assertIsNone(records[1]["settings"]["requested_sample_count"])
        self.assertEqual(records[1]['metrics']['sampling_evidence']['samples'], 6)
        self.assertEqual((self.calls[1][1] / "command.log").read_text(), histogram())

    def test_selected_cpu_wraps_both_commands_without_mutating_host(self):
        with patch.object(wakeup.os, "sched_setaffinity", create=True) as affinity_set, \
                patch.object(wakeup.os, "sched_setscheduler", create=True) as policy_set:
            records = wakeup.wakeup_runs(self.root / "cpu-output", 1, 1, self.execute, cpu=5)
        self.assertTrue(all(call[0][:3] == ["/usr/bin/taskset", "-c", "5"] for call in self.calls))
        self.assertEqual(records[0]["settings"]["effective_requested_affinity"], [5])
        affinity_set.assert_not_called()
        policy_set.assert_not_called()

    def test_invalid_cpu_policy_and_inputs_fail_before_execution(self):
        for cpu in (0, 3, True, "2"):
            with self.subTest(cpu=cpu), self.assertRaises(ValueError):
                wakeup.wakeup_runs(self.root / "invalid", 1, 1, self.execute, cpu=cpu)
        for field, value in (("seconds", 0), ("seconds", 1001), ("seconds", True),
                             ("seconds", 1.1), ("repetitions", 0), ("repetitions", True)):
            values = {"seconds": 1, "repetitions": 1, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                wakeup.wakeup_runs(self.root / "invalid", execute=self.execute, **values)
        with patch.object(wakeup.os, "sched_getscheduler", return_value=1), \
                self.assertRaisesRegex(RuntimeError, "SCHED_OTHER"):
            wakeup.wakeup_runs(self.root / "invalid", 1, 1, self.execute)
        with patch.object(wakeup.os, "sched_getparam", return_value=type("Param", (), {"sched_priority": 2})()), \
                self.assertRaisesRegex(RuntimeError, "priority 0"):
            wakeup.wakeup_runs(self.root / "invalid", 1, 1, self.execute)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "invalid").exists())

    def test_missing_capabilities_fail_without_substituting_tools(self):
        with patch.object(wakeup.shutil, "which", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "cyclictest is unavailable"):
            wakeup.wakeup_runs(self.root / "missing", 1, 1, self.execute)
        with patch.object(wakeup.shutil, "which", side_effect=lambda name: str(self.cyclictest) if name == "cyclictest" else None), \
                self.assertRaisesRegex(RuntimeError, "taskset"):
            wakeup.wakeup_runs(self.root / "missing", 1, 1, self.execute, cpu=2)
        self.s01.unlink()
        with self.assertRaisesRegex(RuntimeError, "built executable missing"):
            wakeup.wakeup_runs(self.root / "missing", 1, 1, self.execute)
        self.assertEqual(self.calls, [])

    def test_failure_preserves_manifest_and_propagates_without_retry(self):
        def fail(command, folder, timeout):
            self.calls.append(command)
            (folder / "stderr.txt").write_text("unsupported option\n", encoding="utf-8")
            raise RuntimeError("tool failed")
        with self.assertRaisesRegex(RuntimeError, "tool failed"):
            wakeup.wakeup_runs(self.root / "failed", 1, 3, fail)
        folder = self.root / "failed" / "wakeup-01-s01"
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((folder / "settings.json").is_file())
        self.assertFalse((folder / "comparison.json").exists())
        self.assertFalse((self.root / "failed" / "wakeup-01-cyclictest").exists())

    def test_existing_output_is_never_overwritten_even_for_later_run(self):
        folder = self.root / "existing" / "wakeup-02-cyclictest"
        folder.mkdir(parents=True)
        raw = folder / "stdout.txt"
        raw.write_text("prior evidence", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            wakeup.wakeup_runs(self.root / "existing", 1, 2, self.execute)
        self.assertEqual(raw.read_text(), "prior evidence")
        self.assertEqual(self.calls, [])

    def test_empty_truncated_and_unexpected_warmup_are_rejected(self):
        header = 'seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured\n'
        for name, rows in [('empty', ''), ('truncated', '0,1000000,1000030,1000090,5,1\n'),
                           ('warmup', '0,1000000,1000030,1000090,5,0\n')]:
            with self.subTest(name=name):
                def incomplete(command, folder, timeout):
                    (folder / 'samples.csv').write_text(header + rows)
                    return {'returncode': 0}
                with self.assertRaisesRegex(RuntimeError, 'sample counts'):
                    wakeup.wakeup_runs(self.root / name, 2, 1, incomplete)
                self.assertFalse((self.root / name / 'wakeup-01-cyclictest').exists())

    def test_histogram_counts_include_overflow_without_claiming_quantiles(self):
        path = self.root / 'histogram.log'
        path.write_text(histogram(3, overflow=2))
        result = wakeup.validate_cyclictest_log(path, bins=3)
        self.assertEqual((result['samples'], result['in_range_samples'], result['overflow_samples']), (8, 6, 2))
        path.write_text(histogram(3, overflow=2).replace('# Thread 0: 0 1', '# Thread 0: 0 # 1 others'))
        self.assertEqual(wakeup.validate_cyclictest_log(path, bins=3)['samples'], 8)

    def test_unqualified_cyclictest_outputs_are_rejected(self):
        valid = histogram(3)
        cases = ['', 'Usage: cyclictest <options>\n', 'unknown format\n',
                 histogram(3, overflow=2).replace('# Thread 0: 0 1', '# Thread 0:'),
                 histogram(3, overflow=2).replace('# Thread 0: 0 1', '# Thread 0: 0 # 99 others'),
                 valid.replace('000002 000003\n', ''), valid.replace('# Total: 6', '# Total: 7'),
                 valid.replace('C: 6', 'C: 0'), valid.replace('# Thread 0:\n', ''),
                 valid.replace('000001 000002', '000001 -2'), valid.replace('# Total: 6', '# Total: 6\n# Total: 6'),
                 valid.replace('000000 000001', '000000 000000').replace('000001 000002', '000001 000000')
                      .replace('000002 000003', '000002 000000').replace('# Total: 6', '# Total: 0').replace('C: 6', 'C: 0')]
        path = self.root / 'invalid.log'
        for text in cases:
            with self.subTest(text=text[:80]):
                path.write_text(text)
                with self.assertRaises(RuntimeError):
                    wakeup.validate_cyclictest_log(path, bins=3)


if __name__ == "__main__":
    unittest.main()
