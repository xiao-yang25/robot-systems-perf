import copy
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import integration_ros_event_chain as chain


def records():
    planned = ['epoch:0:'+str(i) for i in range(30)]
    identities = {role:{'identity':{'pid':pid,'starttime_ticks':50},'node':'/test/'+role}
                  for role,pid in (('source',123),('processor',124),('sink',125))}
    source, processor, sink = [], [], []
    for i,sid in enumerate(planned):
        source.append(dict(sample_id=sid, type='input', valid=True, monotonic_ns=100+i,
                           function_id='source', node='/test/source', **identities['source']['identity']))
        kind, valid = ('drop',True) if i % 6 == 1 else ('output',i % 6 != 2)
        processor.append(dict(sample_id=sid,type=kind,valid=valid,monotonic_ns=200+i,
                              function_id='processor',node='/test/processor',**identities['processor']['identity']))
        if kind == 'output':
            sink.append(dict(sample_id=sid,valid=valid,received_ns=300+i,**identities['sink']['identity']))
    return planned, source, processor, sink, identities


class RosEventChainTests(unittest.TestCase):
    def test_install_timeout_keeps_already_emitted_diagnostic_and_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); output=root/'output'; output.mkdir()
            wheel=root/'placeholder.whl'; wheel.write_bytes(b'placeholder')
            args=SimpleNamespace(output=output,wheel=wheel,domain_id=78,rmw='rmw_fastrtps_cpp',
                                 sdk_prefix=root,ros_python=sys.executable)
            children=[]; original_wait=subprocess.Popen.wait
            def launch(owner,command,**kwargs):
                child=subprocess.Popen([sys.executable,'-c',
                    "import time;print('prior-install-diagnostic',flush=True);time.sleep(.5)"],**kwargs)
                children.append(child)
                end=time.monotonic()+5
                while b'prior-install-diagnostic' not in (output/'install.log').read_bytes():
                    if time.monotonic()>=end: raise AssertionError('diagnostic did not arrive')
                    time.sleep(.001)
                return child
            def wait(process,timeout=None):
                return original_wait(process,timeout=.02 if timeout==30 else timeout)
            try:
                with patch.object(chain,'launch',launch),patch.object(subprocess.Popen,'wait',wait),self.assertRaises(subprocess.TimeoutExpired):
                    chain.verify(args,None)
            finally:
                # These short-lived direct children exit themselves; no PID signals/fallback.
                for child in children: original_wait(child,timeout=5)
            self.assertIn('prior-install-diagnostic',(output/'install.log').read_text())
            self.assertIn('pip',json.loads((output/'install-command.json').read_text()))

    def test_module_check_failure_keeps_raw_diagnostic_and_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); output=root/'output'; output.mkdir()
            wheel=root/'placeholder.whl'; wheel.write_bytes(b'placeholder')
            args=SimpleNamespace(output=output,wheel=wheel,domain_id=78,rmw='rmw_fastrtps_cpp',
                                 sdk_prefix=root,ros_python=sys.executable)
            children=[]
            def launch(owner,command,**kwargs):
                program="print('install-complete')" if '-m' in command else "import sys;print('module-check-diagnostic',flush=True);sys.exit(7)"
                child=subprocess.Popen([sys.executable,'-c',program],**kwargs);children.append(child);return child
            try:
                with patch.object(chain,'launch',launch),self.assertRaisesRegex(RuntimeError,'installed-modules exited 7'):
                    chain.verify(args,None)
            finally:
                for child in children: child.wait(timeout=5)
            self.assertIn('module-check-diagnostic',(output/'installed-modules.log').read_text())
            self.assertTrue((output/'installed-modules-command.json').exists())

    def test_known_complete_transport_and_delays(self):
        self.assertEqual(chain.verify_records(*records()),[100]*20)

    def test_inventory_missing_input_terminal_or_receipt_rejected(self):
        for index in (1,2,3):
            values=list(records()); values[index].pop()
            with self.subTest(index=index),self.assertRaises(ValueError): chain.verify_records(*values)

    def test_duplicate_each_stream_rejected(self):
        for index in (1,2,3):
            values=list(records()); values[index].append(copy.deepcopy(values[index][0]))
            with self.subTest(index=index),self.assertRaises(ValueError): chain.verify_records(*values)

    def test_replaced_or_coerced_identity_rejected_in_all_streams(self):
        for index in (1,2,3):
            for key in ('pid','starttime_ticks'):
                for transform in (lambda v:v+1, float, lambda v:True):
                    values=list(records()); values[index][0][key]=transform(values[index][0][key])
                    with self.subTest(index=index,key=key),self.assertRaises(ValueError): chain.verify_records(*values)

    def test_node_function_or_outcome_mismatch_rejected(self):
        for index,key,value in ((1,'node','/wrong'),(2,'function_id','wrong'),
                                (2,'valid',False),(2,'type','drop'),(3,'valid',False)):
            values=list(records()); values[index][0][key]=value
            with self.subTest(index=index,key=key),self.assertRaises(ValueError): chain.verify_records(*values)

    def test_reversed_terminal_or_receipt_clock_rejected(self):
        for index,key in ((2,'monotonic_ns'),(3,'received_ns')):
            values=list(records()); values[index][0][key]=99
            with self.subTest(index=index),self.assertRaises(ValueError): chain.verify_records(*values)

    def test_unknown_id_and_duplicate_planned_input_rejected(self):
        for index in (0,1,2,3):
            values=list(records())
            if index == 0: values[index][0]=values[index][1]
            else: values[index][0]['sample_id']='unexpected'
            with self.subTest(index=index),self.assertRaises(ValueError): chain.verify_records(*values)

    def test_mkdir_cancel_records_interrupted_without_launching_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); wheel=root/'existing.whl'; wheel.write_bytes(b'placeholder')
            output=root/'new'; mkdir=Path.mkdir
            def cancel(path,*args,**kwargs):
                result=mkdir(path,*args,**kwargs)
                if path==output: signal.raise_signal(signal.SIGTERM)
                return result
            argv=['test','--wheel',str(wheel),'--output',str(output),'--ros-python','/usr/bin/python3',
                  '--sdk-prefix',str(root),'--rmw','rmw_fastrtps_cpp','--domain-id','77']
            with patch('sys.argv',argv),patch.object(Path,'mkdir',cancel),patch.object(chain,'OwnedProcesses') as owner:
                self.assertEqual(chain.main(),130); owner.assert_not_called()
            self.assertEqual(json.loads((output/'test-status.json').read_text())['status'],'interrupted')

    def test_existing_result_is_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); wheel=root/'existing.whl'; wheel.write_bytes(b'placeholder')
            output=root/'existing'; output.mkdir(); (output/'test-status.json').write_bytes(b'original')
            argv=['test','--wheel',str(wheel),'--output',str(output),'--ros-python','/usr/bin/python3',
                  '--sdk-prefix',str(root),'--rmw','rmw_fastrtps_cpp','--domain-id','77']
            with patch('sys.argv',argv),patch.object(chain,'OwnedProcesses') as owner:
                self.assertEqual(chain.main(),1); owner.assert_not_called()
            self.assertEqual((output/'test-status.json').read_bytes(),b'original')
