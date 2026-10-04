"""Scope, fresh counters and common-interval observer accounting contracts."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from perfkit import resources as r
from perfkit.discovery import scan_processes
from tests.test_discovery import fixture, selector, task_record
from tests import test_resource_profiles as profiles
from tests.test_resources import task_stat


class DiscoveryOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_scoped_targets_with_fewer_reads_and_no_sensitive_files(self):
        fixture(self.root, pid=7)
        fixture(self.root, pid=8, uid=2000)
        baseline = scan_processes(self.root)
        scope = selector(include_names=['container'], cgroup_patterns=['robot'])
        observed = []
        original = Path.read_text
        def read(path, *args, **kwargs):
            observed.append((path.parent.name, path.name))
            self.assertIn(path.name, ('stat', 'status', 'cgroup'))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            scoped = scan_processes(self.root, _scope=scope)
        expected = selector(include_names=['container'], cgroup_patterns=['robot']).update(
            baseline['processes'], 0)
        self.assertEqual(scope.update(scoped['processes'], 0), expected)
        self.assertEqual([name for pid, name in observed if pid == '8'], ['stat', 'status'])
        self.assertEqual(scoped['scan']['scope_filtered_by_reason'], {'uid_scope': 1})

    def test_explicit_mode_keeps_scope_checks_and_root_failure(self):
        fixture(self.root, pid=7)
        fixture(self.root, pid=8)
        scope = selector(pids=[7], active_cpu_percent=0)
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertNotEqual(path.parent.name, '8')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root, exclude_pids=(9,), _scope=scope)
        self.assertEqual([p['pid'] for p in result['processes']], [7])
        self.assertEqual(result['scan']['candidate_count'], 2)
        self.assertEqual(result['scan']['scope_filtered_by_reason'], {'pid_scope': 1})
        (self.root / '7/status').write_text('Uid: 2000 2000 2000 2000\n')
        self.assertEqual(scan_processes(self.root, _scope=scope)['processes'], [])
        with self.assertRaises(FileNotFoundError):
            scan_processes(self.root / 'missing', _scope=scope)

    def test_pids_and_names_remain_additive_to_activity(self):
        fixture(self.root, pid=7)
        other = fixture(self.root, pid=8, comm='other', user=0, system=0)
        scope = selector(pids=[7], include_names=['container'])
        scope.update(scan_processes(self.root, _scope=scope)['processes'], 0)
        (other / 'stat').write_text(task_record(pid=8, comm='other', user=50, system=0))
        (other / 'exe').unlink()
        result = scope.update(scan_processes(self.root, _scope=scope)['processes'], 10**9)
        self.assertEqual({p['pid'] for p in result['targets']}, {7, 8})
        self.assertIn('active_cpu', next(p for p in result['targets'] if p['pid'] == 8)['selection_reasons'])

    def test_scope_exit_and_reentry_break_activity_history(self):
        base = fixture(self.root, user=0, system=0)
        scope = selector(cgroup_patterns=['robot'])
        scope.update(scan_processes(self.root, _scope=scope)['processes'], 0)
        (base / 'stat').write_text(task_record(user=50, system=0))
        self.assertEqual(len(scope.update(scan_processes(self.root, _scope=scope)['processes'], 10**9)['targets']), 1)
        (base / 'cgroup').write_text('0::/outside\n')
        self.assertEqual(scope.update(scan_processes(self.root, _scope=scope)['processes'], 2 * 10**9)['targets'], [])
        (base / 'cgroup').write_text('0::/robot\n')
        (base / 'stat').write_text(task_record(user=500, system=0))
        self.assertEqual(scope.update(scan_processes(self.root, _scope=scope)['processes'], 3 * 10**9)['targets'], [])

    def test_identity_change_after_uid_read_is_rejected(self):
        fixture(self.root)
        original = Path.read_text
        count = 0
        def read(path, *args, **kwargs):
            nonlocal count
            if path.name == 'stat':
                count += 1
                if count == 2:
                    return task_record(start=501)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root, _scope=selector(pids=[7]))
        self.assertEqual(result['processes'], [])
        self.assertEqual(result['scan']['skipped_by_reason'], {'identity_changed': 1})


class AcquisitionOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proc_patch = patch.object(r, 'PROC_ROOT', self.root / 'proc')
        self.sys_patch = patch.object(r, 'SYS_ROOT', self.root / 'sys')
        self.proc_patch.start()
        self.sys_patch.start()
        self.sampler = r.ResourceSampler(self.root / 'out.jsonl', .1)

    def tearDown(self):
        self.proc_patch.stop()
        self.sys_patch.stop()
        self.tmp.cleanup()

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
        return target

    def task(self, tid, user=10, start=100, name='selected'):
        self.write(f'proc/11/task/{tid}/stat', task_stat(pid=tid, user=user, start=start, comm=name))
        self.write(f'proc/11/task/{tid}/status', 'Cpus_allowed_list: 0\nvoluntary_ctxt_switches: 2\nnonvoluntary_ctxt_switches: 1\n')
        self.write(f'proc/11/task/{tid}/schedstat', '10 20 1')

    def test_cached_paths_read_fresh_counters_and_prune_dead_threads(self):
        self.write('proc/11/stat', task_stat())
        self.task(12)
        self.task(13)
        self.sampler.register(11, 'fixture')
        first = self.sampler._snapshot({'thread'})
        self.assertEqual(len(self.sampler._thread_paths[11]), 2)
        self.task(12, user=30, start=200)
        shutil.rmtree(self.root / 'proc/11/task/13')
        second = self.sampler._snapshot({'thread'})
        task = second['processes'][0]['tasks'][0]['stat']
        self.assertEqual((task['utime_ticks'], task['starttime_ticks']), (30, 200))
        self.assertEqual(first['processes'][0]['tasks'][0]['stat']['utime_ticks'], 10)
        self.assertEqual(len(self.sampler._thread_paths[11]), 1)
        # A still-registered, now dead process must not remain in path caches.
        self.sampler._snapshot({'process'})
        self.assertEqual(len(self.sampler._process_paths), 1)
        (self.root / 'proc/11/stat').unlink()
        self.sampler._snapshot({'process', 'thread'})
        self.assertEqual(self.sampler._process_paths, {})
        self.assertEqual(self.sampler._thread_paths, {})
        self.sampler.unregister(11)
        self.sampler._snapshot({'process', 'thread'})
        self.assertEqual(self.sampler._thread_paths, {})
        self.assertEqual(self.sampler._process_paths, {})

    def test_name_filter_uses_two_stat_reads_and_rejects_rename(self):
        self.write('proc/11/stat', task_stat())
        self.task(12)
        sampler = r.ResourceSampler(self.root / 'named.jsonl', .1, options={'thread_names': ['^selected$']})
        sampler.register(11, 'fixture')
        original = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            if path == self.root / 'proc/11/task/12/stat':
                reads.append(path)
                if len(reads) == 2:
                    return task_stat(pid=12, comm='other')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = sampler._snapshot({'thread'})
        self.assertEqual(len(reads), 2)
        task = result['processes'][0]['tasks'][0]
        self.assertIsNone(task['stat'])
        self.assertIn('identity/name changed', task['availability']['stat'])

    def test_sysfs_inventory_refresh_values_failure_and_new_interfaces(self):
        self.write('sys/class/hwmon/hwmon0/power1_input', '100')
        self.write('sys/class/hwmon/hwmon0/power1_label', 'rail')
        with patch.object(r.time, 'monotonic_ns', return_value=0):
            first = self.sampler._system()
        self.assertEqual(first['rail_power']['hwmon0/power1_input']['microwatts'], 100)
        self.write('sys/class/hwmon/hwmon0/power1_input', '200')
        with patch.object(r.time, 'monotonic_ns', return_value=10**9):
            self.assertEqual(self.sampler._system()['rail_power']['hwmon0/power1_input']['microwatts'], 200)
        (self.root / 'sys/class/hwmon/hwmon0/power1_input').unlink()
        with patch.object(r.time, 'monotonic_ns', return_value=2 * 10**9):
            data = self.sampler._system()['rail_power']['hwmon0/power1_input']
        self.assertIsNone(data['microwatts'])
        self.assertIn('FileNotFoundError', data['reason'])
        self.write('sys/class/hwmon/hwmon1/power2_input', '300')
        with patch.object(r.time, 'monotonic_ns', return_value=6 * 10**9):
            latest = self.sampler._system()
        self.assertEqual(set(latest['rail_power']), {'hwmon1/power2_input'})

    def test_owned_child_counter_and_identity_race(self):
        self.sampler.options['jetson_telemetry'] = True
        self.sampler._clock_ticks = 100
        self.write('proc/23/stat', task_stat(pid=23, user=10, kernel=5))
        with patch.object(self.sampler, 'owned_process_ids', return_value=(23,)):
            observed = self.sampler._observer()
        self.assertEqual(observed['owned_children'][0]['cpu_time_ns'], 150_000_000)
        self.assertIsNone(observed['owned_children_reason'])
        count = 0
        def read(path, *args, **kwargs):
            nonlocal count
            count += 1
            return task_stat(pid=23, start=100 + count)
        with patch.object(self.sampler, 'owned_process_ids', return_value=(23,)), patch.object(Path, 'read_text', read):
            raced = self.sampler._observer()
        self.assertEqual(raced['owned_children'], [])
        self.assertIn('not verified', raced['owned_children_reason'])


class ObserverAccountingTests(unittest.TestCase):
    def setUp(self):
        self.helper = profiles.ProfileTests()
        self.helper.setUp()

    def tearDown(self):
        self.helper.tearDown()

    def row(self, second, parent, child, identity=100):
        row = self.helper.sparse(second * 10**9, {'observer'})
        row['resource_options'] = {'jetson_telemetry': True, 'max_observer_cpu_percent_one_core': 25}
        row['observer'] = {'process_cpu_ns': parent, 'owned_children_expected': True,
                          'owned_children': [{'pid': 23, 'starttime_ticks': identity, 'cpu_time_ns': child}],
                          'owned_children_reason': None}
        return row

    def test_known_parent_plus_child_cpu_and_budget(self):
        rows = [self.row(1, 100_000_000, 200_000_000), self.row(2, 200_000_000, 400_000_000)]
        report = self.helper.summarize(rows)
        self.assertEqual(report['observer']['cpu_percent_one_core'], 10)
        self.assertEqual(report['observer']['total_cpu_percent_one_core'], 30)
        self.assertEqual(report['observer']['total_coverage']['covered_ns']['total_cpu_ns'], 10**9)
        self.assertEqual(report['overhead_budget']['status'], 'exceeded')

    def test_phase_cpu_costs_and_legacy_log_compatibility(self):
        path = self.helper.root / 'resources-costs.jsonl'
        cost = {'schema_version': 1, 'cycle_id': 1, 'start_ns': 100, 'end_ns': 200,
                'cadence_ns': 1000, 'duration_ns': 100, 'phase_costs_ns': {'thread': 80},
                'phase_thread_cpu_ns': {'thread': 60}}
        path.write_text(json.dumps(cost) + '\n')
        result = r.summarize_costs(path, 0, 1000)
        self.assertEqual(result['phase_thread_cpu_ns']['thread']['mean_ns'], 60)
        cost.pop('phase_thread_cpu_ns')
        path.write_text(json.dumps(cost) + '\n')
        self.assertEqual(r.summarize_costs(path, 0, 1000)['phase_thread_cpu_ns'], {})

    def test_missing_child_does_not_bridge_or_pass_budget(self):
        rows = [self.row(i, i * 100_000_000, i * 200_000_000) for i in range(1, 5)]
        rows[1]['observer']['owned_children_reason'] = 'permission denied'
        report = self.helper.summarize(rows)
        self.assertEqual(report['observer']['total_coverage']['valid_intervals']['total_cpu_ns'], 1)
        self.assertEqual(report['observer']['owned_child_unavailable_samples'], 1)
        self.assertEqual(report['overhead_budget']['status'], 'not_evaluated')

    def test_missing_child_blocks_cycle_only_budget(self):
        rows = [self.row(i, i * 100_000_000, i * 200_000_000) for i in (1, 2)]
        for row in rows:
            row['resource_options'] = {'jetson_telemetry': True, 'max_cycle_fraction': .2}
            row['observer']['owned_children'] = []
        cost = {'schema_version': 1, 'cycle_id': 1, 'start_ns': 10**9, 'end_ns': 10**9 + 10,
                'cadence_ns': 100, 'duration_ns': 10, 'phase_costs_ns': {}}
        (self.helper.root / 'resources-costs.jsonl').write_text(json.dumps(cost) + '\n')
        report = self.helper.summarize(rows)
        self.assertEqual(report['overhead_budget']['status'], 'not_evaluated')
        self.assertIn('owned_child_cpu_incomplete', report['overhead_budget']['reasons'])

    def test_missing_observer_source_breaks_both_cpu_chains(self):
        rows = [self.row(i, i * 100_000_000, i * 200_000_000) for i in range(1, 4)]
        rows[1]['source_windows'] = {}
        rows[1].pop('observer')
        report = self.helper.summarize(rows)
        self.assertIsNone(report['observer']['cpu_percent_one_core'])
        self.assertIsNone(report['observer']['total_cpu_percent_one_core'])
        self.assertEqual(report['observer']['missing_samples'], 1)
        self.assertEqual(report['overhead_budget']['status'], 'not_evaluated')

    def test_child_reuse_or_counter_reset_does_not_bridge(self):
        for identity, child in ((200, 400_000_000), (100, 100_000_000)):
            with self.subTest(identity=identity, child=child):
                report = self.helper.summarize([self.row(1, 100_000_000, 200_000_000),
                                                self.row(2, 200_000_000, child, identity)])
                self.assertIsNone(report['observer']['total_cpu_percent_one_core'])
                self.assertEqual(report['overhead_budget']['status'], 'not_evaluated')

    def test_legacy_enabled_child_is_missing_and_disabled_uses_parent(self):
        rows = [self.row(i, i * 100_000_000, i * 200_000_000) for i in (1, 2)]
        for row in rows:
            row['observer'] = {'process_cpu_ns': row['observer']['process_cpu_ns']}
        self.assertEqual(self.helper.summarize(rows)['overhead_budget']['status'], 'not_evaluated')
        for row in rows:
            row['resource_options']['jetson_telemetry'] = False
        report = self.helper.summarize(rows)
        self.assertEqual(report['observer']['total_cpu_percent_one_core'], 10)
        self.assertEqual(report['overhead_budget']['status'], 'within_observed_scope')


if __name__ == '__main__':
    unittest.main()
