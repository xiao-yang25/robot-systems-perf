import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit.monitor import main, run_monitor, validate_config
from perfkit.resources import ResourceSampler
from perfkit.workload import BusinessRelations, WorkloadSelector, resource_key, validate_workload
from tests.test_monitor import FakeClock, resource_summary


def profile(*roles):
    return {'format_version': 1, 'workload_id': 'robot-demo', 'workload_version': 'fixture-v1',
            'functions': list(roles)}


def role(name, patterns=None, **updates):
    value = {'id': name, 'process_selector': {'include_names': patterns or ['^demo$']}}
    value.update(updates)
    return value


def process(pid, start=10, name='demo', uid=None, group='/svc/a'):
    return {'pid': pid, 'starttime_ticks': start, 'comm': name, 'exe_name': name,
            'uid': os.getuid() if uid is None else uid, 'is_kernel': False,
            'cpu_ticks': 0, 'cgroup_paths': [group]}


def registration(pid, start=10, serial=1):
    return {'pid': pid, 'starttime_ticks': start, 'registration_id': serial}


class WorkloadTests(unittest.TestCase):
    def selector(self, raw, **config):
        validated = validate_workload(raw)
        return WorkloadSelector(validate_config(config), validated, 100), validated

    def observe(self, selector, tracker, processes, registrations, now=100, scan=None):
        return tracker.observe(selector.update(processes, now), registrations,
                               {'scan': scan or {}}, now - 1, now)

    def test_validation_is_strict_bounded_and_does_not_mutate_declarations(self):
        original = profile(role('perception', ros_nodes=['/demo/detector']))
        before = copy.deepcopy(original)
        validated = validate_workload(original)
        self.assertEqual(original, before)
        self.assertEqual(validated['functions'][0]['expected_processes'], 1)
        invalid = [None, dict(original, format_version=True), dict(original, unknown=1),
                   dict(original, ros_domain_id=True), dict(original, functions=[]),
                   profile(role('x'), role('x')), profile(role('x', ros_nodes=['/x', '/x'])),
                   profile(role('x', relation_source='trace_verified')),
                   profile(role('x', expected_processes=True)), profile(role('x', expected_processes=0)),
                   profile(role('x', ['['])), profile(role('x', process_selector={'uids': None})),
                   profile(role('x', process_selector={'pids': [True]})),
                   profile(role('x', process_selector={'include_names': ['x'], 'uids': [True]})),
                   profile(role('x', ros_nodes=['relative']))]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_workload(value)

    def test_per_function_hard_filters_intersect_global_scope_and_do_not_cross_mix(self):
        raw = profile(role('a', process_selector={'include_names': ['^demo$'],
                              'cgroup_patterns': ['^/svc/a$'], 'uids': [os.getuid()]}),
                      role('b', process_selector={'pids': [2], 'cgroup_patterns': ['^/svc/b$']}))
        selector, _ = self.selector(raw, cgroup_patterns=['^/svc/'], exclude_names=['^excluded$'])
        decision = selector.update([process(1), process(2, group='/svc/b'),
            process(3, group='/svc/b'), process(4, uid=os.getuid()+1),
            process(5, group='/other'), process(6, name='excluded')], 100)
        self.assertEqual([item['pid'] for item in decision['targets']], [1, 2])
        self.assertEqual([p['pid'] for p in decision['workload_matches']['a']], [1])
        self.assertEqual([p['pid'] for p in decision['workload_matches']['b']], [2])

    def test_cap_keeps_full_match_count_and_never_confirms_first_candidate(self):
        selector, workload = self.selector(profile(role('a')), max_targets=1)
        tracker = BusinessRelations(workload, 'pid:[fixture]')
        record = self.observe(selector, tracker, [process(1), process(2)], {1: registration(1)})
        row = record['functions'][0]
        self.assertEqual(row['status'], 'ambiguous')
        self.assertEqual(row['matching_count'], 2)
        self.assertEqual(row['omitted_by_target_cap'], 1)
        self.assertEqual(len(row['candidates']), 1)

    def test_shared_process_has_one_resource_reference_and_no_copied_metrics(self):
        selector, workload = self.selector(profile(role('a', ros_nodes=['/demo/a']),
                                                  role('b', ros_nodes=['/demo/b'])))
        tracker = BusinessRelations(workload, 'pid:[fixture]')
        self.observe(selector, tracker, [process(1)], {1: registration(1)})
        key = 'pid=1:registration=1:start=10'
        summary = tracker.summary({'registered_entities': {key: {'cpu_percent_one_core': 25, 'rss_peak_bytes': 4096}}})
        self.assertEqual(summary['unique_resource_refs'], [key])
        self.assertEqual([row['resource_refs'] for row in summary['functions']], [[key], [key]])
        self.assertNotIn('cpu_percent_one_core', json.dumps(summary['functions']))
        self.assertTrue(all(row['last_scan']['status'] == 'candidate' for row in summary['functions']))
        self.assertEqual(summary['business_acceptance'], 'not_evaluated')

    def test_pid_reuse_and_re_registration_expire_old_refs_without_bridging(self):
        selector, workload = self.selector(profile(role('a')))
        tracker = BusinessRelations(workload, None)
        self.observe(selector, tracker, [process(1)], {1: registration(1)})
        changed = self.observe(selector, tracker, [process(1, start=20)],
                               {1: registration(1, start=20, serial=2)}, now=200)
        row = changed['functions'][0]
        self.assertEqual(row['expired_resource_refs'], ['pid=1:registration=1:start=10'])
        self.assertEqual(row['candidates'][0]['resource_ref'], 'pid=1:registration=2:start=20')
        same = self.observe(selector, tracker, [process(1, start=20)],
                            {1: registration(1, start=20, serial=3)}, now=300)
        self.assertEqual(same['functions'][0]['expired_resource_refs'], ['pid=1:registration=2:start=20'])

    def test_selector_leaving_scope_does_not_retain_old_role(self):
        selector, workload = self.selector(profile(role('a', ['^first$']), role('b', ['^second$'])))
        tracker = BusinessRelations(workload, None)
        self.observe(selector, tracker, [process(1, name='first')], {1: registration(1)})
        changed = self.observe(selector, tracker, [process(1, name='second')], {1: registration(1)}, now=200)
        self.assertEqual(changed['functions'][0]['status'], 'unresolved')
        self.assertTrue(changed['functions'][0]['expired_resource_refs'])
        self.assertEqual(changed['functions'][1]['status'], 'candidate')

    def test_registration_race_and_unavailable_resources_remain_explicit(self):
        selector, workload = self.selector(profile(role('a')))
        tracker = BusinessRelations(workload, None)
        record = self.observe(selector, tracker, [process(1)], {1: registration(1, start=20)})
        self.assertIsNone(record['functions'][0]['candidates'][0]['resource_ref'])
        self.assertEqual(record['functions'][0]['candidates'][0]['registration_status'], 'identity_race')
        self.observe(selector, tracker, [process(1)], {1: registration(1)}, now=200)
        summary = tracker.summary({})
        self.assertEqual(summary['functions'][0]['unavailable_resource_refs'], ['pid=1:registration=1:start=10'])

    def test_declared_node_conflict_is_not_runtime_node_verification(self):
        selector, workload = self.selector(profile(role('a', ['^a$'], ros_nodes=['/same']),
                                                  role('b', ['^b$'], ros_nodes=['/same'])))
        tracker = BusinessRelations(workload, None)
        record = self.observe(selector, tracker, [process(1, name='a'), process(2, name='b')],
                              {1: registration(1), 2: registration(2, serial=2)})
        self.assertTrue(all(row['status'] == 'conflict' for row in record['functions']))
        self.assertTrue(all(row['ros_node_evidence'] == 'operator_declared_not_verified'
                            for row in record['functions']))

    def test_node_conflicts_use_full_matches_before_cap_and_registration(self):
        for cap, expected, processes in (
                (1, 1, [process(1, name='a'), process(2, name='b')]),
                (32, 2, [process(1, name='a'), process(2, name='a'), process(3, name='b')])):
            with self.subTest(cap=cap, expected=expected):
                selector, workload = self.selector(profile(
                    role('a', ['^a$'], ros_nodes=['/same'], expected_processes=expected),
                    role('b', ['^b$'], ros_nodes=['/same'])), max_targets=cap)
                record = self.observe(selector, BusinessRelations(workload, None), processes, {})
                self.assertEqual([row['status'] for row in record['functions']], ['conflict', 'conflict'])
                self.assertEqual([row['matching_count'] for row in record['functions']], [expected, 1])
                self.assertTrue(all(row['conflicting_declared_nodes'] == ['/same'] for row in record['functions']))
                if cap == 1:
                    self.assertEqual(record['functions'][1]['candidates'], [])
                    self.assertEqual(record['functions'][1]['omitted_by_target_cap'], 1)

    def test_shared_node_label_one_identity_and_single_function_groups_are_not_conflicts(self):
        for raw, processes in (
                (profile(role('a', ros_nodes=['/same']), role('b', ros_nodes=['/same'])), [process(1)]),
                (profile(role('a', ros_nodes=['/same'], expected_processes=2)), [process(1), process(2)])):
            with self.subTest(raw=raw):
                selector, workload = self.selector(raw)
                record = self.observe(selector, BusinessRelations(workload, None), processes, {})
                self.assertTrue(all(row['status'] == 'candidate' for row in record['functions']))

    def test_cli_null_workload_is_rejected_before_output_or_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            workload = Path(directory) / 'null.json'
            workload.write_text('null')
            output = Path(directory) / 'output'
            with patch('sys.argv', ['robot-perf-monitor', '--workload', str(workload),
                                   '--output', str(output)]), \
                 patch('perfkit.monitor.platform.system', return_value='Linux'), \
                 patch('perfkit.monitor.signal.signal'), \
                 patch('perfkit.monitor.run_monitor') as run, \
                 patch('perfkit.monitor.scan_processes') as scan, \
                 patch('perfkit.monitor.collect_profile') as collect:
                with self.assertRaises(ValueError):
                    main()
                run.assert_not_called()
                scan.assert_not_called()
                collect.assert_not_called()
            self.assertFalse(output.exists())

    def test_cardinality_and_scope_missing_never_become_business_acceptance(self):
        selector, workload = self.selector(profile(role('a', expected_processes=2)))
        tracker = BusinessRelations(workload, None)
        for count, state in ((0, 'unresolved'), (1, 'incomplete'), (2, 'candidate'), (3, 'ambiguous')):
            record = self.observe(selector, tracker, [process(i+1) for i in range(count)], {},
                                  now=100+count, scan={'skipped_count': 1})
            self.assertEqual(record['functions'][0]['status'], state)
            self.assertFalse(record['functions'][0]['scope_complete'])

    def test_workload_rejects_global_soft_selection_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            for config in ({'pids': [1]}, {'include_names': ['x']}, {'active_cpu_percent': 2}):
                with self.subTest(config=config), self.assertRaises(ValueError):
                    run_monitor(config, output, workload=profile(role('a')))
                self.assertFalse(output.exists())

    def test_resource_registration_snapshot_is_a_copy_not_liveness(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler = ResourceSampler(Path(directory) / 'resources.jsonl', .5)
            sampler._registered[1] = registration(1)
            snapshot = sampler.registered_identity(1)
            snapshot['registration_id'] = 99
            self.assertEqual(sampler.registered_identity(1)['registration_id'], 1)
            sampler.unregister(1)
            self.assertIsNone(sampler.registered_identity(1))

    def test_monitor_relation_outputs_restart_and_resource_refs(self):
        class Sampler:
            def __init__(self, path, interval, **kwargs):
                self.error, self.path, self.entries, self.serial = None, path, {}, 0
            def set_window(self, *args): pass
            def __enter__(self): self.path.write_text(''); return self
            def __exit__(self, *args): pass
            def register(self, pid, name, expected_starttime_ticks):
                self.serial += 1
                self.entries[pid] = registration(pid, expected_starttime_ticks, self.serial)
                return True
            def unregister(self, pid): self.entries.pop(pid, None)
            def registered_identity(self, pid): return self.entries.get(pid)
        scans = iter([{'processes': [process(1, start=start)], 'scan': {}} for start in (10, 20)])
        entities = {resource_key(registration(1)): {'kind': 'process', 'pid': 1,
                    'cpu_percent_one_core': 10, 'rss_peak_bytes': 4096},
                    resource_key(registration(1, start=20, serial=2)): {'kind': 'process', 'pid': 1,
                    'cpu_percent_one_core': 20, 'rss_peak_bytes': 4096}}
        with tempfile.TemporaryDirectory() as directory, patch('perfkit.monitor.time', FakeClock()), \
             patch('perfkit.monitor.ResourceSampler', Sampler), patch('perfkit.monitor.collect_profile', return_value={}), \
             patch('perfkit.monitor._source_record', return_value={}), \
             patch('perfkit.monitor.scan_processes', side_effect=lambda *a, **k: next(scans)), \
             patch('perfkit.monitor.summarize_resources', return_value=resource_summary(registered_entities=entities)):
            output = Path(directory) / 'capture'
            summary = run_monitor({'duration_seconds': 1, 'discovery_interval_seconds': .5}, output,
                                  workload=profile(role('a'), role('b')))
            rows = [json.loads(line) for line in (output / 'business-relations.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1]['event'], 'observation_ended')
            self.assertTrue(rows[1]['functions'][0]['expired_resource_refs'])
            self.assertEqual(len(summary['workload']['unique_resource_refs']), 2)
            self.assertEqual(summary['workload']['functions'][0]['resource_refs'],
                             summary['workload']['functions'][1]['resource_refs'])
            self.assertIn('资源只保存在', (output / 'BUSINESS_MAP_REPORT.md').read_text())


if __name__ == '__main__':
    unittest.main()
