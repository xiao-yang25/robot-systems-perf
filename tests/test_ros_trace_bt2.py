"""Official bt2 writer/reader integration using generated synthetic CTF only."""
import json
import os
import platform
import re
import shutil
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from scripts import collect_ros_trace as collect
from scripts.collect_ros_trace import inventory
from scripts.ros_trace_bt2 import decode
from scripts.ros_trace_analysis import analyze
from tests.test_ros_trace_analysis import fixture, pair, BOOT, LIST

try:
    import bt2
except ImportError:
    bt2 = None


def select_babeltrace(version, env=None):
    """Select an actual version, never infer it from the executable's name."""
    env=dict(os.environ if env is None else env)
    variable={'1.5.8':'RSP_TEST_BABELTRACE_158','2.0.4':'RSP_TEST_BABELTRACE_204'}[version]
    configured=env.get(variable)
    names=('babeltrace',) if version=='1.5.8' else ('babeltrace2','babeltrace')
    candidates=[configured] if configured else [shutil.which(name,path=env.get('PATH')) for name in names]
    observations=[]
    for executable in dict.fromkeys(path for path in candidates if path):
        try:
            help_result=subprocess.run([executable,'--help'],env=env,stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=5)
            if help_result.returncode not in (0,1):
                observations.append(executable+': help failed'); continue
            text=help_result.stdout+'\n'+help_result.stderr
            legacy=re.search(r'^BabelTrace Trace Viewer and Converter (1\.\d+\.\d+)(?:\s|$)',text,re.M)
            if legacy:
                actual=legacy[1]  # This family does not support --version.
            else:
                result=subprocess.run([executable,'--version'],env=env,stdin=subprocess.DEVNULL,
                    capture_output=True,text=True,timeout=5)
                banner=re.search(r'^babeltrace(?:2)?\s+(2\.\d+\.\d+)(?:\s|$)',result.stdout+'\n'+result.stderr,re.I|re.M)
                actual=banner[1] if result.returncode==0 and banner else None
            if actual==version: return executable
            observations.append(executable+': actual '+str(actual))
        except (OSError,subprocess.TimeoutExpired) as error:
            observations.append(executable+': '+str(error))
    raise unittest.SkipTest('Babeltrace '+version+' unavailable; set '+variable+' to its explicit executable; '+('; '.join(observations) or 'no candidate on PATH'))


def write_ctf(root, rows, discarded=None):
    """Use the official sink.ctf.fs writer; never hand-encode trace bytes."""
    class Messages(bt2._UserMessageIterator):
        def __init__(self, config, port):
            component = self._component
            tc = component._create_trace_class()
            clock = component._create_clock_class(name='monotonic', frequency=10**9,
                offset=bt2.ClockClassOffset(1700000000,100), origin_is_unix_epoch=True, uuid=uuid.UUID(BOOT))
            context = tc.create_structure_field_class()
            for name in ('vpid','vtid','pid_ns'):
                context.append_member(name,tc.create_unsigned_integer_field_class())
            context.append_member('procname',tc.create_string_field_class())
            sc = tc.create_stream_class(default_clock_class=clock, event_common_context_field_class=context,
                supports_packets=True, packets_have_beginning_default_clock_snapshot=True,
                packets_have_end_default_clock_snapshot=True, supports_discarded_events=True)
            trace = tc(name='synthetic-unit-trace', environment={'synthetic':1,'generator':'official-bt2-sink'})
            stream = trace.create_stream(sc)
            classes = {}
            for row in rows:
                if row['name'] in classes: continue
                fields = tc.create_structure_field_class()
                for name,value in row['payload'].items():
                    fields.append_member(name,tc.create_string_field_class() if isinstance(value,str) else tc.create_unsigned_integer_field_class())
                classes[row['name']] = sc.create_event_class(name=row['name'],payload_field_class=fields)
            packet = stream.create_packet()
            messages = [self._create_stream_beginning_message(stream),self._create_packet_beginning_message(packet,0)]
            for row in rows:
                message = self._create_event_message(classes[row['name']],packet,row['cycles'])
                for key,value in row['context'].items(): message.event.common_context_field[key]=value
                for key,value in row['payload'].items(): message.event.payload_field[key]=value
                messages.append(message)
            messages.append(self._create_packet_end_message(packet,max(r['cycles'] for r in rows)+1))
            if discarded is not None:
                messages.append(self._create_discarded_events_message(stream,count=discarded))
                # CTF stores the cumulative counter in the following packet;
                # a trailing message alone has no packet to serialize it into.
                later=stream.create_packet(); cycle=max(r['cycles'] for r in rows)+2
                messages.append(self._create_packet_beginning_message(later,cycle))
                message=self._create_event_message(classes[rows[0]['name']],later,cycle+1)
                for key,value in rows[0]['context'].items(): message.event.common_context_field[key]=value
                for key,value in rows[0]['payload'].items(): message.event.payload_field[key]=value
                messages.extend([message,self._create_packet_end_message(later,cycle+2)])
            messages.append(self._create_stream_end_message(stream))
            self.messages = iter(messages)
        def __next__(self): return next(self.messages)

    class Source(bt2._UserSourceComponent, message_iterator_class=Messages):
        def __init__(self, config, params, obj): self._add_output_port('out')

    graph = bt2.Graph()
    source = graph.add_component(Source,'generated-test-events')
    sink = graph.add_component(bt2.find_plugin('ctf').sink_component_classes['fs'],'official-ctf-writer',
        params={'path':str(root),'assume-single-trace':True})
    graph.connect_ports(source.output_ports['out'],sink.input_ports['in'])
    graph.run()


@unittest.skipIf(bt2 is None,'optional official bt2 dependency not installed')
class BT2IntegrationTests(unittest.TestCase):
    def test_official_writer_decoder_and_cli(self):
        data,history,emit=fixture()
        emit('rcl_publish',6,{'publisher_handle':2,'message':99})
        emit('rcl_publish',7,{'publisher_handle':2,'message':99})
        pair(emit,10,9418); pair(emit,10000,23664); pair(emit,24000,147808)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); ctf=root/'ctf'; write_ctf(ctf,data['events'])
            decoded=decode(ctf,bt2,True)
            self.assertEqual(decoded['source']['adapter'],'bt2')
            self.assertEqual(len(decoded['events']),13)
            self.assertEqual(decoded['events'][0]['context'],data['events'][0]['context'])
            self.assertEqual(decoded['events'][-1]['cycles'],147808)
            clock=next(iter(decoded['clocks'].values()))
            self.assertEqual(clock['offset_seconds'],1700000000)
            self.assertEqual(clock['offset_cycles'],100)
            self.assertEqual(len(decoded['streams']),1); self.assertEqual(len(decoded['packets']),1)
            result=analyze(decoded,history)
            self.assertEqual(result['callback_interval']['samples'],3)
            self.assertEqual(result['callback_interval']['p50_ns'],13664)
            self.assertEqual(result['callback_interval']['max_ns'],123808)
            self.assertEqual(result['publications'][0]['distinct_message_addresses'],1)
            exported=root/'decoded.json'
            command=[sys.executable,'scripts/ros_trace_bt2.py','--ctf',str(ctf),'--synthetic','true','--output',str(exported)]
            run=subprocess.run(command,capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads(exported.read_text())['events'],decoded['events'])
            prior=exported.read_bytes()
            self.assertNotEqual(subprocess.run(command,capture_output=True,timeout=15).returncode,0)
            self.assertEqual(exported.read_bytes(),prior)
            history_path=root/'history.json'; history_path.write_text(json.dumps(history))
            run=subprocess.run([sys.executable,'scripts/analyze_ros_trace.py','--events',str(exported),'--history',str(history_path),
                '--output',str(root/'analysis')],capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads((root/'analysis/callback-analysis.json').read_text())['callback_interval']['samples'],3)

    def test_official_discard_message_preserves_count_scope(self):
        data,history,emit=fixture(); pair(emit)
        with tempfile.TemporaryDirectory() as directory:
            ctf=Path(directory)/'ctf'; write_ctf(ctf,data['events'],discarded=4)
            result=decode(ctf,bt2,True)
            self.assertEqual([row['count'] for row in result['decoder_loss_messages'] if row['type']=='discarded_events'],[4])
            self.assertTrue(result['decoder_loss_messages'][0]['stream_id'])

    def test_capture_cli_manifest_scoped_loss_and_real_sigterm(self):
        data,history,emit=fixture(); pair(emit)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); capture=root/'capture'; capture.mkdir()
            write_ctf(capture/'ctf',data['events'])
            def save(path,value): path.write_text(json.dumps(value))
            save(capture/'ctf-manifest.json',{'files':inventory(capture/'ctf')})
            save(capture/'trace-status.json',{'synthetic':True,'session_name':'own'})
            for index,operation in enumerate(('stop','list')):
                folder=capture/('control-%02d'%index); folder.mkdir()
                save(folder/'command.json',['lttng','--no-sessiond',operation,'own'])
                save(folder/'result.json',{'returncode':0,'error':None,'start_ns':index+1,'end_ns':index+2})
                (folder/'stdout.bin').write_text(LIST if operation=='list' else '')
            history_path=root/'history.json'; save(history_path,history)
            before=inventory(capture/'ctf')
            command=[sys.executable,'scripts/analyze_ros_trace.py','--capture',str(capture),
                '--history',str(history_path),'--bt2-python',sys.executable,'--output',str(root/'analysis')]
            run=subprocess.run(command,capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            result=json.loads((root/'analysis/callback-analysis.json').read_text())
            self.assertEqual(result['callback_interval']['samples'],1)
            self.assertEqual(result['loss']['channel_discarded_events'][0]['count'],0)
            self.assertIsNone(result['loss']['decoder_discarded_events']['count'])
            self.assertEqual(inventory(capture/'ctf'),before)
            # Explicit synthetic slow interpreter: no tracing or external nodes.
            interpreter=root/'slow-decoder'
            ready=root/'decoder-ready'
            interpreter.write_text('#!'+sys.executable+'\nimport os,time\nopen('+repr(str(ready))+',"w").write(str(os.getpid()))\ntime.sleep(60)\n')
            interpreter.chmod(0o700)
            command[command.index('--bt2-python')+1]=str(interpreter)
            command[-1]=str(root/'cancelled')
            external=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and process.poll() is None and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(ready.exists(),'decoder failed to start')
                child_pid=int(ready.read_text())
                process.send_signal(signal.SIGTERM)
                stdout,stderr=process.communicate(timeout=5)
                self.assertEqual(process.returncode,130,stderr)
                state=json.loads((root/'cancelled/analysis-status.json').read_text())
                self.assertEqual(state['status'],'interrupted'); self.assertIsNotNone(state['ended_ns'])
                self.assertFalse(Path('/proc/'+str(child_pid)).exists(),'owned decoder still alive')
                self.assertIsNone(external.poll(),'external test object was signalled')
                self.assertEqual(inventory(capture/'ctf'),before)
            finally:
                if process.poll() is None: process.kill(); process.wait()
                external.terminate(); external.wait(timeout=5)


class BabeltraceSelectionTests(unittest.TestCase):
    """Synthetic probe text exercises selection policy, not installed versions."""
    def probe(self, argv, **kwargs):
        from tests.test_ros_trace_capture import BABELTRACE_HELP
        if argv[0]=='/test/legacy':
            self.assertEqual(argv[-1],'--help')
            return subprocess.CompletedProcess(argv,1,BABELTRACE_HELP,'')
        text='Usage: babeltrace2 [COMMAND]' if argv[-1]=='--help' else 'Babeltrace 2.0.4'
        return subprocess.CompletedProcess(argv,0,text,'')

    def test_explicit_legacy_path_does_not_assume_path_version(self):
        from unittest.mock import patch
        with patch.object(subprocess,'run',side_effect=self.probe), patch.object(shutil,'which') as lookup:
            self.assertEqual(select_babeltrace('1.5.8',{'RSP_TEST_BABELTRACE_158':'/test/legacy'}),'/test/legacy')
            lookup.assert_not_called()

    def test_path_alias_pointing_to_two_skips_legacy_and_checks_two(self):
        from unittest.mock import patch
        with patch.object(subprocess,'run',side_effect=self.probe), patch.object(shutil,'which',side_effect=lambda name,**kw: '/test/two' if name=='babeltrace' else None):
            with self.assertRaisesRegex(unittest.SkipTest,'actual 2.0.4'): select_babeltrace('1.5.8',{})
            self.assertEqual(select_babeltrace('2.0.4',{}),'/test/two')

    def test_explicit_wrong_version_skips_without_hidden_fallback(self):
        from unittest.mock import patch
        with patch.object(subprocess,'run',side_effect=self.probe), patch.object(shutil,'which') as lookup:
            with self.assertRaisesRegex(unittest.SkipTest,'actual 2.0.4'):
                select_babeltrace('1.5.8',{'RSP_TEST_BABELTRACE_158':'/test/two'})
            lookup.assert_not_called()

    def test_absent_version_and_version_prefix_do_not_pass(self):
        from unittest.mock import patch
        with patch.object(shutil,'which',return_value=None):
            with self.assertRaisesRegex(unittest.SkipTest,'no candidate'): select_babeltrace('1.5.8',{})
        with patch.object(shutil,'which',return_value='/test/two'), patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0,'Babeltrace 2.0.40','')):
            with self.assertRaisesRegex(unittest.SkipTest,'actual 2.0.40'): select_babeltrace('2.0.4',{})


@unittest.skipUnless(platform.system()=='Linux','installed CLI pidfd probe requires Linux')
class BabeltraceProbeTests(unittest.TestCase):
    def assert_installed_probe(self, version):
        from argparse import Namespace
        from unittest.mock import patch
        installed=select_babeltrace(version)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=Namespace(ros_bench=Path(sys.executable),ros_python=sys.executable,rmw='rmw_fastrtps_cpp',sdk_prefix=None)
            original=collect.command
            def only_babeltrace(argv,folder,env,**kw):
                if argv[0]==installed: return original(argv,folder,env,**kw)
                raise RuntimeError('unrelated runtime probe excluded from offline compatibility test')
            with patch.object(collect.shutil,'which',side_effect=lambda name,**kw: installed if name=='babeltrace' else None), patch.object(collect,'command',side_effect=only_babeltrace):
                result=collect.preflight(args,root,dict(os.environ))
            row=next(row for row in result['checks'] if row['name']=='babeltrace')
            self.assertTrue(row['available'],row)
            self.assertRegex(row['value']['version_output'],re.escape(version)+r'(?:\s|$)')
            self.assertEqual(json.loads((root/'probe-babeltrace/command.json').read_text()),[installed,'--help'])
            if version=='1.5.8':
                self.assertFalse((root/'probe-babeltrace2-version').exists())
            else:
                self.assertEqual(json.loads((root/'probe-babeltrace2-version/command.json').read_text()),[installed,'--version'])

    def test_installed_babeltrace_158_help_probe(self): self.assert_installed_probe('1.5.8')
    def test_installed_babeltrace_204_version_probe(self): self.assert_installed_probe('2.0.4')


if __name__=='__main__': unittest.main()
