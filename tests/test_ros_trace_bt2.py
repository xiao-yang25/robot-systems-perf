"""Official bt2 writer/reader integration using generated synthetic CTF only."""
import json
import os
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

    def test_installed_babeltrace_158_help_probe(self):
        from argparse import Namespace
        from unittest.mock import patch
        import shutil
        installed=shutil.which('babeltrace')
        if not installed: self.skipTest('optional Babeltrace 1.x CLI unavailable')
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
            self.assertIn('1.5.8',row['value']['version_output'])
            self.assertEqual(json.loads((root/'probe-babeltrace/command.json').read_text()),[installed,'--help'])
            self.assertFalse((root/'probe-babeltrace2-version').exists())

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


if __name__=='__main__': unittest.main()
