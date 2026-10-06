import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.temperature_guard import guarded_command, assert_temperature_guard


class TemperatureGuardTests(unittest.TestCase):
    def test_installed_script_and_fault_code_keep_arguments_and_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            script=root/'console'
            script.write_text('import sys\nassert sys.argv[1:]==["argument"]\n')
            for index,command in enumerate(([str(script),'argument'],
                    [sys.executable,'-c','import sys; assert sys.argv[1:]==["argument"]','argument'])):
                evidence=root/(str(index)+'.json')
                result=subprocess.run(guarded_command(command,sys.executable,evidence),capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                assert_temperature_guard(evidence)

    def test_discovery_and_reads_rejected_even_when_child_swallows_exception(self):
        operations=["_Probe(Path('/')).children('/sys/class/thermal')",
                    "_Probe(Path('/')).read('/sys/class/hwmon/hwmon0/temp1_input')",
                    "list(Path('/sys/class/thermal').glob('thermal_zone*/temp'))",
                    "list(Path('/sys/class/thermal').iterdir())",
                    "Path('/sys/class/thermal/thermal_zone0/temp').read_text()",
                    "open('/sys/class/hwmon/hwmon0/temp1_input')"]
        with tempfile.TemporaryDirectory() as directory:
            for index,operation in enumerate(operations):
                evidence=Path(directory)/(str(index)+'.json')
                code='from pathlib import Path\nfrom perfkit.platform_probe import _Probe\ntry:\n '+operation+'\nexcept AssertionError: pass\n'
                result=subprocess.run(guarded_command([sys.executable,'-c',code],sys.executable,evidence),
                                      capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                with self.assertRaises(AssertionError): assert_temperature_guard(evidence)
                self.assertEqual(len(json.loads(evidence.read_text())['accesses']),1)
