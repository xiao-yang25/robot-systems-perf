"""Known machine fixtures qualify intake provenance, failures and no execution."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit import intake
from perfkit.monitor import validate_config


class IntakeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fs = self.root / 'fixture'
        self.fs.mkdir()
        self.put('/proc/stat', 'cpu  1 2 3 4\n')
        self.put('/proc/meminfo', 'MemTotal: 2048 kB\n')
        self.put('/etc/os-release', 'ID=ubuntu\nVERSION_ID="24.04"\n')
        self.put('/proc/cmdline', 'do-not-export-private-boot-value')
        self.put('/sys/devices/system/cpu/cpu0/topology/core_id', '0')
        self.put('/sys/devices/system/cpu/cpu0/topology/physical_package_id', '0')
        self.put('/sys/devices/system/cpu/cpu0/topology/thread_siblings_list', '0')
        self.put('/sys/block/nvme0n1/size', '8')
        self.put('/sys/block/nvme0n1/queue/logical_block_size', '4096')
        for mock in (patch('perfkit.platform_probe.platform.system', return_value='Linux'),
                     patch('perfkit.platform_probe.platform.machine', return_value='aarch64'),
                     patch('perfkit.platform_probe.shutil.which', return_value=None),
                     patch('subprocess.Popen', side_effect=AssertionError('intake launched a command'))):
            mock.start()
            self.addCleanup(mock.stop)

    def put(self, source, text):
        path = self.fs / source.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_intake(self, name='output', **kwargs):
        return intake.run_intake(self.root / name, fs_root=self.fs, **kwargs)

    def read(self, name, folder='output'):
        return json.loads((self.root / folder / name).read_text())

    def test_orin_thor_profiles_reuse_observed_platform_and_known_units(self):
        for family, bsp in (('Orin', '# R36 revision fixture'), ('Thor', '# R38 revision fixture')):
            with self.subTest(family=family):
                self.put('/proc/device-tree/model', 'NVIDIA Jetson AGX ' + family + '\0')
                self.put('/etc/nv_tegra_release', bsp)
                machine = self.run_intake(family, machine_id='demo-machine', require_jetson=True)
                self.assertEqual(machine['observed_platform']['jetson_family'], family.lower())
                self.assertEqual(machine['observed_platform']['jetson_linux_release'], bsp)
                self.assertEqual(machine['hardware']['memory_total_bytes']['value'], 2097152)
                self.assertEqual(machine['hardware']['visible_block_devices']['entries'][0]['capacity_bytes']['value'], 4096)
                self.assertEqual(machine['hardware']['cpu_topology']['entries'][0]['core_id']['value'], 0)
                self.assertEqual(self.read('intake-status.json', family)['status'], 'complete')

    def test_optional_missing_tools_keep_unknown_functionality_and_unset_budgets(self):
        machine = self.run_intake()
        self.assertIsNone(machine['observed_platform']['kernel_command_line'])
        self.assertEqual(machine['view']['declared'], 'unknown')
        self.assertEqual(machine['machine_id_source'], 'anonymous_this_run')
        self.assertIsNone(machine['observed_platform']['nvpmodel_readonly']['value'])
        caps = self.read('capabilities.json')
        self.assertFalse(caps['interfaces']['thermal']['available'])
        self.assertTrue(caps['interfaces']['thermal']['reason'])
        self.assertEqual(caps['tools']['cyclictest']['sampling']['status'], 'not_evaluated')
        self.assertEqual(caps['tools']['cyclictest']['version_options']['status'], 'unavailable')
        config = validate_config(self.read('monitor-config.suggested.json'))
        self.assertFalse(config['resource_options']['collect_threads'])
        self.assertFalse(config['resource_options']['jetson_telemetry'])
        self.assertIsNone(config['resource_options']['max_cycle_fraction'])
        self.assertIsNone(config['resource_options']['max_observer_cpu_percent_one_core'])
        self.assertEqual(config['include_names'], [])
        for path in (self.root / 'output').iterdir():
            self.assertNotIn('do-not-export-private-boot-value', path.read_text())
        self.assertEqual({path.name for path in (self.root / 'output').iterdir()},
                         {'machine-profile.json', 'capabilities.json', 'intake-status.json',
                          'monitor-config.suggested.json', 'MACHINE_REPORT.md'})

    def test_found_tool_and_declared_metadata_do_not_become_runtime_verification(self):
        manual = {'format_version': 1, 'ros_distribution': 'humble', 'power_mode': 'operator-example'}
        with patch('perfkit.platform_probe.shutil.which', return_value='/fixture/bin/tool'):
            machine = self.run_intake(metadata=manual, view='container')
        self.assertEqual(machine['view']['source'], 'operator_supplied')
        self.assertEqual(machine['manual_declarations']['ros_distribution']['status'], 'declared_not_verified')
        self.assertIsNone(machine['observed_platform'].get('ros_distribution'))
        caps = self.read('capabilities.json')
        self.assertTrue(caps['tools']['ros2']['found'])
        self.assertEqual(caps['tools']['ros2']['version_options']['status'], 'not_evaluated')
        self.assertEqual(caps['tools']['ros2']['sampling']['status'], 'not_evaluated')
        self.assertEqual(manual, {'format_version': 1, 'ros_distribution': 'humble', 'power_mode': 'operator-example'})

    def test_explicit_missing_requirements_fail_nonzero_semantics_and_retain_evidence(self):
        for name, options in (('jetson', {'require_jetson': True}),
                              ('thermal', {'required_capabilities': ['thermal']})):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'required observations'):
                self.run_intake(name, **options)
            status = self.read('intake-status.json', name)
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(status['requirements'][-1]['met'], False)
            self.assertTrue((self.root / name / 'machine-profile.json').is_file())
            self.assertTrue((self.root / name / 'MACHINE_REPORT.md').is_file())
        with self.assertRaisesRegex(ValueError, 'unknown required'):
            self.run_intake('unknown', required_capabilities=['not-a-capability'])
        self.assertEqual(self.read('intake-status.json', 'unknown')['status'], 'failed')

    def test_existing_output_and_invalid_metadata_never_overwrite_prior_evidence(self):
        self.run_intake()
        before = {p.name: p.read_bytes() for p in (self.root / 'output').iterdir()}
        with self.assertRaises(FileExistsError):
            self.run_intake()
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'output').iterdir()})
        for manual in ([], {'format_version': True}, {'format_version': 1, 'unsupported_field': 'test'},
                       {'format_version': 1, 'cooling': ''}, {'format_version': 1, 'power_mode': 2}):
            with self.subTest(manual=manual), self.assertRaises(ValueError):
                self.run_intake('invalid', metadata=manual)
            self.assertFalse((self.root / 'invalid').exists())

    def test_read_failure_and_cancellation_retain_final_status(self):
        for name, error, expected in (('error', RuntimeError('controlled read failure'), 'failed'),
                                      ('cancel', KeyboardInterrupt('controlled cancellation'), 'interrupted')):
            with self.subTest(name=name), patch.object(intake, 'hardware_facts', side_effect=error):
                with self.assertRaises(type(error)):
                    self.run_intake(name)
            status = self.read('intake-status.json', name)
            self.assertEqual(status['status'], expected)
            self.assertGreaterEqual(status['finished_ns'], status['started_ns'])

    def test_late_cancellation_cannot_leave_complete_final_status(self):
        original = intake._status
        writes = []
        def interrupt_after_write(path, value):
            original(path, value)
            writes.append(value['status'])
            if len(writes) == 1:
                raise KeyboardInterrupt('controlled deferred interrupt')
        with patch.object(intake, '_status', side_effect=interrupt_after_write):
            with self.assertRaises(KeyboardInterrupt):
                self.run_intake()
        self.assertEqual(writes, ['complete', 'interrupted'])
        self.assertEqual(self.read('intake-status.json')['status'], 'interrupted')

    def test_environment_revision_remains_a_declaration(self):
        with patch.dict(os.environ, {'EP_SOURCE_REVISION': 'operator-revision'}):
            machine = self.run_intake()
        self.assertEqual(machine['source']['git_revision'], 'operator-revision')
        self.assertEqual(machine['source']['git_revision_source'], 'environment_declared_not_verified')

    def test_invalid_memory_and_storage_keep_missing_values_with_reasons(self):
        self.put('/proc/meminfo', 'MemTotal: invalid kB\n')
        self.put('/sys/block/nvme0n1/size', '-1')
        machine = self.run_intake()
        for fact in (machine['hardware']['memory_total_bytes'],
                     machine['hardware']['visible_block_devices']['entries'][0]['capacity_bytes']):
            self.assertIsNone(fact['value'])
            self.assertFalse(fact['observation']['available'])
            self.assertEqual(fact['observation']['reason'], 'unrecognized interface contents')

    def test_host_is_not_supported_platform_success(self):
        with patch('perfkit.platform_probe.platform.system', return_value='Darwin'):
            with self.assertRaisesRegex(RuntimeError, 'linux'):
                self.run_intake()
        self.assertEqual(self.read('intake-status.json')['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
