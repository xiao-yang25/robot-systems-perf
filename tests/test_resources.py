import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from perfkit import resources as r


def task_stat(pid=11, comm='worker (with ) spaces', start=100, user=10, kernel=5):
    fields = ['0'] * 50
    fields[0] = 'S'
    for number, value in {10: 2, 12: 1, 14: user, 15: kernel, 18: 20,
                          19: 0, 20: 2, 22: start, 24: 3, 39: 1,
                          40: 0, 41: 0}.items():
        fields[number - 3] = str(value)
    return '{} ({}) {}'.format(pid, comm, ' '.join(fields))


def entity(user=10, wait=100, start=100, tid=None):
    item = {'stat': {'utime_ticks': user, 'stime_ticks': 0, 'starttime_ticks': start,
                     'minor_faults': user, 'major_faults': 0, 'rss_bytes': 4096,
                     'policy': 0, 'priority': 20, 'nice': 0, 'rt_priority': 0},
            'status': {'voluntary_ctxt_switches': user, 'nonvoluntary_ctxt_switches': 2,
                       'affinity_cpu_list': '0-1'},
            'schedstat': {'runtime_ns': user * 100, 'runnable_wait_ns': wait, 'timeslices': user},
            'availability': {'stat': None, 'status': None, 'schedstat': None}}
    if tid is not None:
        item['tid'] = tid
    return item


def sample(now, user=10, wait=100, start=100):
    process = entity(user, wait, start)
    process.update({'pid': 11, 'role': 'worker', 'registration_id': 1,
                    'starttime_ticks': start, 'tasks': [entity(user, wait, start, 12)],
                    'tasks_reason': None, 'cgroup': '/fixture/cgroup', 'cgroup_reason': None})
    return {'schema_version': 1, 'monotonic_ns': now, 'sample_end_ns': now,
            'clock_ticks_per_second': 100,
            'system': {'per_cpu_ticks': {'cpu0': {'user': user, 'nice': 0, 'system': 0,
                                                'idle': 100, 'iowait': 0, 'irq': 0,
                                                'softirq': 0, 'steal': 0,
                                                'guest': user * 5, 'guest_nice': 0}},
                       'meminfo_bytes': {'MemAvailable': 1024},
                       'temperatures': {'thermal_zone0': {'celsius': 40 + user, 'reason': None}},
                       'cpu_frequencies': {'cpu0': {'khz': 1000 + user, 'reason': None}},
                       'rail_power': {'hwmon0/power1_input': {'microwatts': user * 1000,
                                                            'label': 'VDD_IN', 'chip': 'ina',
                                                            'source': 'hwmon_power_input', 'reason': None}},
                       'device_frequencies': {'gpu': {'hz': user * 100, 'source': 'devfreq_cur_freq', 'reason': None}},
                       'sched_schedstats_enabled': 0,
                       'availability': {'per_cpu_ticks': None}},
            'processes': [process],
            'cgroups': {'/fixture/cgroup': {'cpu_stat': {'usage_usec': user * 10,
                                                      'user_usec': user * 10, 'system_usec': 0,
                                                      'nr_periods': user, 'nr_throttled': 1,
                                                      'throttled_usec': wait},
                                             'memory_current_bytes': user * 1024,
                                             'availability': {'cpu_stat': None}}}}


class ResourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def summarize(self, samples, start=0, end=10_000_000_000):
        path = self.root / 'resources.jsonl'
        path.write_text(''.join(json.dumps(item) + '\n' for item in samples))
        return r.summarize_resources(path, start, end)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_stat_parsing_parentheses_and_field_numbers(self):
        stat = r.parse_task_stat(task_stat())
        self.assertEqual(stat['comm'], 'worker (with ) spaces')
        self.assertEqual(stat['utime_ticks'], 10)
        self.assertEqual(stat['stime_ticks'], 5)
        self.assertEqual(stat['starttime_ticks'], 100)
        self.assertEqual(stat['rss_pages'], 3)
        self.assertEqual(stat['policy'], 0)
        self.assertEqual(stat['processor'], 1)
        with self.assertRaises(ValueError):
            r.parse_task_stat('11 (bad) S 0')

    def test_cpu_and_meminfo_keep_units(self):
        cpus = r.parse_cpu_stat('cpu 9 0 0 9\ncpu0 3 0 2 5 1 0 0 0 2 0\nintr 5\n')
        self.assertEqual(cpus['cpu0']['guest'], 2)
        self.assertNotIn('cpu', cpus)
        parsed = r._meminfo('MemTotal: 100 kB\nHugePages_Total: 4\n')
        self.assertEqual(parsed['bytes'], {'MemTotal': 102400})
        self.assertEqual(parsed['raw']['HugePages_Total'], {'value': 4, 'unit': 'count'})

    def test_known_deltas_ranges_and_scopes(self):
        a, b = sample(1_000_000_000, 10, 100), sample(2_000_000_000, 30, 120)
        b['system']['per_cpu_ticks']['cpu0']['idle'] = 180
        b['processes'][0]['stat']['rss_bytes'] = 8192
        report = self.summarize([a, b], 900_000_000, 2_100_000_000)
        self.assertEqual(report['coverage']['samples'], 2)
        self.assertEqual(report['coverage']['start_gap_ns'], 100_000_000)
        cpu = report['system']['per_core_cpu']['cpu0']
        self.assertEqual(cpu['busy_percent'], 20)
        processes = [value for value in report['registered_entities'].values() if value['kind'] == 'process']
        threads = [value for value in report['registered_entities'].values() if value['kind'] == 'thread']
        self.assertEqual(processes[0]['cpu_time_ns'], 200_000_000)
        self.assertEqual(processes[0]['cpu_percent_one_core'], 20)
        self.assertEqual(processes[0]['rss_peak_bytes'], 8192)
        self.assertEqual(processes[0]['schedstat_and_context_switch_scope'], 'leader_thread_only')
        self.assertEqual(threads[0]['delta']['runnable_wait_ns'], 20)
        self.assertEqual(threads[0]['delta']['minor_faults'], 20)
        self.assertEqual(threads[0]['delta']['voluntary_ctxt_switches'], 20)
        cgroup = report['cgroups']['/fixture/cgroup']
        self.assertEqual(cgroup['delta']['usage_usec'], 200)
        self.assertEqual(cgroup['delta']['throttled_usec'], 20)
        self.assertEqual(cgroup['memory_peak_bytes'], 30720)
        self.assertEqual(report['system']['rail_power_microwatt_ranges']['hwmon0/power1_input']['mean'], 20000)
        self.assertEqual(report['system']['named_device_frequency_hz_ranges']['gpu']['max'], 3000)
        self.assertEqual(report['system']['schedstats_setting']['sched_schedstats_enabled']['min'], 0)

    def test_no_outside_boundary_or_crossing_snapshot(self):
        a, b, c = sample(90, 1), sample(120, 20), sample(180, 100)
        c['sample_end_ns'] = 210
        report = self.summarize([a, b, c, sample(220, 200)], 100, 200)
        self.assertEqual(report['coverage']['samples'], 1)
        self.assertIsNone(report['coverage']['observed_span_ns'])
        for data in report['registered_entities'].values():
            self.assertIsNone(data['delta']['cpu_ticks'])
            self.assertIsNone(data['cpu_percent_one_core'])
        self.assertIsNone(report['system']['per_core_cpu']['cpu0']['busy_percent'])

    def test_empty_window_is_null_not_zero(self):
        report = self.summarize([sample(10)], 20, 30)
        self.assertEqual(report['coverage']['samples'], 0)
        self.assertIsNone(report['registered_entities'])
        self.assertIsNone(report['cgroups'])
        self.assertIsNone(report['system']['per_core_cpu'])
        self.assertIsNone(report['system']['rail_power_microwatt_ranges'])

    def test_resets_rejected_but_following_valid_delta_retained(self):
        report = self.summarize([sample(1, 10, 100), sample(2, 5, 50), sample(3, 8, 60)])
        for data in report['registered_entities'].values():
            self.assertEqual(data['delta']['cpu_ticks'], 3)
            self.assertEqual(data['reset_intervals_rejected']['cpu_ticks'], 1)
            self.assertEqual(data['delta']['runnable_wait_ns'], 10)
        cpu = report['system']['per_core_cpu']['cpu0']
        self.assertEqual(cpu['busy_percent'], 100)
        self.assertEqual(cpu['busy_percent_valid_intervals'], 1)

    def test_pid_and_tid_reuse_do_not_cross_identity(self):
        a, b = sample(1, 10, start=100), sample(2, 30, start=200)
        report = self.summarize([a, b])
        self.assertEqual(len(report['registered_entities']), 4)
        for data in report['registered_entities'].values():
            self.assertIsNone(data['delta']['cpu_ticks'])
        b = sample(2, 30)
        b['processes'][0]['tasks'][0]['stat']['starttime_ticks'] = 200
        report = self.summarize([a, b])
        threads = [data for data in report['registered_entities'].values() if data['kind'] == 'thread']
        self.assertEqual(len(threads), 2)
        for data in threads:
            self.assertIsNone(data['delta']['runnable_wait_ns'])
        b = sample(2, 30)
        b['processes'][0]['stat']['starttime_ticks'] = 200
        report = self.summarize([a, b])
        for data in report['registered_entities'].values():
            self.assertIsNone(data['delta']['cpu_ticks'])

    def test_missing_observation_breaks_counter_chain(self):
        a, b, c = sample(1, 10), sample(2, 20), sample(3, 30)
        b['processes'] = []
        b['cgroups'] = {}
        b['system']['per_cpu_ticks'] = None
        b['system']['availability']['per_cpu_ticks'] = 'not exposed'
        report = self.summarize([a, b, c])
        self.assertIsNone(report['system']['per_core_cpu']['cpu0']['busy_percent'])
        self.assertIsNone(report['cgroups']['/fixture/cgroup']['delta']['usage_usec'])
        for data in report['registered_entities'].values():
            self.assertIsNone(data['delta']['cpu_ticks'])
        self.assertEqual(report['availability']['system.per_cpu_ticks']['unavailable_samples'], 1)

    def test_schedstat_zero_retained_without_claiming_enabled(self):
        a, b = sample(1, 10, 0), sample(2, 20, 0)
        for item in (a, b):
            item['processes'][0]['status'] = None
            item['processes'][0]['availability']['status'] = 'permission denied'
        report = self.summarize([a, b])
        process = next(data for data in report['registered_entities'].values() if data['kind'] == 'process')
        self.assertEqual(process['delta']['runnable_wait_ns'], 0)
        self.assertIn('zero_does_not_prove', process['schedstat_interpretation'])
        self.assertIsNone(process['delta']['voluntary_ctxt_switches'])

    def test_proc_registration_reuse_and_unregister(self):
        self.write('proc/11/stat', task_stat())
        self.write('proc/11/status', 'Cpus_allowed_list: 0-1\nvoluntary_ctxt_switches: 3\nnonvoluntary_ctxt_switches: 2\n')
        self.write('proc/11/schedstat', '10 20 1\n')
        self.write('proc/11/task/12/stat', task_stat(pid=12))
        self.write('proc/11/task/12/status', '')
        self.write('proc/11/task/12/schedstat', '0 0 0')
        with patch.object(r, 'PROC_ROOT', self.root / 'proc'), patch.object(r, 'SYS_ROOT', self.root / 'sys'):
            sampler = r.ResourceSampler(self.root / 'out.jsonl', .1)
            sampler.register(11, 'worker')
            data = sampler._snapshot()['processes'][0]
            self.assertEqual(data['stat']['starttime_ticks'], 100)
            self.assertEqual(data['tasks'][0]['tid'], 12)
            self.assertEqual(data['tasks'][0]['availability']['context_switches'], 'context-switch fields absent')
            self.write('proc/11/stat', task_stat(start=200))
            data = sampler._snapshot()['processes'][0]
            self.assertIsNone(data['stat'])
            self.assertIsNone(data['tasks'])
            self.assertIn('identity', data['availability']['stat'])
            sampler.unregister(11)
            self.assertEqual(sampler._snapshot()['processes'], [])
            sampler.register(11, 'new-worker')
            self.assertEqual(sampler._snapshot()['processes'][0]['starttime_ticks'], 200)

    def test_sysfs_optional_sources_and_no_gpu_name_guessing(self):
        self.write('sys/class/hwmon/hwmon0/power1_input', '1234000\n')
        self.write('sys/class/hwmon/hwmon0/power1_label', 'VDD_IN\n')
        self.write('sys/class/hwmon/hwmon0/name', 'ina3221\n')
        self.write('sys/class/devfreq/platform.gpu/cur_freq', '456000000\n')
        self.write('sys/class/devfreq/17000000/cur_freq', '999000000\n')
        self.write('sys/class/devfreq/platform.emc/cur_freq', '123000000\n')
        with patch.object(r, 'PROC_ROOT', self.root / 'proc'), patch.object(r, 'SYS_ROOT', self.root / 'sys'):
            sampler = r.ResourceSampler(self.root / 'out.jsonl', .1)
            system = sampler._snapshot()['system']
            self.assertIsNone(system['per_cpu_ticks'])
            self.assertIn('FileNotFoundError', system['availability']['per_cpu_ticks'])
            self.assertEqual(system['rail_power']['hwmon0/power1_input']['microwatts'], 1234000)
            self.assertEqual(system['rail_power']['hwmon0/power1_input']['label'], 'VDD_IN')
            self.assertEqual(set(system['device_frequencies']), {'platform.gpu', 'platform.emc'})
            self.assertIsNone(system['gpu_utilization'])
            self.assertIsNone(system['emc_bandwidth'])

    def test_sampling_io_does_not_hold_registration_lock(self):
        entered, release = threading.Event(), threading.Event()
        sampler = r.ResourceSampler(self.root / 'out.jsonl', .1)
        def slow_system():
            entered.set()
            release.wait(2)
            return {}
        with patch.object(sampler, '_system', slow_system):
            thread = threading.Thread(target=sampler._snapshot)
            thread.start()
            self.assertTrue(entered.wait(1))
            try:
                sampler.set_window('S01', 1, 'active')
                sampler.register(os.getpid(), 'self')
                sampler.unregister(os.getpid())
                self.assertFalse(release.is_set())
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_sampler_error_and_no_overwrite(self):
        sampler = r.ResourceSampler(self.root / 'out.jsonl', .01)
        with patch.object(sampler, '_snapshot', side_effect=ValueError('fixture failure')):
            with sampler:
                sampler._thread.join(1)
        self.assertFalse(sampler._thread.is_alive())
        self.assertEqual(sampler.error, 'ValueError: fixture failure')
        with self.assertRaises(FileExistsError):
            with r.ResourceSampler(self.root / 'out.jsonl', .01):
                pass
        for interval in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                r.ResourceSampler(self.root / 'bad.jsonl', interval)

    def test_portable_real_local_sampling_and_shutdown(self):
        path = self.root / 'real.jsonl'
        sampler = r.ResourceSampler(path, .01)
        sampler.register(os.getpid(), 'test-process')
        sampler.set_window('portable', 1, 'active')
        with sampler:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if path.exists() and len(path.read_text().splitlines()) >= 2:
                    break
                time.sleep(.01)
        self.assertIsNone(sampler.error)
        self.assertFalse(sampler._thread.is_alive())
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]['window']['phase'], 'active')
        self.assertGreaterEqual(rows[0]['sample_end_ns'], rows[0]['monotonic_ns'])
        if Path('/proc/stat').exists():
            self.assertIsNotNone(rows[0]['system']['per_cpu_ticks'])
            self.assertIsNotNone(rows[0]['processes'][0]['stat'])
        else:
            self.assertIsNone(rows[0]['system']['per_cpu_ticks'])
            self.assertIsNotNone(rows[0]['system']['availability']['per_cpu_ticks'])
        report = r.summarize_resources(path, rows[0]['monotonic_ns'], rows[-1]['sample_end_ns'])
        self.assertGreaterEqual(report['coverage']['samples'], 2)
        json.dumps(report, allow_nan=False)

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            self.summarize([sample(1)], 2, 1)
        with self.assertRaises(ValueError):
            self.summarize([sample(2), sample(1)])
        broken = sample(1)
        broken['sample_end_ns'] = 0
        with self.assertRaisesRegex(ValueError, 'line 1'):
            self.summarize([broken])
        path = self.root / 'invalid.jsonl'
        path.write_text('{incomplete\n')
        with self.assertRaisesRegex(ValueError, 'line 1'):
            r.summarize_resources(path, 0, 100)


if __name__ == '__main__':
    unittest.main()
