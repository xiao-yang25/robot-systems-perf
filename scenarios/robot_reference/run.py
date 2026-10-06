"""Bounded ROS reference scenarios with original records and collector reuse."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scenarios.robot_reference.algorithms import ROLES
from scripts.comparison_common import launch, defer_interrupts
from tests.process_helpers import OwnedProcesses
from tests.integration_ros_event_chain import wait_for
from tests.temperature_guard import guarded_command, assert_temperature_guard


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def distribution(values):
    ordered = sorted(values)
    return {'n':len(ordered), **{k:ordered[math.ceil(f*len(ordered))-1] if ordered else None
        for k,f in (('p50',.5),('p95',.95),('p99',.99))}, 'max':max(ordered) if ordered else None}


def verify_graph(graph, namespace, roles):
    if graph['status'] != 'observed': raise ValueError('reference graph unavailable/incomplete')
    for i, role in enumerate(roles):
        name = namespace+'/hop_'+str(i)
        matches = [t for t in graph['topics'] if t['name'] == name]
        if len(matches) != 1: raise ValueError('missing/duplicate topic: '+name)
        topic = matches[0]
        for field, expected in (('publishers',role),('subscriptions',roles[(i+1)%len(roles)])):
            endpoints = topic[field]
            if not isinstance(endpoints,list) or len(endpoints) != 1:
                raise ValueError('expected one '+field+' endpoint: '+name)
            endpoint = endpoints[0]
            if endpoint['node_name'] != expected or endpoint['node_namespace'] != namespace:
                raise ValueError('graph endpoint differs from requested topology: '+name)
            if endpoint['qos']['reliability']['name'] != 'RELIABLE':
                raise ValueError('reference QoS is not RELIABLE: '+name)
    # Unknown history/depth stays unknown in the raw snapshot.


def verify_records(output, planned, identities, roles):
    records = rows(output/'simulator-events.jsonl')
    inputs = {r['sample_id']:r for r in records if r['type'] == 'input'}
    outputs = {r['sample_id']:r for r in records if r['type'] == 'output'}
    expected = set(planned)
    if len(expected) != len(planned) or len(records) != 2*len(planned) or set(inputs) != expected or set(outputs) != expected:
        raise ValueError('input/terminal inventory has missing/duplicate/unexpected records')
    for record in records:
        if record['type'] not in ('input','output') or record['valid'] is not True:
            raise ValueError('invalid reference terminal')
        identity = identities['simulator']
        if record['node'] != identity['node'] or record['function_id'] != 'simulator' or any(
                type(record[k]) is not int or record[k] != identity['identity'][k]
                for k in ('pid','starttime_ticks')):
            raise ValueError('reference event identity mismatch')
        if not inputs[record['sample_id']]['monotonic_ns'] <= record['monotonic_ns']:
            raise ValueError('reference event clock reversed')
    stage_metrics = {}
    for role in roles:
        stages = rows(output/(role+'-stages.jsonl'))
        callbacks = [r for r in stages if r['role'] == role]
        if len(callbacks) != len(planned) or {r['sample_id'] for r in callbacks} != expected:
            raise ValueError('stage coverage mismatch: '+role)
        for r in stages:
            if any(type(r[k]) is not int or r[k] != identities[role]['identity'][k]
                   for k in ('pid','starttime_ticks')):
                raise ValueError('stage identity mismatch: '+role)
            if 'edge_age_ns' in r and r['received_ns']-r['source_send_ns'] != r['edge_age_ns']:
                raise ValueError('edge age arithmetic mismatch')
            for key in ('edge_age_ns','algorithm_wall_ns','algorithm_cpu_ns','callback_wall_ns',
                        'callback_cpu_ns','publish_call_ns','release_lateness_ns'):
                if r.get(key) is not None and (type(r[key]) is not int or r[key]<0):
                    raise ValueError('negative/non-integer timing')
        stage_metrics[role] = {key:distribution([r[key] for r in callbacks if r.get(key) is not None])
            for key in ('edge_age_ns','algorithm_wall_ns','algorithm_cpu_ns','callback_wall_ns',
                        'callback_cpu_ns','publish_call_ns')}
        if role == 'simulator':
            releases = [r for r in stages if r['role'] == 'sensor_release']
            if len(releases) != len(planned) or {r['sample_id'] for r in releases} != expected:
                raise ValueError('release inventory mismatch')
            stage_metrics[role]['release_lateness_ns'] = distribution([r['release_lateness_ns'] for r in releases])
    return distribution([outputs[s]['monotonic_ns']-inputs[s]['monotonic_ns'] for s in planned]), stage_metrics


def case(args, name, output, owner, logs):
    output.mkdir(exist_ok=False)
    config = {'scenario':name,'seconds':args.seconds,'hz':args.hz,'points':args.points,
              'seed':args.seed,'depth':args.depth,'monitor_interval':args.monitor_interval,
              'domain_id':args.domain_id,'rmw':args.rmw}
    write(output/'config.json', config)
    namespace = '/reference_'+uuid.uuid4().hex
    epoch = uuid.uuid4().hex
    planned = [epoch+':0:'+str(i) for i in range(round(args.seconds*args.hz))]
    write(output/'input-inventory.json', {'kind':'controller_planned_inputs',
          'not_derived_from_event_queue':True, 'sample_ids':planned})
    environment = dict(os.environ, ROS_DOMAIN_ID=str(args.domain_id), RMW_IMPLEMENTATION=args.rmw,
                       PYTHONPATH=str(ROOT)+':'+os.environ.get('PYTHONPATH',''))
    def start(command, label):
        write(output/(label+'-command.json'), list(map(str,command)))
        log = (output/(label+'.log')).open('x'); logs.append(log)
        return launch(owner, command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
    def run(command, label, timeout=15):
        p = start(command,label)
        if p.wait(timeout=timeout): raise RuntimeError(label+' failed; see '+label+'.log')
    sdk = ['--domain-id',str(args.domain_id),'--ros-python',sys.executable,
           '--sdk-prefix',args.sdk_prefix,'--rmw',args.rmw,'--graph-wait','2']
    run([sys.executable,'-m','perfkit.ros_evidence','--preflight',*sdk,
         '--output',output/'preflight'],'preflight')
    roles = ROLES[name]
    workers = {role:start([sys.executable,ROOT/'scenarios/robot_reference/node.py','--scenario',name,
                          '--role',role,'--namespace',namespace,'--output',output],role) for role in roles}
    wait_for(lambda:all((output/(r+'-ready.json')).exists() for r in roles),workers,seconds=15)
    identities = {r:json.loads((output/(r+'-identity.json')).read_text()) for r in roles}
    context = identities['simulator']['context']
    for role,identity in identities.items():
        handle = owner.handle(workers[role])
        if identity['identity'] != {'pid':handle.pid,'starttime_ticks':handle.identity.starttime}:
            raise ValueError('self identity differs from pinned child')
        if identity['context'] != context or context['ros_domain_id'] != args.domain_id or identity['rmw'] != args.rmw:
            raise ValueError('node context/RMW differs from requested environment')
        Path(identity['rclpy_module']).resolve().relative_to(Path(args.sdk_prefix).resolve())
    workload = {'format_version':1,'workload_id':'reference-'+name,
                'workload_version':args.manifest_hash,'ros_domain_id':args.domain_id,
                'functions':[{'id':r,'process_selector':{'pids':[p.pid]},
                              'ros_nodes':[identities[r]['node']]} for r,p in workers.items()]}
    write(output/'workload.local.json',workload)
    monitor_command = [sys.executable,'-c','from perfkit.monitor import main; raise SystemExit(main())',
        '--workload',output/'workload.local.json',
        '--profile','light','--seconds',str(args.seconds+4),'--interval',str(args.monitor_interval),
        '--discovery-interval','1','--skip-temperatures','--output',output/'monitor']
    monitor = start(guarded_command(monitor_command,sys.executable,output/'temperature-guard.json'),'monitor')
    def observed():
        path = output/'monitor/business-relations.jsonl'
        if not path.exists(): return False
        complete = [json.loads(line) for line in path.read_text().splitlines(keepends=True) if line.endswith('\n')]
        return bool(complete and all(any(c['resource_ref'] for c in f['candidates']) for f in complete[-1]['functions']))
    wait_for(observed,workers,seconds=10)
    time.sleep(.2); (output/'go').write_text('go\n')
    wait_for(lambda:all((output/(r+'-done.json')).exists() for r in roles),workers,seconds=args.seconds+3)
    if monitor.wait(timeout=8): raise RuntimeError('monitor failed; see monitor.log')
    assert_temperature_guard(output/'temperature-guard.json')
    # Graph is queried after the measured input interval, with nodes still alive.
    run([sys.executable,'-m','perfkit.ros_evidence','--monitor-run',output/'monitor','--graph',*sdk,
         '--output',output/'graph'],'graph')
    graph = json.loads((output/'graph/graph-query.json').read_text())
    verify_graph(graph,namespace,roles)
    (output/'stop').write_text('stop\n')
    for role,p in workers.items():
        if p.wait(timeout=5): raise RuntimeError(role+' failed; see node log')
    e2e, stages = verify_records(output,planned,identities,roles)
    status = json.loads((output/'monitor/monitor-status.json').read_text())
    raw = output/'simulator-events.jsonl'
    export = {'format_version':1,'kind':'robot_business_events',
              'source':{'adapter':'application_events_v1','tool_version':'robot-reference-v1',
                        'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'synthetic':True},
              'context':context,'window':{'start_ns':status['window_start_ns'],'end_ns':status['window_end_ns'],
                        'expected_inputs':len(planned),'dropped_events':0},'events':rows(raw)}
    chain = {'format_version':1,'chain_id':'reference-'+name,'workload_id':workload['workload_id'],
             'deployment_version':args.manifest_hash,'input':{'function_id':'simulator','node':identities['simulator']['node']},
             'output':{'function_id':'simulator','node':identities['simulator']['node']},'deadline_ns':None}
    write(output/'events.local.json',export); write(output/'chain.local.json',chain)
    run([sys.executable,'-m','perfkit.business_events','--monitor-run',output/'monitor',
         '--chain',output/'chain.local.json','--events',output/'events.local.json',
         '--raw-source',raw,'--output',output/'import'],'import')
    imported = json.loads((output/'import/business-summary.json').read_text())
    if imported['counts']['completed_valid'] != len(planned) or any(imported['counts'][k] for k in
        ('unfinished','explicit_drop','invalid_delivery','unresolved','invalid_sample','orphan_sample_ids')):
        raise ValueError('reference import identity/coverage mismatch')
    if any(imported['e2e_ns'][k] != e2e[k] for k in ('n','p50','p95','p99','max')):
        raise ValueError('independent E2E recomputation differs from importer')
    result = json.loads((output/'simulator-done.json').read_text())
    if not math.isfinite(result['final_goal_error']) or result['final_goal_error'] >= result['initial_goal_error']:
        raise ValueError('closed loop did not reduce goal error')
    monitor_summary = json.loads((output/'monitor/monitor-summary.json').read_text())
    resource = monitor_summary['resources']
    summary = {'scenario':name,'status':'complete','synthetic':True,'nodes':len(roles),
        'inputs':len(planned),'completed_valid':imported['counts']['completed_valid'],
        'boundary':'sensor input before ROS publish to command reception and kinematic state update',
        'e2e_ns':e2e,'stages':stages,'motion':result,'throughput':imported['throughput'],
        'node_resource_refs':{f['function_id']:f['resource_refs'] for f in monitor_summary['workload']['functions']},
        'temperature_accesses':0,
        'observer':resource['observer'],'collection_cost':resource['collection_cost'],
        'resource_source':'monitor/monitor-summary.json; one resource per process, no per-function division',
        'business_acceptance':'not_evaluated','budget':'not_configured','deadline':'not_configured',
        'recording':'synchronous JSONL writer and temperature sentinel included; callback timing ends before stage logging',
        'edge_boundary':'timestamp before encoding/publish to callback entry, includes encoding, DDS and scheduling'}
    write(output/'summary.json',summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario',choices=['all',*ROLES],default='all')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=10)
    parser.add_argument('--hz',type=int,default=20)
    parser.add_argument('--points',type=int,default=512)
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--depth',type=int,default=32)
    parser.add_argument('--monitor-interval',type=float,default=.5)
    parser.add_argument('--domain-id',type=int,default=79)
    parser.add_argument('--rmw',default='rmw_fastrtps_cpp')
    parser.add_argument('--sdk-prefix',default='/opt/ros/humble')
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 2 <= args.seconds <= 30 or not 1 <= args.hz <= 100:
        parser.error('seconds must be 2..30; hz must be 1..100')
    if not 8 <= args.points <= 8192 or not 1 <= args.depth <= 1024 or not 0 <= args.domain_id <= 232:
        parser.error('points 8..8192; depth 1..1024; domain 0..232 required')
    if not math.isfinite(args.monitor_interval) or not .1 <= args.monitor_interval <= 2:
        parser.error('monitor interval .1..2 required')
    if args.output.is_symlink(): parser.error('output must not be a symlink')
    args.output = args.output.resolve()
    status = {'status':'running','started_ns':time.monotonic_ns(),'active_scenario':None}
    primary, owns, owner, logs = None, False, None, []
    def terminate(signum,frame): raise KeyboardInterrupt('reference scenarios interrupted')
    old = signal.signal(signal.SIGTERM,terminate)
    try:
        with defer_interrupts():
            args.output.mkdir(parents=True,exist_ok=False); owns=True
            write(args.output/'reference-status.json',status)
        owner = OwnedProcesses()
        manifest = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for folder in ('perfkit','scenarios/robot_reference','scripts','tests')
                    for p in sorted((ROOT/folder).glob('*.py'))}
        args.manifest_hash = hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
        write(args.output/'environment.json',{'kind':os.environ.get('EP_ENVIRONMENT_KIND','linux-native-reference'),
            'source_revision':os.environ.get('EP_SOURCE_REVISION'),'source_manifest':manifest,
            'source_manifest_sha256':args.manifest_hash,'machine':platform.machine(),'kernel':platform.release(),
            'python':sys.version,'base_image':os.environ.get('EP_BASE_IMAGE'),'image_id':os.environ.get('EP_IMAGE_ID'),
            'cpu_visibility':len(os.sched_getaffinity(0)),'synthetic':True,
            'limitations':['CPU-only simplified simulation; no physical robot, GPU inference, Nav2 or MoveIt',
                           'No production performance acceptance, business deadline or overhead budget configured']})
        if (ROOT/'dependencies.txt').is_file():
            (args.output/'dependencies.txt').write_bytes((ROOT/'dependencies.txt').read_bytes())
        summaries = []
        for name in ROLES if args.scenario == 'all' else (args.scenario,):
            status['active_scenario']=name; write(args.output/'reference-status.json',status)
            summaries.append(case(args,name,args.output/name,owner,logs))
        lines = ['# Docker机器人参考测量','','CPU基础算法、真实ROS通信、简化闭环仿真；synthetic=true。',
                 'E2E是传感输入发布前到控制指令接收并更新仿真状态。不是物理机器人或生产算法验收。','',
                 '| 场景 | 节点 | 输入/有效完成 | E2E P50 ms | P95 ms | P99 ms | max ms | observer CPU %（单核=100） |',
                 '| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |']
        for s in summaries:
            d=s['e2e_ns']; cpu=s['observer']['cpu_percent_one_core']
            lines.append('| {} | {} | {}/{} | {:.3f} | {:.3f} | {:.3f} | {:.3f} | {} |'.format(
                s['scenario'],s['nodes'],s['inputs'],s['completed_valid'],
                *(d[k]/1e6 for k in ('p50','p95','p99','max')),cpu))
        lines += ['','完整窗口吞吐、逐跳数据年龄、算法wall/CPU、publish、释放迟到、闭环目标误差及原始资源见各场景summary.json。',
                  'JSON字符串编码与同步日志的成本包含在运行中；逐跳数据年龄包含编码、DDS和调度，不能当作纯网络延迟。',
                  '分位数仅针对有效完成样本；小样本P99常等于max。资源覆盖只按实际观测区间，不外推。',
                  'Docker Desktop处于Linux VM内，数值不能作为Orin/Thor性能基线。预算及deadline未配置，业务验收not_evaluated。','']
        (args.output/'REFERENCE_REPORT.md').write_text('\n'.join(lines))
    except BaseException as error:
        primary=error
        status.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',
                      error_type=type(error).__name__,error=str(error))
    finally:
        if owns:
            try:
                with defer_interrupts():
                    if owner is not None:
                        owner.cleanup()
                        if any(h.exists() for h in owner.owned.values()): raise RuntimeError('owned reference process survived')
                        write(args.output/'lifecycle.json',{'owned_objects_reaped':True,'count':len(owner.owned)})
                    for log in logs: log.close()
                    if primary is None: status['status']='complete'
                    status['finished_ns']=time.monotonic_ns(); write(args.output/'reference-status.json',status)
            except BaseException as cleanup:
                if primary is None:
                    primary=cleanup
                    status.update(status='interrupted' if isinstance(cleanup,KeyboardInterrupt) else 'failed',
                                  error_type=type(cleanup).__name__,error=str(cleanup))
                else:
                    status.setdefault('cleanup_errors',[]).append({'error_type':type(cleanup).__name__,'error':str(cleanup)})
                try:
                    with defer_interrupts():
                        status['finished_ns']=time.monotonic_ns(); write(args.output/'reference-status.json',status)
                except BaseException: pass
        signal.signal(signal.SIGTERM,old)
    if primary is not None:
        print(str(primary),file=sys.stderr)
        return 130 if isinstance(primary,KeyboardInterrupt) else 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
