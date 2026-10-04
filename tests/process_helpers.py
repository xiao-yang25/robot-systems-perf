"""Linux test-owned process identities; never signal a discovered numeric PID.

Only stat is read during global discovery. Arguments are read through a pinned
/proc directory for a role candidate whose full ancestry has been verified.
Signals require pidfd support; no kill/killpg fallback is permitted.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


class UnsafeProcessError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    pid: int
    starttime: int


@dataclass(frozen=True)
class ProcessInfo:
    identity: Identity
    ppid: int
    comm: str
    state: str = field(compare=False)


def parse_stat(text):
    prefix, separator, suffix = text.rpartition(')')
    pid, opening, comm = prefix.partition(' (')
    fields = suffix.split()
    if not separator or not opening or len(fields) < 20:
        raise ValueError('invalid process stat')
    return ProcessInfo(Identity(int(pid), int(fields[19])), int(fields[1]), comm, fields[0])


def same_identity(info, identity):
    return info is not None and info.identity == identity


class ProcFS:
    def __init__(self, root='/proc'):
        self.root = Path(root)
        if sys.implementation.name != 'cpython':
            raise UnsafeProcessError('Safe test cleanup requires CPython signal semantics')
        # An unreaped direct child's PID is stable only without automatic
        # SIGCHLD reaping. Refuse inherited/custom handlers before fixtures are
        # launched; reinstall SIG_DFL to clear native SA_NOCLDWAIT flags too.
        # This test-only helper requires CPython's sigaction-based Linux runtime
        # and no concurrent wait/reaper or SIGCHLD changes during registration.
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise UnsafeProcessError('Safe test cleanup requires default SIGCHLD before launching fixtures')
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        # Check support before callers launch a fixture they could not safely stop.
        fd = self.open_pidfd(os.getpid())
        self.close(fd)

    def pids(self):
        return [int(path.name) for path in self.root.iterdir() if path.name.isdigit()]

    def read_info(self, pid):
        try:
            info = parse_stat((self.root / str(pid) / 'stat').read_text())
            return info if info.identity.pid == pid else None
        except (OSError, ValueError, UnicodeError, TypeError):
            return None

    def open_directory(self, pid):
        return os.open(self.root / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def read_at(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            return stream.read()

    def bound_info(self, directory):
        try:
            return parse_stat(self.read_at(directory, 'stat').decode())
        except (OSError, ValueError, UnicodeError, TypeError):
            return None

    def arguments(self, directory):
        return self.read_at(directory, 'cmdline').decode().rstrip('\0').split('\0')

    def open_pidfd(self, pid):
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise UnsafeProcessError('Safe test cleanup requires Linux pidfd_open and pidfd_send_signal')
        try:
            return os.pidfd_open(pid, 0)
        except ProcessLookupError:
            raise
        except OSError as error:
            raise UnsafeProcessError('Cannot bind safe cleanup pidfd: ' + str(error)) from error

    def exited(self, pidfd):
        return bool(select.select([pidfd], [], [], 0)[0])

    def send(self, pidfd, signum):
        signal.pidfd_send_signal(pidfd, signum)

    def close(self, fd):
        os.close(fd)


class OwnedProcess:
    def __init__(self, backend, info, directory, pidfd, process=None, role=None):
        self.backend, self.info = backend, info
        self.identity = info.identity
        self.directory, self.pidfd = directory, pidfd
        self.process, self.role = process, role

    @property
    def pid(self):
        return self.identity.pid

    def exists(self):
        """Whether the same identity is still in proc, including a zombie."""
        current = self.backend.read_info(self.pid)
        return bool(current and current.identity == self.identity)

    def running(self):
        return self.pidfd is not None and not self.backend.exited(self.pidfd)

    def alive(self):
        if not self.running():
            return False
        info = self.backend.bound_info(self.directory)
        current = self.backend.read_info(self.pid)
        return bool(info and current and info.identity == current.identity == self.identity
                    and info.state != 'Z' and current.state != 'Z')

    def send_signal(self, signum):
        if not self.alive():
            return False
        # pidfd remains bound even if the process exits/reuses its PID now.
        try:
            self.backend.send(self.pidfd, signum)
        except ProcessLookupError:
            return False
        return True

    def close(self):
        for name in ('directory', 'pidfd'):
            fd = getattr(self, name)
            if fd is not None:
                self.backend.close(fd)
                setattr(self, name, None)


class OwnedProcesses:
    def __init__(self, backend=None):
        self.backend = backend or ProcFS()
        self.owned = {}
        self.roots = {}
        self.pending = {}

    def _bind(self, info):
        directory = pidfd = None
        try:
            # Recheck before and after both opens, including stat through the
            # pinned directory: a reused numeric path cannot supply arguments.
            if not same_identity(self.backend.read_info(info.identity.pid), info.identity):
                return None
            directory = self.backend.open_directory(info.identity.pid)
            if not same_identity(self.backend.bound_info(directory), info.identity):
                return None
            pidfd = self.backend.open_pidfd(info.identity.pid)
            if (not same_identity(self.backend.bound_info(directory), info.identity) or
                    not same_identity(self.backend.read_info(info.identity.pid), info.identity) or
                    self.backend.exited(pidfd)):
                return None
            handle = OwnedProcess(self.backend, info, directory, pidfd)
            directory = pidfd = None
            return handle
        except (OSError, ValueError, UnicodeError, TypeError):
            return None
        finally:
            for fd in (directory, pidfd):
                if fd is not None:
                    self.backend.close(fd)

    def _stop_pending(self, process, timeout=5):
        pidfd = self.pending[process]
        if pidfd is None:
            # A wait can reap an already-exited child; it never signals a PID.
            try:
                process.wait(timeout=0)
            except subprocess.TimeoutExpired as error:
                raise UnsafeProcessError(
                    'Cleanup incomplete: created test PID %s has no safe pidfd' % process.pid) from error
        else:
            for signum in (signal.SIGTERM, signal.SIGKILL):
                if not self.backend.exited(pidfd):
                    try:
                        self.backend.send(pidfd, signum)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=timeout)
                    break
                except subprocess.TimeoutExpired:
                    if signum == signal.SIGKILL:
                        raise UnsafeProcessError(
                            'Cleanup incomplete: created test child survived pidfd SIGKILL')
            self.backend.close(pidfd)
        del self.pending[process]

    def add(self, process):
        """Register immediately after Popen, without concurrent poll/wait callers.

        With ProcFS's default SIGCHLD/no-auto-reap setup and no concurrent wait,
        before a subsequent poll/wait can reap this direct child, its numeric PID
        cannot be reused. Bind pidfd in that interval even if stat/directory reads
        fail, so partial initialization retains safe creator cleanup authority.
        Discovered descendants cannot use this creator-only guarantee.
        """
        if process.poll() is not None:
            raise UnsafeProcessError('Test process exited before ownership registration')
        self.pending[process] = None
        directory = None
        try:
            before = self.backend.read_info(process.pid)
            pidfd = self.backend.open_pidfd(process.pid)
            self.pending[process] = pidfd
            after = self.backend.read_info(process.pid)
            if before is not None and after is not None and before.identity != after.identity:
                # Even a violated caller/reaper contract must not turn a known
                # mismatch into authority to signal a replacement identity.
                self.backend.close(pidfd)
                self.pending[process] = None
                raise UnsafeProcessError('Test process identity changed before pidfd verification')
            if (before is None or after is None or
                    before.identity != after.identity):
                raise UnsafeProcessError('Test process identity unavailable or changed during registration')
            directory = self.backend.open_directory(process.pid)
            if (not same_identity(self.backend.bound_info(directory), before.identity) or
                    not same_identity(self.backend.read_info(process.pid), before.identity) or
                    self.backend.exited(pidfd)):
                raise UnsafeProcessError('Test process identity changed during directory binding')
            handle = OwnedProcess(self.backend, before, directory, pidfd, process)
            self.owned[handle.identity] = self.roots[handle.identity] = handle
            directory = None
            del self.pending[process]
            return handle
        except (OSError, ValueError, UnicodeError, TypeError, UnsafeProcessError) as error:
            try:
                self._stop_pending(process)
            except (OSError, UnsafeProcessError) as cleanup_error:
                raise UnsafeProcessError(str(error) + '; ' + str(cleanup_error)) from error
            raise UnsafeProcessError(str(error) + '; created test child safely reaped') from error
        finally:
            if directory is not None:
                self.backend.close(directory)

    def handle(self, process):
        for handle in self.roots.values():
            if handle.process is process:
                return handle
        raise UnsafeProcessError('Process was not launched and registered by this test')

    def signal(self, process, signum):
        return self.handle(process).send_signal(signum)

    def _lineage(self, info, snapshot):
        chain, seen = [], set()
        while info.identity not in self.roots:
            if info.identity in seen:
                return None
            seen.add(info.identity)
            chain.append(info)
            info = snapshot.get(info.ppid)
            if info is None:
                return None
        root = self.roots[info.identity]
        if not root.alive():
            return None
        chain.append(info)
        return chain

    def _valid_chain(self, chain):
        # Check every edge twice: do not accept a stale parent snapshot as proof.
        return all(self.backend.read_info(info.identity.pid) == info
                   for _ in range(2) for info in reversed(chain))

    def discover(self, comm_roles, argument_role=None):
        """Claim descendants matching exact comm roles or a scoped argv role.

        comm_roles maps a comm to a role (no argument read) or None (the supplied
        argument_role must identify the role). Never match process names globally.
        """
        snapshot = {}
        for pid in self.backend.pids():
            info = self.backend.read_info(pid)
            if info:
                snapshot[pid] = info
        found = []
        for info in snapshot.values():
            if info.identity in self.roots or info.comm not in comm_roles:
                continue
            existing = self.owned.get(info.identity)
            if existing:
                if existing.alive():
                    found.append(existing)
                continue
            chain = self._lineage(info, snapshot)
            if chain is None or not self._valid_chain(chain):
                continue
            handle = self._bind(info)
            if handle is None:
                continue
            try:
                if not self._valid_chain(chain) or not handle.alive():
                    continue
                role = comm_roles[info.comm]
                if role is None and argument_role:
                    # The proc directory is pinned to the validated identity.
                    before = self.backend.bound_info(handle.directory)
                    if before != info or not self._valid_chain(chain) or not handle.alive():
                        continue
                    arguments = self.backend.arguments(handle.directory)
                    after = self.backend.bound_info(handle.directory)
                    if before != info or after != info:
                        continue
                    role = argument_role(info.comm, arguments)
                if role is None or not self._valid_chain(chain) or not handle.alive():
                    continue
                handle.role = role
                self.owned[info.identity] = handle
                found.append(handle)
                handle = None
            except (OSError, ValueError, UnicodeError, TypeError):
                continue
            finally:
                if handle is not None:
                    handle.close()
        return found

    def cleanup(self, timeout=5):
        pending_errors = []
        for process in list(self.pending):
            try:
                self._stop_pending(process, timeout)
            except (OSError, UnsafeProcessError) as error:
                pending_errors.append(str(error))
        handles = list(reversed(self.owned.values()))
        try:
            for signum in (signal.SIGTERM, signal.SIGKILL):
                for handle in handles:
                    handle.send_signal(signum)
                end = time.monotonic() + timeout
                while any(handle.running() for handle in handles) and time.monotonic() < end:
                    time.sleep(.02)
                if not any(handle.running() for handle in handles):
                    break
            remaining = [handle.identity for handle in handles if handle.running()]
            for handle in handles:
                if handle.process is not None:
                    handle.process.wait(timeout=timeout)
            if remaining:
                raise UnsafeProcessError('Owned test processes survived pidfd cleanup: ' + str(remaining))
            if pending_errors:
                raise UnsafeProcessError('; '.join(pending_errors))
        finally:
            for handle in handles:
                # Retain safe descriptors if cleanup failed while a task is live.
                if not handle.running():
                    handle.close()


def benchmark_role(comm, argv):
    # Called only for ancestry-verified test children through their pinned proc fd.
    if comm == 'ros_bench' and '--role' in argv:
        index = argv.index('--role') + 1
        if index < len(argv) and argv[index] in ('publisher', 'subscriber'):
            return argv[index]
    if comm == 'periodic_bench' and '--load' in argv:
        index = argv.index('--load') + 1
        if index < len(argv) and argv[index] == 'cpu':
            return 'cpu-load'
    return None


def benchmark_children(owner):
    return owner.discover({'ros_bench': None, 'periodic_bench': None}, benchmark_role)
