"""Installed M3a with actual fixture timestamps, offline import and lifecycle checks."""
import argparse
from email.parser import Parser
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import venv
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.comparison_common import launch
from tests.process_helpers import OwnedProcesses
from tests.temperature_guard import guarded_command, assert_temperature_guard


PROGRAM = '''import json,os,sys,time
from pathlib import Path
raw,go,done=map(Path,sys.argv[1:])
pid=os.getpid()
start=int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
while not go.exists(): time.sleep(.01)
with raw.open('x') as stream:
 for i in range(60):
  def emit(kind,valid=True,reason=None):
   role='a' if kind=='input' else 'b'
   stream.write(json.dumps(dict(sample_id=str(i),type=kind,monotonic_ns=time.monotonic_ns(),
    pid=pid,starttime_ticks=start,function_id=role,node='/fixture/'+role,valid=valid,reason=reason))+'\\n')
   stream.flush()
  emit('input'); time.sleep(.002)
  if i%6==1: emit('drop',reason='fixture explicit rejection')
  elif i%6==2: emit('output',False,'fixture invalid payload')
  elif i%6!=0: emit('output')
  time.sleep(.03)
done.write_text('done')
time.sleep(60)
'''


def wait_for(check, seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if check(): return
        time.sleep(.03)
    raise AssertionError('bounded fixture wait expired')


def verify(wheel, output):
    owner=OwnedProcesses()
    try:
        with tempfile.TemporaryDirectory(prefix='m3a-install-') as temporary:
            root=Path(temporary); prefix=root/'venv'; cwd=root/'unrelated'; cwd.mkdir()
            venv.EnvBuilder(with_pip=False).create(prefix)
            pip=importlib.util.find_spec('pip')
            if pip is None: raise RuntimeError('prepared offline pip bootstrap required')
            env=dict(os.environ); env['PYTHONPATH']=str(Path(pip.origin).parent.parent)
            install=subprocess.run([str(prefix/'bin/python'),'-m','pip','--isolated','install','--no-index',
                '--disable-pip-version-check',str(wheel.resolve())],env=env,capture_output=True,text=True,timeout=30)
            (output/'install.log').write_text(install.stdout+install.stderr); assert install.returncode==0
            env.pop('PYTHONPATH',None); env.pop('EP_SOURCE_REVISION',None)
            python=str(prefix/'bin/python'); business=str(prefix/'bin/robot-perf-business')
            external=launch(owner,[sys.executable,'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            raw,go,done=output/'fixture-raw.jsonl',root/'go',root/'done'
            worker=launch(owner,[sys.executable,'-c',PROGRAM,str(raw),str(go),str(done)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            workload={'format_version':1,'workload_id':'event-fixture','workload_version':'fixture-v1','ros_domain_id':37,
                'functions':[{'id':name,'process_selector':{'pids':[worker.pid]},'ros_nodes':['/fixture/'+name]} for name in ('a','b')]}
            workload_file=root/'workload.json'; workload_file.write_text(json.dumps(workload))
            capture=output/'monitor'; guard=output/'temperature-guard.json'
            command=guarded_command([str(prefix/'bin/robot-perf-monitor'),'--workload',str(workload_file),
                '--profile','light','--seconds','5','--interval','.1','--discovery-interval','.2',
                '--skip-temperatures','--output',str(capture)],Path(python),guard)
            with (output/'monitor.log').open('x') as log:
                monitor=launch(owner,command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT)
                def first_observation():
                    p=capture/'business-relations.jsonl'
                    if not p.exists(): return False
                    rows=[json.loads(line) for line in p.read_text().splitlines(keepends=True) if line.endswith('\n')]
                    return bool(rows and all(any(c['resource_ref'] for c in r['candidates']) for r in rows[-1]['functions']))
                wait_for(first_observation); time.sleep(.3); go.write_text('go')
                wait_for(done.exists); assert monitor.wait(timeout=10)==0
            assert_temperature_guard(guard)
            files={p.name:p.read_bytes() for p in capture.iterdir() if p.is_file()}
            status=json.loads(files['monitor-status.json']); summary=json.loads(files['monitor-summary.json'])
            recorded_context=json.loads(files['environment.json'])['observation_context']
            context={key:recorded_context[key] for key in ('boot_id','pid_namespace','clock')}
            records=[json.loads(line) for line in raw.read_text().splitlines()]
            export={'format_version':1,'kind':'robot_business_events',
                'source':{'adapter':'application_events_v1','tool_version':'controlled-fixture-1',
                          'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'synthetic':True},
                'context':dict(context,ros_domain_id=37),
                'window':{'start_ns':status['window_start_ns'],'end_ns':status['window_end_ns'],
                          'expected_inputs':60,'dropped_events':0},'events':records}
            chain={'format_version':1,'chain_id':'fixture-chain','workload_id':'event-fixture',
                'deployment_version':'fixture-v1','input':{'function_id':'a','node':'/fixture/a'},
                'output':{'function_id':'b','node':'/fixture/b'},'deadline_ns':None}
            chain_file,events_file=output/'chain.local.json',output/'events.local.json'
            chain_file.write_text(json.dumps(chain)); events_file.write_text(json.dumps(export))
            args=['--monitor-run',str(capture),'--chain',str(chain_file),'--events',str(events_file),'--raw-source',str(raw)]
            def run(folder,expected=0,code=None,monitor_run=None):
                selected=list(args)
                if monitor_run is not None: selected[1]=str(monitor_run)
                command=[business,*selected,'--output',str(folder)] if code is None else [python,'-c',code,*selected,'--output',str(folder)]
                result=subprocess.run(command,cwd=cwd,env=env,text=True,capture_output=True,timeout=15)
                (output/(folder.name+'-cli.log')).write_text(result.stdout+result.stderr)
                assert result.returncode==expected,(folder.name,result.returncode,result.stderr)
            imported=output/'import'; run(imported)
            result=json.loads((imported/'business-summary.json').read_text())
            assert result['counts']['completed_valid']==30 and result['counts']['unfinished']==10
            assert result['counts']['explicit_drop']==10 and result['counts']['invalid_delivery']==10
            assert result['counts']['unresolved']==result['counts']['invalid_sample']==0
            by_sample={}
            for row in records: by_sample.setdefault(row['sample_id'],{})[row['type']]=row
            durations=sorted(pair['output']['monotonic_ns']-pair['input']['monotonic_ns'] for pair in by_sample.values()
                             if 'output' in pair and pair['output']['valid'])
            for name,fraction in (('p50',.5),('p95',.95),('p99',.99)):
                assert result['e2e_ns'][name]==durations[math.ceil(fraction*30)-1]
            assert result['e2e_ns']['max']==max(durations)
            refs=summary['workload']['functions'][0]['resource_refs']
            assert refs==summary['workload']['functions'][1]['resource_refs']==result['mapping']['resource_refs']
            assert result['business_acceptance']=='not_evaluated' and result['source']['synthetic']
            original={p.name:p.read_bytes() for p in imported.iterdir()}; run(imported,1)
            assert original=={p.name:p.read_bytes() for p in imported.iterdir()}
            export['context']['boot_id']='00000000-0000-0000-0000-000000000000'
            events_file.write_text(json.dumps(export)); mismatch=output/'mismatch'; run(mismatch)
            assert json.loads((mismatch/'business-summary.json').read_text())['e2e_ns']['n']==0
            export['source']['raw_sha256']='0'*64; events_file.write_text(json.dumps(export))
            rejected=output/'bad-digest'; run(rejected,1); assert not rejected.exists()
            export['source']['raw_sha256']=hashlib.sha256(raw.read_bytes()).hexdigest()
            export['context']=dict(context,ros_domain_id=37); events_file.write_text(json.dumps(export))
            original_chain=chain_file.read_bytes()
            chain['chain_id']='\ud800'; chain_file.write_text(json.dumps(chain))
            invalid_text=output/'bad-text'; run(invalid_text,1); assert not invalid_text.exists()
            chain_file.write_bytes(original_chain)
            invalid_monitor=output/'invalid-monitor-copy'; invalid_monitor.mkdir()
            for name,data in files.items(): (invalid_monitor/name).write_bytes(data)
            bad_environment=json.loads(files['environment.json']); bad_environment['observation_context']=['unexpected']
            (invalid_monitor/'environment.json').write_text(json.dumps(bad_environment))
            invalid_context=output/'bad-context'; run(invalid_context,1,monitor_run=invalid_monitor)
            assert not invalid_context.exists()
            (invalid_monitor/'environment.json').write_bytes(files['environment.json'])
            bad_summary=json.loads(files['monitor-summary.json'])
            next(iter(bad_summary['resources']['registered_entities'].values()))['cpu_percent_one_core']='overflow-marker'
            (invalid_monitor/'monitor-summary.json').write_text(json.dumps(bad_summary).replace('"overflow-marker"','1e309'))
            invalid_number=output/'bad-number'; run(invalid_number,1,monitor_run=invalid_monitor)
            assert not invalid_number.exists()
            injected='''import os,signal,sys
from perfkit import business_events as b
original=b._json
def write(path,value):
 original(path,value)
 if path.name=='business-status.json' and value['status']==TRIGGER:
  ACTION
b._json=write
sys.argv=['robot-perf-business',*sys.argv[1:]]
raise SystemExit(b.main())
'''
            canceled=output/'canceled'
            run(canceled,130,injected.replace('TRIGGER',repr('running')).replace('ACTION','os.kill(os.getpid(),signal.SIGTERM)'))
            assert json.loads((canceled/'business-status.json').read_text())['status']=='interrupted'
            failed=output/'final-fault'
            run(failed,1,injected.replace('TRIGGER',repr('complete')).replace('ACTION',"raise OSError('controlled final write fault')"))
            assert json.loads((failed/'business-status.json').read_text())['status']=='failed'
            assert files=={p.name:p.read_bytes() for p in capture.iterdir() if p.is_file()}
            assert worker.poll() is None and external.poll() is None
            with zipfile.ZipFile(wheel) as archive:
                version=Parser().parsestr(archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))).decode())['Version']
                hashes={n:hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist() if n.startswith('perfkit/') and n.endswith('.py')}
            code="import perfkit,pathlib,hashlib,json; from importlib import metadata; print(json.dumps({'version':metadata.version('robot-systems-perf'),'hashes':{'perfkit/'+p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path(perfkit.__file__).parent.glob('*.py')}}))"
            actual=json.loads(subprocess.check_output([python,'-c',code],cwd=cwd,env=env,text=True))
            assert actual=={'version':version,'hashes':hashes}
            (output/'verification.json').write_text(json.dumps({'installed_version':version,'module_count':len(hashes),
                'module_hashes_match':True,'real_fixture_timestamps':True,'production_business_test':False,
                'temperature_accesses':0,'source_unchanged':True,'shared_resource_reference':True,
                'invalid_context_text_number_rejected_before_output':True,
                'sigterm_exitcode':130,'final_fault_status':'failed','external_fixtures_alive_before_cleanup':True},indent=2)+'\n')
    finally:
        owner.cleanup()
        assert not any(handle.exists() for handle in owner.owned.values())
        (output/'lifecycle.json').write_text(json.dumps({'owned_fixture_objects_reaped':True,'count':len(owner.owned)})+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',type=Path,required=True); parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    verify(args.wheel,args.output.resolve())


if __name__=='__main__': main()
