"""Independent known counters for sparse acquisition and optional observation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit import resources as r
from tests.test_resources import sample, task_stat


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'resources.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def sparse(self, now, due, user=10):
        row = sample(now, user)
        row['source_windows'] = {name: {'start_ns': now + 10, 'end_ns': now + 20} for name in due}
        row['sample_end_ns'] = now + 100
        row['observer'] = {'process_cpu_ns': user * 1000}
        if 'system' not in due:
            row['system'] = None
        if 'cgroup' not in due:
            row['cgroups'] = {}
        process = row['processes'][0]
        process['process_sampled'] = 'process' in due
        process['tasks_sampled'] = 'thread' in due
        if 'process' not in due:
            process.update(stat=None, schedstat=None, status=None, availability={})
        if 'thread' not in due:
            process.update(tasks=None, tasks_reason='not scheduled this cycle')
        return row

    def summarize(self, rows):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return r.summarize_resources(self.path, 0, 10**10)

    def test_sparse_sources_keep_chains_and_independent_thread_stat(self):
        rows = [self.sparse(10**9, {'system', 'process', 'cgroup', 'observer'}, 10),
                self.sparse(2 * 10**9, {'thread', 'observer'}, 20),
                self.sparse(3 * 10**9, {'system', 'process', 'cgroup', 'observer'}, 30),
                self.sparse(4 * 10**9, {'thread', 'observer'}, 40)]
        report = self.summarize(rows)
        entities = list(report['registered_entities'].values())
        process = next(item for item in entities if item['kind'] == 'process')
        thread = next(item for item in entities if item['kind'] == 'thread')
        for item in (process, thread):
            self.assertEqual(item['delta']['cpu_ticks'], 20)
            self.assertEqual(item['covered_ns']['cpu_ticks'], 2 * 10**9)
            self.assertEqual(item['cpu_percent_one_core'], 10)
        self.assertEqual(report['system']['per_core_cpu']['cpu0']['valid_intervals']['user'], 1)
        self.assertEqual(report['cgroups']['/fixture/cgroup']['delta']['usage_usec'], 200)
        self.assertEqual(report['source_coverage']['thread']['samples'], 2)
        self.assertAlmostEqual(report['observer']['cpu_percent_one_core'], .001)
        self.assertEqual(report['overhead_budget']['status'], 'not_configured')
        self.assertNotIn('not scheduled this cycle', str(report['availability']))

    def test_actual_missing_thread_read_breaks_chain(self):
        rows = [self.sparse(i * 10**9, {'thread'}, 10 * i) for i in range(1, 4)]
        rows[1]['processes'][0].update(tasks=None, tasks_reason='PermissionError')
        report = self.summarize(rows)
        thread = next(item for item in report['registered_entities'].values() if item['kind'] == 'thread')
        self.assertIsNone(thread['delta']['cpu_ticks'])

    def test_source_window_timestamps_and_configuration_rejected(self):
        row = self.sparse(10**9, {'system'})
        row['source_windows']['system']['end_ns'] = row['sample_end_ns'] + 1
        with self.assertRaisesRegex(ValueError, 'source window'):
            self.summarize([row])
        a, b = self.sparse(10**9, {'observer'}), self.sparse(2 * 10**9, {'observer'})
        a['resource_options'] = {}
        b['resource_options'] = {'collect_threads': False}
        with self.assertRaisesRegex(ValueError, 'options changed'):
            self.summarize([a, b])

    def test_budget_costs_and_insufficient_evidence(self):
        row = self.sparse(10**9, {'observer'})
        row['resource_options'] = {'max_cycle_fraction': .2}
        self.assertEqual(self.summarize([row])['overhead_budget']['status'], 'not_evaluated')
        cost = {'schema_version': 1, 'cycle_id': 1, 'start_ns': 10**9, 'end_ns': 10**9 + 30,
                'cadence_ns': 100, 'duration_ns': 30, 'phase_costs_ns': {'encode': 10}}
        costs_path = self.root / 'resources-costs.jsonl'
        costs_path.write_text(json.dumps(cost) + '\n')
        self.assertEqual(self.summarize([row])['overhead_budget']['status'], 'not_evaluated')
        completion = {'schema_version': 1, 'record_type': 'terminal_completion',
                      'completion': {key: cost[key] for key in
                        ('cycle_id', 'start_ns', 'end_ns', 'cadence_ns', 'duration_ns')}}
        costs_path.write_text(json.dumps(cost) + '\n' + json.dumps(completion) + '\n')
        result = self.summarize([row])
        self.assertEqual(result['overhead_budget']['status'], 'exceeded')
        self.assertEqual(result['collection_cost']['phase_costs_ns']['encode']['mean_ns'], 10)
        cost['phase_costs_ns']['encode'] = 31
        costs_path.write_text(json.dumps(cost) + '\n')
        with self.assertRaisesRegex(ValueError, 'cost record'):
            self.summarize([row])

    def test_telemetry_duplicate_stale_and_window_receipt(self):
        rows = [self.sparse(i * 10**9, {'observer'}, i * 10) for i in range(1, 5)]
        jetson = {'available': True, 'reason': None, 'sample_id': 1, 'received_monotonic_ns': 10**9,
                  'values': {'gpu_utilization_percent': 0, 'gpu_frequency_mhz': [100, 200], 'emc_activity_percent': 20, 'availability': {'emc_frequency_mhz': 'frequency not reported'}}}
        rows[0]['jetson_telemetry'] = copy.deepcopy(jetson)
        rows[1]['jetson_telemetry'] = copy.deepcopy(jetson)
        rows[2]['jetson_telemetry'] = dict(jetson, available=False, reason='stale')
        rows[3]['jetson_telemetry'] = dict(jetson, sample_id=2, received_monotonic_ns=-1)
        result = self.summarize(rows)['jetson_telemetry']
        self.assertEqual(result['unique_samples'], 1)
        self.assertEqual(result['value_ranges']['gpu_utilization_percent']['mean'], 0)
        self.assertEqual(result['value_ranges']['gpu_frequency_mhz.gpc1']['samples'], 1)
        self.assertEqual(result['unavailable_snapshots'], 1)
        self.assertIsNone(result['value_ranges']['emc_frequency_mhz'])
        self.assertEqual(result['field_availability']['emc_frequency_mhz']['reasons'], ['frequency not reported'])

    def test_disabled_threads_not_enumerated_and_tid_filter_avoids_reads(self):
        base = self.root / '11'
        (base / 'task/11').mkdir(parents=True)
        (base / 'task/12').mkdir()
        (base / 'stat').write_text(task_stat())
        (base / 'task/12/stat').write_text(task_stat(pid=12, comm='selected'))
        (base / 'task/12/status').write_text('Cpus_allowed_list: 0\nvoluntary_ctxt_switches: 1\nnonvoluntary_ctxt_switches: 0\n')
        (base / 'task/12/schedstat').write_text('1 2 3')
        with patch.object(r, 'PROC_ROOT', self.root):
            sampler = r.ResourceSampler(self.path, .5, options={'collect_threads': False})
            sampler.register(11, 'business')
            with patch.object(sampler, '_threads', side_effect=AssertionError('disabled')):
                result = sampler._snapshot({'thread'})
            self.assertNotIn('thread', result['source_windows'])
            filtered = r.ResourceSampler(self.path, .5, options={'thread_ids': [12]})
            filtered.register(11, 'business')
            result = filtered._snapshot({'thread'})
            selection = result['processes'][0]['thread_selection']
            self.assertEqual(selection['omitted_by_reason'], {'tid_filter': 1})
            self.assertEqual(selection['sampled'], 1)
            self.assertEqual(result['processes'][0]['tasks'][0]['stat']['comm'], 'selected')

    def test_partial_thread_start_interrupt_stops_before_stream_close(self):
        sampler = r.ResourceSampler(self.path, .01)
        original = r.threading.Thread.start
        def start_then_cancel(thread):
            original(thread)
            raise KeyboardInterrupt('after native thread creation')
        with patch.object(r.threading.Thread, 'start', start_then_cancel):
            with self.assertRaises(KeyboardInterrupt):
                sampler.__enter__()
        self.assertFalse(sampler._thread.is_alive())
        self.assertTrue(sampler._stop.is_set())
        self.assertTrue(sampler._stream.closed)
        self.assertTrue(sampler._cost_stream.closed)

    def test_strict_options(self):
        for options in ({'thread_names': ['[']}, {'thread_ids': [True]}, {'collect_threads': 0},
                        {'process_sampling_seconds': float('nan')}, {'unknown': True}):
            with self.assertRaises(ValueError):
                r.validate_resource_options(options)
        original = {'thread_names': ['worker']}
        resolved = r.validate_resource_options(original)
        original['thread_names'].append('other')
        self.assertEqual(resolved['thread_names'], ['worker'])


if __name__ == '__main__':
    unittest.main()
