"""Source-checkout comparison helpers; Linux, standard library, bounded children."""
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import threading
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from perfkit.lifecycle import defer_interrupts as _defer_interrupts
from tests.process_helpers import OwnedProcesses, benchmark_children

# A direct child cannot exit before its creator binds cleanup authority.
GATE = "import os,sys; f=int(sys.argv[1]); b=os.read(f,1); os.close(f); b==b'1' or sys.exit(125); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
_deferral = threading.local()


@contextmanager
def defer_interrupts():
    """Nested command cleanup must defer cancellation until the outer boundary."""
    depth = getattr(_deferral, 'depth', 0)
    _deferral.depth = depth + 1
    try:
        if depth:
            yield
        else:
            with _defer_interrupts():
                yield
    finally:
        _deferral.depth = depth


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def fingerprint(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


class CommandError(RuntimeError):
    def __init__(self, record):
        self.record = record
        super().__init__('command failed: ' + str(record.get('error') or record['returncode']))


def launch(owner, command, **kwargs):
    """Caller owns cleanup, including registration failure and pending handles."""
    # Terminal Ctrl+C reaches the controller; it then stops children by pidfd.
    kwargs.setdefault('start_new_session', True)
    read_fd, write_fd = os.pipe()
    try:
        with defer_interrupts():
            process = subprocess.Popen([sys.executable, '-c', GATE, str(read_fd),
                                        *map(str, command)], pass_fds=(read_fd,), **kwargs)
            owner.add(process)
            os.write(write_fd, b'1')
        return process
    finally:
        os.close(read_fd)
        os.close(write_fd)


def execute(command, folder, timeout, env=None, discover=False, observe=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    command = list(map(str, command))
    for name in ('command.json', 'command.log', 'result.json'):
        if (folder / name).exists():
            raise FileExistsError('command evidence already exists: ' + str(folder / name))
    with (folder / 'command.json').open('x') as stream:
        json.dump(command, stream)
    owner = OwnedProcesses()  # Validate pidfd/SIGCHLD before creating a child.
    begin = time.monotonic_ns()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_begin = usage.ru_utime + usage.ru_stime
    record = {'command': command, 'returncode': None, 'error': None,
              'cpu_scope': 'reaped command children; includes Python exec gate, startup and shutdown'}
    next_discovery = 0
    try:
        with (folder / 'command.log').open('x') as stream:
            process = launch(owner, command, stdout=stream, stderr=subprocess.STDOUT,
                             cwd=ROOT, env=env)
            record['pid'] = process.pid
            while process.poll() is None:
                if observe is not None:
                    # Only a registered direct child is exposed, with its pinned
                    # proc directory. Callback failures use the same cleanup path.
                    observe(owner.handle(process))
                if discover and len(owner.owned) < 3 and time.monotonic() >= next_discovery:
                    benchmark_children(owner)
                    next_discovery = time.monotonic() + .25
                if (time.monotonic_ns() - begin) / 1e9 > timeout:
                    raise TimeoutError('bounded command exceeded %s seconds' % timeout)
                time.sleep(.05)
            record['returncode'] = process.returncode
    except BaseException as error:
        record['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        try:
            with defer_interrupts():
                owner.cleanup()
        finally:
            end = time.monotonic_ns()
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = usage.ru_utime + usage.ru_stime - cpu_begin
            record.update(elapsed_ns=end - begin, cpu_seconds=cpu,
                          cpu_percent_one_core=cpu * 1e11 / (end - begin))
            write_json(folder / 'result.json', record)
    if record['returncode'] != 0:
        raise CommandError(record)
    return record
