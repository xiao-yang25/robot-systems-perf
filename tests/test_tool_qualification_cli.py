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
from tests.test_tool_wakeup import histogram, overflow_tail_histogram
from tests.test_tool_comparison import BABELTRACE_HELP, CYCLICTEST_HELP

HELP = CYCLICTEST_HELP


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
        self.executable(self.root / 'build/periodic_bench', """import pathlib,sys,time
a=sys.argv; n=int(a[a.index('--count')+1]); p=pathlib.Path(a[a.index('--output')+1])
(p.parent/'s01-started').write_text('started')
p.write_text('seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured\\n'+''.join('%d,%d,%d,%d,0,1\\n'%(i,i*1000000,i*1000000+10,i*1000000+10) for i in range(n)))
time.sleep(1.2)
""")
        self.executable(self.tools / 'cyclictest', """import os,sys,time,threading
case=os.environ['FIXTURE_TOOL_CASE']
help_text=%r
if '--help' in sys.argv:
 if case=='missing-option': help_text=help_text.replace('--default-system','')
 if case=='clock-prefix': help_text=help_text.replace('--clock','--clock-extra')
 print(help_text)
 sys.exit(1 if case=='help-exit1' else 0)
valid=%r
overflow=%r
tail_bad=%r
tail_good=%r
texts={'help-only':'cyclictest: unrecognized option --default-system\\n'+help_text,
 'empty':'','zero':valid.replace('000000 000001','000000 000000').replace('000001 000002','000001 000000').replace('000002 000003','000002 000000').replace('# Total: 6','# Total: 0').replace('C: 6','C: 0'),
 'truncated':valid.split('# Total:')[0], 'unknown':'unknown histogram format',
 'mismatch':valid.replace('# Total: 6','# Total: 7'), 'overflow':overflow,
 'overflow-truncated':overflow.replace('# Thread 0: 0 1','# Thread 0:'),
 'overflow-mismatch':overflow.replace('# Thread 0: 0 1','# Thread 0: 0 # 99 others'),
 'overflow-duplicate':overflow.replace('# Thread 0: 0 1','# Thread 0: 1 1'),
 'overflow-range':overflow.replace('# Thread 0: 0 1','# Thread 0: 999999 999999'),
 'overflow-order':overflow.replace('# Thread 0: 0 1','# Thread 0: 1 0'),
 'overflow-tail-invalid':tail_bad, 'overflow-tail-valid':tail_good,
 'quiet-histogram':valid.split('\\n',1)[1]}
print(texts.get(case,valid),end='')
if case not in ('help-only','empty','zero','truncated','unknown','missing-worker'):
 def worker():
  if case=='unbound-worker': os.sched_setaffinity(0,{int(c) for c in os.environ['FIXTURE_ALLOWED_CPUS'].split(',')})
  if case=='batch-worker': os.sched_setscheduler(0,os.SCHED_BATCH,os.sched_param(0))
  time.sleep(1.2)
 thread=threading.Thread(target=worker); thread.start(); thread.join()
""" % (HELP, histogram(), histogram(overflow=2), overflow_tail_histogram(start=2), overflow_tail_histogram()))
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
if case=='babel-prefix': text=text.replace('--fields','--fields-extra')
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

    def run_cli(self, mode, case, preflight=False, configured_cpu=True):
        output = self.root / ('output-' + mode + '-' + case)
        command = [sys.executable, str(self.root / 'scripts/compare_tools.py'), '--mode', mode,
                   '--seconds', '2', '--repetitions', '3', '--output', str(output)]
        if preflight:
            command.append('--preflight')
        if mode == 'wakeup' and configured_cpu:
            command += ['--cpu', str(min(os.sched_getaffinity(0)))]
        result = subprocess.run(command, cwd='/tmp', env=dict(os.environ,
            PATH=str(self.tools) + os.pathsep + os.environ['PATH'], FIXTURE_TOOL_CASE=case,
            FIXTURE_ALLOWED_CPUS=','.join(map(str, sorted(os.sched_getaffinity(0))))),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, text=True)
        return result, output, json.loads((output / 'status.json').read_text())

    def test_exit_zero_without_sampling_fails_and_stops_repetitions(self):
        for case in ('help-only', 'empty', 'zero', 'truncated', 'unknown', 'mismatch',
                     'overflow-truncated', 'overflow-mismatch', 'overflow-duplicate',
                     'overflow-range', 'overflow-order'):
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
                    evidence = row['thread_conditions']
                    self.assertTrue(evidence['validated'])
                    self.assertGreaterEqual(evidence['observations'], 2)
                    self.assertTrue(all(thread['affinity_cpu_list'] == str(min(os.sched_getaffinity(0)))
                                        for thread in evidence['last']['threads']))
                    if row['tool'] == 'cyclictest':
                        evidence = row['metrics']['sampling_evidence']
                        self.assertEqual(evidence['samples'], 8 if case == 'overflow' else 6)
                        self.assertIsNone(row['metrics']['start_deviation_ns'])

    def test_overflow_tail_failure_reaches_main_and_legal_edge_completes(self):
        result, output, status = self.run_cli('wakeup', 'overflow-tail-invalid')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(status['status'], 'failed')
        self.assertIn('overflow cycle indices', result.stdout)
        folder = output / 'wakeup-01-cyclictest'
        self.assertEqual((folder / 'command.log').read_text(), overflow_tail_histogram(start=2))
        execution = json.loads((folder / 'result.json').read_text())
        self.assertEqual(execution['returncode'], 0)
        self.assertFalse(Path('/proc', str(execution['pid'])).exists())
        self.assertFalse((folder / 'comparison.json').exists())
        self.assertFalse((folder / 'metrics.json').exists())
        self.assertFalse((output / 'runs.json').exists())
        self.assertFalse((output / 'wakeup-02-cyclictest').exists())
        self.assertFalse((output / 'wakeup-02-s01').exists())
        result, output, status = self.run_cli('wakeup', 'overflow-tail-valid')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(status['status'], 'execution_completed')
        rows = json.loads((output / 'runs.json').read_text())
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertTrue(row['thread_conditions']['validated'])
            if row['tool'] == 'cyclictest':
                self.assertEqual(Path(row['run_dir'], 'command.log').read_text(), overflow_tail_histogram())
                evidence = row['metrics']['sampling_evidence']
                self.assertEqual((evidence['samples'], evidence['overflow_samples']), (100002, 100001))
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
        for case in ('babel-help', 'babel-truncated', 'babel-error', 'babel-prefix'):
            with self.subTest(case=case):
                _, output, status = self.run_cli('trace', case, preflight=True)
                check = next(c for c in json.loads((output / 'preflight.json').read_text())['checks']
                             if c['tool'] == 'babeltrace')
                self.assertEqual(check['available'], case == 'babel-help')
                self.assertEqual(status['status'], 'failed')  # Isolated checkout lacks ROS libraries.
                self.assertTrue(any(output.glob('version-*/command.log')))

    def test_cpu_is_required_and_option_prefix_is_rejected_before_launch(self):
        for case, configured in [('no-cpu', False), ('clock-prefix', True)]:
            result, output, status = self.run_cli('wakeup', case, configured_cpu=configured)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(status['status'], 'failed')
            self.assertFalse((output / 'wakeup-01-s01').exists())

    def test_mismatched_or_missing_measurement_worker_stops_comparison(self):
        cases = ['batch-worker', 'missing-worker']
        if len(os.sched_getaffinity(0)) > 1:
            cases.append('unbound-worker')
        for case in cases:
            with self.subTest(case=case):
                result, output, status = self.run_cli('wakeup', case)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(status['status'], 'failed')
                folder = output / 'wakeup-01-cyclictest'
                self.assertTrue((folder / 'command.log').exists())
                conditions = json.loads((folder / 'thread_conditions.json').read_text())
                self.assertFalse(conditions['validated'])
                self.assertFalse((folder / 'comparison.json').exists())
                self.assertFalse((output / 'wakeup-02-cyclictest').exists())
                execution = json.loads((folder / 'result.json').read_text())
                self.assertFalse(Path('/proc', str(execution['pid'])).exists())


if __name__ == '__main__':
    unittest.main()
