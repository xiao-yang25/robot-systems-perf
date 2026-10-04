"""Real C01/S01 capture: additive evidence and unconfigured acceptance stay honest."""
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from perfkit.runner import run_experiment
from perfkit.suite import run_suite

ROOT = Path(__file__).resolve().parents[1]


def main(output):
    config = json.loads((ROOT/'configs/smoke.json').read_text())
    config.update(warmup_seconds=.1, measurement_seconds=1, repetitions=2, drain_seconds=.2)
    for scenario in config['scenarios']:
        scenario.update(deadline_us=None, max_data_age_us=None,
                        acceptance_limits={'max_deadline_miss_fraction': None,
                                           'max_data_age_expired_fraction': None})
    run = run_experiment(config, output/'smoke')
    for result in run['results']:
        states = result['acceptance']
        assert states['execution']['status'] == 'complete'
        assert states['deadline']['status'] == 'not_configured'
        assert states['data_age']['status'] == 'not_configured'
        assert states['collector_budget']['status'] == 'not_configured'
        cost = result['resources']['collection_cost']
        assert cost['completed_cycles'] > 0
        if cost['completed_cycles'] == cost['cycles']:
            assert cost['full_cycle_fraction_max'] >= cost['cycle_fraction_max']
        if result['scenario'] != 'C01':
            continue
        for role in ('publisher', 'subscriber'):
            evidence = result['runtime_evidence'][role]
            assert evidence['identity_matches']
            assert evidence['transport_verified'] is False
            for phase in ('start', 'end'):
                snapshot = evidence['snapshots'][phase]
                assert snapshot['available'], snapshot['reason']
                metadata = snapshot['metadata']
                assert metadata['actual_qos']['available']
                assert metadata['rmw_identifier']['available']
                assert metadata['identity_query_phase'] == 'start'
                assert any(item['available'] and item['sha256'] for item in snapshot['libraries'])
        for event in result['metrics']['diagnostics']['response_tail_decomposition']:
            assert sum(event[name] for name in ('release_lateness_ns', 'generation_to_publish_ns',
                'publish_to_callback_ns', 'callback_wall_time_ns')) == event['response_time_ns']
    suite = json.loads((ROOT/'configs/suite-smoke.json').read_text())
    order = suite['sampler_comparisons'][0]['abba_cases']
    suite['cases'] = [case for case in suite['cases'] if case['name'] in order]
    suite['defaults'].update(warmup_seconds=.1, measurement_seconds=1, drain_seconds=.2)
    suite['sampler_comparisons'][0].update(repeat_blocks=2, max_p99_increase_percent=None)
    run_suite(suite, output/'abba')
    summary = json.loads((output/'abba/suite-summary.json').read_text())
    assert len(summary['cases']) == 8
    for comparison in summary['sampler_comparisons']:
        assert comparison['repeat_blocks'] == 2
        assert comparison['budget_status'] == 'not_evaluated'
        assert all(all(block['case_manifest_complete'].values()) for block in comparison['blocks'])
    print('PASS: real C01/S01 evidence, post-flush costs, tail boundaries and two ABBA blocks')


if __name__ == '__main__':
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=False)
    main(output)
