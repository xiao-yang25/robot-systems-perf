import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit.platform_probe import collect_profile, main


class PlatformTests(unittest.TestCase):
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
            self.assertIsNone(profile['jetson_linux_release'])
            self.assertIsNone(profile['nvpmodel_readonly']['value'])
            self.assertNotIn('serial', profile)

    def test_arm_vm_is_not_claimed_to_be_jetson(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('perfkit.platform_probe.platform.system', return_value='Linux'), \
             patch('perfkit.platform_probe.platform.machine', return_value='aarch64'), \
             patch('perfkit.platform_probe.query', return_value={}):
            profile = collect_profile(Path(directory))
            self.assertFalse(profile['checks']['jetson_detected'])

    def test_monitor_profile_does_not_read_kernel_command_line(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('perfkit.platform_probe.query', return_value={}):
            from perfkit import platform_probe as p
            original = p.optional
            observed = []
            def checked(path):
                observed.append(str(path))
                self.assertFalse(str(path).endswith('/proc/cmdline'))
                return original(path)
            with patch.object(p, 'optional', side_effect=checked):
                profile = collect_profile(Path(directory), include_kernel_command_line=False)
            self.assertIsNone(profile['kernel_command_line'])
            self.assertTrue(observed)

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
