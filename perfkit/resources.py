"""Read-only Linux resource telemetry and streaming, sample-scoped summaries.

Window tags describe runner state, not the measurement boundary. The CSV's
monotonic timestamps define that boundary. Counters are differenced only across
consecutive samples inside it; no interpolation or extrapolation is performed.
"""

import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
from .lifecycle import defer_interrupts


PROC_ROOT = Path('/proc')
SYS_ROOT = Path('/sys')
CPU_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq',
              'steal', 'guest', 'guest_nice')

RESOURCE_OPTIONS = {
    'system_sampling_seconds': None, 'process_sampling_seconds': None,
    'thread_sampling_seconds': None, 'collect_threads': True,
    'thread_names': [], 'thread_ids': [], 'skip_temperatures': False,
    'jetson_telemetry': False, 'jetson_sampling_seconds': 1.0,
    'max_cycle_fraction': None, 'max_observer_cpu_percent_one_core': None,
}


def validate_resource_options(options=None):
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - RESOURCE_OPTIONS.keys():
        raise ValueError('resource_options: object with supported fields required')
    result = dict(RESOURCE_OPTIONS, **options)
    for name in ('system_sampling_seconds', 'process_sampling_seconds',
                 'thread_sampling_seconds', 'jetson_sampling_seconds'):
        value = result[name]
        if value is None and name != 'jetson_sampling_seconds':
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .1 <= value <= 60:
            raise ValueError(name + ': finite seconds in [0.1, 60] required')
    for name in ('collect_threads', 'skip_temperatures', 'jetson_telemetry'):
        if not isinstance(result[name], bool):
            raise ValueError(name + ': boolean required')
    names, tids = result['thread_names'], result['thread_ids']
    if not isinstance(names, list) or len(names) > 64 or any(not isinstance(name, str) or not name or len(name) > 512 for name in names):
        raise ValueError('thread_names: bounded list of regular expressions required')
    for name in names:
        try:
            re.compile(name)
        except re.error as error:
            raise ValueError('thread_names: invalid regular expression') from error
    if not isinstance(tids, list) or len(tids) > 4096 or any(isinstance(tid, bool) or not isinstance(tid, int) or tid <= 0 for tid in tids):
        raise ValueError('thread_ids: bounded list of positive TIDs required')
    for name in ('max_cycle_fraction', 'max_observer_cpu_percent_one_core'):
        value = result[name]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1000000):
            raise ValueError(name + ': null or finite nonnegative budget required')
    # Do not retain mutable lists owned by callers.
    result['thread_names'], result['thread_ids'] = list(names), list(tids)
    return result


class _Costs:
    """Bounded count/sum/max statistics; no retained per-cycle population."""
    def __init__(self):
        self.values = {}

    def add(self, name, value):
        state = self.values.setdefault(name, {'count': 0, 'sum_ns': 0, 'max_ns': 0})
        state['count'] += 1
        state['sum_ns'] += value
        state['max_ns'] = max(state['max_ns'], value)

    def result(self):
        return {name: dict(state, mean_ns=state['sum_ns'] / state['count'])
                for name, state in self.values.items()}


def parse_cpu_stat(text):
    """Keep raw jiffy counters; guest fields are already included in user/nice."""
    cpus = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0].startswith('cpu') and fields[0][3:].isdigit():
            values = [int(v) for v in fields[1:]]
            if len(values) < 4 or any(v < 0 for v in values):
                raise ValueError('invalid CPU counters')
            cpus[fields[0]] = dict(zip(CPU_FIELDS, values))
    if not cpus:
        raise ValueError('per-core CPU counters absent')
    return cpus


def parse_task_stat(text):
    """/proc stat's comm can contain whitespace and closing parentheses."""
    left, right = text.find('('), text.rfind(')')
    if left < 0 or right <= left:
        raise ValueError('invalid task stat comm')
    pid = int(text[:left].strip())
    # Only fields through 41 are used. Keep each selected token separate while
    # avoiding allocations for the unused trailing kernel fields.
    fields = text[right + 1:].split(maxsplit=39)  # starts at field 3, state
    if len(fields) < 39:
        raise ValueError('short task stat')
    return {'pid': pid, 'comm': text[left + 1:right], 'state': fields[0],
            'minor_faults': int(fields[7]), 'major_faults': int(fields[9]),
            'utime_ticks': int(fields[11]), 'stime_ticks': int(fields[12]),
            'priority': int(fields[15]), 'nice': int(fields[16]),
            'num_threads': int(fields[17]), 'starttime_ticks': int(fields[19]),
            'rss_pages': int(fields[21]), 'processor': int(fields[36]),
            'rt_priority': int(fields[37]), 'policy': int(fields[38])}


def _read(path, parser=lambda text: text.strip()):
    try:
        return parser(path.read_text(encoding='utf-8')), None
    except (OSError, ValueError, IndexError, TypeError) as exc:
        return None, type(exc).__name__ + ': ' + str(exc)


def _kv(text):
    result = {}
    for line in text.splitlines():
        fields = line.replace(':', ' ', 1).split()
        if len(fields) >= 2:
            value = int(fields[1])
            if len(fields) > 2 and fields[2] == 'kB':
                value *= 1024
            result[fields[0]] = value
    if not result:
        raise ValueError('numeric counters absent')
    return result


def _meminfo(text):
    raw, byte_values = {}, {}
    for line in text.splitlines():
        key, sep, tail = line.partition(':')
        fields = tail.split()
        if not sep or not fields:
            continue
        value = int(fields[0])
        unit = fields[1] if len(fields) > 1 else 'count'
        raw[key] = {'value': value, 'unit': unit}
        if unit == 'kB':
            byte_values[key] = value * 1024
    if not raw:
        raise ValueError('meminfo fields absent')
    return {'raw': raw, 'bytes': byte_values}


def _status(text):
    selected = {}
    for line in text.splitlines():
        if not line.startswith(('voluntary_ctxt_switches:', 'nonvoluntary_ctxt_switches:',
                                'Cpus_allowed_list:')):
            continue
        key, sep, value = line.partition(':')
        if not sep:
            continue
        if key in ('voluntary_ctxt_switches', 'nonvoluntary_ctxt_switches'):
            selected[key] = int(value.strip())
        elif key == 'Cpus_allowed_list':
            selected['affinity_cpu_list'] = value.strip()
    return selected


def _schedstat(text):
    values = [int(value) for value in text.split()]
    if len(values) < 3 or any(value < 0 for value in values[:3]):
        raise ValueError('invalid schedstat')
    return {'runtime_ns': values[0], 'runnable_wait_ns': values[1],
            'timeslices': values[2]}


def _sysconf(name):
    try:
        return os.sysconf(name)
    except (OSError, ValueError, AttributeError):
        return None


def _cgroup_path(pid):
    text, reason = _read(PROC_ROOT / str(pid) / 'cgroup')
    if text is None:
        return None, reason
    for line in text.splitlines():
        if line.startswith('0::'):
            relative = PurePosixPath(line[3:])
            if '..' in relative.parts:
                return None, 'invalid cgroup path'
            # Linux proc exposes paths relative to the current cgroup namespace.
            return str(SYS_ROOT / 'fs/cgroup' / str(relative).lstrip('/')), None
    return None, 'cgroup v2 unified hierarchy unavailable'


def _task_paths(path):
    return path / 'stat', path / 'schedstat', path / 'status'


def _task(path, page_size, paths=None, initial_stat=None, name_patterns=()):
    stat_path, sched_path, status_path = paths if paths is not None else _task_paths(path)
    stat, reason = (initial_stat, None) if initial_stat is not None else _read(stat_path, parse_task_stat)
    if stat is None:
        return {'stat': None, 'schedstat': None, 'status': None,
                'availability': {'stat': reason, 'schedstat': 'task unavailable',
                                 'status': 'task unavailable'}}
    sched, sched_reason = _read(sched_path, _schedstat)
    status, status_reason = _read(status_path, _status)
    verify, verify_reason = _read(stat_path, parse_task_stat)
    if verify is None or verify['starttime_ticks'] != stat['starttime_ticks'] or (
            name_patterns and not any(pattern.search(verify['comm']) for pattern in name_patterns)):
        return {'stat': None, 'schedstat': None, 'status': None,
                'availability': {'stat': verify_reason or 'task identity/name changed',
                                 'schedstat': 'task identity not verified',
                                 'status': 'task identity not verified'}}
    stat['rss_bytes'] = stat['rss_pages'] * page_size if page_size else None
    return {'stat': stat, 'schedstat': sched, 'status': status,
            'availability': {'stat': None, 'schedstat': sched_reason,
                             'status': status_reason,
                             'context_switches': None if status is not None and
                             all(key in status for key in ('voluntary_ctxt_switches', 'nonvoluntary_ctxt_switches'))
                             else status_reason or 'context-switch fields absent',
                             'affinity': None if status is not None and 'affinity_cpu_list' in status
                             else status_reason or 'CPU affinity field absent'},
            'schedstat_source': 'proc_schedstat_cumulative; zero_does_not_prove_enabled',
            'rss_scope': 'shared_process_address_space'}


class ResourceSampler:
    """One owned sampler thread; shared-state locks never cover filesystem I/O.

    register/unregister snapshot ownership is identified by registration_id and
    the PID's starttime. A snapshot already in progress can contain a PID removed
    concurrently; the subsequent snapshot will not. Re-registration starts a new
    identity even if the same operating-system process remains alive.
    """

    def __init__(self, output: Path, interval: float, window_source='CSV_monotonic_timestamps', options=None):
        if isinstance(interval, bool) or not math.isfinite(interval) or interval <= 0:
            raise ValueError('resource sampling interval must be positive and finite')
        self.output = Path(output)
        self.interval = interval
        self.options = validate_resource_options(options)
        self.source_intervals = {name: self.options[name + '_sampling_seconds'] or interval
                                 for name in ('system', 'process', 'thread')}
        self._thread_patterns = [re.compile(name) for name in self.options['thread_names']]
        self._thread_ids = set(self.options['thread_ids'])
        self.cadence = min([interval, self.source_intervals['system'], self.source_intervals['process']] +
                          ([self.source_intervals['thread']] if self.options['collect_threads'] else []))
        self.costs_path = self.output.with_name(self.output.stem + '-costs.jsonl')
        self._cost_stream = None
        self._telemetry = None
        self._cycle_id = 0
        if window_source not in ('CSV_monotonic_timestamps', 'monitor_monotonic_timestamps'):
            raise ValueError('unsupported resource window source')
        self.window_source = window_source
        self.error = None
        self._lock = threading.Lock()
        self._registered = {}
        self._serial = 0
        self._window = {'scenario': None, 'rep': None, 'phase': 'idle'}
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._page_size = _sysconf('SC_PAGE_SIZE')
        self._clock_ticks = _sysconf('SC_CLK_TCK')
        # Paths only: no open descriptors, counter values or identity decisions.
        # Each cache is replaced by the last acquisition of that source.
        self._process_paths = {}
        self._thread_paths = {}
        self._system_inventory = None
        self._inventory_refreshed_ns = None

    def register(self, pid, role, expected_starttime_ticks=None):
        pid = int(pid)
        if pid <= 0:
            raise ValueError('PID must be positive')
        stat, reason = _read(PROC_ROOT / str(pid) / 'stat', parse_task_stat)
        if expected_starttime_ticks is not None and (
                stat is None or stat['starttime_ticks'] != expected_starttime_ticks):
            return False
        with self._lock:
            self._serial += 1
            self._registered[pid] = {'pid': pid, 'role': str(role),
                                     'registration_id': self._serial,
                                     'starttime_ticks': stat['starttime_ticks'] if stat else None,
                                     'identity_reason': reason}
        return True

    def owned_process_ids(self):
        return self._telemetry.owned_process_ids() if self._telemetry is not None else ()

    def unregister(self, pid):
        with self._lock:
            self._registered.pop(int(pid), None)

    def set_window(self, scenario, rep, phase):
        with self._lock:
            self._window = {'scenario': scenario, 'rep': rep, 'phase': phase}

    def __enter__(self):
        if self._thread is not None:
            raise RuntimeError('ResourceSampler cannot be entered twice')
        # Do not overwrite existing evidence, including a previous failed run.
        self._stream = self.output.open('x', encoding='utf-8')
        try:
            self._cost_stream = self.costs_path.open('x', encoding='utf-8')
            if self.options['jetson_telemetry']:
                from .jetson import TegrastatsCollector
                self._telemetry = TegrastatsCollector(
                    self.output.with_name(self.output.stem + '-tegrastats.jsonl'),
                    self.options['jetson_sampling_seconds'], enabled=True)
                self._telemetry.__enter__()
            self._thread = threading.Thread(target=self._run, name='resource-sampler', daemon=True)
            with defer_interrupts():
                self._thread.start()
        except BaseException:
            with defer_interrupts():
                self._stop.set()
                if self._thread is not None and self._thread.ident is not None:
                    self._thread.join()
                try:
                    if self._telemetry is not None:
                        self._telemetry.__exit__(None, None, None)
                finally:
                    if self._cost_stream is not None:
                        self._cost_stream.close()
                    self._stream.close()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with defer_interrupts():
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
            try:
                if self._telemetry is not None:
                    self._telemetry.__exit__(exc_type, exc_value, traceback)
            finally:
                for stream in (self._stream, self._cost_stream):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError as exc:
                            self.error = self.error or type(exc).__name__ + ': ' + str(exc)
        return False

    def _refresh_system_inventory(self, now):
        temperatures, frequencies, rails, devices = [], [], [], []
        for path in ([] if self.options['skip_temperatures'] else
                     sorted((SYS_ROOT / 'class/thermal').glob('thermal_zone*/temp'))):
            label, _ = _read(path.parent / 'type')
            temperatures.append((path, path.parent.name, label))
        for cpu in sorted((SYS_ROOT / 'devices/system/cpu').glob('cpu[0-9]*')):
            if not cpu.name[3:].isdigit():
                continue
            # scaling_cur_freq may be a driver estimate, not a measured clock.
            frequencies.append((cpu / 'cpufreq/scaling_cur_freq', cpu.name))
        for path in sorted((SYS_ROOT / 'class/hwmon').glob('hwmon*/power*_input')):
            label, _ = _read(path.with_name(path.name.replace('_input', '_label')))
            chip, _ = _read(path.parent / 'name')
            rails.append((path, path.parent.name + '/' + path.name, label, chip))
        for path in sorted((SYS_ROOT / 'class/devfreq').glob('*/cur_freq')):
            name = path.parent.name
            label, _ = _read(path.parent / 'name')
            words = set(re.split(r'[^a-z0-9]+', (name + ' ' + (label or '')).lower()))
            if not words & {'gpu', 'emc', 'ga10b', 'gv11b', 'gb20b'}:
                continue
            devices.append((path, name))
        self._system_inventory = temperatures, frequencies, rails, devices
        self._inventory_refreshed_ns = now

    def _system(self):
        now = time.monotonic_ns()
        if self._system_inventory is None or now - self._inventory_refreshed_ns >= 5_000_000_000:
            self._refresh_system_inventory(now)
        cpus, cpu_reason = _read(PROC_ROOT / 'stat', parse_cpu_stat)
        memory, memory_reason = _read(PROC_ROOT / 'meminfo', _meminfo)
        sched_enabled, enabled_reason = _read(PROC_ROOT / 'sys/kernel/sched_schedstats', int)
        temperatures, frequencies, rails, device_frequencies = {}, {}, {}, {}
        thermal_paths, cpu_paths, rail_paths, device_paths = self._system_inventory
        for path, name, label in thermal_paths:
            value, reason = _read(path, lambda text: int(text.strip()) / 1000)
            temperatures[name] = {'celsius': value, 'label': label, 'reason': reason}
        for path, name in cpu_paths:
            value, reason = _read(path, int)
            frequencies[name] = {'khz': value, 'source': 'scaling_cur_freq', 'reason': reason}
        for path, name, label, chip in rail_paths:
            value, reason = _read(path, int)
            rails[name] = {'microwatts': value, 'label': label, 'chip': chip,
                           'source': 'hwmon_power_input', 'reason': reason}
        for path, name in device_paths:
            value, reason = _read(path, int)
            device_frequencies[name] = {'hz': value, 'source': 'devfreq_cur_freq', 'reason': reason}
        return {'per_cpu_ticks': cpus, 'meminfo_bytes': memory['bytes'] if memory else None,
                'meminfo_raw': memory['raw'] if memory else None,
                'sched_schedstats_enabled': sched_enabled,
                'temperatures': temperatures or None,
                'cpu_frequencies': frequencies or None,
                'rail_power': rails or None, 'device_frequencies': device_frequencies or None,
                'availability': {'per_cpu_ticks': cpu_reason, 'meminfo_bytes': memory_reason,
                                 'sched_schedstats_enabled': enabled_reason,
                                 'temperatures': ('disabled by configuration' if self.options['skip_temperatures'] else
                                                  None if temperatures else 'thermal sysfs unavailable'),
                                 'cpu_frequencies': None if frequencies else 'CPU frequency sysfs unavailable',
                                 'rail_power': None if rails else 'hwmon power inputs not exposed',
                                 'device_frequencies': None if device_frequencies else 'named GPU/EMC devfreq not exposed'},
                'interface_inventory': {'refreshed_monotonic_ns': self._inventory_refreshed_ns,
                                        'refresh_seconds': 5},
                'gpu_utilization': None, 'emc_bandwidth': None,
                'unimplemented': ['gpu_utilization', 'emc_bandwidth']}

    def _threads(self, registration):
        """Read only selected tasks, verifying the containing process both sides."""
        base = PROC_ROOT / str(registration['pid'])
        before, reason = _read(base / 'stat', parse_task_stat)
        expected = registration['starttime_ticks']
        selection = {'seen': 0, 'sampled': 0, 'omitted': 0,
                     'omitted_by_reason': {}, 'scope': 'configured_thread_selection'}
        if before is None or expected is None or before['starttime_ticks'] != expected:
            self._thread_paths.pop(registration['pid'], None)
            return None, reason or 'PID identity not verified', selection
        try:
            task_root = base / 'task'
            with os.scandir(task_root) as directory:
                names = sorted(entry.name for entry in directory
                               if entry.name.isascii() and entry.name.isdecimal())
            tasks = []
            old_paths = self._thread_paths.get(registration['pid'], {})
            current_paths = {}
            for name in names:
                selection['seen'] += 1
                tid = int(name)
                omitted = None
                stat = None
                if self._thread_ids and tid not in self._thread_ids:
                    omitted = 'tid_filter'
                else:
                    files = old_paths.get(tid) or _task_paths(task_root / name)
                if not omitted and self._thread_patterns:
                    stat, failure = _read(files[0], parse_task_stat)
                    if stat is None:
                        # Preserve failed reads instead of treating unreadable names as filtered.
                        tasks.append({'tid': tid, 'stat': None, 'status': None, 'schedstat': None,
                                      'availability': {'stat': failure}})
                        selection['sampled'] += 1
                        continue
                    if not any(pattern.search(stat['comm']) for pattern in self._thread_patterns):
                        omitted = 'name_filter'
                if omitted:
                    selection['omitted'] += 1
                    reasons = selection['omitted_by_reason']
                    reasons[omitted] = reasons.get(omitted, 0) + 1
                    continue
                # Supplying immutable paths avoids constructing a task Path
                # for each already-known TID. Values/identity are never cached.
                task = _task(None, self._page_size, files, stat, self._thread_patterns)
                task['tid'] = tid
                if self._thread_patterns and task.get('stat') is not None and (
                        task['stat']['starttime_ticks'] != stat['starttime_ticks'] or
                        not any(pattern.search(task['stat']['comm']) for pattern in self._thread_patterns)):
                    task = {'tid': tid, 'stat': None, 'status': None, 'schedstat': None,
                            'availability': {'stat': 'thread identity/name changed during selection'}}
                if task.get('stat') is not None:
                    current_paths[tid] = files
                tasks.append(task)
                selection['sampled'] += 1
            after, reason = _read(base / 'stat', parse_task_stat)
            if after is None or after['starttime_ticks'] != expected:
                self._thread_paths.pop(registration['pid'], None)
                return None, reason or 'PID identity changed during thread snapshot', selection
            self._thread_paths[registration['pid']] = current_paths
            return tasks, None, selection
        except OSError as error:
            self._thread_paths.pop(registration['pid'], None)
            return None, type(error).__name__ + ': ' + str(error), selection

    def _observer(self):
        children = []
        pids = self.owned_process_ids()
        reason = None
        if self.options['jetson_telemetry'] and not pids:
            reason = 'owned telemetry child unavailable'
        for pid in pids:
            path = PROC_ROOT / str(pid) / 'stat'
            stat, error = _read(path, parse_task_stat)
            verify, verify_error = _read(path, parse_task_stat)
            if (stat is None or verify is None or stat['pid'] != pid or verify['pid'] != pid or
                    stat['starttime_ticks'] != verify['starttime_ticks'] or
                    min(stat['utime_ticks'], stat['stime_ticks']) < 0 or not self._clock_ticks):
                reason = error or verify_error or 'owned child identity/counters not verified'
                continue
            children.append({'pid': pid, 'starttime_ticks': stat['starttime_ticks'],
                             'cpu_time_ns': (stat['utime_ticks'] + stat['stime_ticks']) *
                             1_000_000_000 // self._clock_ticks})
        if pids != self.owned_process_ids():
            reason = 'owned child changed during observation'
        return {'process_cpu_ns': time.process_time_ns(), 'pid': os.getpid(),
                'scope': 'whole_collector_process', 'owned_children': children,
                'owned_children_expected': self.options['jetson_telemetry'],
                'owned_children_reason': reason}

    def _snapshot(self, due=None):
        if due is None:
            due = {'system', 'process', 'thread'}
        if not self.options['collect_threads']:
            due = set(due) - {'thread'}
        with self._lock:
            registrations = [dict(item) for item in self._registered.values()]
            window = dict(self._window)
        started = time.monotonic_ns()
        processes, cgroups = [], {}
        source_windows, phase_costs, phase_cpu_costs = {}, {}, {}

        def phase(name, action):
            begin = time.monotonic_ns()
            cpu_begin = time.thread_time_ns()
            value = action()
            cpu_end = time.thread_time_ns()
            end = time.monotonic_ns()
            source_windows[name] = {'start_ns': begin, 'end_ns': end}
            phase_costs[name] = end - begin
            phase_cpu_costs[name] = cpu_end - cpu_begin
            return value

        system = phase('system', self._system) if 'system' in due else None
        own_cgroup, own_reason = None, 'not scheduled this cycle'
        if 'process' in due:
            own_cgroup, own_reason = _cgroup_path('self')
        cgroup_paths = {own_cgroup} if own_cgroup else set()
        if 'process' in due or 'thread' in due:
            processes = [dict(registration, stat=None, schedstat=None, status=None,
                              availability={}, process_sampled='process' in due,
                              tasks_sampled='thread' in due, tasks=None,
                              tasks_reason=('disabled by configuration' if not self.options['collect_threads'] else
                                            'not scheduled this cycle'),
                              cgroup=None, cgroup_reason='not scheduled this cycle')
                         for registration in registrations]

        def read_processes():
            current_paths = {}
            for process in processes:
                base = PROC_ROOT / str(process['pid'])
                expected = process['starttime_ticks']
                files = self._process_paths.get(base) or _task_paths(base)
                data = _task(base, self._page_size, files)
                if expected is None or not data['stat'] or data['stat']['starttime_ticks'] != expected:
                    data = {'stat': None, 'schedstat': None, 'status': None,
                            'availability': {'stat': process['identity_reason'] or 'PID identity not verified',
                                             'schedstat': 'PID identity not verified',
                                             'status': 'PID identity not verified'}}
                    cgroup, cgroup_reason = None, 'PID identity not verified'
                else:
                    cgroup, cgroup_reason = _cgroup_path(process['pid'])
                    verify, reason = _read(base / 'stat', parse_task_stat)
                    if verify is None or verify['starttime_ticks'] != expected:
                        data = {'stat': None, 'schedstat': None, 'status': None,
                                'availability': {'stat': reason or 'PID identity changed during snapshot',
                                                 'schedstat': 'PID identity not verified', 'status': 'PID identity not verified'}}
                        cgroup, cgroup_reason = None, 'PID identity not verified'
                if cgroup:
                    cgroup_paths.add(cgroup)
                process.update(data, cgroup=cgroup, cgroup_reason=cgroup_reason)
                if data['stat'] is not None:
                    current_paths[base] = files
            self._process_paths = current_paths

        def read_threads():
            current_pids = {process['pid'] for process in processes}
            self._thread_paths = {pid: paths for pid, paths in self._thread_paths.items()
                                  if pid in current_pids}
            for process in processes:
                tasks, reason, selection = self._threads(process)
                process.update(tasks=tasks, tasks_reason=reason, thread_selection=selection)

        def read_cgroups():
            for path in sorted(cgroup_paths):
                cpu, cpu_reason = _read(Path(path) / 'cpu.stat', _kv)
                memory, memory_reason = _read(Path(path) / 'memory.current', int)
                cgroups[path] = {'cpu_stat': cpu, 'memory_current_bytes': memory,
                                'availability': {'cpu_stat': cpu_reason, 'memory_current_bytes': memory_reason}}

        if 'process' in due:
            phase('process', read_processes)
            phase('cgroup', read_cgroups)
        if 'thread' in due:
            phase('thread', read_threads)
        observer = phase('observer', self._observer)
        telemetry = self._telemetry.snapshot() if self._telemetry is not None else None
        return {'schema_version': 1, 'monotonic_ns': started,
                'sample_end_ns': time.monotonic_ns(), 'window': window,
                'window_semantics': 'tags_only; measurement_window_uses_' + self.window_source,
                'clock_ticks_per_second': self._clock_ticks, 'page_size_bytes': self._page_size,
                'system': system, 'processes': processes, 'cgroups': cgroups,
                'sampler_cgroup': own_cgroup, 'sampler_cgroup_reason': own_reason,
                'source_windows': source_windows, 'phase_costs_ns': phase_costs,
                'phase_thread_cpu_ns': phase_cpu_costs,
                'observer': observer, 'jetson_telemetry': telemetry,
                'resource_options': self.options,
                'thread_scope': {'enabled': self.options['collect_threads'],
                                 'name_patterns': self.options['thread_names'],
                                 'tids': self.options['thread_ids']}}

    def _run(self):
        try:
            periods = {name: round(value * 1e9) for name, value in self.source_intervals.items()
                       if name != 'thread' or self.options['collect_threads']}
            cadence_ns = max(1, round(self.cadence * 1e9))
            planned = time.monotonic_ns()
            next_due = {name: planned for name in periods}
            completion = None
            while not self._stop.is_set():
                before, cpu_before = time.monotonic_ns(), time.thread_time_ns()
                due = {name for name, deadline in next_due.items() if before >= deadline}
                self._cycle_id += 1
                sample = self._snapshot(due)
                sample['cycle_id'] = self._cycle_id
                encode_start = time.monotonic_ns()
                encode_cpu = time.thread_time_ns()
                payload = json.dumps(sample, allow_nan=False, separators=(',', ':')) + '\n'
                encode_cpu_end = time.thread_time_ns()
                encode_end = time.monotonic_ns()
                self._stream.write(payload)
                self._stream.flush()
                write_cpu_end = time.thread_time_ns()
                write_end = time.monotonic_ns()
                costs = {'schema_version': 1, 'cycle_id': self._cycle_id,
                         'start_ns': before, 'end_ns': write_end,
                         'cadence_ns': cadence_ns, 'duration_ns': write_end - before,
                         'over_period': write_end - before > cadence_ns,
                         'thread_cpu_ns': time.thread_time_ns() - cpu_before,
                         'phase_costs_ns': dict(sample['phase_costs_ns'],
                                                encode=encode_end - encode_start,
                                                resource_write_flush=write_end - encode_end),
                         'phase_thread_cpu_ns': dict(sample['phase_thread_cpu_ns'],
                             encode=encode_cpu_end - encode_cpu,
                             resource_write_flush=write_cpu_end - encode_cpu_end),
                         'scope': 'sampler_collection_encoding_resource_write; excludes_cost_record_write'}
                costs['previous_cycle_completion'] = completion
                self._cost_stream.write(json.dumps(costs, allow_nan=False, separators=(',', ':')) + '\n')
                self._cost_stream.flush()
                finished = time.monotonic_ns()
                completion = {'cycle_id': self._cycle_id, 'start_ns': before, 'end_ns': finished,
                              'cadence_ns': cadence_ns, 'duration_ns': finished - before,
                              'thread_cpu_ns': time.thread_time_ns() - cpu_before}
                for name in due:
                    next_due[name] += max(1, (finished - next_due[name]) // periods[name] + 1) * periods[name]
                planned += max(1, (finished - planned) // cadence_ns + 1) * cadence_ns
                self._stop.wait(max(0, (planned - time.monotonic_ns()) / 1e9))
            if completion is not None:
                # Persist the last completion after the measured pipeline has
                # ended. This terminal bookkeeping write is outside its scope.
                self._cost_stream.write(json.dumps({'schema_version': 1,
                    'record_type': 'terminal_completion', 'completion': completion},
                    separators=(',', ':')) + '\n')
                self._cost_stream.flush()
        except Exception as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)


class _Counters:
    """Retain only previous counters and accumulated valid deltas."""
    def __init__(self):
        self.previous = None
        self.sums = {}
        self.intervals = {}
        self.covered_ns = {}
        self.rejected = {}

    def add(self, now, values):
        if self.previous is not None:
            before, old = self.previous
            if now > before:
                for key, value in values.items():
                    prev = old.get(key)
                    if value is None or prev is None:
                        continue
                    delta = value - prev
                    if delta < 0:
                        self.rejected[key] = self.rejected.get(key, 0) + 1
                        continue
                    self.sums[key] = self.sums.get(key, 0) + delta
                    self.intervals[key] = self.intervals.get(key, 0) + 1
                    self.covered_ns[key] = self.covered_ns.get(key, 0) + now - before
        self.previous = now, values

    def result(self, keys):
        return {'delta': {key: self.sums.get(key) for key in keys},
                'valid_intervals': {key: self.intervals.get(key, 0) for key in keys},
                'covered_ns': {key: self.covered_ns.get(key, 0) for key in keys},
                'reset_intervals_rejected': {key: self.rejected.get(key, 0) for key in keys}}


class _CpuCounters(_Counters):
    def __init__(self):
        super().__init__()
        self.total_ticks = 0
        self.busy_ticks = 0
        self.cpu_intervals = 0
        self.cpu_covered_ns = 0

    def add(self, now, values):
        if self.previous is not None:
            before, old = self.previous
            if now > before and values.keys() == old.keys() and 'idle' in values:
                changes = {key: value - old[key] for key, value in values.items()}
                if all(value >= 0 for value in changes.values()):
                    total = sum(changes.values())
                    if total > 0:
                        self.total_ticks += total
                        self.busy_ticks += total - changes['idle'] - changes.get('iowait', 0)
                        self.cpu_intervals += 1
                        self.cpu_covered_ns += now - before
        super().add(now, values)


ENTITY_KEYS = ('cpu_ticks', 'minor_faults', 'major_faults', 'voluntary_ctxt_switches',
               'nonvoluntary_ctxt_switches', 'runtime_ns', 'runnable_wait_ns', 'timeslices')


def _entity_values(data):
    stat, status, sched = data.get('stat') or {}, data.get('status') or {}, data.get('schedstat') or {}
    user, kernel = stat.get('utime_ticks'), stat.get('stime_ticks')
    return {'cpu_ticks': user + kernel if user is not None and kernel is not None else None,
            'minor_faults': stat.get('minor_faults'), 'major_faults': stat.get('major_faults'),
            'voluntary_ctxt_switches': status.get('voluntary_ctxt_switches'),
            'nonvoluntary_ctxt_switches': status.get('nonvoluntary_ctxt_switches'),
            'runtime_ns': sched.get('runtime_ns'), 'runnable_wait_ns': sched.get('runnable_wait_ns'),
            'timeslices': sched.get('timeslices')}


def _range_add(ranges, key, value):
    if value is not None:
        old = ranges.get(key)
        ranges[key] = {'min': min(old['min'], value) if old else value,
                       'max': max(old['max'], value) if old else value,
                       '_sum': (old['_sum'] if old else 0) + value,
                       'samples': (old['samples'] if old else 0) + 1}


def _finish_ranges(ranges):
    return {key: {'min': value['min'], 'max': value['max'],
                  'mean': value['_sum'] / value['samples'], 'samples': value['samples']}
            for key, value in ranges.items()} or None


def summarize_costs(path, start_ns, end_ns):
    """Keep legacy phase costs plus completions through the cost-record flush."""
    result = {'available': False, 'reason': 'cost log absent or no complete cycles in window',
              'cycles': 0, 'over_period_cycles': 0, 'cycle_fraction_max': None,
              'phase_costs_ns': {}, 'phase_thread_cpu_ns': {},
              'completed_cycles': 0, 'full_over_period_cycles': 0, 'full_cycle_fraction_max': None,
              'full_pipeline_scope': 'sampler read, encoding, resource and cost-record flush; '
                  'excludes scheduling bookkeeping and terminal completion write; discovery has separate wall cost',
              'scope': 'sampler collection, encoding, resource write/flush; excludes cost-log write and tegrastats child CPU'}
    if not path.exists():
        return result
    phases, cpu_phases, previous = _Costs(), _Costs(), None
    pending, previous_completion, terminated = None, None, False
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            try:
                item = json.loads(line)
                if terminated:
                    raise ValueError('record after terminal completion')
                terminal = item.get('record_type') == 'terminal_completion'
                completion = item.get('completion') if terminal else item.get('previous_cycle_completion')
                if completion is not None:
                    if item.get('schema_version') != 1 or pending is None:
                        raise ValueError('completion without preceding cycle')
                    cb, ce, cp = completion['start_ns'], completion['end_ns'], completion['cadence_ns']
                    if (any(type(value) is not int for value in (cb, ce, cp, completion['cycle_id'], completion['duration_ns']))
                            or cb != pending['start_ns'] or cp != pending['cadence_ns']
                            or completion['cycle_id'] != pending['cycle_id'] or ce < pending['end_ns']
                            or cp <= 0 or completion['duration_ns'] != ce - cb
                            or (not terminal and ce > item['start_ns'])):
                        raise ValueError('invalid pipeline completion')
                    full_cpu = completion.get('thread_cpu_ns')
                    if full_cpu is not None and (type(full_cpu) is not int or full_cpu < 0):
                        raise ValueError('invalid pipeline CPU')
                    if cb >= start_ns and ce <= end_ns:
                        fraction = (ce - cb) / cp
                        result['completed_cycles'] += 1
                        result['full_over_period_cycles'] += fraction > 1
                        result['full_cycle_fraction_max'] = max(result['full_cycle_fraction_max'] or 0, fraction)
                        phases.add('full_pipeline_cycle', ce - cb)
                        if full_cpu is not None:
                            cpu_phases.add('full_pipeline_cycle', full_cpu)
                    pending = None
                if terminal:
                    if completion is None:
                        raise ValueError('empty terminal completion')
                    terminated = True
                    continue
                begin, end, period = item['start_ns'], item['end_ns'], item['cadence_ns']
                if any(isinstance(v, bool) or not isinstance(v, int) for v in (begin, end, period)) or begin > end or period <= 0 or item['duration_ns'] != end - begin:
                    raise ValueError('invalid cost interval')
                identity = item['cycle_id']
                if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0 or item.get('schema_version') != 1:
                    raise ValueError('invalid cost identity')
                if previous_completion is not None and (identity <= previous_completion[0] or begin <= previous_completion[1]):
                    raise ValueError('cost intervals must increase')
                previous_completion = identity, begin
                pending = item
                if begin < start_ns or end > end_ns:
                    continue
                if previous is not None and (identity <= previous[0] or begin <= previous[1]):
                    raise ValueError('cost intervals must increase')
                previous = identity, begin
                fraction = (end - begin) / period
                phases.add('cycle', end - begin)
                for name, duration in item['phase_costs_ns'].items():
                    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0 or duration > end - begin:
                        raise ValueError('invalid phase cost')
                    phases.add(name, duration)
                for name, duration in item.get('phase_thread_cpu_ns', {}).items():
                    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
                        raise ValueError('invalid phase CPU cost')
                    cpu_phases.add(name, duration)
                result['cycles'] += 1
                result['over_period_cycles'] += fraction > 1
                result['cycle_fraction_max'] = max(result['cycle_fraction_max'] or 0, fraction)
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError('invalid cost record at line ' + str(number)) from error
    result.update(available=result['cycles'] > 0, phase_costs_ns=phases.result(),
                  phase_thread_cpu_ns=cpu_phases.result())
    if result['available']:
        result['reason'] = None
    return result


def summarize_resources(path: Path, start_ns: int, end_ns: int) -> dict:
    """Stream raw JSONL and summarize only [start_ns, end_ns] observations.

    Returned deltas cover sampled subintervals, never the full requested window.
    Missing observations break delta chains. Identity changes create separate
    entities. Linux process stat CPU is process-wide; process schedstat/status
    belong to the leader only. Thread wait and context-switch deltas are separate.
    """
    if end_ns < start_ns:
        raise ValueError('resource window ends before it starts')
    count, first, last, previous_sample = 0, None, None, None
    gap_sum, gap_min, gap_max, duration_max = 0, None, None, 0
    cpu_counters, entities, cgroups = {}, {}, {}
    temperatures, frequencies, meminfo, rails, device_frequencies = {}, {}, {}, {}, {}
    rail_labels, device_sources, sched_settings = {}, {}, {}
    availability = {}
    clock_ticks = None
    identity_keys_previous = {'process': set(), 'thread': set()}
    source_coverage, source_previous = {}, {}
    observer_counter = _Counters()
    total_observer_counter = _Counters()
    previous_owned_identity = None
    owned_cpu_unavailable = 0
    observer_missing_samples = 0
    thread_scope = {'enabled': None, 'name_patterns': [], 'tids': [],
                    'selection': {'seen': 0, 'sampled': 0, 'omitted': 0, 'omitted_by_reason': {}}}
    options = None
    telemetry = {'enabled': False, 'available': False, 'reason': 'disabled or legacy input',
                 'unique_samples': 0, 'available_snapshots': 0, 'unavailable_snapshots': 0,
                 'reasons': [], 'value_ranges': None, 'field_availability': {},
                 'units': {'gpu_utilization_percent': 'percent', 'gpu_frequency_mhz': 'MHz',
                           'emc_activity_percent': 'percent', 'emc_frequency_mhz': 'MHz'},
                 'clock_semantics': 'local receipt monotonic time; not device generation time'}
    telemetry_ranges, last_telemetry_id = {}, None
    cgroup_keys_previous = set()
    cpu_keys_previous = set()

    def unavailable(key, reason):
        item = availability.setdefault(key, {'available_samples': 0, 'unavailable_samples': 0, 'reasons': []})
        if reason:
            item['unavailable_samples'] += 1
            # Bound repeated errors and avoid retaining an error per sample.
            if reason not in item['reasons'] and len(item['reasons']) < 8:
                item['reasons'].append(reason)
        else:
            item['available_samples'] += 1

    def observe_entity(key, data, metadata, now):
        entity = entities.setdefault(key, {'metadata': metadata, 'counter': _Counters(),
                                            'rss_peak_bytes': None, 'samples': 0,
                                            'first_ns': now, 'last_ns': now,
                                            'scheduling_observations': []})
        entity['samples'] += 1
        entity['last_ns'] = now
        entity['counter'].add(now, _entity_values(data))
        stat = data.get('stat') or {}
        rss = stat.get('rss_bytes')
        if rss is not None:
            entity['rss_peak_bytes'] = max(entity['rss_peak_bytes'] or 0, rss)
        scheduling = {name: stat.get(name) for name in ('policy', 'priority', 'nice', 'rt_priority')}
        scheduling['affinity_cpu_list'] = (data.get('status') or {}).get('affinity_cpu_list')
        if scheduling not in entity['scheduling_observations'] and len(entity['scheduling_observations']) < 16:
            entity['scheduling_observations'].append(scheduling)
        for field, reason in data.get('availability', {}).items():
            unavailable(key + '.' + field, reason)

    with Path(path).open(encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                sample = json.loads(line)
                if sample.get('schema_version') != 1:
                    raise ValueError('unsupported resource schema')
                now = sample['monotonic_ns']
                if not isinstance(now, int) or isinstance(now, bool):
                    raise ValueError('invalid monotonic timestamp')
                finished = sample.get('sample_end_ns', now)
                if not isinstance(finished, int) or isinstance(finished, bool) or finished < now:
                    raise ValueError('invalid collection end timestamp')
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError('invalid resource sample at line ' + str(line_number)) from exc
            if now < start_ns or finished > end_ns:
                continue
            if previous_sample is not None and now <= previous_sample:
                raise ValueError('resource timestamps must increase inside window')
            count += 1
            if first is None:
                first = now
            last = now
            if previous_sample is not None:
                gap = now - previous_sample
                gap_sum += gap
                gap_min = gap if gap_min is None else min(gap_min, gap)
                gap_max = gap if gap_max is None else max(gap_max, gap)
            previous_sample = now
            duration_max = max(duration_max, sample.get('sample_end_ns', now) - now)
            windows = sample.get('source_windows')
            if windows is None:
                windows = {name: {'start_ns': now, 'end_ns': finished}
                           for name in ('system', 'process', 'thread', 'cgroup')}
            if not isinstance(windows, dict):
                raise ValueError('invalid source windows')
            for name, window in windows.items():
                if name not in ('system', 'process', 'thread', 'cgroup', 'observer') or not isinstance(window, dict):
                    raise ValueError('invalid source window')
                begin, finish = window.get('start_ns'), window.get('end_ns')
                if any(isinstance(v, bool) or not isinstance(v, int) for v in (begin, finish)) or not now <= begin <= finish <= finished:
                    raise ValueError('source window outside collection interval')
                previous = source_previous.get(name)
                if previous is not None and begin <= previous:
                    raise ValueError('source timestamps must increase')
                state = source_coverage.setdefault(name, {'samples': 0, 'observed_start_ns': begin,
                                                          'observed_end_ns': finish, 'sample_gap_ns_max': None,
                                                          'sample_duration_ns_max': 0})
                state['samples'] += 1
                state['observed_end_ns'] = finish
                state['sample_duration_ns_max'] = max(state['sample_duration_ns_max'], finish - begin)
                if previous is not None:
                    state['sample_gap_ns_max'] = max(state['sample_gap_ns_max'] or 0, begin - previous)
                source_previous[name] = begin
            if 'process' in windows:
                unavailable('sampler_cgroup', sample.get('sampler_cgroup_reason'))
            if sample.get('resource_options') is not None:
                current_options = validate_resource_options(sample['resource_options'])
                if options is not None and options != current_options:
                    raise ValueError('resource options changed inside window')
                options = current_options
            current_scope = sample.get('thread_scope')
            if current_scope:
                for key in ('enabled', 'name_patterns', 'tids'):
                    thread_scope[key] = current_scope[key]
            if 'observer' in windows:
                observed = sample.get('observer') or {}
                value = observed.get('process_cpu_ns')
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError('invalid observer CPU counter')
                observer_counter.add(windows['observer']['start_ns'], {'process_cpu_ns': value})
                children = observed.get('owned_children')
                expected = observed.get('owned_children_expected',
                                        (sample.get('resource_options') or {}).get('jetson_telemetry',
                                            sample.get('jetson_telemetry') is not None))
                if not isinstance(expected, bool) or (children is not None and not isinstance(children, list)):
                    raise ValueError('invalid owned child observation')
                child_cpu, identity = 0, ()
                if expected:
                    if observed.get('owned_children_reason') or not children:
                        child_cpu = None
                    else:
                        identities = []
                        for child in children:
                            if not isinstance(child, dict) or not all(key in child for key in
                                    ('pid', 'starttime_ticks', 'cpu_time_ns')):
                                raise ValueError('invalid owned child observation')
                            fields = [child['pid'], child['starttime_ticks'], child['cpu_time_ns']]
                            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in fields) or not fields[0]:
                                raise ValueError('invalid owned child CPU counter')
                            identities.append((fields[0], fields[1]))
                            child_cpu += fields[2]
                        if len(set(identities)) != len(identities):
                            raise ValueError('duplicate owned child identity')
                        identity = tuple(sorted(identities))
                if child_cpu is None:
                    owned_cpu_unavailable += 1
                if identity != previous_owned_identity or child_cpu is None:
                    total_observer_counter.previous = None
                values = {'parent_cpu_ns': value, 'owned_child_cpu_ns': child_cpu,
                          'total_cpu_ns': value + child_cpu if child_cpu is not None else None}
                old = total_observer_counter.previous
                if old is not None and any(values[key] < old[1][key]
                        for key in ('parent_cpu_ns', 'owned_child_cpu_ns') if old[1][key] is not None):
                    total_observer_counter.previous = None
                total_observer_counter.add(windows['observer']['start_ns'], values)
                previous_owned_identity = identity
            else:
                # Observer is acquired every cycle; absence is a real gap, not
                # the sparse scheduling used by process/thread sources.
                observer_counter.previous = None
                total_observer_counter.previous = None
                previous_owned_identity = None
                observer_missing_samples += 1
                if (sample.get('resource_options') or {}).get('jetson_telemetry',
                        sample.get('jetson_telemetry') is not None):
                    owned_cpu_unavailable += 1
            jetson = sample.get('jetson_telemetry')
            if jetson is not None:
                telemetry['enabled'] = True
                usable = jetson.get('available') is True
                telemetry['available_snapshots' if usable else 'unavailable_snapshots'] += 1
                for field, field_reason in (jetson.get('values') or {}).get('availability', {}).items():
                    unavailable('jetson.' + field, field_reason)
                reason = jetson.get('reason')
                if reason and reason not in telemetry['reasons'] and len(telemetry['reasons']) < 8:
                    telemetry['reasons'].append(reason)
                received, identity = jetson.get('received_monotonic_ns'), jetson.get('sample_id')
                if usable and isinstance(received, int) and not isinstance(received, bool) and start_ns <= received <= end_ns and identity != last_telemetry_id:
                    if not isinstance(identity, int) or isinstance(identity, bool) or identity < 1 or (last_telemetry_id is not None and identity < last_telemetry_id):
                        raise ValueError('invalid telemetry identity')
                    last_telemetry_id = identity
                    telemetry['unique_samples'] += 1
                    for key, value in (jetson.get('values') or {}).items():
                        if key == 'gpu_frequency_mhz' and isinstance(value, list):
                            for index, frequency in enumerate(value):
                                _range_add(telemetry_ranges, key + '.gpc' + str(index), frequency)
                        elif key in ('gpu_utilization_percent', 'emc_activity_percent', 'emc_frequency_mhz'):
                            _range_add(telemetry_ranges, key, value)
            system_now = windows.get('system', {}).get('start_ns', now)
            ticks = sample.get('clock_ticks_per_second')
            if clock_ticks is None:
                clock_ticks = ticks
            elif ticks != clock_ticks:
                raise ValueError('resource clock tick frequency changed')
            system = (sample.get('system') or {}) if 'system' in windows else {}
            for field, reason in system.get('availability', {}).items():
                unavailable('system.' + field, reason)
            cpus = system.get('per_cpu_ticks') or {}
            for key in (cpu_keys_previous - cpus.keys()) if 'system' in windows else ():
                cpu_counters[key].previous = None
            for name, values in cpus.items():
                # Do not invent absent fields, nor double-count guest/guest_nice.
                counters = {key: values.get(key) for key in CPU_FIELDS[:8] if key in values}
                state = cpu_counters.setdefault(name, _CpuCounters())
                state.add(system_now, counters)
            if 'system' in windows:
                cpu_keys_previous = set(cpus)
            for name, data in (system.get('temperatures') or {}).items():
                _range_add(temperatures, name, data.get('celsius'))
                unavailable('temperature.' + name, data.get('reason'))
            for name, data in (system.get('cpu_frequencies') or {}).items():
                _range_add(frequencies, name, data.get('khz'))
                unavailable('frequency.' + name, data.get('reason'))
            for name, data in (system.get('rail_power') or {}).items():
                _range_add(rails, name, data.get('microwatts'))
                rail_labels[name] = {'label': data.get('label'), 'chip': data.get('chip'),
                                     'unit': 'microwatt', 'source': data.get('source')}
                unavailable('rail_power.' + name, data.get('reason'))
            for name, data in (system.get('device_frequencies') or {}).items():
                _range_add(device_frequencies, name, data.get('hz'))
                device_sources[name] = {'unit': 'Hz', 'source': data.get('source')}
                unavailable('device_frequency.' + name, data.get('reason'))
            _range_add(sched_settings, 'sched_schedstats_enabled', system.get('sched_schedstats_enabled'))
            for name, value in (system.get('meminfo_bytes') or {}).items():
                _range_add(meminfo, name, value)
            present_entities = {'process': set(), 'thread': set()}
            for process in sample.get('processes') or []:
                stat = process.get('stat')
                base = 'pid={}:registration={}:start={}'.format(process['pid'], process['registration_id'], process.get('starttime_ticks'))
                if 'process' in windows and process.get('process_sampled', True):
                    unavailable(base + '.cgroup', process.get('cgroup_reason'))
                    if stat is None:
                        for field, reason in process.get('availability', {}).items():
                            unavailable(base + '.' + field, reason)
                    elif stat['starttime_ticks'] != process.get('starttime_ticks'):
                        unavailable(base + '.stat', 'PID identity differs from registration')
                    else:
                        metadata = {'pid': process['pid'], 'role': process['role'], 'comm': stat.get('comm'),
                                    'registration_id': process['registration_id'],
                                    'starttime_ticks': stat['starttime_ticks'], 'kind': 'process',
                                    'cpu_scope': 'all_process_threads',
                                    'schedstat_and_context_switch_scope': 'leader_thread_only',
                                    'rss_scope': 'process_address_space', 'cgroup': process.get('cgroup')}
                        observe_entity(base, process, metadata, windows['process']['start_ns'])
                        present_entities['process'].add(base)
                if 'thread' not in windows or not process.get('tasks_sampled', True):
                    continue
                unavailable(base + '.tasks', process.get('tasks_reason'))
                selection = process.get('thread_selection') or {}
                aggregate = thread_scope['selection']
                for key in ('seen', 'sampled', 'omitted'):
                    aggregate[key] += selection.get(key, 0)
                for reason, amount in selection.get('omitted_by_reason', {}).items():
                    aggregate['omitted_by_reason'][reason] = aggregate['omitted_by_reason'].get(reason, 0) + amount
                # Legacy records tie thread validity to the containing process stat.
                if 'source_windows' not in sample and (stat is None or stat['starttime_ticks'] != process.get('starttime_ticks')):
                    continue
                for task in process.get('tasks') or []:
                    if task.get('stat') is None:
                        for field, reason in task.get('availability', {}).items():
                            unavailable(base + '.tid=' + str(task['tid']) + '.' + field, reason)
                        continue
                    key = base + ':tid={}:start={}'.format(task['tid'], task['stat']['starttime_ticks'])
                    metadata = {'pid': process['pid'], 'tid': task['tid'], 'role': process['role'],
                                'comm': task['stat'].get('comm'),
                                'registration_id': process['registration_id'],
                                'starttime_ticks': task['stat']['starttime_ticks'], 'kind': 'thread',
                                'cpu_scope': 'thread', 'schedstat_and_context_switch_scope': 'thread',
                                'rss_scope': 'shared_process_address_space'}
                    observe_entity(key, task, metadata, windows['thread']['start_ns'])
                    present_entities['thread'].add(key)
            for kind in ('process', 'thread'):
                if kind in windows:
                    for key in identity_keys_previous[kind] - present_entities[kind]:
                        entities[key]['counter'].previous = None
                    identity_keys_previous[kind] = present_entities[kind]
            current_cgroups = (sample.get('cgroups') or {}) if 'cgroup' in windows else {}
            for key in (cgroup_keys_previous - current_cgroups.keys()) if 'cgroup' in windows else ():
                cgroups[key]['counter'].previous = None
            for key, data in current_cgroups.items():
                state = cgroups.setdefault(key, {'counter': _Counters(), 'memory_peak_bytes': None, 'samples': 0})
                state['samples'] += 1
                state['counter'].add(windows['cgroup']['start_ns'], {field: (data.get('cpu_stat') or {}).get(field)
                                          for field in ('usage_usec', 'user_usec', 'system_usec', 'nr_periods', 'nr_throttled', 'throttled_usec')})
                memory = data.get('memory_current_bytes')
                if memory is not None:
                    state['memory_peak_bytes'] = max(state['memory_peak_bytes'] or 0, memory)
                for field, reason in data.get('availability', {}).items():
                    unavailable('cgroup.' + key + '.' + field, reason)
            if 'cgroup' in windows:
                cgroup_keys_previous = set(current_cgroups)

    cpu_results = {}
    for name, state in cpu_counters.items():
        raw = state.result(CPU_FIELDS[:8])
        raw['busy_percent'] = 100 * state.busy_ticks / state.total_ticks if state.total_ticks else None
        raw['busy_percent_valid_intervals'] = state.cpu_intervals
        raw['busy_percent_covered_ns'] = state.cpu_covered_ns
        raw['scope'] = 'machine_wide_per_core; iowait_excluded_from_busy'
        cpu_results[name] = raw
    entity_results = {}
    for key, entity in entities.items():
        raw = entity['counter'].result(ENTITY_KEYS)
        ticks = raw['delta']['cpu_ticks']
        elapsed = raw['covered_ns']['cpu_ticks']
        cpu_ns = ticks * 1_000_000_000 / clock_ticks if ticks is not None and clock_ticks else None
        raw.update(entity['metadata'])
        raw.update({'samples': entity['samples'], 'observed_start_ns': entity['first_ns'],
                    'observed_end_ns': entity['last_ns'], 'cpu_time_ns': cpu_ns,
                    'cpu_percent_one_core': 100 * cpu_ns / elapsed if cpu_ns is not None and elapsed else None,
                    'rss_peak_bytes': entity['rss_peak_bytes'],
                    'scheduling_observations': entity['scheduling_observations'],
                    'schedstat_interpretation': 'cumulative_kernel_counters; zero_does_not_prove_schedstats_enabled'})
        entity_results[key] = raw
    cgroup_results = {}
    for key, state in cgroups.items():
        raw = state['counter'].result(('usage_usec', 'user_usec', 'system_usec', 'nr_periods', 'nr_throttled', 'throttled_usec'))
        raw.update({'memory_peak_bytes': state['memory_peak_bytes'], 'samples': state['samples'],
                    'scope': 'cgroup_v2; may_include_unregistered_processes'})
        cgroup_results[key] = raw
    for state in source_coverage.values():
        state['observed_span_ns'] = state['observed_end_ns'] - state['observed_start_ns'] if state['samples'] >= 2 else None
    observer = observer_counter.result(('process_cpu_ns',))
    observer_elapsed = observer['covered_ns']['process_cpu_ns']
    observer_cpu = observer['delta']['process_cpu_ns']
    observer.update(cpu_percent_one_core=100 * observer_cpu / observer_elapsed if observer_elapsed and observer_cpu is not None else None,
                    scope='whole_collector_process; excludes owned tegrastats child CPU',
                    reason=None if observer_elapsed else 'fewer than two valid observer observations')
    total_observer = total_observer_counter.result(('parent_cpu_ns', 'owned_child_cpu_ns', 'total_cpu_ns'))
    total_elapsed = total_observer['covered_ns']['total_cpu_ns']
    total_cpu = total_observer['delta']['total_cpu_ns']
    observer.update(total_cpu_percent_one_core=100 * total_cpu / total_elapsed if total_elapsed and total_cpu is not None else None,
                    total_scope='collector process plus owned telemetry child; common valid identity intervals only',
                    total_coverage=total_observer, owned_child_unavailable_samples=owned_cpu_unavailable,
                    missing_samples=observer_missing_samples,
                    total_reason=('owned child CPU missing in some observations' if owned_cpu_unavailable else
                                  'observer missing in some observations' if observer_missing_samples else
                                  None if total_elapsed else 'fewer than two valid total CPU observations'))
    telemetry['value_ranges'] = dict.fromkeys(('gpu_utilization_percent', 'gpu_frequency_mhz',
                                              'emc_activity_percent', 'emc_frequency_mhz'))
    telemetry['value_ranges'].update(_finish_ranges(telemetry_ranges) or {})
    gpu_ranges = {key.split('.gpc')[1]: value for key, value in telemetry['value_ranges'].items()
                  if key.startswith('gpu_frequency_mhz.gpc')}
    telemetry['value_ranges']['gpu_frequency_mhz'] = gpu_ranges or None
    telemetry['field_availability'] = {key.removeprefix('jetson.'): value for key, value in availability.items()
                                       if key.startswith('jetson.')}
    telemetry['available'] = telemetry['unique_samples'] > 0
    telemetry['reason'] = None if telemetry['available'] else ('no fresh unique sample received inside window' if telemetry['enabled'] else 'disabled or legacy input')
    costs = summarize_costs(Path(path).with_name(Path(path).stem + '-costs.jsonl'), start_ns, end_ns)
    limits = {key: (options or {}).get(key) for key in ('max_cycle_fraction', 'max_observer_cpu_percent_one_core')}
    observations = {'max_cycle_fraction': costs.get('full_cycle_fraction_max'),
                    'max_observer_cpu_percent_one_core': (observer['total_cpu_percent_one_core']
                        if not owned_cpu_unavailable else None)}
    configured = {key: limit for key, limit in limits.items() if limit is not None}
    missing = [key for key in configured if observations[key] is None]
    if 'max_cycle_fraction' in configured and costs.get('completed_cycles', 0) < costs.get('cycles', 0):
        missing.append('pipeline_completion_incomplete')
    if configured and owned_cpu_unavailable:
        missing.append('owned_child_cpu_incomplete')
    if configured and observer_missing_samples:
        missing.append('observer_cpu_incomplete')
    exceeded = [key for key, limit in configured.items() if observations[key] is not None and observations[key] > limit]
    budget = {'status': 'not_configured' if not configured else
              'not_evaluated' if missing else 'exceeded' if exceeded else 'within_observed_scope',
              'limits': limits, 'observed': observations, 'reasons': missing + exceeded,
              'scope': 'instrumented sampler cost and collector plus owned-child CPU; not business-impact validation'}
    return {'schema_version': 1, 'window_start_ns': start_ns, 'window_end_ns': end_ns,
            'scope': 'observed_subintervals_inside_CSV_measurement_window; no_boundary_extrapolation',
            'snapshot_inclusion': 'entire_collection_interval_inside_window',
            'range_semantics': 'min/max/mean_of_visible_samples; arithmetic_mean; peaks_are_sampled_peaks',
            'counter_units': {'cpu_ticks': 'clock_ticks_per_second', 'runtime_ns': 'nanosecond',
                              'runnable_wait_ns': 'nanosecond', 'timeslices': 'count',
                              'minor_faults': 'count', 'major_faults': 'count',
                              'voluntary_ctxt_switches': 'count', 'nonvoluntary_ctxt_switches': 'count'},
            'coverage': {'samples': count, 'observed_start_ns': first, 'observed_end_ns': last,
                         'observed_span_ns': last - first if count >= 2 else None,
                         'start_gap_ns': first - start_ns if count else None,
                         'end_gap_ns': end_ns - last if count else None,
                         'sample_gap_ns_min': gap_min, 'sample_gap_ns_max': gap_max,
                         'sample_gap_ns_mean': gap_sum / (count - 1) if count >= 2 else None,
                         'sample_duration_ns_max': duration_max if count else None},
            'system': {'per_core_cpu': cpu_results or None, 'meminfo_bytes_ranges': _finish_ranges(meminfo),
                       'temperature_celsius_ranges': _finish_ranges(temperatures),
                       'cpu_frequency_khz_ranges': _finish_ranges(frequencies),
                       'schedstats_setting': _finish_ranges(sched_settings),
                       'rail_power_microwatt_ranges': _finish_ranges(rails),
                       'rail_power_sources': rail_labels or None,
                       'named_device_frequency_hz_ranges': _finish_ranges(device_frequencies),
                       'named_device_frequency_sources': device_sources or None,
                       'memory_scope': 'machine_meminfo; not_process_RSS'},
            'registered_entities': entity_results or None, 'cgroups': cgroup_results or None,
            'availability': availability, 'source_coverage': source_coverage,
            'observer': observer, 'collection_cost': costs, 'overhead_budget': budget,
            'thread_scope': thread_scope, 'jetson_telemetry': telemetry,
            'gpu_utilization': None, 'emc_bandwidth': None,
            'legacy_metric_fields': {'gpu_utilization': 'use jetson_telemetry.value_ranges.gpu_utilization_percent',
                                     'emc_bandwidth': 'exact bandwidth not measured'},
            'unimplemented': ['emc_bandwidth'] + (['gpu_utilization'] if telemetry['value_ranges']['gpu_utilization_percent'] is None else [])}
