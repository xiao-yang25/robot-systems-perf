import copy
import unittest
from tests.integration_ros_evidence import verify_endpoint_qos


def topic(history='UNKNOWN', reported=0):
    qos = {'reliability': {'name': 'RELIABLE'}, 'history': {'name': history},
           'reported_depth': reported, 'depth': None if history == 'UNKNOWN' else reported,
           'depth_reason': 'RMW graph does not expose queue depth' if history == 'UNKNOWN' else None}
    return {role: [{'qos': copy.deepcopy(qos)} for _ in range(2)]
            for role in ('publishers', 'subscriptions')}


class EndpointContractTests(unittest.TestCase):
    def test_unknown_and_known_depth_are_valid_for_both_endpoint_roles(self):
        for value in (topic(), topic('KEEP_LAST', 3)):
            verify_endpoint_qos(value)

    def test_missing_reason_or_fabricated_unknown_depth_fails(self):
        for role in ('publishers', 'subscriptions'):
            for field, value in (('depth_reason', None), ('depth', 0)):
                row = topic(); row[role][0]['qos'][field] = value
                with self.subTest(role=role, field=field), self.assertRaises(AssertionError):
                    verify_endpoint_qos(row)

    def test_known_depth_and_reliability_must_match_fixture(self):
        for role in ('publishers', 'subscriptions'):
            for field, value in (('history', {'name': 'KEEP_ALL'}), ('history', {'name': 'SYSTEM_DEFAULT'}),
                                 ('depth', 2), ('reported_depth', 2), ('depth_reason', 'missing'),
                                 ('reliability', {'name': 'BEST_EFFORT'})):
                row = topic('KEEP_LAST', 3); row[role][0]['qos'][field] = value
                with self.subTest(role=role, field=field), self.assertRaises(AssertionError):
                    verify_endpoint_qos(row)
