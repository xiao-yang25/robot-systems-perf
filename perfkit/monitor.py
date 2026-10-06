"""Discover and observe existing Linux processes without owning their lifecycle."""
import argparse
import copy
from contextlib import ExitStack, contextmanager
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import time

from .discovery import DiscoverySelector, scan_processes
from .lifecycle import defer_interrupts
from .acceptance import resource_state
from .platform_probe import collect_profile
from .resources import ResourceSampler, summarize_resources, validate_resource_options, _Costs
from .workload import BusinessRelations, WorkloadSelector, validate_workload, write_business_report


LIMITS = [
    'CPU activity identifies candidates, not algorithm semantics; inspect selection reasons.',
    'Only processes visible and readable in this PID namespace can be observed.',
    'No business processes are started, stopped, signalled or reconfigured.',
    'Resource counters do not measure message latency, callback duration or business deadlines.',
    'CPU-idle GPU workers may be missed; use name/PID selectors, optionally restricted by cgroup.',
    'Short-lived processes and threads between scans/samples may be missed.',
    'Sampling overhead is uncalibrated; inspect coverage and compare against a no-monitor run.',
    'Process command-line arguments, environment variables and process memory are not collected; kernel command line is omitted.',
]


def profile_config(name):
    """Explicit coverage presets; budgets require the operator's own limits."""
    if name not in ('light', 'full'):
        raise ValueError('unknown business sampling profile')
    light = name == 'light'
    period = 1 if light else .5
    return {'format_version': 1, 'duration_seconds': 60,
            'resource_sampling_seconds': period,
            'discovery_interval_seconds': 2 if light else 1,
            'include_names': [], 'exclude_names': [], 'cgroup_patterns': [],
            'cgroup_prefilter': False, 'pids': [], 'active_cpu_percent': 1,
            'max_targets': 256, 'require_jetson': False,
            'resource_options': {'system_sampling_seconds': period,
                'process_sampling_seconds': period, 'thread_sampling_seconds': period,
                'collect_threads': not light, 'thread_names': [], 'thread_ids': [],
                'skip_temperatures': True, 'jetson_telemetry': False,
                'jetson_sampling_seconds': 1, 'max_cycle_fraction': None,
                'max_observer_cpu_percent_one_core': None}}


def defaults():
    return {'format_version': 1, 'duration_seconds': 60,
            'resource_sampling_seconds': .5, 'discovery_interval_seconds': 1,
            'uids': [os.getuid()], 'include_names': [], 'exclude_names': [],
            'cgroup_patterns': [], 'cgroup_prefilter': False, 'pids': [], 'active_cpu_percent': 1,
            'max_targets': 64, 'require_jetson': False, 'resource_options': {}}


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('monitor configuration must be an object')
    resolved = defaults()
    if set(config) - resolved.keys():
        raise ValueError('unknown monitor configuration fields: ' + ', '.join(sorted(set(config) - resolved.keys())))
    resolved.update(copy.deepcopy(config))
    if not isinstance(resolved['resource_options'], dict):
        raise ValueError('resource_options must be an object')
    validate_resource_options(resolved['resource_options'])
    if resolved['format_version'] != 1 or isinstance(resolved['format_version'], bool):
        raise ValueError('unsupported monitor configuration version')
    for key, low, high in [('duration_seconds', 1, 86400),
                           ('resource_sampling_seconds', .1, 60),
                           ('discovery_interval_seconds', .1, 60),
                           ('active_cpu_percent', 0, 100000), ('max_targets', 1, 256)]:
        value = resolved[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(key + ': finite value within supported range required')
    if not isinstance(resolved['max_targets'], int):
        raise ValueError('max_targets must be an integer')
    if not isinstance(resolved['require_jetson'], bool):
        raise ValueError('require_jetson must be boolean')
    if not isinstance(resolved['cgroup_prefilter'], bool) or (resolved['cgroup_prefilter'] and not resolved['cgroup_patterns']):
        raise ValueError('cgroup_prefilter requires a boolean and nonempty cgroup_patterns')
    for key in ('uids', 'pids'):
        values = resolved[key]
        if key == 'uids' and values is None:
            continue
        if not isinstance(values, list) or any(isinstance(v, bool) or not isinstance(v, int) or v < (1 if key == 'pids' else 0) for v in values):
            raise ValueError(key + ': list of valid integers required')
    for key in ('include_names', 'exclude_names', 'cgroup_patterns'):
        values = resolved[key]
        if not isinstance(values, list) or len(values) > 64 or any(not isinstance(v, str) or not v or len(v) > 512 for v in values):
            raise ValueError(key + ': bounded list of nonempty patterns required')
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(key + ': invalid regular expression') from exc
    if resolved['active_cpu_percent'] == 0 and not resolved['include_names'] and not resolved['pids']:
        raise ValueError('activity disabled: provide include_names or pids to select targets')
    return resolved


def _source_record(*, query_git=True):
    root = Path(__file__).resolve().parent.parent
    revision = None
    if query_git and (root / '.git').exists():
        try:
            result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True, timeout=5)
            revision = result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        package_version = metadata.version('robot-systems-perf')
    except metadata.PackageNotFoundError:
        package_version = None
    return {'git_revision': os.environ.get('EP_SOURCE_REVISION') or revision,
            'package_version': package_version,
            'sha256': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted((root / 'perfkit').glob('*.py'))}}


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def _cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def write_monitor_report(output, summary):
    entities = summary['resources']['registered_entities'] or {}
    processes = sorted((item for item in entities.values() if item['kind'] == 'process'),
                       key=lambda item: -(item['cpu_percent_one_core'] or 0))
    lines = ['# 业务进程资源采集报告', '',
             '采集状态：' + summary['status'], '',
             ('业务清单按功能筛选候选，人工声明不证明算法或ROS节点归属。' if 'workload' in summary else
              '按 CPU 活动发现的是候选对象，名称规则和 PID 指定也会记入筛选依据。'),
             '完整资源、线程与发现记录见 monitor-summary.json、resources.jsonl、discovery.jsonl。', '',
             '| PID | 进程名 | CPU %（单核=100） | RSS 采样峰值（bytes） |',
             '| ---: | --- | ---: | ---: |']
    for item in processes:
        lines.append('| {} | {} | {} | {} |'.format(item['pid'], _cell(item.get('comm')),
                     item['cpu_percent_one_core'], item['rss_peak_bytes']))
    lines += ['', '## 采集范围与观测开销', '', '```json',
              json.dumps({key: summary['resources'].get(key) for key in
                          ('source_coverage', 'observer', 'collection_cost',
                           'overhead_budget', 'thread_scope', 'jetson_telemetry')},
                         ensure_ascii=False, indent=2), '```', '',
              '开销预算仅评价已观测采集成本；业务受扰动仍需无采集对照。', '',
              '## 采集质量', '', '```json',
              json.dumps(summary['quality'], ensure_ascii=False, indent=2), '```', '',
              '## 分项验收', '', '```json',
              json.dumps(summary.get('acceptance', {}), ensure_ascii=False, indent=2), '```', '',
              '## 解释边界', '']
    lines.extend('- ' + limit for limit in summary['limits'])
    (output / 'MONITOR_REPORT.md').write_text('\n'.join(lines) + '\n')
    if 'workload' in summary:
        write_business_report(output, summary)


def _observation_context(proc_root):
    """Identity/time domain for later imports; absent facts never become claims."""
    reasons = {}
    try:
        boot_id = (proc_root / 'sys/kernel/random/boot_id').read_text().strip()
        if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', boot_id):
            raise ValueError('invalid boot identifier')
    except (OSError, ValueError) as error:
        boot_id = None
        reasons['boot_id'] = type(error).__name__
    try:
        namespace = os.readlink(proc_root / 'self/ns/pid')
    except OSError as error:
        namespace = None
        reasons['pid_namespace'] = type(error).__name__
    return {'boot_id': boot_id, 'pid_namespace': namespace,
            'clock': 'linux_monotonic', 'missing_reasons': reasons}


def _cleanup_error(status, stage, error):
    status.setdefault('cleanup_errors', []).append(
        {'stage': stage, 'error_type': type(error).__name__, 'error': str(error)})


@contextmanager
def _sampler_window(sampler, status):
    primary = None
    try:
        with sampler:
            try:
                yield
            except BaseException as exc:
                primary = exc
                raise
    except BaseException as exc:
        if primary is not None and exc is not primary:
            _cleanup_error(status, 'resource_sampler_close', exc)
            raise primary from exc
        raise


def run_monitor(config, output, proc_root=Path('/proc'), *, workload=None):
    config = validate_config(config)
    if workload is not None:
        workload = validate_workload(workload)
        selector = WorkloadSelector(config, workload, os.sysconf('SC_CLK_TCK'))
    else:
        selector = DiscoverySelector(config, os.sysconf('SC_CLK_TCK'))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    status = {'status': 'running', 'error': None, 'scans': 0, 'registrations': 0}
    sampler = None
    start = end = None
    interrupted = None
    observation_error = primary_error = None
    peak_selected = omitted_scans = skipped_process_reads = 0
    discovery_duration_sum = discovery_duration_max = discovery_overruns = 0
    discovery_cpu_sum = discovery_cpu_max = 0
    discovery_phases, discovery_cpu_phases = _Costs(), _Costs()
    discovery_gap_sum = discovery_gap_count = discovery_gap_max = 0
    discovery_gap_min = previous_scan_start = None
    skipped_discovery_deadlines = 0
    selected = {}
    summary = None
    relations = None
    try:
        _json(output / 'monitor-config.json', config)
        _json(output / 'monitor-status.json', status)
        profile_kwargs = {'include_kernel_command_line': False}
        if workload is not None:
            profile_kwargs['skip_temperature'] = config['resource_options'].get('skip_temperatures', False)
        profile = collect_profile(**profile_kwargs)
        namespace = os.readlink(proc_root / 'self/ns/pid') if (proc_root / 'self/ns/pid').exists() else None
        _json(output / 'environment.json', {'host_profile': profile, 'source': _source_record(),
              'pid_namespace': namespace, 'observation_context': _observation_context(proc_root),
              'observer_pid': os.getpid(), 'observer_uid': os.getuid(), 'limits': LIMITS})
        if config['require_jetson'] and not profile['checks']['jetson_detected']:
            raise RuntimeError('Expected native Linux ARM64 Jetson; inspect environment.json')
        if workload is not None:
            _json(output / 'workload-profile.json', {'format_version': 1, 'workload': workload,
                'workload_sha256': hashlib.sha256(json.dumps(workload, sort_keys=True,
                    ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
                'pid_namespace': namespace, 'monitor_scope': config,
                'discovery_mode': selector.discovery_mode,
                'selection_mode': 'function_selectors_only; global hard filters intersect each function',
                'limitations': ['Node/domain/version labels are operator declarations, not ROS runtime verification.',
                                'Relations and resource registration IDs are local to this monitor run.']})
            relations = BusinessRelations(workload, namespace)
        sampler_kwargs = {'window_source': 'monitor_monotonic_timestamps'}
        if config['resource_options']:
            sampler_kwargs['options'] = config['resource_options']
        sampler = ResourceSampler(output / 'resources.jsonl', config['resource_sampling_seconds'],
                                  **sampler_kwargs)
        sampler.set_window('business-monitor', None, 'observing')
        try:
            with ExitStack() as files:
                stream = files.enter_context((output / 'discovery.jsonl').open('x'))
                relation_stream = files.enter_context((output / 'business-relations.jsonl').open('x')) if relations else None
                try:
                    with _sampler_window(sampler, status):
                        start = time.monotonic_ns()
                        planned_end = start + round(config['duration_seconds'] * 1e9)
                        discovery_interval_ns = round(config['discovery_interval_seconds'] * 1e9)
                        next_scan_ns = start
                        while time.monotonic_ns() < planned_end:
                            if sampler.error:
                                raise RuntimeError('resource sampling failed: ' + sampler.error)
                            scan_start = time.monotonic_ns()
                            if scan_start >= planned_end:
                                break
                            if scan_start < next_scan_ns:
                                time.sleep((min(next_scan_ns, planned_end) - scan_start) / 1e9)
                                continue
                            if previous_scan_start is not None:
                                gap = scan_start - previous_scan_start
                                discovery_gap_sum += gap
                                discovery_gap_count += 1
                                discovery_gap_max = max(discovery_gap_max, gap)
                                discovery_gap_min = gap if discovery_gap_min is None else min(discovery_gap_min, gap)
                            previous_scan_start = scan_start
                            scan_cpu_start = time.thread_time_ns()
                            observer_children = getattr(sampler, 'owned_process_ids', lambda: ())()
                            inventory = scan_processes(proc_root, exclude_pids=(os.getpid(),) + observer_children,
                                                       _scope=selector)
                            now = time.monotonic_ns()
                            read_cpu_end = time.thread_time_ns()
                            decision = selector.update(inventory['processes'], now)
                            targets = {item['pid']: item for item in decision['targets']}
                            changes = []
                            for pid, old in list(selected.items()):
                                new = targets.get(pid)
                                if new is None or new['starttime_ticks'] != old['starttime_ticks']:
                                    sampler.unregister(pid)
                                    selected.pop(pid)
                                    changes.append({'event': 'unregistered', 'pid': pid,
                                                    'starttime_ticks': old['starttime_ticks']})
                            for pid, item in targets.items():
                                if pid not in selected:
                                    registered = sampler.register(pid, 'business-candidate',
                                                          expected_starttime_ticks=item['starttime_ticks'])
                                    changes.append({'event': 'registered' if registered else 'registration_identity_race',
                                                    'pid': pid, 'starttime_ticks': item['starttime_ticks']})
                                    if registered:
                                        selected[pid] = item
                                        status['registrations'] += 1
                            status['scans'] += 1
                            peak_selected = max(peak_selected, len(selected))
                            omitted_scans += decision['selection']['omitted_count'] > 0
                            skipped_process_reads += inventory['scan'].get('skipped_count', 0)
                            if relations:
                                registrations = {pid: sampler.registered_identity(pid) for pid in selected}
                                with defer_interrupts():
                                    record = relations.observe(decision, registrations, inventory,
                                                               scan_start, time.monotonic_ns())
                                    relation_payload = json.dumps(record, ensure_ascii=False, allow_nan=False,
                                                                  separators=(',', ':')) + '\n'
                                    relation_stream.write(relation_payload)
                                    relation_stream.flush()
                            selection_end, selection_cpu_end = time.monotonic_ns(), time.thread_time_ns()
                            payload = json.dumps({'monotonic_ns': now, 'scan_start_ns': scan_start,
                                'scan': inventory['scan'], 'selection': decision['selection'],
                                'targets': decision['targets'], 'registered_pids': sorted(selected),
                                'changes': changes,
                                'read_selection_cost': {'read_ns': now - scan_start,
                                    'selection_registration_ns': selection_end - now,
                                    'read_thread_cpu_ns': read_cpu_end - scan_cpu_start,
                                    'selection_registration_thread_cpu_ns': selection_cpu_end - read_cpu_end}},
                                ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
                            encode_end, encode_cpu_end = time.monotonic_ns(), time.thread_time_ns()
                            stream.write(payload)
                            stream.flush()
                            scan_finished = time.monotonic_ns()
                            scan_cpu_finished = time.thread_time_ns()
                            for name, wall, cpu in (
                                    ('read', now - scan_start, read_cpu_end - scan_cpu_start),
                                    ('selection_registration', selection_end - now, selection_cpu_end - read_cpu_end),
                                    ('encode', encode_end - selection_end, encode_cpu_end - selection_cpu_end),
                                    ('write_flush', scan_finished - encode_end, scan_cpu_finished - encode_cpu_end)):
                                discovery_phases.add(name, wall)
                                discovery_cpu_phases.add(name, cpu)
                            cpu_duration = scan_cpu_finished - scan_cpu_start
                            discovery_cpu_sum += cpu_duration
                            discovery_cpu_max = max(discovery_cpu_max, cpu_duration)
                            duration = scan_finished - scan_start
                            discovery_duration_sum += duration
                            discovery_duration_max = max(discovery_duration_max, duration)
                            discovery_overruns += duration > discovery_interval_ns
                            next_scan_ns += discovery_interval_ns
                            if next_scan_ns <= scan_finished:
                                missed = (scan_finished - next_scan_ns) // discovery_interval_ns + 1
                                skipped_discovery_deadlines += missed
                                next_scan_ns += missed * discovery_interval_ns
                        end = time.monotonic_ns()
                    if sampler.error:
                        raise RuntimeError('resource sampling failed: ' + sampler.error)
                except KeyboardInterrupt as exc:
                    interrupted = exc
                except BaseException as exc:
                    observation_error = exc
                finally:
                    if end is None:
                        end = time.monotonic_ns()
                    if relations:
                        try:
                            with defer_interrupts():
                                outcome = 'failed' if observation_error else 'interrupted' if interrupted else 'closed'
                                reason = ('monitor failed: ' + type(observation_error).__name__ if observation_error else
                                          'monitor interrupted: KeyboardInterrupt' if interrupted else
                                          'monitor window closed; no continuing validity asserted')
                                relation_stream.write(json.dumps({'schema_version': 1, 'event': 'observation_ended',
                                    'monotonic_ns': end, 'outcome': outcome, 'reason': reason},
                                    separators=(',', ':')) + '\n')
                                relation_stream.flush()
                        except BaseException as exc:
                            _cleanup_error(status, 'relation_window_close', exc)
                            if observation_error is None and interrupted is None:
                                observation_error = exc
                                raise
        except BaseException as exc:
            if observation_error is not None and exc is not observation_error:
                _cleanup_error(status, 'observation_stream_close', exc)
                raise observation_error from exc
            if interrupted is not None and exc is not interrupted:
                _cleanup_error(status, 'observation_stream_close', exc)
                raise interrupted from exc
            raise
        if observation_error is not None:
            raise observation_error
        # Finish reading and writing evidence before responding to late signals.
        # A pending cancellation is reconciled by the outer handler below.
        with defer_interrupts():
            resources = summarize_resources(output / 'resources.jsonl', start, end)
            resources['scope'] = 'observed_subintervals_inside_monitor_window; no_boundary_extrapolation'
            status['status'] = 'interrupted' if interrupted else 'complete'
            warnings = []
            if not status['registrations']:
                warnings.append('No target was registered; check scope, visibility, selectors and activity threshold.')
            if omitted_scans:
                warnings.append('Target cap excluded candidates; inspect selection omitted_count.')
            if skipped_process_reads:
                warnings.append('Some proc entries vanished or were unreadable; inspect per-scan reasons.')
            source_coverage = resources.get('source_coverage', {})
            for source in ('system', 'process'):
                samples = source_coverage.get(source, {}).get('samples', 0)
                if samples < 2:
                    warnings.append('Insufficient ' + source + ' samples; cumulative metrics unavailable.')
            if resources.get('thread_scope', {}).get('enabled', config['resource_options'].get('collect_threads', True)):
                thread_samples = source_coverage.get('thread', {}).get('samples', 0)
                if thread_samples < 2:
                    warnings.append('Insufficient thread samples; cumulative metrics unavailable.')
            measured_processes = [item for item in (resources.get('registered_entities') or {}).values()
                                  if item.get('kind') == 'process' and item.get('cpu_percent_one_core') is not None]
            if status['registrations'] and not measured_processes:
                warnings.append('No registered process has a valid CPU interval; inspect identity, permissions and coverage.')
            if config['resource_options'].get('jetson_telemetry') and not resources.get('jetson_telemetry', {}).get('available'):
                warnings.append('Requested Jetson telemetry has no usable sample; inspect source availability.')
            if discovery_overruns:
                warnings.append('Discovery scans exceeded the configured period; missed deadlines were skipped.')
            if resources.get('collection_cost', {}).get('over_period_cycles', 0):
                warnings.append('Resource collection cycles exceeded the configured period; inspect collection_cost.')
            budget_configured = any(config['resource_options'].get(key) is not None for key in
                                    ('max_cycle_fraction', 'max_observer_cpu_percent_one_core'))
            budget_status = resources.get('overhead_budget', {}).get('status', 'not_evaluated')
            if budget_configured and budget_status in ('exceeded', 'not_evaluated'):
                warnings.append('Configured observer overhead budget ' + budget_status + '; inspect overhead_budget.')
            summary = {'schema_version': 1, 'kind': 'external_process_monitor', 'status': status['status'],
                       'window_start_ns': start, 'window_end_ns': end, 'config': config,
                       'quality': {'status': 'review_required' if warnings else 'observed',
                                   'scans': status['scans'], 'registrations': status['registrations'],
                                   'peak_registered_targets': peak_selected, 'scans_with_omitted_candidates': omitted_scans,
                                   'skipped_process_reads': skipped_process_reads, 'warnings': warnings,
                                   'discovery': {'duration_ns': {'count': status['scans'],
                                       'mean_ns': discovery_duration_sum / status['scans'] if status['scans'] else None,
                                       'max_ns': discovery_duration_max if status['scans'] else None},
                                       'thread_cpu_ns': {'count': status['scans'],
                                           'mean_ns': discovery_cpu_sum / status['scans'] if status['scans'] else None,
                                           'max_ns': discovery_cpu_max if status['scans'] else None},
                                       'phase_costs_ns': discovery_phases.result(),
                                       'phase_thread_cpu_ns': discovery_cpu_phases.result(),
                                       'scan_start_interval_ns': {'count': discovery_gap_count,
                                           'mean_ns': discovery_gap_sum / discovery_gap_count if discovery_gap_count else None,
                                           'min_ns': discovery_gap_min,
                                           'max_ns': discovery_gap_max if discovery_gap_count else None},
                                       'over_period_scans': discovery_overruns,
                                       'skipped_deadlines': skipped_discovery_deadlines,
                                       'scheduling': 'absolute_deadlines_without_catch_up',
                                       'duration_scope': 'proc_scan_selection_registration_and_discovery_log_write'},
                                   'business_acceptance': 'not_evaluated'},
                       'resources': resources,
                       'acceptance': {'resources': resource_state(resources),
                           'collector_budget': resources['overhead_budget'],
                           'business_chain': {'status': 'not_evaluated',
                               'reason': 'no correlated business events, input evidence or application deadline'}},
                       'limits': LIMITS}
            if relations:
                summary['quality']['discovery']['duration_scope'] += '_including_workload_relations_when_enabled'
                summary['workload'] = relations.summary(resources)
                if any(row.get('last_scan') is None or
                       row['last_scan']['status'] != 'candidate' or
                       not row['last_scan']['scope_complete'] or
                       any(state != 'candidate' and count for state, count in row['scan_status_counts'].items()) or
                       any(row['quality_counts'].values()) or
                       row['last_scan']['omitted_by_target_cap'] or
                       any(item['resource_ref'] is None for item in row['last_scan']['candidates']) or
                       row['unavailable_resource_refs'] for row in summary['workload']['functions']):
                    summary['quality']['status'] = 'review_required'
                    summary['quality']['warnings'].append('Business relations need review; inspect workload statuses, scope and resource references.')
            _json(output / 'monitor-summary.json', summary)
            write_monitor_report(output, summary)
        if interrupted:
            raise interrupted
        return summary
    except BaseException as exc:
        if interrupted is not None and exc is not interrupted:
            _cleanup_error(status, 'interrupted_summary_or_report', exc)
            primary_error = interrupted
        else:
            primary_error = exc
        status.update(status='interrupted' if isinstance(primary_error, KeyboardInterrupt) else 'failed',
                      error=str(primary_error), error_type=type(primary_error).__name__)
        if isinstance(primary_error, KeyboardInterrupt) and summary is not None and summary['status'] != 'interrupted':
            try:
                with defer_interrupts():
                    summary['status'] = 'interrupted'
                    _json(output / 'monitor-summary.json', summary)
                    write_monitor_report(output, summary)
            except BaseException as cleanup:
                _cleanup_error(status, 'interrupted_report_write', cleanup)
        if primary_error is not exc:
            raise primary_error from exc
        raise
    finally:
        if end is None:
            end = time.monotonic_ns()
        status['resource_sampler_error'] = sampler.error if sampler is not None else None
        status['window_start_ns'], status['window_end_ns'] = start, end
        try:
            with defer_interrupts():
                _json(output / 'monitor-status.json', status)
        except KeyboardInterrupt as exc:
            if primary_error is not None:
                _cleanup_error(status, 'final_status_write', exc)
                try:
                    with defer_interrupts():
                        _json(output / 'monitor-status.json', status)
                except BaseException as cleanup:
                    _cleanup_error(status, 'final_status_retry', cleanup)
            else:
                status.update(status='interrupted', error=str(exc), error_type='KeyboardInterrupt')
                try:
                    with defer_interrupts():
                        if summary is not None:
                            try:
                                summary['status'] = 'interrupted'
                                _json(output / 'monitor-summary.json', summary)
                                write_monitor_report(output, summary)
                            except BaseException as cleanup:
                                _cleanup_error(status, 'interrupted_summary_or_report_write', cleanup)
                        # Save authoritative state even if an optional artifact
                        # could not be rewritten after the deferred signal.
                        _json(output / 'monitor-status.json', status)
                except BaseException as cleanup:
                    _cleanup_error(status, 'interrupted_status_write', cleanup)
                raise
        except BaseException as exc:
            if primary_error is None:
                raise
            _cleanup_error(status, 'final_status_write', exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--workload', type=Path, help='operator-declared functions and per-function process selectors')
    parser.add_argument('--profile', choices=('light', 'full'),
                        help='coverage preset; config and CLI override it; budgets remain unset')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--interval', type=float)
    parser.add_argument('--discovery-interval', type=float)
    parser.add_argument('--active-cpu-percent', type=float)
    parser.add_argument('--max-targets', type=int)
    parser.add_argument('--all-users', action='store_true')
    parser.add_argument('--require-jetson', action='store_true')
    for option in ('system-interval', 'process-interval', 'thread-interval',
                   'jetson-interval', 'max-cycle-fraction', 'max-observer-cpu-percent'):
        parser.add_argument('--' + option, type=float)
    parser.add_argument('--no-threads', action='store_true')
    parser.add_argument('--cgroup-prefilter', action='store_true',
                        help='check configured cgroup patterns before detailed proc reads; may cost more if most PIDs match')
    parser.add_argument('--thread-name', action='append')
    parser.add_argument('--tid', type=int, action='append')
    parser.add_argument('--skip-temperatures', action='store_true')
    parser.add_argument('--jetson-telemetry', action='store_true')
    for option in ('include-name', 'exclude-name', 'cgroup-pattern'):
        parser.add_argument('--' + option, action='append')
    parser.add_argument('--pid', type=int, action='append')
    args = parser.parse_args()
    if args.workload and (args.include_name or args.pid or args.active_cpu_percent is not None):
        parser.error('--workload uses per-function selectors; global name/PID/activity options are unsupported')
    if platform.system() != 'Linux':
        parser.error('Business monitoring requires Linux procfs; run on the target host')
    config = profile_config(args.profile) if args.profile else {}
    loaded = json.loads(args.config.read_text()) if args.config else {}
    if not isinstance(loaded, dict):
        parser.error('monitor configuration must be an object')
    profile_options = config.get('resource_options', {})
    config.update(loaded)
    if args.profile:
        loaded_options = loaded.get('resource_options', {})
        if not isinstance(loaded_options, dict):
            parser.error('resource_options must be an object')
        config['resource_options'] = dict(profile_options, **loaded_options)
    for option, field in [('seconds', 'duration_seconds'), ('interval', 'resource_sampling_seconds'),
                          ('discovery_interval', 'discovery_interval_seconds'),
                          ('active_cpu_percent', 'active_cpu_percent'), ('max_targets', 'max_targets')]:
        if getattr(args, option) is not None:
            config[field] = getattr(args, option)
    for option, field in [('include_name', 'include_names'), ('exclude_name', 'exclude_names'),
                          ('cgroup_pattern', 'cgroup_patterns'), ('pid', 'pids')]:
        if getattr(args, option):
            config[field] = config.get(field, []) + getattr(args, option)
    if args.all_users:
        config['uids'] = None
    if args.require_jetson:
        config['require_jetson'] = True
    if args.cgroup_prefilter:
        config['cgroup_prefilter'] = True
    options = copy.deepcopy(config.get('resource_options', {}))
    if not isinstance(options, dict):
        parser.error('resource_options must be an object')
    for option, field in [('system_interval', 'system_sampling_seconds'),
                          ('process_interval', 'process_sampling_seconds'),
                          ('thread_interval', 'thread_sampling_seconds'),
                          ('jetson_interval', 'jetson_sampling_seconds'),
                          ('max_cycle_fraction', 'max_cycle_fraction'),
                          ('max_observer_cpu_percent', 'max_observer_cpu_percent_one_core')]:
        value = getattr(args, option)
        if value is not None:
            options[field] = value
    for option, field in [('thread_name', 'thread_names'), ('tid', 'thread_ids')]:
        if getattr(args, option):
            previous = options.get(field, [])
            if not isinstance(previous, list):
                parser.error(field + ' must be a list')
            options[field] = previous + getattr(args, option)
    if args.no_threads:
        options['collect_threads'] = False
    if args.skip_temperatures:
        options['skip_temperatures'] = True
    if args.jetson_telemetry:
        options['jetson_telemetry'] = True
    if options:
        config['resource_options'] = options
    def terminate(signum, frame):
        raise KeyboardInterrupt('monitor interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.workload:
            workload = validate_workload(json.loads(args.workload.read_text(encoding='utf-8')))
            run_monitor(config, args.output, workload=workload)
        else:
            run_monitor(config, args.output)
    except KeyboardInterrupt:
        raise SystemExit(130)
    print('Monitor report:', args.output / 'MONITOR_REPORT.md')


if __name__ == '__main__':
    main()
