"""Linux external-process checks; fixture ownership belongs only to this test."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SECRET = 'FIXTURE_ARGUMENT_NOT_FOR_TELEMETRY'
PROGRAM = r'''
import ctypes, sys, threading, time
lib = ctypes.CDLL(None)
lib.prctl(15, sys.argv[1].encode(), 0, 0, 0)
def busy(name):
    lib.prctl(15, name.encode(), 0, 0, 0)
    while True:
        for _ in range(10000): pass
if sys.argv[1] == 'rk_busy_fixture':
    for name in ('rk_thread_a','rk_thread_b'):
        threading.Thread(target=busy,args=(name,),daemon=True).start()
while True: time.sleep(.1)
'''


def wait_for(check, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.05)
    raise AssertionError('Timed out waiting for external observation')


def records(output):
    path = output/'discovery.jsonl'
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def spawn(name, owned):
    proc = subprocess.Popen([sys.executable, '-c', PROGRAM, name, SECRET])
    owned.append(proc)
    wait_for(lambda: Path(f'/proc/{proc.pid}/comm').read_text().strip() == name)
    return proc


def monitor(output, seconds, owned, *options):
    proc = subprocess.Popen([sys.executable, '-m', 'perfkit.monitor', '--output', str(output),
                             '--seconds', str(seconds), '--interval', '.1',
                             '--discovery-interval', '.2', *options], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owned.append(proc)
    return proc


def cleanup(owned):
    for proc in reversed(owned):
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def dynamic_discovery(directory):
    owned = []
    output = directory/'dynamic'
    try:
        original = spawn('rk_busy_fixture', owned)
        observer = monitor(output, 9, owned, '--include-name', '^rk_idle_fixture$')
        wait_for(lambda: any(original.pid in row['registered_pids'] for row in records(output)))
        idle = spawn('rk_idle_fixture', owned)
        second = spawn('rk_busy_fixture', owned)
        wait_for(lambda: any({idle.pid,second.pid} <= set(row['registered_pids']) for row in records(output)))
        original.terminate()
        original.wait(timeout=5)
        replacement = spawn('rk_busy_fixture', owned)
        assert observer.wait(timeout=20) == 0
        rows = records(output)
        assert any(replacement.pid in row['registered_pids'] for row in rows)
        assert any(any(event['event']=='unregistered' and event['pid']==original.pid for event in row['changes']) for row in rows)
        assert all(proc.poll() is None for proc in (idle,second,replacement)), 'Observer stopped external fixtures'
        reason_rows = [item for row in rows for item in row['targets'] if item['pid']==original.pid]
        assert any(item['observed_cpu_percent'] is not None and item['observed_cpu_percent'] >= 1 for item in reason_rows)
        summary = json.loads((output/'monitor-summary.json').read_text())
        assert summary['status']=='complete'
        entities = summary['resources']['registered_entities'] or {}
        threads = [item for item in entities.values() if item['kind']=='thread' and item['pid']==second.pid]
        assert len(threads)>=3, threads
        assert {'rk_thread_a','rk_thread_b'} <= {item['comm'] for item in threads}
        for path in output.iterdir():
            if path.is_file():
                assert SECRET not in path.read_text(), 'Command argument leaked into evidence'
        status = (output/'monitor-status.json').read_bytes()
        duplicate = monitor(output, 1, owned)
        assert duplicate.wait(timeout=10)!=0
        assert (output/'monitor-status.json').read_bytes()==status
        print('PASS automatic activity discovery, idle name selection, new/restarted processes, thread capture, privacy and no overwrite')
    finally:
        cleanup(owned)


def interruption(directory):
    owned = []
    output = directory/'interrupted'
    try:
        target = spawn('rk_busy_fixture', owned)
        observer = monitor(output, 30, owned, '--active-cpu-percent', '0',
                           '--include-name', '^rk_busy_fixture$')
        wait_for(lambda: any(target.pid in row['registered_pids'] for row in records(output)))
        time.sleep(.4)
        observer.send_signal(signal.SIGTERM)
        assert observer.wait(timeout=10)==130
        assert target.poll() is None, 'Monitor cancellation signalled external target'
        assert json.loads((output/'monitor-status.json').read_text())['status']=='interrupted'
        assert json.loads((output/'monitor-summary.json').read_text())['status']=='interrupted'
        assert (output/'MONITOR_REPORT.md').exists()
        assert not Path(f'/proc/{observer.pid}').exists()
        print('PASS monitor SIGTERM preserves partial report and external target remains alive')
    finally:
        cleanup(owned)


def main():
    if sys.platform != 'linux':
        raise SystemExit('These integration checks require Linux')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Keep evidence in a new directory')
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        dynamic_discovery(args.output)
        interruption(args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='perfkit-monitor-check-') as directory:
            dynamic_discovery(Path(directory))
            interruption(Path(directory))


if __name__ == '__main__':
    main()
