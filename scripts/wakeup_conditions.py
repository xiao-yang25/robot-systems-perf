"""Sample conditions of owned wakeup commands through pinned proc directories."""
import os
import time

from perfkit.resources import parse_task_stat


def _read_at(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        return stream.read().decode()


def read_threads(handle):
    """No global discovery, numeric-TID syscalls or signals are needed."""
    info = handle.backend.bound_info(handle.directory)
    if info is None:
        raise RuntimeError('owned wakeup identity unavailable')
    if info.identity != handle.identity:
        raise RuntimeError('owned wakeup identity changed')
    directory = os.open('task', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                        dir_fd=handle.directory)
    try:
        threads = []
        for tid in sorted(int(name) for name in os.listdir(directory) if name.isdigit()):
            task = os.open(str(tid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                           dir_fd=directory)
            try:
                before = parse_task_stat(_read_at(task, 'stat'))
                status = dict(line.split(':', 1) for line in _read_at(task, 'status').splitlines() if ':' in line)
                after = parse_task_stat(_read_at(task, 'stat'))
                if (before['pid'] != tid or after['pid'] != tid
                        or before['starttime_ticks'] != after['starttime_ticks']
                        or int(status['Tgid']) != handle.pid or int(status['Pid']) != tid
                        or any(before[key] != after[key] for key in ('policy', 'rt_priority', 'nice'))):
                    raise RuntimeError('wakeup thread identity/conditions changed during read')
                threads.append({'tid': tid, 'starttime_ticks': before['starttime_ticks'],
                                'affinity_cpu_list': status['Cpus_allowed_list'].strip(),
                                'policy': after['policy'], 'rt_priority': after['rt_priority'],
                                'nice': after['nice']})
            finally:
                os.close(task)
        info = handle.backend.bound_info(handle.directory)
        if info is None:
            raise RuntimeError('owned wakeup identity unavailable during thread read')
        if info.identity != handle.identity:
            raise RuntimeError('owned wakeup identity changed during thread read')
        return threads
    finally:
        os.close(directory)


class ThreadConditions:
    """Check every 250 ms, retaining only first/last/failure observations.

    S01's sole main thread measures; supported cyclictest has exactly one
    non-main measurement thread. Ambiguous topology cannot qualify. These are
    sampled checks, not continuous tracing or a performance acceptance verdict.
    """
    def __init__(self, tool, command, cpu, nice):
        self.tool, self.command, self.cpu, self.nice = tool, list(command), cpu, nice
        self.next_probe = 0
        self.exec_seen = None
        self.worker_identity = None
        self.finished = False
        self.count = self.cost_ns = self.cpu_ns = self.missing_reads = 0
        self.first = self.last = self.failure = None

    def __call__(self, handle):
        now = time.monotonic_ns()
        if self.finished or now < self.next_probe:
            return
        self.next_probe = now + 250_000_000
        start_cpu, start = time.thread_time_ns(), time.monotonic_ns()
        try:
            argv = handle.backend.read_at(handle.directory, 'cmdline').decode().rstrip('\0').split('\0')
            # Skip the exec gate/taskset. The second form supports script-based
            # controlled test tools; both forms require the exact full command.
            if argv != self.command and argv[1:] != self.command:
                if self.exec_seen is not None:
                    raise RuntimeError('owned wakeup command changed after exec')
                return
            if self.exec_seen is None:
                self.exec_seen = now
            if now - self.exec_seen < 250_000_000:
                return  # Allow initial thread creation/configuration.
            threads = read_threads(handle)
            self.check(handle.pid, threads, now)
        except FileNotFoundError:
            self.missing_reads += 1  # Concurrent command/thread exit; qualification still needs evidence.
        except (RuntimeError, OSError, ValueError, KeyError, UnicodeError) as error:
            self.failure = {'reason': str(error), 'monotonic_ns': now,
                            'observation': self.last}
            raise RuntimeError('wakeup thread conditions unavailable/mismatched: ' + str(error)) from error
        finally:
            self.cost_ns += time.monotonic_ns() - start
            self.cpu_ns += time.thread_time_ns() - start_cpu

    def check(self, pid, threads, now):
        main = [thread for thread in threads if thread['tid'] == pid]
        workers = [thread for thread in threads if thread['tid'] != pid]
        if self.tool == 'cyclictest' and main and not workers:
            if self.worker_identity is not None:
                self.finished = True  # Worker joined; main may still be printing the histogram.
            return
        self.last = {'monotonic_ns': now, 'pid': pid, 'threads': threads}
        if len(main) != 1 or len(workers) != (1 if self.tool == 'cyclictest' else 0):
            raise RuntimeError('unsupported measurement-thread topology')
        worker = workers[0] if workers else main[0]
        identity = (worker['tid'], worker['starttime_ticks'])
        if self.worker_identity is not None and identity != self.worker_identity:
            raise RuntimeError('measurement thread identity changed')
        self.worker_identity = identity
        self.last['measurement_tid'] = worker['tid']
        for thread in threads:
            if (thread['affinity_cpu_list'] != str(self.cpu) or thread['policy'] != 0
                    or thread['rt_priority'] != 0 or thread['nice'] != self.nice):
                raise RuntimeError('actual thread mask/policy/priority/nice differs from requested')
        if self.first is None:
            self.first = self.last
        self.count += 1

    def result(self):
        return {'validated': self.failure is None and self.count >= 2,
                'requested': {'affinity_cpu_list': str(self.cpu), 'policy': 0,
                              'rt_priority': 0, 'nice': self.nice},
                'measurement_role': 'sole main thread' if self.tool == 's01' else 'unique non-main thread',
                'interval_ms': 250, 'initial_configuration_grace_ms': 250,
                'observations': self.count, 'first': self.first, 'last': self.last,
                'failure': self.failure, 'missing_reads': self.missing_reads,
                'checker_wall_ns': self.cost_ns, 'checker_cpu_ns': self.cpu_ns,
                'scope': 'sampled owned thread conditions; not continuous coverage or SLA acceptance'}

    def qualify(self):
        if not self.result()['validated']:
            if self.failure is None:
                self.failure = {'reason': 'at least two actual measurement-thread observations required'}
            raise RuntimeError('wakeup measurement-thread evidence missing or mismatched')
