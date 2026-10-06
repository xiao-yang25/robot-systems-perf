"""One bounded, installed real-ROS event chain; no production business or performance acceptance."""
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
import uuid
import venv
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.comparison_common import launch, defer_interrupts
from tests.process_helpers import OwnedProcesses
from tests.temperature_guard import guarded_command, assert_temperature_guard
from perfkit.ros_graph import validate_graph_request, validate_ros_environment

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT/'tests/fixtures/ros_event_chain.py'


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def wait_for(check, processes, seconds=10):
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        for name, process in processes.items():
            if process.poll() is not None:
                raise RuntimeError('fixture exited before completion: '+name+'; see '+name+'.log')
        if check(): return
        time.sleep(.02)
    raise TimeoutError('bounded readiness/data wait exceeded '+str(seconds)+' seconds')


def verify_records(planned, source, processor, sink, identities):
    """Reject loss, duplicates, ID/identity changes and wrong payloads before import."""
    expected = set(planned)
    if len(planned) != 30 or len(expected) != 30:
        raise ValueError('fixture requires 30 independently planned unique inputs')
    indexed = {}
    for role, rows in (('source', source), ('processor', processor), ('sink', sink)):
        ids = [row['sample_id'] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate '+role+' records')
        for row in rows:
            if any(type(row.get(key)) is not int or row[key] != identities[role]['identity'][key]
                   for key in ('pid', 'starttime_ticks')):
                raise ValueError('event-time identity mismatch: '+role)
        indexed[role] = {row['sample_id']: row for row in rows}
    if set(indexed['source']) != expected or set(indexed['processor']) != expected:
        raise ValueError('planned/input/terminal inventory mismatch')
    expected_sink = {sid for i, sid in enumerate(planned) if i % 6 != 1}
    if set(indexed['sink']) != expected_sink:
        raise ValueError('downstream delivery inventory mismatch')
    delays = []
    for i, sid in enumerate(planned):
        first, last = indexed['source'][sid], indexed['processor'][sid]
        for role, row in (('source', first), ('processor', last)):
            if row['function_id'] != role or row['node'] != identities[role]['node']:
                raise ValueError('event node/function mismatch')
        if first['type'] != 'input' or first['valid'] is not True:
            raise ValueError('invalid input event')
        if last['monotonic_ns'] < first['monotonic_ns']:
            raise ValueError('reversed event clock')
        kind, valid = ('drop', True) if i % 6 == 1 else ('output', i % 6 != 2)
        if last['type'] != kind or last['valid'] is not valid:
            raise ValueError('terminal kind/validity mismatch')
        if sid in indexed['sink'] and indexed['sink'][sid]['valid'] is not valid:
            raise ValueError('downstream validity mismatch')
        if sid in indexed['sink'] and indexed['sink'][sid]['received_ns'] < first['monotonic_ns']:
            raise ValueError('downstream receipt precedes input')
        if kind == 'output' and valid:
            delays.append(last['monotonic_ns']-first['monotonic_ns'])
    return sorted(delays)


def verify(args, owner):
    output = args.output.resolve()
    with tempfile.TemporaryDirectory(prefix='ros-event-install-') as temporary:
        root = Path(temporary); prefix = root/'venv'; cwd = root/'unrelated'; cwd.mkdir()
        venv.EnvBuilder(with_pip=False).create(prefix)
        pip = importlib.util.find_spec('pip')
        if pip is None: raise RuntimeError('prepared offline pip bootstrap required')
        env = dict(os.environ)
        sdk_path = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = str(Path(pip.origin).parent.parent)
        python = prefix/'bin/python'
        logs = []
        def start(command, name):
            log = (output/(name+'.log')).open('x'); logs.append(log)
            write(output/(name+'-command.json'), list(map(str, command)))
            return launch(owner, command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        def run(command, name, timeout=20):
            process = start(command, name)
            code = process.wait(timeout=timeout)
            if code: raise RuntimeError(name+' exited '+str(code)+'; see '+name+'.log')
        try:
            run([python, '-m', 'pip', '--isolated', 'install', '--no-index',
                 '--disable-pip-version-check', args.wheel.resolve()], 'install', timeout=30)
            env['PYTHONPATH'] = ':'.join(p for p in sdk_path.split(':') if p and
                                       Path(p).resolve() != ROOT and not p.endswith('pip.zip'))
            env.pop('EP_SOURCE_REVISION', None)
            env.update(ROS_DOMAIN_ID=str(args.domain_id), RMW_IMPLEMENTATION=args.rmw)
            # Installed modules are checked before any fixture can be mistaken for business evidence.
            code = "import perfkit,pathlib,hashlib,json; from importlib import metadata; print(json.dumps({'version':metadata.version('robot-systems-perf'),'hashes':{'perfkit/'+p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path(perfkit.__file__).parent.glob('*.py')}}))"
            run([python, '-c', code], 'installed-modules', timeout=10)
            actual = json.loads((output/'installed-modules.log').read_text())
            with zipfile.ZipFile(args.wheel) as archive:
                version = Parser().parsestr(archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))).decode())['Version']
                hashes = {n:hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist()
                          if n.startswith('perfkit/') and n.endswith('.py')}
            if actual != {'version':version, 'hashes':hashes}: raise RuntimeError('installed wheel hashes mismatch')
            sdk = ['--domain-id', str(args.domain_id), '--ros-python', args.ros_python,
                   '--rmw', args.rmw, '--sdk-prefix', str(args.sdk_prefix)]
            run([prefix/'bin/robot-perf-ros', '--preflight', *sdk, '--graph-wait', '.2',
                 '--output', output/'preflight'], 'preflight')
            namespace = '/perfkit_event_'+uuid.uuid4().hex
            epoch = uuid.uuid4().hex
            planned = [epoch+':0:'+str(i) for i in range(30)]
            write(output/'input-inventory.json', {'kind':'controller_planned_inputs', 'sample_ids':planned,
                                                 'not_derived_from_event_queue':True})
            fixture_digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
            write(output/'fixture-provenance.json', {'fixture':str(FIXTURE), 'sha256':fixture_digest,
                  'python_source_direct_execution':True, 'wheel_sha256':hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
                  'requested_sdk':str(args.sdk_prefix.resolve()), 'requested_rmw':args.rmw,
                  'namespace':namespace, 'domain_id':args.domain_id, 'synthetic':True})
            workers = {role:start([args.ros_python, FIXTURE, '--role', role, '--namespace', namespace,
                                  '--directory', output], role) for role in ('sink','processor','source')}
            wait_for(lambda: all((output/(role+'-ready.json')).exists() for role in workers), workers)
            identities = {role:json.loads((output/(role+'-identity.json')).read_text()) for role in workers}
            for role, identity in identities.items():
                handle = owner.handle(workers[role])
                if identity['identity'] != {'pid':handle.pid, 'starttime_ticks':handle.identity.starttime}:
                    raise RuntimeError('self-recorded fixture identity differs from pinned process')
                if identity['context']['ros_domain_id'] != args.domain_id or identity['source']['rmw'] != args.rmw:
                    raise RuntimeError('fixture domain/RMW mismatch')
                Path(identity['source']['rclpy_module']).resolve().relative_to(args.sdk_prefix.resolve())
                if identity['source']['fixture_sha256'] != fixture_digest:
                    raise RuntimeError('executed fixture bytes mismatch')
            workload = {'format_version':1, 'workload_id':'controlled-ros-chain',
                        'workload_version':fixture_digest, 'ros_domain_id':args.domain_id,
                        'functions':[{'id':role, 'process_selector':{'pids':[p.pid]},
                                      'ros_nodes':[namespace+'/'+role]} for role,p in workers.items()]}
            write(output/'workload.local.json', workload)
            monitor = output/'monitor'; guard = output/'temperature-guard.json'
            monitored = start(guarded_command([prefix/'bin/robot-perf-monitor', '--workload', output/'workload.local.json',
                '--profile','light','--seconds','5','--interval','.1','--discovery-interval','.2',
                '--skip-temperatures','--output',monitor], python, guard), 'monitor')
            def observed():
                path = monitor/'business-relations.jsonl'
                if not path.exists(): return False
                rows = [json.loads(line) for line in path.read_text().splitlines(keepends=True) if line.endswith('\n')]
                return bool(rows and all(any(c['resource_ref'] for c in role['candidates']) for role in rows[-1]['functions']))
            wait_for(observed, workers)
            time.sleep(.3); (output/'go').write_text('go')
            wait_for(lambda: all((output/(role+'-done.json')).exists() for role in workers), workers)
            if monitored.wait(timeout=10): raise RuntimeError('monitor failed; see monitor.log')
            assert_temperature_guard(guard)
            snapshots = {p.name:p.read_bytes() for p in monitor.iterdir() if p.is_file()}
            status = json.loads(snapshots['monitor-status.json'])
            context = json.loads(snapshots['environment.json'])['observation_context']
            event_context = dict(identities['source']['context'])
            for identity in identities.values():
                if identity['context'] != event_context: raise RuntimeError('fixture contexts differ')
            if any(event_context[k] != context[k] for k in ('boot_id','pid_namespace','clock')):
                raise RuntimeError('fixture/monitor context mismatch')
            (output/'stop').write_text('stop')
            for role,p in workers.items():
                if p.wait(timeout=5): raise RuntimeError(role+' failed to stop; see role log')
            source, processor, sink = [read_rows(output/(role+'-raw.jsonl')) for role in ('source','processor','sink')]
            delays = verify_records(planned, source, processor, sink, identities)
            for role, rows in (('source',source), ('processor',processor)):
                recording = json.loads((output/(role+'-recording.json')).read_text())
                if recording['emitted_events'] != len(rows) or recording['write_errors'] != 0 or recording['dropped_events'] != 0:
                    raise RuntimeError('recording completion/counts inconsistent')
            # Preserve per-process raw bytes; deterministic concatenation is the imported raw source.
            raw = output/'events-raw.jsonl'
            raw.write_bytes((output/'source-raw.jsonl').read_bytes()+(output/'processor-raw.jsonl').read_bytes())
            export = {'format_version':1, 'kind':'robot_business_events',
                      'source':{'adapter':'application_events_v1','tool_version':'controlled-ros-chain-v1',
                                'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'synthetic':True},
                      'context':event_context, 'window':{'start_ns':status['window_start_ns'],
                      'end_ns':status['window_end_ns'],'expected_inputs':len(planned),'dropped_events':0},
                      'events':source+processor}
            chain = {'format_version':1, 'chain_id':'controlled-ros-chain', 'workload_id':workload['workload_id'],
                     'deployment_version':fixture_digest, 'input':{'function_id':'source','node':namespace+'/source'},
                     'output':{'function_id':'processor','node':namespace+'/processor'}, 'deadline_ns':None}
            write(output/'events.local.json', export); write(output/'chain.local.json', chain)
            run([prefix/'bin/robot-perf-business','--monitor-run',monitor,'--chain',output/'chain.local.json',
                 '--events',output/'events.local.json','--raw-source',raw,'--output',output/'import'], 'import')
            summary = json.loads((output/'import/business-summary.json').read_text())
            expected_counts = {'observed_input_ids':30,'completed_valid':20,'explicit_drop':5,'invalid_delivery':5,
                               'unfinished':0,'unresolved':0,'invalid_sample':0,'orphan_sample_ids':0}
            if any(summary['counts'].get(k) != v for k,v in expected_counts.items()):
                raise RuntimeError('import outcome mismatch: '+json.dumps(summary['counts']))
            for key, fraction in (('p50',.5),('p95',.95),('p99',.99)):
                if summary['e2e_ns'][key] != delays[math.ceil(fraction*len(delays))-1]:
                    raise RuntimeError('independent quantile mismatch')
            if summary['e2e_ns']['max'] != max(delays) or summary['e2e_ns']['n'] != 20:
                raise RuntimeError('independent latency count/max mismatch')
            throughput = 20*1e9/(status['window_end_ns']-status['window_start_ns'])
            if summary['throughput']['completed_valid_per_second'] != throughput:
                raise RuntimeError('whole-window throughput mismatch')
            if not summary['source']['synthetic'] or summary['business_acceptance'] != 'not_evaluated':
                raise RuntimeError('controlled data must never become production acceptance')
            if snapshots != {p.name:p.read_bytes() for p in monitor.iterdir() if p.is_file()}:
                raise RuntimeError('source monitor changed')
            write(output/'verification.json', {'installed_version':version,'module_count':len(hashes),
                  'module_hashes_match':True,'real_ros_transport':True,'synthetic':True,'production_business_test':False,
                  'inputs':30,'valid_outputs':20,'drops':5,'invalid_outputs':5,'sink_receipts':25,
                  'temperature_accesses':0,'source_unchanged':True,'independent_quantiles_and_throughput':True,
                  'deadline':'not_configured','budget':'not_configured','business_acceptance':'not_evaluated',
                  'boundary':'source input event before publish to processor terminal publish return/drop',
                  'recording_overhead':'synchronous test file writes included; not a production exporter or performance benchmark'})
        finally:
            # Logs stay available even if a fixture/query failed.
            for log in logs: log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ros-python', required=True)
    parser.add_argument('--sdk-prefix', type=Path, required=True)
    parser.add_argument('--rmw', required=True)
    parser.add_argument('--domain-id', type=int, required=True)
    args = parser.parse_args()
    validate_graph_request(args.domain_id, args.ros_python, .2, 10, ())
    validate_ros_environment(args.rmw, args.sdk_prefix)
    if not args.wheel.is_file(): parser.error('wheel must exist')
    status = {'status':'running','started_ns':time.monotonic_ns()}
    owns = False; owner = None; primary = None
    def terminate(signum, frame): raise KeyboardInterrupt('controlled ROS chain interrupted')
    old = signal.signal(signal.SIGTERM, terminate)
    try:
        with defer_interrupts():
            args.output.mkdir(parents=True, exist_ok=False); owns = True
            write(args.output/'test-status.json', status)
        owner = OwnedProcesses()
        verify(args, owner)
    except BaseException as error:
        primary = error
        status.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',
                      error_type=type(error).__name__, error=str(error))
    finally:
        if owns:
            try:
                with defer_interrupts():
                    if owner is not None:
                        owner.cleanup()
                        if any(handle.exists() for handle in owner.owned.values()):
                            raise RuntimeError('test-owned process still exists after cleanup')
                        write(args.output/'lifecycle.json', {'owned_fixture_objects_reaped':True,'count':len(owner.owned)})
                    if primary is None: status['status'] = 'complete'
                    status['finished_ns'] = time.monotonic_ns()
                    write(args.output/'test-status.json', status)
            except BaseException as cleanup:
                if primary is None:
                    primary = cleanup
                    status.update(status='interrupted' if isinstance(cleanup,KeyboardInterrupt) else 'failed',
                                  error_type=type(cleanup).__name__, error=str(cleanup))
                else:
                    status.setdefault('cleanup_errors',[]).append({'error_type':type(cleanup).__name__,'error':str(cleanup)})
                try:
                    with defer_interrupts():
                        status['finished_ns'] = time.monotonic_ns(); write(args.output/'test-status.json',status)
                except BaseException: pass
        signal.signal(signal.SIGTERM, old)
    if primary is not None:
        print(str(primary), file=sys.stderr)
        return 130 if isinstance(primary,KeyboardInterrupt) else 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
