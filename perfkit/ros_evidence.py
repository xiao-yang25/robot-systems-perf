"""Explicit M2b graph snapshots and imported node-init evidence, never metrics."""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time

from .lifecycle import defer_interrupts
from .monitor import _source_record
from .ros_graph import collect_graph, validate_graph_request, validate_ros_environment
from .workload import validate_workload


NODE = re.compile(r'/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*')
UUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')
LIMITS = [
    'Graph names/endpoints can belong to remote machines; a graph does not expose local process ownership.',
    'Snapshots are bounded point queries, not atomic or continuous graph coverage.',
    'Imported node-init metadata provenance and clock conversion remain the exporter responsibility.',
    'Identity consistency is historical evidence at an initialization event, not continuing node existence or algorithm verification.',
    'Resource records are referenced, never copied, divided or summed by node/function.',
    'No business messages, callback CPU, end-to-end latency or application deadlines are measured.',
]


def _read(path, maximum=16 * 1024 * 1024):
    # NONBLOCK plus fstat rejects FIFOs/devices without waiting for a writer.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('evidence input must be a regular file')
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('evidence input exceeds size limit')
    def reject_constant(token):
        raise ValueError('nonfinite JSON')
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
    return value, raw


def _integer(value, low=0):
    return type(value) is int and value >= low


def validate_trace(value):
    """Versioned normalized export, not arbitrary CTF/text or a claim of authenticity."""
    if not isinstance(value, dict) or set(value) != {'format_version', 'kind', 'source', 'context', 'events'}:
        raise ValueError('unsupported trace metadata fields')
    if type(value['format_version']) is not int or value['format_version'] != 1 or value['kind'] != 'ros2_node_init_metadata':
        raise ValueError('unsupported trace metadata format')
    source, context = value['source'], value['context']
    if not isinstance(source, dict) or set(source) != {'adapter', 'tool_version', 'raw_sha256'}:
        raise ValueError('trace source provenance required')
    if source['adapter'] != 'ros2_tracing_normalized_v1' or not re.fullmatch(r'[0-9a-f]{64}', str(source['raw_sha256'])):
        raise ValueError('unsupported trace adapter or raw digest')
    version = source['tool_version']
    if not isinstance(version, str) or not version.strip() or len(version) > 128 or any(ord(c) < 32 for c in version):
        raise ValueError('trace exporter tool version required')
    if not isinstance(context, dict) or set(context) != {'boot_id', 'pid_namespace', 'clock', 'ros_domain_id'}:
        raise ValueError('trace identity and time context required')
    if not UUID.fullmatch(str(context['boot_id'])) or not re.fullmatch(r'pid:\[[0-9]+\]', str(context['pid_namespace'])):
        raise ValueError('trace boot/PID namespace invalid')
    if context['clock'] != 'linux_monotonic' or not _integer(context['ros_domain_id']) or context['ros_domain_id'] > 232:
        raise ValueError('trace needs a supported monotonic clock and domain')
    events = value['events']
    if not isinstance(events, list) or len(events) > 100000:
        raise ValueError('bounded trace event list required')
    seen = set()
    for event in events:
        if not isinstance(event, dict) or set(event) != {
                'event', 'monotonic_ns', 'pid', 'starttime_ticks', 'node_handle', 'node_name', 'namespace'}:
            raise ValueError('unsupported trace event fields')
        if event['event'] != 'ros2:rcl_node_init' or any(not _integer(event[key], low) for key, low in
                (('monotonic_ns', 0), ('pid', 1), ('starttime_ticks', 1), ('node_handle', 1))):
            raise ValueError('unsupported or invalid node-init event')
        name, namespace = event['node_name'], event['namespace']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError('trace node name invalid')
        if not isinstance(namespace, str) or not re.fullmatch(r'/(?:[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*)?', namespace):
            raise ValueError('trace namespace invalid')
        if len(name) + len(namespace) > 512:
            raise ValueError('trace node label exceeds limit')
        identity = tuple(event[key] for key in ('pid', 'starttime_ticks', 'node_handle', 'monotonic_ns'))
        if identity in seen:
            raise ValueError('duplicate trace initialization event')
        seen.add(identity)
    return value


def load_monitor(directory):
    names = ('monitor-status.json', 'monitor-summary.json', 'environment.json', 'workload-profile.json')
    values, digests = {}, {}
    for name in names:
        value, raw = _read(Path(directory) / name)
        if not isinstance(value, dict):
            raise ValueError('monitor evidence must be JSON objects')
        values[name] = value
        digests[name] = hashlib.sha256(raw).hexdigest()
    status, summary = values[names[0]], values[names[1]]
    if status.get('status') not in ('complete', 'interrupted'):
        raise ValueError('only terminal complete/interrupted M2a evidence can be associated')
    start, end = status.get('window_start_ns'), status.get('window_end_ns')
    if not _integer(start) or not _integer(end) or end < start:
        raise ValueError('monitor window invalid')
    workload = validate_workload(values['workload-profile.json'].get('workload'))
    expected_hash = hashlib.sha256(json.dumps(workload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if expected_hash != values['workload-profile.json'].get('workload_sha256'):
        raise ValueError('workload profile digest inconsistent')
    business = summary.get('workload')
    if not isinstance(business, dict) or business.get('workload_id') != workload['workload_id'] or \
            business.get('ros_domain_id') != workload['ros_domain_id']:
        raise ValueError('monitor workload evidence inconsistent')
    roles = business.get('functions')
    if not isinstance(roles, list) or not all(isinstance(r, dict) and isinstance(r.get('function_id'), str) for r in roles) or {r.get('function_id') for r in roles} != {r['id'] for r in workload['functions']} or len(roles) != len(workload['functions']):
        raise ValueError('monitor function evidence inconsistent')
    resources = summary.get('resources')
    if not isinstance(resources, dict):
        raise ValueError('monitor resource evidence invalid')
    entities = resources.get('registered_entities') or {}
    if not isinstance(entities, dict) or not all(isinstance(e, dict) for e in entities.values()):
        raise ValueError('monitor resource table invalid')
    for role in roles:
        declarations = next(item['ros_nodes'] for item in workload['functions'] if item['id'] == role['function_id'])
        if role.get('ros_nodes') != declarations or not isinstance(role.get('reference_observations'), dict):
            raise ValueError('monitor declarations/reference windows invalid')
        refs = role.get('resource_refs')
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs) or set(refs) != set(role['reference_observations']) or len(refs) != len(set(refs)):
            raise ValueError('monitor reference list inconsistent')
        for ref, window in role['reference_observations'].items():
            if not isinstance(ref, str) or not re.fullmatch(r'pid=[1-9][0-9]*:registration=[1-9][0-9]*:start=[0-9]+', ref):
                raise ValueError('monitor resource identity invalid')
            if not isinstance(window, dict) or any(not _integer(window.get(key)) for key in ('first_observed_ns', 'last_observed_ns')) or not start <= window['first_observed_ns'] <= window['last_observed_ns'] <= end:
                raise ValueError('monitor reference window invalid')
    return {'status': status, 'summary': summary, 'workload': workload,
            'environment': values['environment.json'], 'source_hashes': digests, 'entities': entities}


def associate(monitor, graph=None, trace=None):
    business = monitor['summary']['workload']
    context = monitor['environment'].get('observation_context') or {}
    graph_counts = Counter(node['full_name'] for node in (graph or {}).get('nodes', []))
    graph_usable = graph is not None and graph['status'] in ('observed', 'empty')
    declared_domain = monitor['workload']['ros_domain_id']
    context_reason = None
    if trace:
        for key in ('boot_id', 'pid_namespace', 'clock'):
            if context.get(key) is None:
                context_reason = 'monitor_' + key + '_unrecorded'
                break
            if context[key] != trace['context'][key]:
                context_reason = key + '_mismatch'
                break
        if not context_reason and declared_domain != trace['context']['ros_domain_id']:
            context_reason = 'ros_domain_unrecorded_or_mismatch'
    functions = []
    for role in business['functions']:
        nodes = []
        for name in role['ros_nodes']:
            count = graph_counts[name]
            graph_state = ('unavailable' if not graph_usable else 'ambiguous' if count > 1 else
                           'present' if count == 1 else 'not_observed')
            matches, reasons = set(), []
            relevant = [e for e in (trace or {}).get('events', [])
                        if e['namespace'].rstrip('/') + '/' + e['node_name'] == name]
            if trace and context_reason:
                reasons.append(context_reason)
            elif relevant:
                for event in relevant:
                    possible = [ref for ref, window in role['reference_observations'].items()
                        if ref.startswith('pid={}:'.format(event['pid'])) and
                        ref.endswith(':start={}'.format(event['starttime_ticks'])) and
                        window['first_observed_ns'] <= event['monotonic_ns'] <= window['last_observed_ns']]
                    for ref in possible:
                        entity = monitor['entities'].get(ref)
                        if entity and entity.get('kind') == 'process' and entity.get('pid') == event['pid'] and entity.get('starttime_ticks') == event['starttime_ticks']:
                            matches.add(ref)
                    if not possible:
                        reasons.append('no_scoped_identity_reference_at_event')
                    elif not any(ref in matches for ref in possible):
                        reasons.append('no_matching_process_resource_record')
            if len(matches) > 1:
                state = 'ambiguous'
            elif len(matches) == 1:
                state = 'imported_trace_identity_consistent'
            else:
                state = 'unresolved'
                if not reasons:
                    reasons.append('no_node_init_metadata' if trace else 'trace_not_supplied')
            nodes.append({'full_name': name, 'declaration': 'operator_declared',
                'graph': {'status': graph_state, 'matching_count': count if graph_usable else None,
                          'local_pid_evidence': False},
                'process_relation': {'status': state, 'resource_refs': sorted(matches), 'reasons': sorted(set(reasons)),
                    'provenance': 'external_normalized_export_not_authenticated' if trace else None,
                    'validity': 'initialization events within historical reference observation bounds only'}})
        functions.append({'function_id': role['function_id'], 'm2a_last_state': (role.get('last_scan') or {}).get('status'),
                          'nodes': nodes, 'resource_refs': role['resource_refs']})
    incomplete = any(role['m2a_last_state'] != 'candidate' or
        any(node['graph']['status'] != 'present' or node['process_relation']['status'] != 'imported_trace_identity_consistent' or
            node['process_relation']['reasons'] for node in role['nodes']) or not role['nodes'] for role in functions)
    return {'format_version': 1, 'kind': 'ros_business_evidence', 'workload_id': business['workload_id'],
        'monitor_window': {key: monitor['status'][key] for key in ('window_start_ns', 'window_end_ns')},
        'graph_window': graph.get('query_window') if graph else None,
        'quality': {'status': 'review_required' if incomplete else 'identity_consistency_observed',
                    'source_authenticity': 'not_evaluated'},
        'monitor_status': monitor['status']['status'], 'monitor_source_hashes': monitor['source_hashes'],
        'graph_status': graph['status'] if graph else 'not_requested',
        'trace_status': 'imported' if trace else 'not_supplied', 'functions': functions,
        'resource_table': 'source monitor-summary.json:resources.registered_entities',
        'business_acceptance': 'not_evaluated', 'limits': LIMITS}


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')


def _report(output, result):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    lines = ['# ROS 对象关系证据', '', '最终执行状态以 ros-status.json 为准。', '',
             '节点声明、ROS图可见性和导入追踪的身份一致性分别保留；不证明算法语义或业务达标。', '',
             '| 功能 | 节点 | 图可见性 | 导入身份关系 | 资源引用 |', '| --- | --- | --- | --- | --- |']
    for role in result['functions']:
        for node in role['nodes']:
            relation = node['process_relation']
            lines.append('| {} | {} | {} | {} | {} |'.format(cell(role['function_id']), cell(node['full_name']),
                node['graph']['status'], relation['status'], cell(', '.join(relation['resource_refs']))))
    lines += ['', '没有匹配不表示没有业务；远端节点、重复名字、缺初始化事件和旧版本身份缺项均需保留未知。',
              '图快照时间与原monitor窗口分别记录，不假称同期。资源引用不复制或分配CPU/RSS。', '',
              '详情见 ros-relations.json、graph-query.json、trace-metadata.json（仅提供时）。']
    (output / 'ROS_REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_ros_evidence(monitor_run, output, *, graph=False, domain_id=None, ros_python=sys.executable,
                     wait_seconds=2, timeout_seconds=10, component_managers=(), trace_metadata=None,
                     preflight=False, rmw=None, sdk_prefix=None):
    validate_ros_environment(rmw, sdk_prefix)
    if preflight:
        if monitor_run is not None or trace_metadata is not None:
            raise ValueError('preflight cannot read monitor or trace inputs')
        graph = True
    elif monitor_run is None:
        raise ValueError('monitor_run required unless preflight is selected')
    if not graph and (rmw is not None or sdk_prefix is not None):
        raise ValueError('SDK/RMW selection requires a graph query or preflight')
    if not graph and trace_metadata is None:
        raise ValueError('explicit --graph or --trace-metadata is required')
    if graph:
        validate_graph_request(domain_id, ros_python, wait_seconds, timeout_seconds, component_managers)
    if not graph and (domain_id is not None or component_managers):
        raise ValueError('domain/component queries require --graph')
    if not isinstance(ros_python, str) or not ros_python.strip():
        raise ValueError('ROS Python executable required')
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in (wait_seconds, timeout_seconds)) or not 0 <= wait_seconds <= 30 or not wait_seconds < timeout_seconds <= 60:
        raise ValueError('bounded query wait/timeout required')
    if not isinstance(component_managers, (list, tuple)) or len(component_managers) > 16 or any(not isinstance(m, str) or not NODE.fullmatch(m) for m in component_managers):
        raise ValueError('bounded absolute component manager names required')
    output = Path(output)
    monitor = None
    if not preflight:
        # A new subdirectory would mutate the supposedly read-only source run.
        source_directory = Path(monitor_run).resolve()
        try:
            output.resolve().relative_to(source_directory)
        except ValueError:
            pass
        else:
            raise ValueError('output must be separate from the source monitor directory')
        monitor = load_monitor(source_directory)
        if graph and monitor['workload']['ros_domain_id'] is not None and monitor['workload']['ros_domain_id'] != domain_id:
            raise ValueError('graph domain contradicts recorded workload domain')
    trace, trace_raw = _read(trace_metadata) if trace_metadata is not None else (None, None)
    if trace_metadata is not None:
        trace = validate_trace(trace)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    status = {'status': 'running', 'error': None, 'error_type': None, 'started_ns': time.monotonic_ns()}
    primary = None
    try:
        _json(output / 'ros-status.json', status)
        _json(output / 'ros-inputs.json', {'source': _source_record(), 'monitor_source_hashes': monitor['source_hashes'] if monitor else None,
            'preflight': preflight, 'rmw_requested': rmw, 'sdk_prefix': str(sdk_prefix) if sdk_prefix else None,
            'domain_id': domain_id, 'graph_requested': graph, 'component_managers': component_managers,
            'wait_seconds': wait_seconds, 'timeout_seconds': timeout_seconds,
            'trace_input_sha256': hashlib.sha256(trace_raw).hexdigest() if trace_raw else None})
        if trace_raw is not None:
            (output / 'trace-metadata.json').write_bytes(trace_raw)
        graph_result = None
        if graph:
            graph_result = collect_graph(output, domain_id, ros_python, wait_seconds, timeout_seconds,
                                         component_managers=component_managers, rmw=rmw, sdk_prefix=sdk_prefix)
            _json(output / 'graph-query.json', graph_result)
        if preflight:
            result = {'format_version': 1, 'kind': 'ros_environment_preflight',
                      'ready': graph_result['status'] in ('observed', 'empty'),
                      'source': graph_result['source'], 'reason': graph_result.get('reason'),
                      'components': graph_result['components'],
                      'business_acceptance': 'not_evaluated',
                      'scope': 'Only the explicitly selected SDK/Python/RMW and read-only query.'}
            _json(output / 'ros-preflight.json', result)
        else:
            result = associate(monitor, graph_result, trace)
            _json(output / 'ros-relations.json', result)
            _report(output, result)
        if graph_result is not None and graph_result['status'] not in ('observed', 'empty'):
            raise RuntimeError('ROS graph query ' + graph_result['status'] + ': ' + str(graph_result.get('reason')))
        status['status'] = 'complete'
        return result
    except BaseException as error:
        primary = error
        status.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=str(error), error_type=type(error).__name__)
        raise
    finally:
        status['finished_ns'] = time.monotonic_ns()
        try:
            with defer_interrupts():
                _json(output / 'ros-status.json', status)
        except BaseException as cleanup:
            unhandled = primary is None
            if unhandled:
                status.update(status='interrupted' if isinstance(cleanup, KeyboardInterrupt) else 'failed',
                              error=str(cleanup), error_type=type(cleanup).__name__)
            else:
                status.setdefault('cleanup_errors', []).append(
                    {'stage': 'final_status_write', 'error_type': type(cleanup).__name__, 'error': str(cleanup)})
            try:
                with defer_interrupts():
                    _json(output / 'ros-status.json', status)
            except BaseException as retry:
                # A permanently unwritable status cannot be saved; a secondary
                # fault must not replace the first failure/interrupt.
                if hasattr(cleanup, 'add_note'):
                    cleanup.add_note('final status retry failed: ' + repr(retry))
            if unhandled:
                raise



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor-run', type=Path)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--sdk-prefix', type=Path)
    parser.add_argument('--rmw')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--graph', action='store_true')
    parser.add_argument('--domain-id', type=int)
    parser.add_argument('--ros-python', default=sys.executable)
    parser.add_argument('--graph-wait', type=float, default=2)
    parser.add_argument('--query-timeout', type=float, default=10)
    parser.add_argument('--component-manager', action='append', default=[])
    parser.add_argument('--trace-metadata', type=Path)
    args = parser.parse_args()
    def terminate(signum, frame):
        raise KeyboardInterrupt('ROS evidence interrupted by SIGTERM')
    old = signal.signal(signal.SIGTERM, terminate)
    try:
        run_ros_evidence(args.monitor_run, args.output, graph=args.graph, domain_id=args.domain_id,
            ros_python=args.ros_python, wait_seconds=args.graph_wait, timeout_seconds=args.query_timeout,
            component_managers=args.component_manager, trace_metadata=args.trace_metadata,
            preflight=args.preflight, rmw=args.rmw, sdk_prefix=args.sdk_prefix)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print('ROS evidence failed:', error, file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old)
    print('ROS evidence saved:', args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
