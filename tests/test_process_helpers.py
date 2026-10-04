import importlib
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tests.process_helpers import (
    Identity, OwnedProcesses, ProcessInfo, ProcFS, UnsafeProcessError,
    benchmark_children, parse_stat,
)


def info(pid, parent, name, start=100):
    return ProcessInfo(Identity(pid, start), parent, name, 'S')


class FakePopen:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class FakeProc:
    """No children interface, cmdline scan or PID signal operation exists here."""
    def __init__(self):
        self.processes = {10: info(10, 1, 'test-root')}
        self.arguments_by_pid = {}
        self.argument_reads = []
        self.signals = []
        self.closed = []
        self.on_read = self.on_directory = self.on_pidfd = self.on_arguments = None
        self.on_send = self.on_bound = None
        self.no_pidfd = False

    def pids(self):
        return list(self.processes)

    def read_info(self, pid):
        if self.on_read:
            self.on_read(pid)
        return self.processes.get(pid)

    def open_directory(self, pid):
        directory = ('directory', self.processes[pid].identity)
        if self.on_directory:
            self.on_directory(pid)
        return directory

    def bound_info(self, directory):
        identity = directory[1]
        if self.on_bound:
            self.on_bound(identity.pid)
        current = self.processes.get(identity.pid)
        return current if current and current.identity == identity else None

    def open_pidfd(self, pid):
        if self.no_pidfd:
            raise UnsafeProcessError('pidfd unavailable')
        descriptor = ('pidfd', self.processes[pid].identity)
        if self.on_pidfd:
            self.on_pidfd(pid)
        return descriptor

    def exited(self, descriptor):
        identity = descriptor[1]
        current = self.processes.get(identity.pid)
        return current is None or current.identity != identity

    def arguments(self, directory):
        identity = directory[1]
        self.argument_reads.append(identity)
        arguments = self.arguments_by_pid[identity.pid]
        if self.on_arguments:
            self.on_arguments(identity.pid)
        return arguments

    def send(self, descriptor, signum):
        if self.on_send:
            self.on_send()
        # Emulate the kernel guarantee: descriptor targets its original task.
        if not self.exited(descriptor):
            self.signals.append((descriptor[1], signum))
            del self.processes[descriptor[1].pid]

    def close(self, descriptor):
        self.closed.append(descriptor)


class ProcessHelpersTests(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        self.owner = OwnedProcesses(self.proc)
        self.root = self.owner.add(FakePopen(10))

    def child(self, pid=20, parent=10, comm='ros_bench', start=200):
        self.proc.processes[pid] = info(pid, parent, comm, start)
        self.proc.arguments_by_pid[pid] = ['ros_bench', '--role', 'publisher']

    def test_stat_comm_spaces_and_parentheses(self):
        record = parse_stat('21 (name (a)) S 10 ' + ' '.join(['0'] * 17) + ' 999')
        self.assertEqual(record.identity, Identity(21, 999))
        self.assertEqual(record.ppid, 10)
        self.assertEqual(record.comm, 'name (a)')

    def test_created_root_stat_failure_still_reaps_with_pidfd(self):
        proc = FakeProc()
        owner = OwnedProcesses(proc)
        proc.read_info = lambda pid: None
        with self.assertRaisesRegex(UnsafeProcessError, 'safely reaped'):
            owner.add(FakePopen(10))
        self.assertEqual(proc.signals, [(Identity(10, 100), signal.SIGTERM)])
        self.assertEqual(owner.pending, {})

    def test_created_root_directory_failure_still_reaps_with_pidfd(self):
        proc = FakeProc()
        owner = OwnedProcesses(proc)
        def denied(pid):
            raise PermissionError('fixture proc directory unreadable')
        proc.open_directory = denied
        with self.assertRaisesRegex(UnsafeProcessError, 'safely reaped'):
            owner.add(FakePopen(10))
        self.assertEqual(proc.signals, [(Identity(10, 100), signal.SIGTERM)])
        self.assertEqual(owner.pending, {})

    def test_created_root_without_pidfd_retains_explicit_cleanup_gap(self):
        proc = FakeProc()
        proc.no_pidfd = True
        owner = OwnedProcesses(proc)
        process = FakePopen(10)
        def still_live(timeout=None):
            raise subprocess.TimeoutExpired('fixture', timeout)
        process.wait = still_live
        with self.assertRaisesRegex(UnsafeProcessError, 'Cleanup incomplete'):
            owner.add(process)
        self.assertEqual(owner.pending, {process: None})
        with self.assertRaisesRegex(UnsafeProcessError, 'Cleanup incomplete'):
            owner.cleanup(timeout=0)
        self.assertEqual(proc.signals, [])

    def test_root_comm_change_during_launch_does_not_lose_ownership(self):
        proc = FakeProc()
        proc.on_directory = lambda pid: proc.processes.__setitem__(
            pid, info(pid, 1, 'fixture-ready', 100))
        owner = OwnedProcesses(proc)
        root = owner.add(FakePopen(10))
        self.assertEqual(root.identity, Identity(10, 100))
        self.assertTrue(root.alive())

    def test_created_root_known_identity_change_never_signals_replacement(self):
        proc = FakeProc()
        owner = OwnedProcesses(proc)
        original = proc.open_pidfd
        def replaced(pid):
            proc.processes[pid] = info(pid, 1, 'unrelated-business', 101)
            return original(pid)
        proc.open_pidfd = replaced
        with self.assertRaisesRegex(UnsafeProcessError, 'identity changed'):
            owner.add(FakePopen(10))
        self.assertEqual(proc.signals, [])

    def test_missing_children_interface_uses_ancestry_and_roles(self):
        self.child()
        self.child(21, comm='periodic_bench')
        self.proc.arguments_by_pid[21] = ['periodic_bench', '--load', 'cpu']
        self.child(22, 20)
        self.proc.arguments_by_pid[22] = ['ros_bench', '--role', 'subscriber']
        found = benchmark_children(self.owner)
        self.assertEqual({child.role for child in found}, {'publisher', 'subscriber', 'cpu-load'})
        self.assertEqual({child.pid for child in found}, {20, 21, 22})

    def test_unrelated_same_name_never_reads_arguments_or_signals(self):
        self.child(20, 1)
        self.child(21, 20)
        self.assertEqual(benchmark_children(self.owner), [])
        self.owner.cleanup(timeout=0)
        self.assertEqual(self.proc.argument_reads, [])
        self.assertEqual(self.proc.signals, [(self.root.identity, signal.SIGTERM)])

    def test_comm_role_never_reads_arguments(self):
        self.child(comm='rp_tegra')
        found = self.owner.discover({'rp_tegra': 'telemetry'})
        self.assertEqual([child.role for child in found], ['telemetry'])
        self.assertEqual(self.proc.argument_reads, [])

    def test_changed_root_identity_rejects_descendants(self):
        self.child()
        self.proc.processes[10] = info(10, 1, 'test-root', 101)
        self.assertEqual(benchmark_children(self.owner), [])
        self.assertEqual(self.proc.argument_reads, [])
        self.assertFalse(self.root.send_signal(signal.SIGTERM))
        self.assertEqual(self.proc.signals, [])

    def test_identity_changes_between_snapshot_and_claim(self):
        self.child()
        calls = 0
        def change(pid):
            nonlocal calls
            if pid == 20:
                calls += 1
                if calls == 2:
                    self.proc.processes[20] = info(20, 10, 'ros_bench', 201)
        self.proc.on_read = change
        self.assertEqual(benchmark_children(self.owner), [])
        self.assertEqual(self.proc.argument_reads, [])

    def test_parent_edge_changes_reject_claim(self):
        self.child()
        calls = 0
        def change(pid):
            nonlocal calls
            if pid == 20:
                calls += 1
                if calls == 2:
                    self.proc.processes[20] = info(20, 1, 'ros_bench', 200)
        self.proc.on_read = change
        self.assertEqual(benchmark_children(self.owner), [])
        self.assertEqual(self.proc.argument_reads, [])

    def test_open_directory_and_pidfd_identity_races_reject_claim(self):
        for hook in ('on_directory', 'on_pidfd'):
            with self.subTest(hook=hook):
                self.setUp()
                self.child()
                def change(pid):
                    if pid == 20:
                        self.proc.processes[20] = info(20, 10, 'ros_bench', 201)
                setattr(self.proc, hook, change)
                self.assertEqual(benchmark_children(self.owner), [])
                self.assertEqual(self.proc.argument_reads, [])
                self.assertTrue(self.proc.closed)

    def test_identity_changes_just_before_arguments_are_not_read(self):
        self.child()
        calls = 0
        def change(pid):
            nonlocal calls
            if pid == 20:
                calls += 1
                if calls == 4:
                    self.proc.processes[20] = info(20, 10, 'ros_bench', 201)
        self.proc.on_bound = change
        self.assertEqual(benchmark_children(self.owner), [])
        self.assertEqual(self.proc.argument_reads, [])

    def test_identity_changes_during_arguments_reject_claim(self):
        self.child()
        self.proc.on_arguments = lambda pid: self.proc.processes.__setitem__(
            pid, info(pid, 10, 'ros_bench', 201))
        self.assertEqual(benchmark_children(self.owner), [])
        self.assertNotIn(Identity(20, 200), self.owner.owned)
        self.assertEqual(self.proc.signals, [])
        self.assertEqual(self.proc.argument_reads, [Identity(20, 200)])

    def test_parent_identity_changes_during_arguments_reject_claim(self):
        self.child()
        self.proc.on_arguments = lambda pid: self.proc.processes.__setitem__(
            10, info(10, 1, 'test-root', 101))
        self.assertEqual(benchmark_children(self.owner), [])
        self.assertNotIn(Identity(20, 200), self.owner.owned)

    def test_missing_stat_does_not_report_live_pidfd_cleaned(self):
        self.child()
        child = benchmark_children(self.owner)[0]
        original = self.proc.read_info
        self.proc.read_info = lambda pid: None if pid == 20 else original(pid)
        with self.assertRaisesRegex(UnsafeProcessError, 'survived pidfd cleanup'):
            self.owner.cleanup(timeout=0)
        self.assertIsNotNone(child.pidfd)
        self.assertFalse(any(identity.pid == 20 for identity, _ in self.proc.signals))
        self.proc.read_info = original
        self.owner.cleanup(timeout=0)
        self.assertIsNone(child.pidfd)

    def test_claimed_process_exit_and_pid_reuse_never_signal_replacement(self):
        self.child()
        child = benchmark_children(self.owner)[0]
        del self.proc.processes[20]
        self.assertFalse(child.send_signal(signal.SIGTERM))
        self.proc.processes[20] = info(20, 1, 'business', 201)
        self.assertFalse(child.send_signal(signal.SIGKILL))
        self.assertEqual(self.proc.signals, [])

    def test_pidfd_prevents_reuse_after_final_identity_check(self):
        self.child()
        child = benchmark_children(self.owner)[0]
        self.proc.on_send = lambda: self.proc.processes.__setitem__(
            20, info(20, 1, 'business', 201))
        child.send_signal(signal.SIGKILL)
        self.assertEqual(self.proc.signals, [])

    def test_unavailable_pidfd_is_failure_without_unsafe_fallback(self):
        self.child()
        self.proc.no_pidfd = True
        with self.assertRaisesRegex(UnsafeProcessError, 'pidfd unavailable'):
            benchmark_children(self.owner)
        self.assertEqual(self.proc.signals, [])
        self.assertEqual(self.proc.argument_reads, [])

    def test_procfs_checks_pidfd_before_launch(self):
        with patch('tests.process_helpers.os.pidfd_open', create=True,
                   side_effect=OSError('not supported')):
            with self.assertRaisesRegex(UnsafeProcessError, 'pidfd'):
                ProcFS()

    def test_unsupported_or_missing_roles_are_not_claimed(self):
        self.child()
        for argv in (['ros_bench'], ['ros_bench', '--role'],
                     ['ros_bench', '--role', 'business']):
            self.proc.arguments_by_pid[20] = argv
            self.assertEqual(benchmark_children(self.owner), [])

    def test_integration_modules_import_as_package(self):
        for name in ('integration_checks', 'integration_resource_profiles',
                     'integration_monitor', 'integration_install'):
            importlib.import_module('tests.' + name)

    def test_scripts_help_from_unrelated_directory(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as cwd:
            for name in ('integration_checks', 'integration_resource_profiles',
                         'integration_monitor', 'integration_install'):
                result = subprocess.run([sys.executable, '-c',
                    'import runpy,sys;runpy.run_path(sys.argv[1],run_name="bootstrap")',
                    str(root / 'tests' / (name + '.py'))], cwd=cwd,
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            for name in ('integration_resource_profiles', 'integration_monitor', 'integration_install'):
                result = subprocess.run([sys.executable, str(root / 'tests' / (name + '.py')), '--help'],
                                        cwd=cwd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(sys.platform == 'linux', 'Linux pidfd/proc integration')
class RealProcessTests(unittest.TestCase):
    def test_owned_descendant_cleanup_and_unrelated_same_name_survival(self):
        owner = OwnedProcesses()
        child_program = "import ctypes,time; ctypes.CDLL(None).prctl(15,b'rp_owned_child',0,0,0); time.sleep(60)"
        program = """import subprocess,signal,sys,time
child = None
def stop(signum, frame):
    if child is not None: child.wait(timeout=5)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
child = subprocess.Popen([sys.executable, '-c', CHILD_PROGRAM])
time.sleep(60)
""".replace('CHILD_PROGRAM', repr(child_program))
        unrelated_owner = OwnedProcesses()
        unrelated = subprocess.Popen([sys.executable, '-c', child_program])
        unrelated_owner.add(unrelated)
        root = subprocess.Popen([sys.executable, '-c', program])
        owner.add(root)
        try:
            until = time.monotonic() + 5
            found = []
            while time.monotonic() < until:
                found = owner.discover({'rp_owned_child': 'fixture'})
                if found:
                    break
                time.sleep(.02)
            self.assertEqual(len(found), 1)
            self.assertNotEqual(found[0].pid, unrelated.pid)
            owner.cleanup()
            self.assertIsNone(unrelated.poll())
            self.assertFalse(found[0].exists())
        finally:
            owner.cleanup()
            unrelated_owner.cleanup()


class ProfileInitializationTests(unittest.TestCase):
    def test_unsafe_sigchld_is_rejected_before_pidfd_or_fixture_creation(self):
        for handler in (signal.SIG_IGN, lambda *args: None):
            with self.subTest(handler=handler), \
                    patch.object(signal, 'getsignal', return_value=handler), \
                    patch.object(ProcFS, 'open_pidfd') as open_pidfd, \
                    patch.object(signal, 'signal') as install:
                with self.assertRaisesRegex(UnsafeProcessError, 'default SIGCHLD'):
                    ProcFS()
                open_pidfd.assert_not_called()
                install.assert_not_called()

    def test_default_sigchld_is_reinstalled_before_pidfd_probe(self):
        calls = []
        with patch.object(signal, 'getsignal', return_value=signal.SIG_DFL), \
                patch.object(signal, 'signal', side_effect=lambda *args: calls.append('sigchld')), \
                patch.object(ProcFS, 'open_pidfd', side_effect=lambda pid: calls.append('pidfd') or 42), \
                patch.object(ProcFS, 'close'):
            ProcFS()
        self.assertEqual(calls, ['sigchld', 'pidfd'])

    def test_observer_start_or_registration_failure_cleans_existing_target(self):
        from tests import integration_resource_profiles as profiles
        for failure in ('spawn', 'register'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                target, observer, owner = Mock(), Mock(), Mock()
                if failure == 'spawn':
                    starts = [target, OSError('synthetic observer spawn failure')]
                else:
                    starts = [target, observer]
                    owner.add.side_effect = [None, UnsafeProcessError('synthetic observer registration failure')]
                with patch.object(profiles, 'OwnedProcesses', return_value=owner), \
                        patch.object(profiles.subprocess, 'Popen', side_effect=starts):
                    with self.assertRaises((OSError, UnsafeProcessError)):
                        profiles.owned_telemetry(Path(directory))
                owner.cleanup.assert_called_once_with()
                if failure == 'register':
                    observer.stderr.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
