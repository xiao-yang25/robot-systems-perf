"""Literal intervals distinguish the pipeline's pre-log and post-flush costs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from perfkit.resources import summarize_costs


class CompletionTests(unittest.TestCase):
    def records(self):
        first = {'schema_version': 1, 'cycle_id': 1, 'start_ns': 100, 'end_ns': 120,
                 'duration_ns': 20, 'cadence_ns': 100, 'phase_costs_ns': {'encode': 5}}
        first_done = {'cycle_id': 1, 'start_ns': 100, 'end_ns': 230,
                      'duration_ns': 130, 'cadence_ns': 100, 'thread_cpu_ns': 30}
        second = dict(first, cycle_id=2, start_ns=300, end_ns=320,
                      previous_cycle_completion=first_done)
        terminal = {'schema_version': 1, 'record_type': 'terminal_completion',
                    'completion': {'cycle_id': 2, 'start_ns': 300, 'end_ns': 340,
                                   'duration_ns': 40, 'cadence_ns': 100, 'thread_cpu_ns': 25}}
        return [first, second, terminal]

    def summarize(self, records, start=0, end=1000):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'costs.jsonl'
            path.write_text(''.join(json.dumps(item)+'\n' for item in records))
            return summarize_costs(path, start, end)

    def test_known_flush_delay_counts_as_over_period(self):
        result = self.summarize(self.records())
        self.assertEqual(result['cycles'], 2)
        self.assertEqual(result['over_period_cycles'], 0)
        self.assertEqual(result['completed_cycles'], 2)
        self.assertEqual(result['full_over_period_cycles'], 1)
        self.assertEqual(result['full_cycle_fraction_max'], 1.3)
        self.assertEqual(result['phase_costs_ns']['full_pipeline_cycle']['sum_ns'], 170)
        self.assertEqual(result['phase_thread_cpu_ns']['full_pipeline_cycle']['sum_ns'], 55)

    def test_window_does_not_include_completion_outside_it(self):
        result = self.summarize(self.records(), end=200)
        self.assertEqual(result['cycles'], 1)
        self.assertEqual(result['completed_cycles'], 0)
        self.assertIsNone(result['full_cycle_fraction_max'])

    def test_legacy_or_interrupted_log_keeps_missing_completion_visible(self):
        records = self.records()
        legacy = self.summarize([records[0]])
        self.assertEqual(legacy['cycles'], 1)
        self.assertEqual(legacy['completed_cycles'], 0)
        interrupted = self.summarize(records[:2])
        self.assertEqual(interrupted['cycles'], 2)
        self.assertEqual(interrupted['completed_cycles'], 1)

    def test_mismatched_and_repeated_completions_are_rejected(self):
        for key, value in (('cycle_id', 2), ('start_ns', 101), ('end_ns', 119),
                           ('duration_ns', 129), ('cadence_ns', True), ('thread_cpu_ns', -1)):
            rows = self.records()
            rows[1]['previous_cycle_completion'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'cost record'):
                self.summarize(rows)
        rows = self.records()
        with self.assertRaisesRegex(ValueError, 'cost record'):
            self.summarize(rows + [copy.deepcopy(rows[0])])
