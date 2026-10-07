"""Tracing evidence contracts; fake transport tests do not prove real CTF compatibility."""
from argparse import Namespace
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import collect_ros_trace as trace


def request(root):
    return Namespace(output=root/'run', ros_python=sys.executable, rmw='rmw_fastrtps_cpp',
                     sdk_prefix=None, domain_id=31, ros_bench=root/'ros_bench', seconds=2,
                     view='container', preflight=False)


def event_text():
    return ('[1] ros2:rcl_node_init: { vpid = 10, pid_ns = 101, procname = "rsp-test" }, {}\n'
            '[2] ros2:rclcpp_publish: { vpid = 10, pid_ns = 101, procname = "rsp-test" }, {}\n'
            '[3] ros2:rcl_node_init: { vpid = 20, pid_ns = 101, procname = "rsp-test" }, {}\n'
            '[4] ros2:callback_start: { vpid = 20, pid_ns = 101, procname = "rsp-test" }, {}\n'
            '[5] ros2:callback_end: { vpid = 20, pid_ns = 101, procname = "rsp-test" }, {}\n')


class EvidenceTests(unittest.TestCase):
    def test_requires_initialization_publish_and_callback_events(self):
        ids = {'publisher': {'pid': 10, 'pid_namespace_inode': 101}, 'subscriber': {'pid': 20, 'pid_namespace_inode': 101}}
        self.assertEqual(trace.validate_events(event_text(), ids, "rsp-test")['20']['ros2:callback_start'], 1)
        for missing in ('rcl_node_init', 'rclcpp_publish', 'callback_start', 'callback_end'):
            text = '\n'.join(line for line in event_text().splitlines() if missing not in line)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                trace.validate_events(text, ids, "rsp-test")

    def test_foreign_and_missing_pid_rejected(self):
        ids = {'publisher': {'pid': 10, 'pid_namespace_inode': 101}, 'subscriber': {'pid': 20, 'pid_namespace_inode': 101}}
        for line in ('[6] ros2:callback_start: { vpid = 120 }, {}', '[6] ros2:callback_start: {}'):
            with self.assertRaises(ValueError):
                trace.validate_events(event_text()+line, ids, "rsp-test")

    def test_pid_collision_in_other_namespace_or_name_rejected(self):
        ids = {'publisher': {'pid': 10, 'pid_namespace_inode': 101}, 'subscriber': {'pid': 20, 'pid_namespace_inode': 101}}
        for replacement in ('pid_ns = 202', 'pid_ns = 0'):
            with self.assertRaises(ValueError):
                trace.validate_events(event_text().replace('pid_ns = 101', replacement), ids, 'rsp-test')
        with self.assertRaises(ValueError):
            trace.validate_events(event_text().replace('rsp-test', 'business'), ids, 'rsp-test')

    def test_manifest_preserves_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'metadata').write_bytes(b'CTF\x00')
            (root/'stream').write_bytes(b'\x00\xffevent')
            rows = trace.inventory(root)
            self.assertEqual(sum(row['bytes'] for row in rows), 11)
            self.assertEqual(next(row['sha256'] for row in rows if row['path']=='stream'), trace.digest(root/'stream'))

    def test_manifest_rejects_symlink_and_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'stream').write_bytes(b'1234')
            with patch.object(trace, 'MAX_CTF_BYTES', 3), self.assertRaises(ValueError):
                trace.inventory(root)
            (root/'linked').symlink_to(root/'stream')
            with self.assertRaises(ValueError): trace.inventory(root)

    def test_invalid_requests_create_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            args = request(Path(directory))
            for key, value in (('seconds', 11), ('seconds', True), ('domain_id', 233), ('domain_id', True), ('rmw', 'x;rm')):
                test = Namespace(**vars(args)); setattr(test, key, value)
                with self.subTest(key=key, value=value), self.assertRaises(ValueError): trace.run(test)
                self.assertFalse(args.output.exists())

    def test_existing_output_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            args = request(Path(directory)); args.output.mkdir(); (args.output/'keep').write_text('old')
            self.assertEqual(trace.run(args), 1)
            self.assertEqual(list(args.output.iterdir()), [args.output/'keep'])

    def test_early_cancel_preserves_status(self):
        with tempfile.TemporaryDirectory() as directory:
            args = request(Path(directory)); original = trace.save
            def cancel(path, value):
                original(path, value)
                if value.get('status') == 'running': raise KeyboardInterrupt('test initial cancellation')
            with patch.object(trace, 'save', side_effect=cancel): self.assertEqual(trace.run(args), 130)
            state = json.loads((args.output/'trace-status.json').read_text())
            self.assertEqual(state['status'], 'interrupted'); self.assertIsNotNone(state['ended_ns'])

    def test_preflight_missing_tools_keeps_first_sdk_error(self):
        with tempfile.TemporaryDirectory() as directory:
            args = request(Path(directory)); args.output.mkdir()
            with patch.object(trace.shutil, 'which', return_value=None), patch.object(trace, 'command', side_effect=RuntimeError('rcl library missing')):
                result = trace.preflight(args, args.output, {})
            self.assertFalse(result['ready'])
            self.assertIn('rcl library missing', next(r['reason'] for r in result['checks'] if r['name']=='sdk-runtime'))
            self.assertFalse(result['events_observed'])


# Controlled stand-ins exercise the real CLI, Linux pidfds, signals and disk evidence.
# Their streams are textual synthetic data, NOT real LTTng/CTF samples.
FAKE_LTTNG = '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if '--version' in args:
 print('lttng (LTTng Trace Control) 2.13.9'); sys.exit(0)
root=Path(os.environ['RSP_TEST_ROOT'])
with (root/'actions.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
a=args[1]
if a=='create':
 out=Path(args[args.index('--output')+1]); out.mkdir(); (out/'metadata').write_text('SYNTHETIC metadata')
 (root/'ctf-path').write_text(str(out))
if a=='start':
 out=Path((root/'ctf-path').read_text())
 assert not list(out.parent.glob('*.release')), 'ROS initialization before trace start'
if a=='stop' and os.getenv('RSP_TEST_FAIL_STOP'): sys.exit(4)
if a=='destroy':
 if os.getenv('RSP_TEST_FAIL_DESTROY'): sys.exit(5)
 (root/'destroyed').write_text('yes')
print('ok')
'''
FAKE_BABELTRACE = '''import sys
from pathlib import Path
if '--version' in sys.argv: print('BabelTrace Trace Viewer and Converter 1.5.8')
else: print((Path(sys.argv[1])/'stream').read_text())
'''
FAKE_PYTHON = '''import json,os,sys
from pathlib import Path
print(json.dumps({'python':sys.executable,'ros_distro':'TEST'}))
print(json.dumps({'tracing_compiled':True,'rmw':sys.argv[-1],'tracetools_paths':[str(Path(os.environ['RSP_TEST_ROOT'])/'libtracetools.so')]}))
'''
FAKE_BENCH = '''import json,os,signal,sys,time
from pathlib import Path
a=sys.argv; role=a[a.index('--role')+1]; out=Path(a[a.index('--output')+1]); prefix=a[a.index('--runtime-evidence')+1]
root=Path(os.environ['RSP_TEST_ROOT']); ctf=Path((root/'ctf-path').read_text()); pid=os.getpid()
Path(prefix+'-start.maps').write_text(str(root/'libtracetools.so')+'\\n')
Path(prefix+'-start.json').write_text(json.dumps({'pid':pid,'rmw_identifier':{'value':os.environ['RMW_IMPLEMENTATION']}}))
with (ctf/'stream').open('a') as f:
 names=['rcl_node_init','rclcpp_publish'] if role=='publisher' else ['rcl_node_init','callback_start','callback_end']
 for name in names: f.write('[1] ros2:'+name+': { vpid = '+str(pid)+', pid_ns = '+str(os.stat('/proc/self/ns/pid').st_ino)+', procname = \"'+Path('/proc/self/comm').read_text().strip()+'\" }, {}\\n')
if role=='subscriber':
 signal.signal(signal.SIGINT,lambda *args: sys.exit(0))
 while True: time.sleep(.02)
time.sleep(float(os.getenv('RSP_TEST_BENCH_SECONDS','.15')))
out.write_text('synthetic fixture csv')
'''


@unittest.skipUnless(platform.system() == 'Linux', 'real pidfd/CLI lifecycle requires Linux')
class LinuxCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.root = Path(self.temporary.name)
        self.args = request(self.root)
        (self.root/'libtracetools.so').write_bytes(b'synthetic library fingerprint')
        for name, source in [('lttng', FAKE_LTTNG), ('babeltrace', FAKE_BABELTRACE), ('sdk-python', FAKE_PYTHON), ('ros_bench', FAKE_BENCH)]:
            path=self.root/name; path.write_text('#!'+sys.executable+'\n'+source); path.chmod(0o755)
        self.args.ros_python=str(self.root/'sdk-python')
        self.env=dict(os.environ, PATH=str(self.root)+':'+os.environ['PATH'], RSP_TEST_ROOT=str(self.root))
        self.command=[sys.executable,str(Path(trace.__file__)), '--output',str(self.args.output),
            '--ros-python',self.args.ros_python,'--rmw',self.args.rmw,'--domain-id','31','--seconds','2',
            '--ros-bench',str(self.args.ros_bench),'--view','container']
    def tearDown(self): self.temporary.cleanup()
    def status(self): return json.loads((self.args.output/'trace-status.json').read_text())
    def assert_reaped(self):
        ids=json.loads((self.args.output/'identities.json').read_text())
        for row in ids.values(): self.assertFalse(Path('/proc',str(row['pid'])).exists())
    def test_positive_transport_cleanup_and_scope(self):
        result=subprocess.run(self.command,env=self.env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        state=self.status(); self.assertEqual(state['status'],'events_observed')
        self.assertTrue(state['events_observed']); self.assertFalse(state['session_active'])
        self.assertEqual(state['cleanup_errors'],[]); self.assertIsNone(state['trace_lost_events'])
        actions=[json.loads(line) for line in (self.root/'actions.jsonl').read_text().splitlines()]
        self.assertEqual([row[1] for row in actions], ['list','create','enable-channel','add-context','enable-event','start','stop','list','destroy'])
        selected=next(row for row in actions if row[1]=='enable-event')
        self.assertEqual(selected[-1],state['event_filter']); self.assertNotIn('--all', selected)
        self.assert_reaped()
    def test_real_sigterm_cleans_only_own_objects(self):
        self.env['RSP_TEST_BENCH_SECONDS']='10'
        external=subprocess.Popen([sys.executable,'-c','import time; time.sleep(25)'])
        process=subprocess.Popen(self.command,env=self.env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            end=time.monotonic()+10
            while not (self.args.output/'publisher.release').exists():
                if process.poll() is not None: self.fail(process.stderr.read().decode())
                if time.monotonic()>end: self.fail('capture not started')
                time.sleep(.02)
            process.send_signal(signal.SIGTERM); process.wait(timeout=15)
            self.assertEqual(process.returncode,130)
            self.assertEqual(self.status()['status'],'interrupted')
            self.assertTrue((self.root/'destroyed').exists()); self.assert_reaped()
            self.assertIsNone(external.poll())
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stderr.close(); external.terminate(); external.wait()
    def test_stop_failure_retained_destroy_still_attempted(self):
        self.env['RSP_TEST_FAIL_STOP']='1'
        result=subprocess.run(self.command,env=self.env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,1)
        self.assertEqual(self.status()['status'],'failed')
        self.assertIn('stop:',self.status()['cleanup_errors'][0]); self.assertTrue((self.root/'destroyed').exists())
        self.assert_reaped()
    def test_unconfirmed_session_retained_with_namespace_and_name_guard(self):
        self.env.update(RSP_TEST_FAIL_STOP='1', RSP_TEST_FAIL_DESTROY='1')
        result=subprocess.run(self.command,env=self.env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,1)
        state=self.status(); self.assertIsNone(state['session_active'])
        self.assertTrue(state['session_may_remain']); self.assertIn('$ctx.pid_ns ==',state['event_filter'])
        self.assertIn('$ctx.procname ==',state['event_filter']); self.assert_reaped()

    def test_final_status_signal_and_secondary_save_error(self):
        original_save=trace.save; previous=signal.getsignal(signal.SIGTERM)
        def failing(path,value):
            if isinstance(value,dict) and value.get('status')=='events_observed':
                original_save(path,value)
                os.kill(os.getpid(),signal.SIGTERM)
                raise OSError('injected final-save fault')
            original_save(path,value)
        trace.install_signal_handler()
        try:
            with patch.dict(os.environ,self.env), patch.object(trace,'save',side_effect=failing):
                self.assertEqual(trace.run(self.args),130)
        finally: signal.signal(signal.SIGTERM,previous)
        state=self.status(); self.assertEqual(state['status'],'interrupted')
        self.assertIn('injected final-save fault',state['cleanup_errors'][-1])
        self.assert_reaped()

    def test_manifest_late_cancel_preserves_primary_error(self):
        previous=signal.getsignal(signal.SIGTERM)
        def fail_capture(args,output,env,state):
            (output/'ctf').mkdir()
            raise RuntimeError('original fixture failure marker')
        def cancel_manifest(root):
            os.kill(os.getpid(),signal.SIGTERM)
            return []
        trace.install_signal_handler()
        try:
            with patch.dict(os.environ,self.env), patch.object(trace,'capture',side_effect=fail_capture), patch.object(trace,'inventory',side_effect=cancel_manifest):
                self.assertEqual(trace.run(self.args),130)
        finally: signal.signal(signal.SIGTERM,previous)
        state=self.status(); self.assertEqual(state['status'],'interrupted')
        self.assertIn('original fixture failure marker',state['primary_error'])
        self.assertIsNotNone(state['interruption_error'])

    def test_command_timeout_keeps_evidence_and_reaps_child(self):
        folder=self.root/'timeout'
        with self.assertRaises(RuntimeError):
            trace.command([sys.executable,'-c','import time; print("raw-before-timeout",flush=True); time.sleep(30)'],folder,self.env,timeout=.2)
        row=json.loads((folder/'result.json').read_text())
        self.assertIn('timeout',row['error']); self.assertIsNotNone(row['returncode'])
        self.assertFalse(Path('/proc',str(row['identity']['pid'])).exists())
        self.assertIn(b'raw-before-timeout',(folder/'stdout.bin').read_bytes())

    def test_randomized_elf_comm_guard(self):
        # Same Linux exec semantics as C01, independent of a tracing SDK.
        from scripts.comparison_common import launch
        from tests.process_helpers import OwnedProcesses
        import shutil
        binary=self.root/'rsp-01234567890'; shutil.copyfile('/bin/sleep',binary); binary.chmod(0o500)
        owner=OwnedProcesses()
        try:
            process=launch(owner,[str(binary),'2'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            end=time.monotonic()+1
            while Path('/proc',str(process.pid),'comm').read_text().strip()!=binary.name:
                if time.monotonic()>end: self.fail('ELF comm does not match guard')
                time.sleep(.01)
            self.assertEqual(len(binary.name),15)
        finally: owner.cleanup(timeout=1)

    def test_preflight_does_not_create_session_or_fixture(self):
        result=subprocess.run(self.command+['--preflight'],env=self.env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.status()['status'],'preflight_ready')
        self.assertFalse((self.root/'ctf-path').exists()); self.assertFalse((self.args.output/'identities.json').exists())


if __name__=='__main__': unittest.main()
