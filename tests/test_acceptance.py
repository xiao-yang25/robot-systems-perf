import copy
import unittest

from perfkit.acceptance import evaluate_run, resource_state, validate_limits


def completed():
    return {'metrics': {'scenario': 'C01', 'counts': {'measured_sent': 1000,
                'missing_delivery': 0, 'invalid_payload_events': 0, 'duplicate_events': 0,
                'unexpected_id_events': 0}, 'deadline': {'deadline_ns': 100, 'violation_fraction': 0},
                'data_age_threshold': {'max_data_age_ns': 80, 'expired_fraction': 0}},
            'quality': {'release_late_fraction': 0, 'limits': {'min_samples': 1000,
                'max_release_late_fraction': .01}, 'warnings': ['Resource sampler disabled.']},
            'resources': {'available': False, 'reason': 'sampler disabled'}}


class AcceptanceTests(unittest.TestCase):
    def test_threshold_alone_does_not_invent_acceptance_fraction(self):
        result = evaluate_run(completed(), {})
        self.assertEqual(result['input']['status'], 'valid')
        self.assertEqual(result['delivery']['status'], 'complete')
        self.assertEqual(result['deadline']['status'], 'not_configured')
        self.assertEqual(result['collector_budget']['status'], 'not_evaluated')

    def test_explicit_zero_budget_and_input_warning_independence(self):
        result = evaluate_run(completed(), {'acceptance_limits': {'max_deadline_miss_fraction': 0,
                                                                 'max_data_age_expired_fraction': 0}})
        self.assertEqual(result['deadline']['status'], 'within_observed_scope')
        self.assertEqual(result['data_age']['status'], 'within_observed_scope')
        self.assertEqual(result['resources']['status'], 'unavailable')

    def test_observed_miss_fraction_exceeds_limit(self):
        run = completed()
        run['metrics']['deadline']['violation_fraction'] = .02
        verdict = evaluate_run(run, {'acceptance_limits': {'max_deadline_miss_fraction': .01}})
        self.assertEqual(verdict['deadline']['status'], 'exceeded')

    def test_missing_or_invalid_input_never_passes_deadline_budget(self):
        for kind in ('missing', 'invalid', 'duplicate', 'unexpected', 'late', 'few', 'no_quality'):
            with self.subTest(kind=kind):
                run = completed()
                fields = {'missing': 'missing_delivery', 'invalid': 'invalid_payload_events',
                          'duplicate': 'duplicate_events', 'unexpected': 'unexpected_id_events'}
                if kind in fields:
                    run['metrics']['counts'][fields[kind]] = 1
                elif kind == 'late':
                    run['quality']['release_late_fraction'] = .02
                elif kind == 'few':
                    run['metrics']['counts']['measured_sent'] = 10
                else:
                    run['quality'] = {}
                verdict = evaluate_run(run, {'acceptance_limits': {'max_deadline_miss_fraction': 0}})
                self.assertEqual(verdict['deadline']['status'], 'not_evaluated')

    def test_release_limit_unconfigured_is_explicit(self):
        run = completed()
        run['quality']['limits'].pop('max_release_late_fraction')
        self.assertEqual(evaluate_run(run, {})['input']['status'], 'not_configured')

    def test_missing_threshold_cannot_pass_fraction_budget(self):
        run = completed()
        run['metrics']['deadline']['deadline_ns'] = None
        self.assertEqual(evaluate_run(run, {'acceptance_limits': {'max_deadline_miss_fraction': 0}})
                         ['deadline']['status'], 'not_configured')

    def test_resource_identity_gap_is_partial_and_optional_sensor_independent(self):
        data = {'source_coverage': {'process': {'samples': 3}},
                'registered_entities': {'1': {'kind': 'process', 'cpu_percent_one_core': 0}},
                'availability': {'jetson.gpu_utilization_percent': {'unavailable_samples': 3}}}
        self.assertEqual(resource_state(data)['status'], 'observed')
        data['registered_entities']['1']['cpu_percent_one_core'] = None
        self.assertEqual(resource_state(data)['status'], 'partial')
        data['registered_entities']['1']['cpu_percent_one_core'] = 0
        data['registered_entities']['2'] = {'kind': 'thread', 'cpu_percent_one_core': None}
        self.assertEqual(resource_state(data)['status'], 'partial')

    def test_fraction_configuration_rejects_invalid_values(self):
        for value in (True, float('nan'), float('inf'), -.1, 1.1, '0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_limits({'max_deadline_miss_fraction': value})
        for value in ([], {'unknown': None}):
            with self.assertRaises(ValueError):
                validate_limits(value)
        validate_limits({'max_deadline_miss_fraction': None})


if __name__ == '__main__':
    unittest.main()
