import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit.suite import expand_suite, run_suite, sampler_comparisons


ROOT = Path(__file__).resolve().parents[1]


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((ROOT / 'configs/suite-smoke.json').read_text())

    def test_checked_in_suites_are_bounded_and_explicit(self):
        for name in ('suite-smoke', 'jetson-suite'):
            cases = expand_suite(json.loads((ROOT / f'configs/{name}.json').read_text()))
            self.assertEqual(len(cases), 12)
            self.assertEqual(cases[-1][1]['sampling_mode'], 'minimal')

    def test_unsafe_or_duplicate_case_names_rejected(self):
        for name in ('../escape', '/escape', self.suite['cases'][0]['name']):
            bad = copy.deepcopy(self.suite)
            bad['cases'][1]['name'] = name
            with self.assertRaises(ValueError):
                expand_suite(bad)

    def test_abba_rejects_workload_change_or_execution_order_change(self):
        bad = copy.deepcopy(self.suite)
        bad['cases'][-1]['scenarios'][0]['payload_bytes'] = 100
        with self.assertRaisesRegex(ValueError, 'identical workloads'):
            expand_suite(bad)
        bad = copy.deepcopy(self.suite)
        bad['cases'][-1], bad['cases'][-2] = bad['cases'][-2], bad['cases'][-1]
        with self.assertRaisesRegex(ValueError, 'consecutive'):
            expand_suite(bad)

    def test_comparison_preserves_each_run_and_has_no_pooled_percentile(self):
        order = ['sampler-min-a', 'sampler-basic-a', 'sampler-basic-b', 'sampler-min-b']
        runs = []
        for name, mode, p99 in zip(order, ['minimal', 'basic', 'basic', 'minimal'], [100, 240, 260, 300]):
            runs.append((name, {'config': {'sampling_mode': mode}, 'results': [
                {'scenario': 'C01', 'metrics': {'distributions': {'response_time_ns': {'p99': p99}}}}]}))
        summary = sampler_comparisons(self.suite, runs)[0]
        self.assertEqual(summary['minimal_per_run_values'], [100, 300])
        self.assertEqual(summary['basic_per_run_values'], [240, 260])
        self.assertEqual(summary['increase_percent'], 25)
        self.assertEqual(summary['budget_status'], 'not_evaluated')
        self.assertNotIn('pooled_p99', summary)

    def test_failure_preserves_completed_case_and_aborts_remaining_work(self):
        suite = copy.deepcopy(self.suite)
        suite['cases'] = suite['cases'][:3]
        suite['sampler_comparisons'] = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            with patch('perfkit.suite.run_experiment', side_effect=[{'config': {}, 'results': []}, RuntimeError('capture failed')]) as run:
                with self.assertRaisesRegex(RuntimeError, 'capture failed'):
                    run_suite(suite, output)
                self.assertEqual(run.call_count, 2)
            status = json.loads((output / 'suite-status.json').read_text())
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(status['completed_cases'], ['c01-reference'])
            self.assertFalse((output / 'suite-summary.json').exists())

    def test_empty_delivery_comparison_is_unavailable_without_inventing_zero(self):
        order = self.suite['sampler_comparisons'][0]['abba_cases']
        runs = [(name, {'config': {'sampling_mode': mode}, 'results': [
            {'scenario': 'C01', 'metrics': {'counts': {'missing_delivery': 5},
             'distributions': {'response_time_ns': {'p99': None}}}}]})
                for name, mode in zip(order, ['minimal', 'basic', 'basic', 'minimal'])]
        result = sampler_comparisons(self.suite, runs)[0]
        self.assertEqual(result['minimal_per_run_values'], [None, None])
        self.assertIsNone(result['increase_percent'])
        self.assertEqual(result['comparison_status'], 'unavailable')
        self.assertEqual(result['budget_status'], 'not_evaluated')

    def test_delivery_loss_cannot_pass_sampling_budget_from_conditional_latency(self):
        suite = copy.deepcopy(self.suite)
        suite['sampler_comparisons'][0]['max_p99_increase_percent'] = 50
        order = suite['sampler_comparisons'][0]['abba_cases']
        runs = [(name, {'config': {'sampling_mode': mode}, 'results': [
            {'scenario': 'C01', 'metrics': {'counts': {'missing_delivery': 1},
             'distributions': {'response_time_ns': {'p99': 100}}}}]})
                for name, mode in zip(order, ['minimal', 'basic', 'basic', 'minimal'])]
        result = sampler_comparisons(suite, runs)[0]
        self.assertEqual(result['comparison_status'], 'review_required')
        self.assertEqual(result['budget_status'], 'not_evaluated')


if __name__ == '__main__':
    unittest.main()
