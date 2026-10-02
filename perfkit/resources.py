"""Read-only Linux resource telemetry and streaming, sample-scoped summaries.

Window tags describe runner state, not the measurement boundary. The CSV's
monotonic timestamps define that boundary. Counters are differenced only across
consecutive samples inside it; no interpolation or extrapolation is performed.
"""

import json
import math
import os
from pathlib import Path, PurePosixPath
import threading
import time
from .lifecycle import defer_interrupts


PROC_ROOT = Path('/proc')
SYS_ROOT = Path('/sys')
CPU_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq',
              'steal', 'guest', 'guest_nice')


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
    fields = text[right + 1:].split()  # starts at field 3, state
    if len(fields) < 39:
        raise ValueError('short task stat')
    def number(field):
        return int(fields[field - 3])
    return {'pid': pid, 'comm': text[left + 1:right], 'state': fields[0],
            'minor_faults': number(10), 'major_faults': number(12),
            'utime_ticks': number(14), 'stime_ticks': number(15),
            'priority': number(18), 'nice': number(19),
            'num_threads': number(20), 'starttime_ticks': number(22),
            'rss_pages': number(24), 'processor': number(39),
            'rt_priority': number(40), 'policy': number(41)}


def _read(path, parser=lambda text: text.strip()):
    try:
        return parser(path.read_text(encoding='utf-8')), None
    except (OSError, ValueError, IndexError) as exc:
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


def _task(path, page_size):
    stat, reason = _read(path / 'stat', parse_task_stat)
    if stat is None:
        return {'stat': None, 'schedstat': None, 'status': None,
                'availability': {'stat': reason, 'schedstat': 'task unavailable',
                                 'status': 'task unavailable'}}
    sched, sched_reason = _read(path / 'schedstat', _schedstat)
    status, status_reason = _read(path / 'status', _status)
    verify, verify_reason = _read(path / 'stat', parse_task_stat)
    if verify is None or verify['starttime_ticks'] != stat['starttime_ticks']:
        return {'stat': None, 'schedstat': None, 'status': None,
                'availability': {'stat': verify_reason or 'task identity changed',
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

    def __init__(self, output: Path, interval: float):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('resource sampling interval must be positive and finite')
        self.output = Path(output)
        self.interval = interval
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

    def register(self, pid, role):
        pid = int(pid)
        if pid <= 0:
            raise ValueError('PID must be positive')
        stat, reason = _read(PROC_ROOT / str(pid) / 'stat', parse_task_stat)
        with self._lock:
            self._serial += 1
            self._registered[pid] = {'pid': pid, 'role': str(role),
                                     'registration_id': self._serial,
                                     'starttime_ticks': stat['starttime_ticks'] if stat else None,
                                     'identity_reason': reason}

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
        self._thread = threading.Thread(target=self._run, name='resource-sampler', daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._stream.close()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with defer_interrupts():
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError as exc:
                    self.error = self.error or type(exc).__name__ + ': ' + str(exc)
        return False

    def _system(self):
        cpus, cpu_reason = _read(PROC_ROOT / 'stat', parse_cpu_stat)
        memory, memory_reason = _read(PROC_ROOT / 'meminfo', _meminfo)
        sched_enabled, enabled_reason = _read(PROC_ROOT / 'sys/kernel/sched_schedstats', int)
        temperatures = {}
        for path in sorted((SYS_ROOT / 'class/thermal').glob('thermal_zone*/temp')):
            value, reason = _read(path, lambda text: int(text.strip()) / 1000)
            label, _ = _read(path.parent / 'type')
            temperatures[path.parent.name] = {'celsius': value, 'label': label, 'reason': reason}
        frequencies = {}
        for cpu in sorted((SYS_ROOT / 'devices/system/cpu').glob('cpu[0-9]*')):
            if not cpu.name[3:].isdigit():
                continue
            # scaling_cur_freq may be a driver estimate, not a measured clock.
            path = cpu / 'cpufreq/scaling_cur_freq'
            value, reason = _read(path, int)
            frequencies[cpu.name] = {'khz': value, 'source': 'scaling_cur_freq', 'reason': reason}
        rails = {}
        for path in sorted((SYS_ROOT / 'class/hwmon').glob('hwmon*/power*_input')):
            value, reason = _read(path, int)
            label, _ = _read(path.with_name(path.name.replace('_input', '_label')))
            chip, _ = _read(path.parent / 'name')
            rails[path.parent.name + '/' + path.name] = {
                'microwatts': value, 'label': label, 'chip': chip,
                'source': 'hwmon_power_input', 'reason': reason}
        device_frequencies = {}
        for path in sorted((SYS_ROOT / 'class/devfreq').glob('*/cur_freq')):
            name = path.parent.name
            if not any(kind in name.lower() for kind in ('gpu', 'emc')):
                continue
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
                                 'temperatures': None if temperatures else 'thermal sysfs unavailable',
                                 'cpu_frequencies': None if frequencies else 'CPU frequency sysfs unavailable',
                                 'rail_power': None if rails else 'hwmon power inputs not exposed',
                                 'device_frequencies': None if device_frequencies else 'named GPU/EMC devfreq not exposed'},
                'gpu_utilization': None, 'emc_bandwidth': None,
                'unimplemented': ['gpu_utilization', 'emc_bandwidth']}

    def _snapshot(self):
        with self._lock:
            registrations = [dict(item) for item in self._registered.values()]
            window = dict(self._window)
        started = time.monotonic_ns()
        processes, cgroups = [], {}
        own_cgroup, own_reason = _cgroup_path('self')
        cgroup_paths = {own_cgroup} if own_cgroup else set()
        for registration in registrations:
            pid = registration['pid']
            base = PROC_ROOT / str(pid)
            data = _task(base, self._page_size)
            expected = registration['starttime_ticks']
            actual = data['stat']['starttime_ticks'] if data['stat'] else None
            if expected is None or actual != expected:
                data = {'stat': None, 'schedstat': None, 'status': None,
                        'availability': {'stat': registration['identity_reason'] or 'PID identity unavailable or changed',
                                         'schedstat': 'PID identity not verified',
                                         'status': 'PID identity not verified'}}
                tasks = None
                task_reason = 'PID identity not verified'
                cgroup, cgroup_reason = None, 'PID identity not verified'
            else:
                try:
                    paths = sorted(path for path in (base / 'task').iterdir() if path.name.isdigit())
                    tasks = []
                    for path in paths:
                        task = _task(path, self._page_size)
                        task['tid'] = int(path.name)
                        tasks.append(task)
                    task_reason = None
                except OSError as exc:
                    tasks, task_reason = None, type(exc).__name__ + ': ' + str(exc)
                cgroup, cgroup_reason = _cgroup_path(pid)
                verify, _ = _read(base / 'stat', parse_task_stat)
                if verify is None or verify['starttime_ticks'] != expected:
                    data = {'stat': None, 'schedstat': None, 'status': None,
                            'availability': {'stat': 'PID identity changed during snapshot',
                                             'schedstat': 'PID identity not verified', 'status': 'PID identity not verified'}}
                    tasks, task_reason = None, 'PID identity not verified'
                    cgroup, cgroup_reason = None, 'PID identity not verified'
            if cgroup:
                cgroup_paths.add(cgroup)
            processes.append(dict(registration, **data, tasks=tasks,
                                  tasks_reason=task_reason, cgroup=cgroup, cgroup_reason=cgroup_reason))
        for path in sorted(cgroup_paths):
            cpu, cpu_reason = _read(Path(path) / 'cpu.stat', _kv)
            memory, memory_reason = _read(Path(path) / 'memory.current', int)
            cgroups[path] = {'cpu_stat': cpu, 'memory_current_bytes': memory,
                             'availability': {'cpu_stat': cpu_reason, 'memory_current_bytes': memory_reason}}
        system = self._system()
        return {'schema_version': 1, 'monotonic_ns': started,
                'sample_end_ns': time.monotonic_ns(), 'window': window,
                'window_semantics': 'tags_only; measurement_window_uses_CSV_monotonic_timestamps',
                'clock_ticks_per_second': self._clock_ticks, 'page_size_bytes': self._page_size,
                'system': system, 'processes': processes, 'cgroups': cgroups,
                'sampler_cgroup': own_cgroup, 'sampler_cgroup_reason': own_reason}

    def _run(self):
        try:
            while not self._stop.is_set():
                before = time.monotonic()
                self._stream.write(json.dumps(self._snapshot(), allow_nan=False) + '\n')
                self._stream.flush()
                self._stop.wait(max(0, self.interval - (time.monotonic() - before)))
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
    identity_keys_previous = set()
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
            unavailable('sampler_cgroup', sample.get('sampler_cgroup_reason'))
            ticks = sample.get('clock_ticks_per_second')
            if clock_ticks is None:
                clock_ticks = ticks
            elif ticks != clock_ticks:
                raise ValueError('resource clock tick frequency changed')
            system = sample.get('system') or {}
            for field, reason in system.get('availability', {}).items():
                unavailable('system.' + field, reason)
            cpus = system.get('per_cpu_ticks') or {}
            for key in cpu_keys_previous - cpus.keys():
                cpu_counters[key].previous = None
            for name, values in cpus.items():
                # Do not invent absent fields, nor double-count guest/guest_nice.
                counters = {key: values.get(key) for key in CPU_FIELDS[:8] if key in values}
                state = cpu_counters.setdefault(name, _CpuCounters())
                state.add(now, counters)
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
            present_entities = set()
            for process in sample.get('processes') or []:
                stat = process.get('stat')
                base = 'pid={}:registration={}:start={}'.format(process['pid'], process['registration_id'], process.get('starttime_ticks'))
                unavailable(base + '.tasks', process.get('tasks_reason'))
                unavailable(base + '.cgroup', process.get('cgroup_reason'))
                if stat is None:
                    for field, reason in process.get('availability', {}).items():
                        unavailable(base + '.' + field, reason)
                    continue
                if stat['starttime_ticks'] != process.get('starttime_ticks'):
                    unavailable(base + '.stat', 'PID identity differs from registration')
                    continue
                metadata = {'pid': process['pid'], 'role': process['role'],
                            'registration_id': process['registration_id'],
                            'starttime_ticks': stat['starttime_ticks'], 'kind': 'process',
                            'cpu_scope': 'all_process_threads',
                            'schedstat_and_context_switch_scope': 'leader_thread_only',
                            'rss_scope': 'process_address_space', 'cgroup': process.get('cgroup')}
                observe_entity(base, process, metadata, now)
                present_entities.add(base)
                for task in process.get('tasks') or []:
                    if task.get('stat') is None:
                        for field, reason in task.get('availability', {}).items():
                            unavailable(base + '.tid=' + str(task['tid']) + '.' + field, reason)
                        continue
                    key = base + ':tid={}:start={}'.format(task['tid'], task['stat']['starttime_ticks'])
                    metadata = {'pid': process['pid'], 'tid': task['tid'], 'role': process['role'],
                                'registration_id': process['registration_id'],
                                'starttime_ticks': task['stat']['starttime_ticks'], 'kind': 'thread',
                                'cpu_scope': 'thread', 'schedstat_and_context_switch_scope': 'thread',
                                'rss_scope': 'shared_process_address_space'}
                    observe_entity(key, task, metadata, now)
                    present_entities.add(key)
            for key in identity_keys_previous - present_entities:
                entities[key]['counter'].previous = None
            identity_keys_previous = present_entities
            current_cgroups = sample.get('cgroups') or {}
            for key in cgroup_keys_previous - current_cgroups.keys():
                cgroups[key]['counter'].previous = None
            for key, data in current_cgroups.items():
                state = cgroups.setdefault(key, {'counter': _Counters(), 'memory_peak_bytes': None, 'samples': 0})
                state['samples'] += 1
                state['counter'].add(now, {field: (data.get('cpu_stat') or {}).get(field)
                                          for field in ('usage_usec', 'user_usec', 'system_usec', 'nr_periods', 'nr_throttled', 'throttled_usec')})
                memory = data.get('memory_current_bytes')
                if memory is not None:
                    state['memory_peak_bytes'] = max(state['memory_peak_bytes'] or 0, memory)
                for field, reason in data.get('availability', {}).items():
                    unavailable('cgroup.' + key + '.' + field, reason)
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
            'availability': availability,
            'gpu_utilization': None, 'emc_bandwidth': None,
            'unimplemented': ['gpu_utilization', 'emc_bandwidth']}
