"""Read-only process discovery; CPU activity is a candidate clue, not semantics.

Only proc stat/status/cgroup and the exe link basename are observed. Identities
are (pid, starttime_ticks); selection state is bounded to the latest scan.
"""

import math
import os
from pathlib import Path
import re

from .resources import parse_task_stat


PF_KTHREAD = 0x00200000


def _increment(counts, reason):
    counts[reason] = counts.get(reason, 0) + 1


def _failure(field, error):
    if isinstance(error, PermissionError):
        return field + '_permission_denied'
    if isinstance(error, (FileNotFoundError, ProcessLookupError)):
        return field + '_missing'
    if isinstance(error, OSError):
        return field + '_io_error'
    return field + '_invalid'


def _stat(text, pid):
    stat = parse_task_stat(text)
    # flags is field 9; parse_task_stat deliberately exposes resource fields.
    flags = int(text[text.rfind(')') + 1:].split()[6])
    if (stat['pid'] != pid or stat['starttime_ticks'] < 0 or
            stat['utime_ticks'] < 0 or stat['stime_ticks'] < 0 or flags < 0):
        raise ValueError('invalid process identity or counters')
    return stat, flags


def _identity_status(text):
    uid, kernel = None, False
    for line in text.splitlines():
        name, separator, value = line.partition(':')
        if not separator:
            continue
        if name == 'Uid':
            fields = value.split()
            # proc status supplies real, effective, saved and filesystem UIDs.
            if len(fields) == 4 and all(item.isdecimal() for item in fields):
                uid = int(fields[0])
        elif name == 'Kthread':
            kernel = value.strip() == '1'
    return uid, kernel


def _cgroup_paths(text):
    paths = []
    for line in text.splitlines():
        fields = line.split(':', 2)
        if len(fields) != 3 or not fields[0].isdecimal() or not fields[2].startswith('/'):
            raise ValueError('invalid proc cgroup record')
        if fields[2] not in paths:
            paths.append(fields[2])
    return paths


def scan_processes(proc_root=Path('/proc'), exclude_pids=()):
    """Scan visible proc PIDs, skipping unverifiable identities with reason counts.

    Proc root iteration errors propagate; inaccessible individual processes do
    not invalidate other observations. An unavailable exe link is optional and
    never establishes that a process is a kernel thread.
    """
    root = Path(proc_root)
    excluded = set(exclude_pids)
    # Materialize iteration before per-process handling so root failures raise.
    entries = sorted((path for path in root.iterdir()
                      if path.name.isascii() and path.name.isdecimal()),
                     key=lambda path: int(path.name))
    processes = []
    scan = {'candidate_count': len(entries), 'process_count': 0,
            'skipped_count': 0, 'excluded_count': 0,
            'skipped_by_reason': {}, 'optional_unavailable_by_reason': {}}
    for base in entries:
        pid = int(base.name)
        if pid in excluded:
            scan['excluded_count'] += 1
            continue
        field = 'stat'
        try:
            stat, flags = _stat((base / 'stat').read_text(encoding='utf-8'), pid)
            field = 'status'
            uid, kernel = _identity_status((base / 'status').read_text(encoding='utf-8'))
            if uid is None:
                scan['skipped_count'] += 1
                _increment(scan['skipped_by_reason'], 'uid_unknown')
                continue
            try:
                cgroups = _cgroup_paths((base / 'cgroup').read_text(encoding='utf-8'))
            except (OSError, ValueError, UnicodeError) as error:
                cgroups = []
                _increment(scan['optional_unavailable_by_reason'], _failure('cgroup', error))
            try:
                exe_name = Path(os.readlink(base / 'exe')).name or None
            except OSError as error:
                exe_name = None
                _increment(scan['optional_unavailable_by_reason'], _failure('exe', error))
            field = 'stat_verify'
            verify, _ = _stat((base / 'stat').read_text(encoding='utf-8'), pid)
            if verify['starttime_ticks'] != stat['starttime_ticks']:
                scan['skipped_count'] += 1
                _increment(scan['skipped_by_reason'], 'identity_changed')
                continue
        except (OSError, ValueError, IndexError, UnicodeError) as error:
            scan['skipped_count'] += 1
            _increment(scan['skipped_by_reason'], _failure(field, error))
            continue
        processes.append({'pid': pid, 'starttime_ticks': stat['starttime_ticks'],
                          'comm': stat['comm'], 'exe_name': exe_name, 'uid': uid,
                          'cpu_ticks': stat['utime_ticks'] + stat['stime_ticks'],
                          'cgroup_paths': cgroups,
                          'is_kernel': kernel or bool(flags & PF_KTHREAD)})
    scan['process_count'] = len(processes)
    return {'processes': processes, 'scan': scan}


class DiscoverySelector:
    """Keep scoped selections alive through inactivity until identity disappears.

    Name includes and explicit PIDs trigger selection; UID, exclusions and
    cgroups constrain every trigger and retained selection. Rates use one CPU
    core as 100%, so a multithreaded process may exceed 100%.
    """

    def __init__(self, config, clock_ticks):
        if (not isinstance(clock_ticks, (int, float)) or isinstance(clock_ticks, bool)
                or not math.isfinite(clock_ticks) or clock_ticks <= 0):
            raise ValueError('clock_ticks must be positive and finite')
        self.clock_ticks = clock_ticks
        uids = config.get('uids')
        self.uids = None if uids is None else set(uids)
        self.pids = set(config.get('pids', []))
        self.include = [re.compile(item) for item in config.get('include_names', [])]
        self.exclude = [re.compile(item) for item in config.get('exclude_names', [])]
        self.cgroups = [re.compile(item) for item in config.get('cgroup_patterns', [])]
        self.active = config.get('active_cpu_percent', 0)
        if (not isinstance(self.active, (int, float)) or isinstance(self.active, bool)
                or not math.isfinite(self.active) or self.active < 0):
            raise ValueError('active_cpu_percent must be nonnegative and finite')
        self.cap = config.get('max_targets', 64)
        if not isinstance(self.cap, int) or isinstance(self.cap, bool) or self.cap < 0:
            raise ValueError('max_targets must be a nonnegative integer')
        for name, values in (('uids', self.uids), ('pids', self.pids)):
            if values is not None and any(not isinstance(value, int) or isinstance(value, bool)
                                          or value < (1 if name == 'pids' else 0)
                                          for value in values):
                raise ValueError(name + ' must contain valid integers')
        self._previous = {}
        self._selected = set()

    @staticmethod
    def _name_matches(patterns, process):
        return any(pattern.search(name) for pattern in patterns
                   for name in (process.get('comm'), process.get('exe_name')) if name)

    def _eligible(self, process):
        uid = process.get('uid')
        return (uid is not None and not process.get('is_kernel', False)
                and (self.uids is None or uid in self.uids)
                and not self._name_matches(self.exclude, process)
                and (not self.cgroups or any(pattern.search(path)
                     for pattern in self.cgroups for path in process.get('cgroup_paths', []))))

    def update(self, processes, now_ns):
        eligible, candidates, previous = 0, [], {}
        for process in processes:
            key = (process['pid'], process['starttime_ticks'])
            ticks = process['cpu_ticks']
            old = self._previous.get(key)
            rate = None
            if old is not None and now_ns > old[0] and ticks >= old[1]:
                rate = 100 * (ticks - old[1]) * 1_000_000_000 / self.clock_ticks / (now_ns - old[0])
            previous[key] = (now_ns, ticks)
            if not self._eligible(process):
                continue
            eligible += 1
            reasons = []
            if process['pid'] in self.pids:
                reasons.append('explicit_pid')
            if self._name_matches(self.include, process):
                reasons.append('include_name')
            explicit = bool(reasons)
            if self.active > 0 and rate is not None and rate >= self.active:
                reasons.append('active_cpu')
            retained = key in self._selected
            if retained:
                reasons.append('retained_identity')
            if not reasons:
                continue
            target = dict(process, selection_reasons=reasons, observed_cpu_percent=rate)
            candidates.append((retained, explicit, rate, key, target))
        # Keeping only the current scan prevents counter bridges across absence
        # and unbounded retention of dead PID histories.
        self._previous = previous
        candidates.sort(key=lambda item: (not item[0], not item[1],
                                          -(item[2] if item[2] is not None else -1), item[3][0]))
        chosen = candidates[:self.cap]
        self._selected = {item[3] for item in chosen}
        return {'targets': [item[4] for item in chosen],
                'selection': {'eligible_count': eligible, 'matched_count': len(candidates),
                              'selected_count': len(chosen),
                              'omitted_count': len(candidates) - len(chosen)}}
