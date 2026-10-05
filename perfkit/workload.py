"""Operator-declared business relations; resources stay owned by the monitor."""
import copy
import re

from .discovery import DiscoverySelector


def _label(value, name, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError(name + ': bounded nonempty single-line string required')


def validate_workload(value):
    if not isinstance(value, dict) or set(value) - {
            'format_version', 'workload_id', 'workload_version', 'ros_domain_id', 'functions'}:
        raise ValueError('unknown or invalid workload fields')
    if type(value.get('format_version')) is not int or value['format_version'] != 1:
        raise ValueError('workload format_version must be 1')
    _label(value.get('workload_id'), 'workload_id')
    _label(value.get('workload_version'), 'workload_version', optional=True)
    domain = value.get('ros_domain_id')
    if domain is not None and (type(domain) is not int or not 0 <= domain <= 4294967295):
        raise ValueError('ros_domain_id: declared nonnegative uint32 or null required')
    functions = value.get('functions')
    if not isinstance(functions, list) or not 1 <= len(functions) <= 64:
        raise ValueError('functions: 1..64 declarations required')
    result = copy.deepcopy(value)
    result.setdefault('workload_version', None)
    result.setdefault('ros_domain_id', None)
    seen = set()
    for role in result['functions']:
        if not isinstance(role, dict) or set(role) - {
                'id', 'process_selector', 'ros_nodes', 'relation_source', 'expected_processes'}:
            raise ValueError('unknown or invalid function fields')
        name = role.get('id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name) or name in seen:
            raise ValueError('function id: unique 1..64 character identifier required')
        seen.add(name)
        role.setdefault('relation_source', 'operator_declared')
        if role['relation_source'] != 'operator_declared':
            raise ValueError('M2a supports only operator_declared relations')
        role.setdefault('expected_processes', 1)
        if type(role['expected_processes']) is not int or not 1 <= role['expected_processes'] <= 256:
            raise ValueError('expected_processes: integer in 1..256 required')
        nodes = role.setdefault('ros_nodes', [])
        if not isinstance(nodes, list) or len(nodes) > 64:
            raise ValueError('ros_nodes: bounded declared label list required')
        for node in nodes:
            _label(node, 'ros_nodes')
            if not re.fullmatch(r'/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*', node):
                raise ValueError('ros_nodes: supported absolute node label required')
        if len(set(nodes)) != len(nodes):
            raise ValueError('ros_nodes: duplicate declaration within function')
        selector = role.get('process_selector')
        if not isinstance(selector, dict) or set(selector) - {
                'include_names', 'exclude_names', 'pids', 'uids', 'cgroup_patterns'}:
            raise ValueError('unknown or invalid process_selector fields')
        for key in ('include_names', 'exclude_names', 'cgroup_patterns'):
            items = selector.setdefault(key, [])
            if not isinstance(items, list) or len(items) > 64:
                raise ValueError(key + ': bounded pattern list required')
            for item in items:
                _label(item, key)
                try:
                    re.compile(item)
                except re.error as error:
                    raise ValueError(key + ': invalid pattern') from error
        for key in ('pids', 'uids'):
            items = selector.setdefault(key, None if key == 'uids' else [])
            if key == 'uids' and items is None:
                continue
            if not isinstance(items, list) or len(items) > 256 or any(
                    type(item) is not int or item < (1 if key == 'pids' else 0) for item in items):
                raise ValueError(key + ': bounded valid integer list required')
        if not selector['pids'] and not selector['include_names']:
            raise ValueError('process_selector needs pids or include_names; cgroup alone is a hard filter')
    return result


class WorkloadSelector(DiscoverySelector):
    """One hard-scoped proc scan; individual function matches before global cap."""
    def __init__(self, config, workload, clock_ticks):
        if config['include_names'] or config['pids'] or config['active_cpu_percent'] != 1:
            raise ValueError('workload mode uses function selectors; global names/PIDs/activity overrides are unsupported')
        explicit_only = all(not role['process_selector']['include_names'] for role in workload['functions'])
        requested = sorted({pid for role in workload['functions'] for pid in role['process_selector']['pids']})
        scope = dict(config, include_names=[] if explicit_only else ['.*'],
                     pids=requested if explicit_only else [], active_cpu_percent=0)
        super().__init__(scope, clock_ticks)
        self.discovery_mode = 'explicit_pids' if explicit_only else 'scoped_discovery'
        self.workload = workload
        self.roles = {role['id']: DiscoverySelector(dict(role['process_selector'],
                      active_cpu_percent=0, max_targets=256), clock_ticks) for role in workload['functions']}

    def update(self, processes, now_ns):
        matches = {name: [] for name in self.roles}
        targets = {}
        eligible = 0
        for process in processes:
            if not self._eligible(process):
                continue
            eligible += 1
            for name, selector in self.roles.items():
                if (selector._eligible(process) and (process['pid'] in selector.pids or
                        selector._name_matches(selector.include, process))):
                    matches[name].append(process)
                    item = targets.setdefault(process['pid'], dict(process,
                        selection_reasons=[], observed_cpu_percent=None))
                    item['selection_reasons'].append('workload:' + name)
        chosen = [targets[pid] for pid in sorted(targets)[:self.cap]]
        return {'targets': chosen, 'workload_matches': matches,
                'selection': {'eligible_count': eligible, 'matched_count': len(targets),
                    'selected_count': len(chosen), 'omitted_count': len(targets) - len(chosen),
                    'mode': 'function_selectors_only; global_uid_exclusions_cgroup_intersection'}}


def resource_key(identity):
    return 'pid={}:registration={}:start={}'.format(identity['pid'], identity['registration_id'],
                                                  identity['starttime_ticks'])


class BusinessRelations:
    def __init__(self, workload, namespace):
        self.workload, self.namespace = workload, namespace
        self.latest = {}
        self.counts = {role['id']: {} for role in workload['functions']}
        self.quality = {role['id']: {'incomplete_scope_scans': 0,
            'target_cap_scans': 0, 'registration_race_scans': 0} for role in workload['functions']}
        self.references = {role['id']: {} for role in workload['functions']}
        self.scans = 0

    def observe(self, decision, registrations, inventory, scan_start, now):
        roles = []
        selected = {item['pid'] for item in decision['targets']}
        complete = not (inventory['scan'].get('skipped_count', 0) or
                        inventory['scan'].get('optional_unavailable_by_reason'))
        for role in self.workload['functions']:
            name = role['id']
            matches = decision['workload_matches'][name]
            count, expected = len(matches), role['expected_processes']
            state = ('unresolved' if count == 0 else 'ambiguous' if count > expected else
                     'incomplete' if count < expected else 'candidate')
            candidates = []
            for process in matches:
                if process['pid'] not in selected:
                    continue
                identity = registrations.get(process['pid'])
                registered = bool(identity and identity['starttime_ticks'] == process['starttime_ticks'])
                key = resource_key(identity) if registered else None
                candidates.append({'pid': process['pid'], 'starttime_ticks': process['starttime_ticks'],
                    'resource_ref': key, 'registration_status': 'registered' if registered else 'identity_race',
                    'process_identity_evidence': 'proc_scan_stat_recheck',
                    'relation_evidence': 'operator_declared_selector_match'})
                if key:
                    window = self.references[name].setdefault(key, {'first_observed_ns': now,
                        'last_observed_ns': now, 'observations': 0})
                    window.update(last_observed_ns=now, observations=window['observations'] + 1)
            current = {item['resource_ref'] for item in candidates if item['resource_ref']}
            previous = {item['resource_ref'] for item in self.latest.get(name, {}).get('candidates', [])
                        if item['resource_ref']}
            roles.append({'function_id': name, 'status': state, 'matching_count': count,
                'expected_processes': expected, 'scope_complete': complete,
                'omitted_by_target_cap': count - len(candidates), 'candidates': candidates,
                'expired_resource_refs': sorted(previous - current),
                'ros_nodes': role['ros_nodes'], 'ros_node_evidence': 'operator_declared_not_verified'})
        # This is a contradiction in declarations, not runtime ROS duplicate detection.
        node_pids, node_functions = {}, {}
        for role in self.workload['functions']:
            identities = {(item['pid'], item['starttime_ticks'])
                          for item in decision['workload_matches'][role['id']]}
            for node in role['ros_nodes']:
                node_pids.setdefault(node, set()).update(identities)
                node_functions.setdefault(node, set()).add(role['id'])
        for row in roles:
            conflicts = [node for node in row['ros_nodes']
                         if len(node_functions[node]) > 1 and len(node_pids[node]) > 1]
            if conflicts:
                row.update(status='conflict', conflicting_declared_nodes=conflicts,
                           conflict_scope='full_function_matches_before_target_cap')
            self.latest[row['function_id']] = row
            counts = self.counts[row['function_id']]
            counts[row['status']] = counts.get(row['status'], 0) + 1
            quality = self.quality[row['function_id']]
            quality['incomplete_scope_scans'] += not row['scope_complete']
            quality['target_cap_scans'] += row['omitted_by_target_cap'] > 0
            quality['registration_race_scans'] += any(item['resource_ref'] is None for item in row['candidates'])
        self.scans += 1
        return {'schema_version': 1, 'event': 'business_relation_scan',
            'workload_id': self.workload['workload_id'], 'workload_version': self.workload['workload_version'],
            'ros_domain_id': self.workload['ros_domain_id'], 'pid_namespace': self.namespace,
            'read_window': {'start_ns': scan_start, 'end_ns': now}, 'observation_ns': now,
            'validity': 'point_observations_only; identity and scope may change between scans',
            'functions': roles}

    def summary(self, resources):
        entities = resources.get('registered_entities') or {}
        functions = []
        for role in self.workload['functions']:
            name = role['id']
            refs = self.references[name]
            functions.append({'function_id': name, 'ros_nodes': role['ros_nodes'],
                'ros_node_evidence': 'operator_declared_not_verified',
                'last_scan': self.latest.get(name), 'scan_status_counts': self.counts[name],
                'quality_counts': self.quality[name],
                'resource_refs': sorted(refs), 'reference_observations': refs,
                'resource_reference_status': {key: {'entity_recorded': key in entities,
                    'reason': None if key in entities else 'no valid process resource sample for this registration'}
                    for key in sorted(refs)},
                'unavailable_resource_refs': [key for key in sorted(refs) if key not in entities]})
        return {'format_version': 1, 'workload_id': self.workload['workload_id'],
            'workload_version': self.workload['workload_version'], 'ros_domain_id': self.workload['ros_domain_id'],
            'pid_namespace': self.namespace, 'scans': self.scans, 'functions': functions,
            'unique_resource_refs': sorted({key for refs in self.references.values() for key in refs}),
            'resource_table': 'monitor-summary.json:resources.registered_entities',
            'metric_scope': 'whole_resource_registration; not aggregated over function relation windows',
            'relation_evidence': 'operator_declared_selectors; no ROS graph, callback or trace verification',
            'business_acceptance': 'not_evaluated'}


def write_business_report(output, monitor_summary):
    business = monitor_summary['workload']
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    lines = ['# 业务功能与进程关系', '', '采集状态：' + monitor_summary['status'],
        '业务代号：' + cell(business['workload_id']), '',
        '节点与功能归属来自人工声明；进程身份及选择条件按扫描核对，不证明节点真实运行。',
        '资源只保存在 monitor-summary.json 的 registered_entities；这里引用，不复制求和或均分。',
        '资源指标覆盖整段注册窗口，不能直接作为功能关系子窗口的独立 CPU/RSS。', '',
        '| 功能 | 最后扫描状态 | 匹配数/期望数 | 范围完整 | ROS 节点声明 | 资源引用 |',
        '| --- | --- | --- | --- | --- | --- |']
    for role in business['functions']:
        last = role['last_scan'] or {}
        lines.append('| {} | {} | {}/{} | {} | {} | {} |'.format(cell(role['function_id']),
            last.get('status', 'not_observed'), last.get('matching_count', 0),
            last.get('expected_processes', 'unknown'), last.get('scope_complete', 'unknown'),
            cell(', '.join(role['ros_nodes'])),
            cell(', '.join(role['resource_refs']))))
    lines += ['', '每个扫描记录保留读取窗口、命名空间、候选、截断/注册竞态及过期引用。',
        '零匹配不表示没有业务；范围不可读或不完整需复核。旧引用保留为历史，PID重用不继承。',
        '多个功能重复声明同一节点标签且完整匹配身份并集大于一时标 conflict，不推断运行时ROS冲突。',
        '业务链路时延、回调耗时与业务deadline尚未实现；采集成本预算沿用monitor，默认未配置。', '',
        '详情见 business-relations.jsonl、workload-profile.json 与 monitor-summary.json.workload。']
    (output / 'BUSINESS_MAP_REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
