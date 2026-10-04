"""Opt-in scope prefilter and explicit profile coverage/override contracts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit import monitor as m
from perfkit import resources as r
from perfkit.discovery import scan_processes
from tests.test_discovery import fixture, selector, task_record
from tests.test_resources import task_stat


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cgroup_prefilter_skips_other_services_and_keeps_target_records(self):
        fixture(self.root, pid=7)
        outside = fixture(self.root, pid=8)
        (outside / 'cgroup').write_text('0::/other.service\n')
        config = {'pids': [7, 8], 'cgroup_patterns': ['^/robot'], 'active_cpu_percent': 0}
        baseline = selector(**config)
        expected = baseline.update(scan_processes(self.root, _scope=baseline)['processes'], 0)
        scope = selector(**config, cgroup_prefilter=True)
        original = Path.read_text
        observed = []
        def read(path, *args, **kwargs):
            observed.append(path)
            if path.parent == outside:
                self.assertEqual(path.name, 'cgroup')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root, _scope=scope)
        self.assertEqual(scope.update(result['processes'], 0), expected)
        self.assertEqual(result['scan']['scope_filtered_by_reason'], {'cgroup_prefilter': 1})
        self.assertEqual(sum(path.parent == outside for path in observed), 1)

    def test_membership_rechecked_after_early_match(self):
        base = fixture(self.root)
        scope = selector(pids=[7], cgroup_patterns=['^/robot'], cgroup_prefilter=True)
        original = Path.read_text
        calls = 0
        def read(path, *args, **kwargs):
            nonlocal calls
            if path == base / 'cgroup':
                calls += 1
                if calls == 2:
                    return '0::/other.service\n'
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root, _scope=scope)
        self.assertEqual(result['processes'], [])
        self.assertEqual(calls, 2)
        self.assertEqual(scope._scan_paths, {})

    def test_early_read_failure_falls_back_to_fresh_full_validation(self):
        base = fixture(self.root)
        scope = selector(pids=[7], cgroup_patterns=['robot'], cgroup_prefilter=True)
        original = Path.read_text
        calls = 0
        def read(path, *args, **kwargs):
            nonlocal calls
            if path == base / 'cgroup':
                calls += 1
                if calls == 1:
                    raise PermissionError('synthetic early failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = scan_processes(self.root, _scope=scope)
        self.assertEqual(result['scan']['prefilter_fallback_count'], 1)
        self.assertEqual([p['pid'] for p in result['processes']], [7])

    def test_cached_discovery_paths_do_not_cache_identity_uid_or_membership(self):
        base = fixture(self.root)
        scope = selector(pids=[7])
        scan_processes(self.root, _scope=scope)
        old_files = scope._scan_paths[7]
        (base / 'stat').write_text(task_record(start=900, user=999))
        result = scan_processes(self.root, _scope=scope)
        self.assertEqual(result['processes'][0]['starttime_ticks'], 900)
        self.assertEqual(result['processes'][0]['cpu_ticks'], 1029)
        self.assertIs(scope._scan_paths[7][0], old_files[0])
        (base / 'status').write_text('Uid: 2000 2000 2000 2000\n')
        self.assertEqual(scan_processes(self.root, _scope=scope)['processes'], [])
        self.assertEqual(scope._scan_paths, {})

    def test_invalid_prefilter_configuration_is_rejected(self):
        for config in ({'cgroup_prefilter': True}, {'cgroup_prefilter': 1},
                       {'cgroup_prefilter': None, 'cgroup_patterns': ['robot']}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                m.validate_config(config)
            with self.assertRaises(ValueError):
                selector(**config)


class ProfileTests(unittest.TestCase):
    def test_profiles_are_generic_explicit_and_do_not_invent_budgets(self):
        root = Path(__file__).resolve().parent.parent
        for name in ('light', 'full'):
            config = m.profile_config(name)
            self.assertEqual(config, json.loads((root / 'configs' / ('business-' + name + '.json')).read_text()))
            m.validate_config(config)
            self.assertFalse(config['require_jetson'])
            options = config['resource_options']
            self.assertTrue(options['skip_temperatures'])
            self.assertIsNone(options['max_cycle_fraction'])
            self.assertIsNone(options['max_observer_cpu_percent_one_core'])
        self.assertFalse(m.profile_config('light')['resource_options']['collect_threads'])
        self.assertTrue(m.profile_config('full')['resource_options']['collect_threads'])
        self.assertTrue(m.validate_config({})['resource_options'] == {})

    def test_profile_then_file_then_cli_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'config.json'
            config_path.write_text(json.dumps({'resource_options': {'collect_threads': True,
                                                  'process_sampling_seconds': .5}}))
            argv = ['monitor', '--profile', 'light', '--config', str(config_path),
                    '--output', str(Path(directory) / 'output'), '--thread-interval', '2',
                    '--max-observer-cpu-percent', '3', '--cgroup-pattern', '^/robot', '--cgroup-prefilter']
            with patch.object(m.platform, 'system', return_value='Linux'), patch('sys.argv', argv), \
                    patch.object(m, 'run_monitor') as run, patch.object(m.signal, 'signal'):
                m.main()
            config = run.call_args[0][0]
            options = config['resource_options']
            self.assertTrue(options['collect_threads'])
            self.assertEqual(options['process_sampling_seconds'], .5)
            self.assertEqual(options['thread_sampling_seconds'], 2)
            self.assertTrue(options['skip_temperatures'])
            self.assertEqual(options['max_observer_cpu_percent_one_core'], 3)
            self.assertTrue(config['cgroup_prefilter'])

    def test_scandir_thread_paths_are_reused_but_counters_are_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '11/task/12').mkdir(parents=True)
            (root / '11/stat').write_text(task_stat())
            path = root / '11/task/12/stat'
            path.write_text(task_stat(pid=12, user=10))
            with patch.object(r, 'PROC_ROOT', root):
                sampler = r.ResourceSampler(root / 'resources.jsonl', .1)
                sampler.register(11, 'fixture')
                sampler._snapshot({'thread'})
                path.write_text(task_stat(pid=12, user=30))
                with patch.object(r, '_task_paths', side_effect=AssertionError('known TID rebuilt paths')):
                    row = sampler._snapshot({'thread'})
                self.assertEqual(row['processes'][0]['tasks'][0]['stat']['utime_ticks'], 30)
                path.unlink()
                sampler._snapshot({'thread'})
                self.assertEqual(sampler._thread_paths[11], {})


if __name__ == '__main__':
    unittest.main()
