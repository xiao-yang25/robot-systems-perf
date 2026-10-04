import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit.platform_probe import _Probe, collect_profile, main, optional


class PlatformTests(unittest.TestCase):
    def write(self, root, source, value):
        path = root / source.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def profile(self, root, **kwargs):
        with patch('perfkit.platform_probe.platform.system', return_value='Linux'), \
             patch('perfkit.platform_probe.platform.machine', return_value='aarch64'), \
             patch('perfkit.platform_probe.shutil.which', return_value=None), \
             patch('perfkit.platform_probe.query', return_value={'available': False, 'value': None, 'reason': 'not installed'}):
            return collect_profile(root, **kwargs)

    def test_jetson_detection_and_missing_capabilities_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'proc/device-tree').mkdir(parents=True)
            (root / 'proc/device-tree/model').write_bytes(b'NVIDIA Jetson AGX Thor\0')
            with patch('perfkit.platform_probe.platform.system', return_value='Linux'), \
                 patch('perfkit.platform_probe.platform.machine', return_value='aarch64'), \
                 patch('perfkit.platform_probe.query', return_value={'available': False, 'value': None, 'reason': 'not installed'}):
                profile = collect_profile(root)
            self.assertTrue(profile['checks']['jetson_detected'])
            self.assertEqual(profile['jetson_family'], 'thor')
            self.assertIsNone(profile['jetson_linux_release'])
            self.assertIsNone(profile['nvpmodel_readonly']['value'])
            for name, capability in profile['capabilities'].items():
                with self.subTest(capability=name):
                    self.assertFalse(capability['available'])
                    self.assertFalse(capability['present'])
                    self.assertTrue(capability['reason'])
                    self.assertIn('source', capability)
            self.assertNotIn('serial', profile)

    def test_family_comes_from_model_and_not_bsp_version(self):
        cases = [('NVIDIA Jetson AGX Orin', 'orin'), ('NVIDIA Jetson AGX Thor', 'thor'),
                 ('NVIDIA Jetson Xavier', 'unknown'), (None, 'unknown')]
        for model, expected in cases:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write(root, '/etc/nv_tegra_release', '# R38 (release), REVISION: 1.0')
                if model is not None:
                    self.write(root, '/sys/firmware/devicetree/base/model', model + '\x00')
                profile = self.profile(root)
                self.assertTrue(profile['checks']['jetson_detected'])
                self.assertEqual(profile['jetson_family'], expected)

    def test_readable_matrix_and_disabled_schedstats_do_not_claim_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                '/proc/stat': 'cpu  10 20 30 40 50 60 70 80\n',
                '/proc/meminfo': 'MemTotal:       2048000 kB\nMemAvailable: 100 kB\n',
                '/proc/self/task/101/stat': '101 (worker (name)) R ' + ' '.join(['0'] * 19),
                '/proc/self/task/101/schedstat': '0 0 0\n',
                '/proc/sys/kernel/sched_schedstats': '0\n',
                '/proc/self/cgroup': '0::/workload\n',
                '/sys/fs/cgroup/workload/cpu.stat': 'usage_usec 20\n',
                '/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq': '1200000\n',
                '/sys/devices/system/cpu/cpufreq/policy0/scaling_governor': 'schedutil\n',
                '/sys/class/devfreq/17000000.gpu/cur_freq': '400000000\n',
                '/sys/class/devfreq/controller/name': 'emc\n',
                '/sys/class/devfreq/controller/cur_freq': '800000000\n',
                '/sys/class/hwmon/hwmon0/power1_input': '5000000\n',
                '/sys/class/thermal/thermal_zone0/temp': '42000\n',
                '/sys/kernel/tracing/available_events': 'sched:sched_switch\n',
                '/proc/sys/kernel/perf_event_paranoid': '3\n'}
            for source, value in fixtures.items():
                self.write(root, source, value)
            profile = self.profile(root)
            for name, capability in profile['capabilities'].items():
                with self.subTest(capability=name):
                    self.assertTrue(capability['available'])
                    self.assertTrue(capability['present'])
                    self.assertIsNone(capability['reason'])
            sched = profile['capabilities']['task_schedstat']
            self.assertFalse(sched['schedstats_enabled'])
            self.assertFalse(sched['attribution_available'])
            self.assertEqual(profile['schedstats_enabled'], '0')
            self.assertEqual(profile['frequency_policies']['policy0']['scaling_governor'], 'schedutil')
            self.assertEqual(profile['capabilities']['cgroup']['mode'], 'v2')
            self.assertIsNone(profile['capabilities']['tracefs']['functional'])
            self.assertIsNone(profile['capabilities']['perf']['functional'])

    def test_permission_denied_and_invalid_reads_are_distinct_from_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, '/proc/stat', 'cpu  1 2 3 4\n')
            self.write(root, '/proc/meminfo', 'not meminfo')
            original = Path.read_text
            def denied(path, *args, **kwargs):
                if str(path).endswith('/proc/stat'):
                    raise PermissionError('denied for test')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', denied):
                profile = self.profile(root)
            cpu = profile['capabilities']['proc_cpu']
            self.assertTrue(cpu['present'])
            self.assertFalse(cpu['available'])
            self.assertEqual(cpu['reason'], 'permission denied')
            memory = profile['capabilities']['proc_memory']
            self.assertTrue(memory['present'])
            self.assertEqual(memory['reason'], 'unrecognized interface contents')
            sched = profile['capabilities']['task_schedstat']
            self.assertFalse(sched['present'])
            self.assertFalse(sched['available'])
            self.assertIsNone(sched['schedstats_enabled'])

    def test_cgroup_v1_and_membership_without_visible_interfaces(self):
        cases = [('2:cpu,cpuacct:/work\n3:memory:/work\n', 'v1',
                  '/sys/fs/cgroup/cpu,cpuacct/work/cpuacct.usage', '123'),
                 ('0::/work\n', 'v2', None, None),
                 ('0::/work\n2:cpu:/work\n', 'hybrid', '/sys/fs/cgroup/work/cpu.stat', 'usage_usec 1'),
                 ('malformed', 'unknown', None, None)]
        for membership, mode, source, contents in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write(root, '/proc/self/cgroup', membership)
                if source:
                    self.write(root, source, contents)
                capability = self.profile(root)['capabilities']['cgroup']
                self.assertEqual(capability['mode'], mode)
                self.assertEqual(capability['available'], source is not None)
                if not source:
                    self.assertTrue(capability['reason'])

    def test_cgroup_membership_traversal_is_not_a_visible_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, '/proc/self/cgroup', '0::/../../../proc\n')
            self.write(root, '/proc/cpu.stat', 'usage_usec 1')
            capability = self.profile(root)['capabilities']['cgroup']
            self.assertEqual(capability['mode'], 'unknown')
            self.assertFalse(capability['available'])
            self.assertEqual(capability['reason'], 'no recognized cgroup membership')

    def test_directory_permission_errors_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sys/class/hwmon').mkdir(parents=True)
            original = Path.iterdir
            def denied(path):
                if str(path).endswith('/sys/class/hwmon'):
                    raise PermissionError('denied for test')
                return original(path)
            with patch.object(Path, 'iterdir', denied):
                capability = self.profile(root)['capabilities']['hwmon_power']
            self.assertFalse(capability['available'])
            self.assertIn('permission denied', capability['reason'])

    def test_unnamed_devfreq_does_not_imply_gpu_or_emc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, '/sys/class/devfreq/other/cur_freq', '123')
            profile = self.profile(root)
            self.assertFalse(profile['capabilities']['gpu_devfreq']['available'])
            self.assertFalse(profile['capabilities']['emc_devfreq']['available'])

    def test_gpu_hardware_names_are_candidates_and_still_require_readable_frequency(self):
        for name in ('ga10b', 'gv11b', 'gb20b'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = '/sys/class/devfreq/17000000.' + name + '/cur_freq'
                path = self.write(root, source, 'invalid')
                capability = self.profile(root)['capabilities']['gpu_devfreq']
                self.assertFalse(capability['available'])
                self.assertTrue(capability['present'])
                self.assertEqual(capability['reason'], 'unrecognized interface contents')
                path.write_text('400000000')
                capability = self.profile(root)['capabilities']['gpu_devfreq']
                self.assertTrue(capability['available'])
                self.assertEqual(capability['source'], [source])

    def test_fixture_symlinks_cannot_read_or_list_outside_root(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root, external = Path(directory).resolve(), Path(outside).resolve()
            self.write(external, '/stat', 'cpu  1 2 3 4\n')
            self.write(external, '/hwmon0/power1_input', '100')
            (root / 'proc').mkdir()
            (root / 'proc/stat').symlink_to(external / 'stat')
            (root / 'sys/class').mkdir(parents=True)
            (root / 'sys/class/hwmon').symlink_to(external)
            original_read, original_iterdir = Path.read_text, Path.iterdir
            def checked_read(path, *args, **kwargs):
                self.assertTrue(path.resolve().is_relative_to(root))
                return original_read(path, *args, **kwargs)
            def checked_list(path):
                self.assertTrue(path.resolve().is_relative_to(root))
                return original_iterdir(path)
            with patch.object(Path, 'read_text', checked_read), patch.object(Path, 'iterdir', checked_list):
                profile = self.profile(root)
            self.assertFalse(profile['capabilities']['proc_cpu']['available'])
            self.assertIn('outside fs_root', profile['capabilities']['proc_cpu']['reason'])
            self.assertFalse(profile['capabilities']['hwmon_power']['available'])
            self.assertIn('outside fs_root', profile['capabilities']['hwmon_power']['reason'])

    def test_tools_are_discovered_without_functional_tests(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('perfkit.platform_probe.shutil.which', return_value='/usr/bin/found'), \
             patch('perfkit.platform_probe.query', return_value={}) as query:
            profile = collect_profile(Path(directory))
            query.assert_called_once_with(['nvpmodel', '-q'])
            for name in ('pidstat', 'mpstat', 'cyclictest', 'rtla', 'nsys', 'ros2', 'lttng'):
                self.assertTrue(profile['checks']['tools'][name])
            self.assertTrue(profile['capabilities']['perf']['found'])
            self.assertIsNone(profile['capabilities']['perf']['functional'])
            self.assertTrue(any('found != functional' in item for item in profile['limitations']))

    def test_arm_vm_is_not_claimed_to_be_jetson(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('perfkit.platform_probe.platform.system', return_value='Linux'), \
             patch('perfkit.platform_probe.platform.machine', return_value='aarch64'), \
             patch('perfkit.platform_probe.query', return_value={}):
            profile = collect_profile(Path(directory))
            self.assertFalse(profile['checks']['jetson_detected'])
            self.assertIsNone(profile['jetson_family'])

    def test_monitor_profile_does_not_read_kernel_command_line(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('perfkit.platform_probe.query', return_value={}):
            original = Path.read_text
            observed = []
            root = Path(directory)
            self.write(root, '/proc/cmdline', 'private kernel arguments')
            self.write(root, '/etc/os-release', 'ID=test')
            def checked(path, *args, **kwargs):
                observed.append(str(path))
                self.assertFalse(str(path).endswith('/proc/cmdline'))
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', checked):
                profile = collect_profile(Path(directory), include_kernel_command_line=False)
            self.assertIsNone(profile['kernel_command_line'])
            self.assertTrue(observed)

    def test_optional_type_errors_are_null_with_explicit_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, '/proc/stat', 'cpu 1 2 3 4')
            with patch.object(Path, 'read_text', side_effect=TypeError('unsupported read')):
                profile = self.profile(root)
                self.assertIsNone(profile['board_model'])
                capability = profile['capabilities']['proc_cpu']
                self.assertFalse(capability['available'])
                self.assertTrue(capability['present'])
                self.assertIn('unsupported read', capability['reason'])
                self.assertIsNone(optional(root / 'proc/stat'))
            with patch.object(Path, 'iterdir', side_effect=TypeError('unsupported list')):
                profile = self.profile(root)
                self.assertFalse(profile['capabilities']['task_stat']['available'])
                self.assertIn('unsupported list', profile['capabilities']['task_stat']['reason'])
            with patch.object(_Probe, 'path', side_effect=TypeError('unsupported path')):
                value, status = _Probe(root).read('/proc/stat')
                self.assertIsNone(value)
                self.assertFalse(status['available'])
                self.assertIn('unsupported path', status['reason'])
                children, reason = _Probe(root).children('/proc')
                self.assertEqual(children, [])
                self.assertIn('unsupported path', reason)
            def broken_validator(value):
                raise TypeError('unsupported parser')
            value, status = _Probe(root).read('/proc/stat', broken_validator)
            self.assertIsNone(value)
            self.assertIn('unsupported parser', status['reason'])
        self.assertIsNone(optional(object()))

    def test_required_fs_root_type_error_is_not_optional(self):
        with self.assertRaises(TypeError):
            collect_profile(object())

    def test_require_jetson_preserves_failed_profile_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'host.json'
            profile = {'checks': {'jetson_detected': False}}
            with patch('perfkit.platform_probe.collect_profile', return_value=profile), \
                 patch('sys.argv', ['probe', '--output', str(output), '--require-jetson']):
                with self.assertRaises(SystemExit):
                    main()
                original = output.read_bytes()
                with self.assertRaises(FileExistsError):
                    main()
            self.assertEqual(json.loads(original), profile)
            self.assertEqual(output.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
