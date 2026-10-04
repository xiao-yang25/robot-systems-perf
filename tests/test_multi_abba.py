import copy
import json
from pathlib import Path
import unittest

from perfkit.suite import comparison_orders, expand_suite, sampler_comparisons


class MultiAbbaTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((Path(__file__).resolve().parents[1]/'configs/suite-smoke.json').read_text())
        self.comparison = self.suite['sampler_comparisons'][0]
        self.comparison.update(repeat_blocks=2, max_p99_increase_percent=10)

    def runs(self, values):
        runs = []
        names = [name for order in comparison_orders(self.comparison) for name in order]
        planned = dict(expand_suite(self.suite))
        for index, (name, value) in enumerate(zip(names, values)):
            mode = ('minimal', 'basic', 'basic', 'minimal')[index % 4]
            result = {'scenario': 'C01', 'repetition': 1,
                'metrics': {'scenario': 'C01', 'counts': {'measured_sent': 1000,
                    'missing_delivery': 0, 'duplicate_events': 0, 'invalid_payload_events': 0,
                    'unexpected_id_events': 0}, 'distributions': {'response_time_ns': {'p99': value}}},
                'quality': {'release_late_fraction': 0, 'limits': {'min_samples': 1000,
                    'max_release_late_fraction': .01}}}
            runs.append((name, {'config': copy.deepcopy(planned[name]), 'results': [result]}))
        return runs

    def test_expansion_retains_each_consecutive_abba_block(self):
        expanded = expand_suite(self.suite)
        self.assertEqual(len(expanded), 16)
        for order in comparison_orders(self.comparison):
            positions = [next(i for i, (name, _) in enumerate(expanded) if name == wanted) for wanted in order]
            self.assertEqual(positions, list(range(positions[0], positions[0]+4)))
            self.assertEqual([expanded[i][1]['sampling_mode'] for i in positions],
                             ['minimal', 'basic', 'basic', 'minimal'])

    def test_bad_block_cannot_hide_behind_aggregate_median(self):
        runs = self.runs([100, 120, 120, 100, 100, 80, 80, 100])
        summary = sampler_comparisons(self.suite, runs)[0]
        self.assertEqual(summary['increase_percent'], 0)
        self.assertEqual(summary['block_increase_percent'], [20, -20])
        self.assertEqual(summary['budget_status'], 'exceeded')
        self.assertEqual(summary['worst_block_increase_percent'], 20)

    def test_invalid_or_missing_evidence_block_prevents_budget_pass(self):
        for failure in ('quality', 'missing', 'invalid', 'population'):
            with self.subTest(failure=failure):
                runs = self.runs([100]*8)
                last = runs[-1][1]['results'][0]
                if failure == 'quality': last['quality'] = {}
                if failure == 'missing': last['metrics']['counts']['missing_delivery'] = 1
                if failure == 'invalid': last['metrics']['counts']['invalid_payload_events'] = 1
                if failure == 'population': last['metrics']['counts']['measured_sent'] = 5
                summary = sampler_comparisons(self.suite, runs)[0]
                self.assertEqual(summary['budget_status'], 'not_evaluated')
                self.assertEqual(summary['comparison_status'], 'review_required')

    def test_no_budget_never_implies_pass(self):
        self.comparison['max_p99_increase_percent'] = None
        summary = sampler_comparisons(self.suite, self.runs([100]*8))[0]
        self.assertEqual(summary['comparison_status'], 'observed')
        self.assertEqual(summary['budget_status'], 'not_evaluated')

    def test_missing_case_or_repetition_never_passes(self):
        for failure in ('empty', 'absent', 'duplicate', 'wrong-repetition', 'missing-population'):
            with self.subTest(failure=failure):
                runs = self.runs([100]*8)
                last = runs[-1][1]
                if failure == 'empty': last['results'] = []
                elif failure == 'absent': runs.pop()
                elif failure == 'duplicate': last['results'].append(copy.deepcopy(last['results'][0]))
                elif failure == 'wrong-repetition': last['results'][0]['repetition'] = 2
                else:
                    last['config']['scenarios'] = [{'id': 'C01'}, {'id': 'S01'}]
                summaries = sampler_comparisons(self.suite, runs)
                self.assertTrue(summaries)
                self.assertTrue(all(item['budget_status'] == 'not_evaluated' for item in summaries))

    def test_zero_or_unavailable_reference_is_unavailable(self):
        for value in (0, None):
            with self.subTest(value=value):
                summary = sampler_comparisons(self.suite, self.runs([value, 100, 100, value]*2))[0]
                self.assertEqual(summary['comparison_status'], 'unavailable')
                self.assertEqual(summary['budget_status'], 'not_evaluated')

    def test_returned_config_cannot_reduce_planned_repetitions(self):
        for case in self.suite['cases']:
            if case['name'] in self.comparison['abba_cases']:
                case['repetitions'] = 2
        runs = self.runs([100]*8)
        for _, run in runs:
            run['config']['repetitions'] = 1
        summary = sampler_comparisons(self.suite, runs)[0]
        self.assertEqual(summary['comparison_status'], 'review_required')
        self.assertEqual(summary['budget_status'], 'not_evaluated')

    def test_generated_case_collision_and_unbounded_blocks_rejected(self):
        self.suite['cases'][0]['name'] = self.comparison['abba_cases'][0] + '-block-002'
        with self.assertRaises(ValueError): expand_suite(self.suite)
        for value in (0, 17, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                comparison_orders(dict(self.comparison, repeat_blocks=value))


if __name__ == '__main__':
    unittest.main()
