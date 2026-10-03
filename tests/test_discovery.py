"""Independent proc records and arithmetic expectations for discovery."""

from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from perfkit.discovery import DiscoverySelector, scan_processes


def task_record(pid=7, comm='worker (main)', start=500, user=120, system=30, flags=0):
    # Literal Linux stat field order: state is field 3, flags field 9, CPU
    # counters fields 14/15 and identity starttime field 22.
    return (f'{pid} ({comm}) S 1 2 3 0 -1 {flags} 0 0 0 0 {user} {system} '
            f'0 0 20 0 4 0 {start} 4096 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0\n')


def fixture(root, pid=7, uid=1000, **stat_values):
    base = root / str(pid)
    base.mkdir()
    (base / 'stat').write_text(task_record(pid=pid, **stat_values))
    (base / 'status').write_text(f'Name:\tworker\nUid:\t{uid}\t2000\t2000\t2000\nKthread:\t0\n')
    (base / 'cgroup').write_text('0::/robot.slice/component\n4:cpu,cpuacct:/docker/abc\n')
    (base / 'exe').symlink_to('/usr/local/bin/component_container_mt')
    return base


def process(pid=7, start=500, ticks=100, uid=1000, name='worker', exe=None,
            cgroups=('/robot',), kernel=False):
    return {'pid': pid, 'starttime_ticks': start, 'comm': name, 'exe_name': exe,
            'uid': uid, 'cpu_ticks': ticks, 'cgroup_paths': list(cgroups), 'is_kernel': kernel}


def selector(**overrides):
    config = {'uids': [1000], 'include_names': [], 'exclude_names': [],
              'cgroup_patterns': [], 'pids': [], 'active_cpu_percent': 25,
              'max_targets': 10}
    config.update(overrides)
    return DiscoverySelector(config, 100)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_literal_proc_fields_real_uid_raw_cgroups_and_safe_names(self):
        fixture(self.root)
        (self.root / 'nonpid').mkdir()
        original = Path.read_text
        observed = []
        def read(path, *args, **kwargs):
            observed.append(path.name)
            self.assertIn(path.name, ('stat', 'status', 'cgroup'))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root)
        self.assertEqual(observed, ['stat', 'status', 'cgroup', 'stat'])
        self.assertEqual(result['processes'], [process(ticks=150, name='worker (main)',
            exe='component_container_mt', cgroups=('/robot.slice/component', '/docker/abc'))])
        self.assertEqual(result['scan']['skipped_count'], 0)
        self.assertEqual(set(result['processes'][0]), {'pid', 'starttime_ticks', 'comm',
            'exe_name', 'uid', 'cpu_ticks', 'cgroup_paths', 'is_kernel'})

    def test_unknown_uid_and_explicit_exclusion(self):
        base = fixture(self.root)
        fixture(self.root, pid=9)
        (base / 'status').write_text('Uid:\tinvalid\n')
        result = scan_processes(self.root, exclude_pids=(9,))
        self.assertEqual(result['processes'], [])
        self.assertEqual(result['scan']['skipped_by_reason'], {'uid_unknown': 1})
        self.assertEqual(result['scan']['excluded_count'], 1)

    def test_status_permission_and_disappeared_stat_are_skipped(self):
        base = fixture(self.root)
        missing = fixture(self.root, pid=9)
        (missing / 'stat').unlink()
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == base / 'status':
                raise PermissionError('hidden')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root)
        self.assertEqual(result['processes'], [])
        self.assertEqual(result['scan']['skipped_by_reason'],
                         {'status_permission_denied': 1, 'stat_missing': 1})

    def test_exe_permission_does_not_imply_kernel(self):
        fixture(self.root)
        with patch('perfkit.discovery.os.readlink', side_effect=PermissionError('hidden')):
            result = scan_processes(self.root)
        self.assertIsNone(result['processes'][0]['exe_name'])
        self.assertFalse(result['processes'][0]['is_kernel'])
        self.assertEqual(result['scan']['optional_unavailable_by_reason'], {'exe_permission_denied': 1})

    def test_kernel_status_and_flag_without_exe(self):
        base = fixture(self.root)
        flagged = fixture(self.root, pid=9, flags=2097152)
        (base / 'status').write_text('Uid:\t0\t0\t0\t0\nKthread:\t1\n')
        (base / 'exe').unlink()
        (flagged / 'exe').unlink()
        result = scan_processes(self.root)
        self.assertTrue(all(item['is_kernel'] for item in result['processes']))

    def test_cgroup_missing_permission_and_invalid_are_optional(self):
        missing = fixture(self.root)
        denied = fixture(self.root, pid=8)
        invalid = fixture(self.root, pid=9)
        (missing / 'cgroup').unlink()
        (invalid / 'cgroup').write_text('not a cgroup record\n')
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == denied / 'cgroup':
                raise PermissionError('hidden')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root)
        self.assertEqual(len(result['processes']), 3)
        self.assertTrue(all(item['cgroup_paths'] == [] for item in result['processes']))
        self.assertEqual(result['scan']['optional_unavailable_by_reason'],
                         {'cgroup_missing': 1, 'cgroup_permission_denied': 1, 'cgroup_invalid': 1})
        self.assertEqual(selector(pids=[7, 8, 9], cgroup_patterns=['robot']).update(
            result['processes'], 0)['targets'], [])
        self.assertEqual(len(selector(pids=[7, 8, 9]).update(result['processes'], 0)['targets']), 3)

    def test_identity_changed_during_scan_and_verify_disappearance(self):
        fixture(self.root)
        fixture(self.root, pid=9)
        reads = {}
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == 'stat':
                reads[path] = reads.get(path, 0) + 1
                if reads[path] == 2:
                    if path.parent.name == '9':
                        raise FileNotFoundError('gone')
                    return task_record(start=501)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root)
        self.assertEqual(result['processes'], [])
        self.assertEqual(result['scan']['skipped_by_reason'],
                         {'identity_changed': 1, 'stat_verify_missing': 1})

    def test_bad_stat_and_root_iteration_failure(self):
        base = fixture(self.root)
        (base / 'stat').write_text('7 (oops) S\n')
        self.assertEqual(scan_processes(self.root)['scan']['skipped_by_reason'], {'stat_invalid': 1})
        with self.assertRaises(FileNotFoundError):
            scan_processes(self.root / 'missing')


class SelectorTests(unittest.TestCase):
    def test_first_scan_no_rate_then_multicore_arithmetic(self):
        subject = selector(active_cpu_percent=200)
        first = subject.update([process(ticks=100)], 1_000_000_000)
        self.assertEqual(first['targets'], [])
        second = subject.update([process(ticks=600)], 3_000_000_000)
        self.assertEqual(second['targets'][0]['observed_cpu_percent'], 250.0)
        self.assertEqual(second['targets'][0]['selection_reasons'], ['active_cpu'])

    def test_zero_threshold_disables_activity_and_explicit_rate_is_none(self):
        subject = selector(active_cpu_percent=0)
        subject.update([process(ticks=0)], 0)
        self.assertEqual(subject.update([process(ticks=1000)], 1_000_000_000)['targets'], [])
        explicit = selector(pids=[7]).update([process()], 0)['targets'][0]
        self.assertIsNone(explicit['observed_cpu_percent'])

    def test_base_scope_applies_to_every_trigger_unknown_uid_and_kernel(self):
        subject = selector(pids=[1, 2, 3, 4, 5], include_names=['worker'],
                           exclude_names=['blocked'], cgroup_patterns=['^/robot'])
        records = [process(pid=1, uid=2000), process(pid=2, uid=None),
                   process(pid=3, name='blocked', exe='worker'),
                   process(pid=4, cgroups=('/other',)), process(pid=5, kernel=True), process(pid=6)]
        result = subject.update(records, 0)
        self.assertEqual([item['pid'] for item in result['targets']], [6])
        self.assertEqual(result['selection']['eligible_count'], 1)
        self.assertEqual(len(selector(uids=None, pids=[7]).update([process(uid=2000)], 0)['targets']), 1)
        self.assertEqual(selector(uids=[], pids=[7]).update([process()], 0)['targets'], [])

    def test_name_matches_exe_and_include_does_not_block_activity(self):
        subject = selector(include_names=['^container$'])
        result = subject.update([process(exe='container'), process(pid=8, ticks=0)], 0)
        self.assertEqual(result['targets'][0]['selection_reasons'], ['include_name'])
        result = subject.update([process(exe='container'), process(pid=8, ticks=50)], 1_000_000_000)
        self.assertEqual({item['pid'] for item in result['targets']}, {7, 8})

    def test_retains_idle_identity_but_drops_filtered_dead_and_reused(self):
        subject = selector()
        subject.update([process(ticks=0)], 0)
        subject.update([process(ticks=50)], 1_000_000_000)
        idle = subject.update([process(ticks=50, name='renamed')], 2_000_000_000)['targets'][0]
        self.assertEqual(idle['observed_cpu_percent'], 0.0)
        self.assertEqual(idle['selection_reasons'], ['retained_identity'])
        self.assertEqual(subject.update([process(start=501, ticks=1000)], 3_000_000_000)['targets'], [])
        subject.update([process(start=501, ticks=1050)], 4_000_000_000)
        self.assertEqual(subject.update([process(start=501, ticks=1050, uid=2000)],
                                        5_000_000_000)['targets'], [])
        self.assertEqual(subject.update([], 6_000_000_000)['targets'], [])
        self.assertEqual(subject._previous, {})
        self.assertEqual(subject._selected, set())

    def test_reset_nonpositive_interval_and_absence_do_not_make_rates(self):
        subject = selector(pids=[7])
        subject.update([process(ticks=100)], 10)
        self.assertIsNone(subject.update([process(ticks=90)], 20)['targets'][0]['observed_cpu_percent'])
        self.assertIsNone(subject.update([process(ticks=100)], 20)['targets'][0]['observed_cpu_percent'])
        self.assertIsNone(subject.update([process(ticks=110)], 19)['targets'][0]['observed_cpu_percent'])
        subject.update([], 30)
        self.assertIsNone(subject.update([process(ticks=200)], 40)['targets'][0]['observed_cpu_percent'])

    def test_cap_prioritizes_retention_explicit_activity_and_pid(self):
        subject = selector(include_names=['explicit'], max_targets=2)
        subject.update([process(pid=9, name='explicit'), process(pid=5, ticks=0),
                        process(pid=6, ticks=0), process(pid=7, ticks=0)], 0)
        result = subject.update([process(pid=9), process(pid=5, ticks=200),
                                 process(pid=6, ticks=500), process(pid=7, name='explicit', ticks=50)],
                                1_000_000_000)
        self.assertEqual([item['pid'] for item in result['targets']], [9, 7])
        self.assertEqual(result['selection'], {'eligible_count': 4, 'matched_count': 4,
                                             'selected_count': 2, 'omitted_count': 2})
        activity = selector(max_targets=2)
        activity.update([process(pid=9, ticks=0), process(pid=5, ticks=0), process(pid=6, ticks=0)], 0)
        ranked = activity.update([process(pid=9, ticks=50), process(pid=5, ticks=50),
                                  process(pid=6, ticks=100)], 1_000_000_000)
        self.assertEqual([item['pid'] for item in ranked['targets']], [6, 5])

    def test_cap_zero_and_invalid_configuration(self):
        result = selector(max_targets=0, pids=[7]).update([process()], 0)
        self.assertEqual(result['selection']['omitted_count'], 1)
        for values in ({'active_cpu_percent': -1}, {'active_cpu_percent': float('nan')},
                       {'max_targets': -1}, {'uids': [-1]}, {'pids': [0]}, {'include_names': ['[']}):
            with self.assertRaises((ValueError, re.error)):
                selector(**values)
        with self.assertRaises(ValueError):
            DiscoverySelector({}, 0)


if __name__ == '__main__':
    unittest.main()
