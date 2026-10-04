"""Linux main-entry qualification failures; controlled tools, no Jetson claims."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.comparison_common import ROOT
from tests.test_tool_wakeup import histogram
from tests.test_tool_comparison import BABELTRACE_HELP

HELP = ('cyclictest V 2.50\nUsage:\ncyclictest <options>\n'
        '--default-system --priority --policy --threads --clock --interval --duration --quiet --histogram\n')


@unittest.skipUnless(platform.system() == 'Linux', 'main entry requires Linux pidfd')
class ToolQualificationCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = Path(os.environ.get('PERFKIT_COMPARE_TEST_SOURCE', str(ROOT)))
        for name in ('scripts', 'perfkit'):
            shutil.copytree(source / name, self.root / name)
        (self.root / 'tests').mkdir()
        for name in ('__init__.py', 'process_helpers.py', 'integration_resource_profiles.py'):
            shutil.copyfile(source / 'tests' / name, self.root / 'tests' / name)
        for name in ('CMakeLists.txt', 'Dockerfile'):
            shutil.copyfile(source / name, self.root / name)
        (self.root / 'build').mkdir()
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        self.executable(self.root / 'build/periodic_bench', """import pathlib,sys
a=sys.argv; n=int(a[a.index('--count')+1]); p=pathlib.Path(a[a.index('--output')+1])
(p.parent/'s01-started').write_text('started')
p.write_text('seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured\\n'+''.join('%d,%d,%d,%d,0,1\\n'%(i,i*1000000,i*1000000+10,i*1000000+10) for i in range(n)))
""")
        self.executable(self.tools / 'cyclictest', """import os,sys
case=os.environ['FIXTURE_TOOL_CASE']
help_text=%r
if '--help' in sys.argv:
 print(help_text.replace('--default-system','') if case=='missing-option' else help_text)
 sys.exit(1 if case=='help-exit1' else 0)
valid=%r
overflow=%r
texts={'help-only':'cyclictest: unrecognized option --default-system\\n'+help_text,
 'empty':'','zero':valid.replace('000000 000001','000000 000000').replace('000001 000002','000001 000000').replace('000002 000003','000002 000000').replace('# Total: 6','# Total: 0').replace('C: 6','C: 0'),
 'truncated':valid.split('# Total:')[0], 'unknown':'unknown histogram format',
 'mismatch':valid.replace('# Total: 6','# Total: 7'), 'overflow':overflow,
 'overflow-truncated':overflow.replace('# Thread 0: 0 1','# Thread 0:'),
 'overflow-mismatch':overflow.replace('# Thread 0: 0 1','# Thread 0: 0 # 99 others'),
 'quiet-histogram':valid.split('\\n',1)[1]}
print(texts.get(case,valid),end='')
""" % (HELP, histogram(), histogram(overflow=2)))
        self.executable(self.tools / 'pidstat', """import os,sys,time
case=os.environ['FIXTURE_TOOL_CASE']
if case=='timeout': time.sleep(30)
if case=='exit127': sys.exit(127)
print('' if case=='empty' else 'sysstat version 12.8.1')
""")
        self.executable(self.tools / 'babeltrace', """import os,sys
case=os.environ['FIXTURE_TOOL_CASE']
text=%r
if case=='babel-truncated': text=text.split('  FILE')[0]
if case=='babel-error': text+='Error parsing options.\\n'
print(text,end='')
sys.exit(1)
""" % BABELTRACE_HELP)
        self.executable(self.tools / 'lttng', """import sys
print('lttng (LTTng Trace Control) 2.13.4' if '--version' in sys.argv else '<sessions/>')
""")
        self.executable(self.tools / 'ros2', 'pass\n')
        self.executable(self.root / 'build/ros_bench', 'pass\n')

    def executable(self, path, body):
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)

    def run_cli(self, mode, case, preflight=False):
        output = self.root / ('output-' + mode + '-' + case)
        command = [sys.executable, str(self.root / 'scripts/compare_tools.py'), '--mode', mode,
                   '--seconds', '2', '--repetitions', '3', '--output', str(output)]
        if preflight:
            command.append('--preflight')
        result = subprocess.run(command, cwd='/tmp', env=dict(os.environ,
            PATH=str(self.tools) + os.pathsep + os.environ['PATH'], FIXTURE_TOOL_CASE=case),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, text=True)
        return result, output, json.loads((output / 'status.json').read_text())

    def test_exit_zero_without_sampling_fails_and_stops_repetitions(self):
        for case in ('help-only', 'empty', 'zero', 'truncated', 'unknown', 'mismatch',
                     'overflow-truncated', 'overflow-mismatch'):
            with self.subTest(case=case):
                result, output, status = self.run_cli('wakeup', case)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(status['status'], 'failed')
                self.assertTrue((output / 'wakeup-01-cyclictest/command.log').is_file())
                self.assertFalse((output / 'wakeup-01-cyclictest/comparison.json').exists())
                self.assertFalse((output / 'wakeup-02-cyclictest').exists())

    def test_valid_and_overflow_histograms_complete_with_null_quantiles(self):
        for case in ('valid', 'overflow', 'help-exit1', 'quiet-histogram'):
            with self.subTest(case=case):
                result, output, status = self.run_cli('wakeup', case)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(status['status'], 'execution_completed')
                rows = json.loads((output / 'runs.json').read_text())
                self.assertEqual(len(rows), 6)
                for row in rows:
                    if row['tool'] == 'cyclictest':
                        evidence = row['metrics']['sampling_evidence']
                        self.assertEqual(evidence['samples'], 8 if case == 'overflow' else 6)
                        self.assertIsNone(row['metrics']['start_deviation_ns'])

    def test_probe_exit127_timeout_and_empty_are_unavailable(self):
        for case in ('exit127', 'timeout', 'empty'):
            with self.subTest(case=case):
                result, output, status = self.run_cli('resource', case, preflight=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(status['status'], 'failed')
                preflight = json.loads((output / 'preflight.json').read_text())
                self.assertFalse(preflight['ready'])
                self.assertFalse(next(c for c in preflight['checks'] if c['tool']=='pidstat')['available'])

    def test_unsupported_power_option_prevents_benchmark_start(self):
        result, output, status = self.run_cli('wakeup', 'missing-option')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(status['status'], 'failed')
        self.assertFalse((output / 'wakeup-01-s01').exists())
        self.assertIn('--default-system', (output / 'preflight.json').read_text())

    def test_babeltrace_exit1_only_qualifies_complete_clean_help(self):
        for case in ('babel-help', 'babel-truncated', 'babel-error'):
            with self.subTest(case=case):
                _, output, status = self.run_cli('trace', case, preflight=True)
                check = next(c for c in json.loads((output / 'preflight.json').read_text())['checks']
                             if c['tool'] == 'babeltrace')
                self.assertEqual(check['available'], case == 'babel-help')
                self.assertEqual(status['status'], 'failed')  # Isolated checkout lacks ROS libraries.
                self.assertTrue(any(output.glob('version-*/command.log')))


if __name__ == '__main__':
    unittest.main()
