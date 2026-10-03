"""Discover and observe existing Linux processes without owning their lifecycle."""
import argparse
import copy
import hashlib
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
from .platform_probe import collect_profile
from .resources import ResourceSampler, summarize_resources


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


def defaults():
    return {'format_version': 1, 'duration_seconds': 60,
            'resource_sampling_seconds': .5, 'discovery_interval_seconds': 1,
            'uids': [os.getuid()], 'include_names': [], 'exclude_names': [],
            'cgroup_patterns': [], 'pids': [], 'active_cpu_percent': 1,
            'max_targets': 64, 'require_jetson': False}


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('monitor configuration must be an object')
    resolved = defaults()
    if set(config) - resolved.keys():
        raise ValueError('unknown monitor configuration fields: ' + ', '.join(sorted(set(config) - resolved.keys())))
    resolved.update(copy.deepcopy(config))
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


def _source_record():
    root = Path(__file__).resolve().parent.parent
    try:
        revision = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                  capture_output=True, text=True, timeout=5)
        revision = revision.stdout.strip() if revision.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        revision = None
    return {'git_revision': os.environ.get('EP_SOURCE_REVISION') or revision,
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
             '按 CPU 活动发现的是候选对象，名称规则和 PID 指定也会记入筛选依据。',
             '完整资源、线程与发现记录见 monitor-summary.json、resources.jsonl、discovery.jsonl。', '',
             '| PID | 进程名 | CPU %（单核=100） | RSS 采样峰值（bytes） |',
             '| ---: | --- | ---: | ---: |']
    for item in processes:
        lines.append('| {} | {} | {} | {} |'.format(item['pid'], _cell(item.get('comm')),
                     item['cpu_percent_one_core'], item['rss_peak_bytes']))
    lines += ['', '## 采集质量', '', '```json',
              json.dumps(summary['quality'], ensure_ascii=False, indent=2), '```', '',
              '## 解释边界', '']
    lines.extend('- ' + limit for limit in summary['limits'])
    (output / 'MONITOR_REPORT.md').write_text('\n'.join(lines) + '\n')


def run_monitor(config, output, proc_root=Path('/proc')):
    config = validate_config(config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    status = {'status': 'running', 'error': None, 'scans': 0, 'registrations': 0}
    sampler = None
    start = end = None
    interrupted = None
    peak_selected = omitted_scans = skipped_process_reads = 0
    selected = {}
    summary = None
    try:
        _json(output / 'monitor-config.json', config)
        _json(output / 'monitor-status.json', status)
        profile = collect_profile(include_kernel_command_line=False)
        _json(output / 'environment.json', {'host_profile': profile, 'source': _source_record(),
              'pid_namespace': os.readlink(proc_root / 'self/ns/pid') if (proc_root / 'self/ns/pid').exists() else None,
              'observer_pid': os.getpid(), 'observer_uid': os.getuid(), 'limits': LIMITS})
        if config['require_jetson'] and not profile['checks']['jetson_detected']:
            raise RuntimeError('Expected native Linux ARM64 Jetson; inspect environment.json')
        selector = DiscoverySelector(config, os.sysconf('SC_CLK_TCK'))
        sampler = ResourceSampler(output / 'resources.jsonl', config['resource_sampling_seconds'],
                                  window_source='monitor_monotonic_timestamps')
        sampler.set_window('business-monitor', None, 'observing')
        with (output / 'discovery.jsonl').open('x') as stream:
            try:
                with sampler:
                    start = time.monotonic_ns()
                    planned_end = start + round(config['duration_seconds'] * 1e9)
                    while time.monotonic_ns() < planned_end:
                        if sampler.error:
                            raise RuntimeError('resource sampling failed: ' + sampler.error)
                        scan_start = time.monotonic_ns()
                        inventory = scan_processes(proc_root, exclude_pids=(os.getpid(),))
                        now = time.monotonic_ns()
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
                        stream.write(json.dumps({'monotonic_ns': now, 'scan_start_ns': scan_start,
                            'scan': inventory['scan'], 'selection': decision['selection'],
                            'targets': decision['targets'], 'registered_pids': sorted(selected),
                            'changes': changes}, ensure_ascii=False, allow_nan=False) + '\n')
                        stream.flush()
                        remaining = min(config['discovery_interval_seconds'],
                                        (planned_end - time.monotonic_ns()) / 1e9)
                        if remaining > 0:
                            time.sleep(remaining)
                    end = time.monotonic_ns()
            except KeyboardInterrupt as exc:
                interrupted = exc
                end = time.monotonic_ns()
        if sampler.error:
            raise RuntimeError('resource sampling failed: ' + sampler.error)
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
            if resources['coverage']['samples'] < 2:
                warnings.append('Insufficient complete resource snapshots; cumulative metrics unavailable.')
            summary = {'schema_version': 1, 'kind': 'external_process_monitor', 'status': status['status'],
                       'window_start_ns': start, 'window_end_ns': end, 'config': config,
                       'quality': {'status': 'review_required' if warnings else 'observed',
                                   'scans': status['scans'], 'registrations': status['registrations'],
                                   'peak_registered_targets': peak_selected, 'scans_with_omitted_candidates': omitted_scans,
                                   'skipped_process_reads': skipped_process_reads, 'warnings': warnings,
                                   'business_acceptance': 'not_evaluated'},
                       'resources': resources, 'limits': LIMITS}
            _json(output / 'monitor-summary.json', summary)
            write_monitor_report(output, summary)
        if interrupted:
            raise interrupted
        return summary
    except BaseException as exc:
        status.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                      error=str(exc), error_type=type(exc).__name__)
        if isinstance(exc, KeyboardInterrupt) and summary is not None:
            with defer_interrupts():
                summary['status'] = 'interrupted'
                _json(output / 'monitor-summary.json', summary)
                write_monitor_report(output, summary)
        raise
    finally:
        status['resource_sampler_error'] = sampler.error if sampler is not None else None
        status['window_start_ns'], status['window_end_ns'] = start, end
        try:
            with defer_interrupts():
                _json(output / 'monitor-status.json', status)
        except KeyboardInterrupt as exc:
            status.update(status='interrupted', error=str(exc), error_type='KeyboardInterrupt')
            with defer_interrupts():
                if summary is not None:
                    summary['status'] = 'interrupted'
                    _json(output / 'monitor-summary.json', summary)
                    write_monitor_report(output, summary)
                _json(output / 'monitor-status.json', status)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--interval', type=float)
    parser.add_argument('--discovery-interval', type=float)
    parser.add_argument('--active-cpu-percent', type=float)
    parser.add_argument('--max-targets', type=int)
    parser.add_argument('--all-users', action='store_true')
    parser.add_argument('--require-jetson', action='store_true')
    for option in ('include-name', 'exclude-name', 'cgroup-pattern'):
        parser.add_argument('--' + option, action='append')
    parser.add_argument('--pid', type=int, action='append')
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('Business monitoring requires Linux procfs; run on the target host')
    config = json.loads(args.config.read_text()) if args.config else {}
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
    def terminate(signum, frame):
        raise KeyboardInterrupt('monitor interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, terminate)
    try:
        run_monitor(config, args.output)
    except KeyboardInterrupt:
        raise SystemExit(130)
    print('Monitor report:', args.output / 'MONITOR_REPORT.md')


if __name__ == '__main__':
    main()
