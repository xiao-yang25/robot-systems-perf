"""Run Linux/ROS integration checks inside the built image, separately from benchmarks."""
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def config_file(root, name, **scenario_changes):
    config = json.loads((ROOT / 'configs/smoke.json').read_text())
    config.update(warmup_seconds=0, measurement_seconds=0.05, repetitions=1,
                  drain_seconds=0.01, resource_sampling_seconds=0.1)
    scenario = config['scenarios'][0]
    scenario.update(deadline_us=1000000, **scenario_changes)
    config['scenarios'] = [scenario]
    path = root / name
    path.write_text(json.dumps(config))
    return path


def command(config, output):
    return [sys.executable, '-m', 'perfkit.runner', '--config', str(config), '--output', str(output)]


def main():
    if not (ROOT / 'build/ros_bench').exists() or not Path('/proc').exists():
        raise RuntimeError('Integration checks require the built Linux ROS environment')
    with tempfile.TemporaryDirectory(prefix='embodied-perf-integration-') as directory:
        root = Path(directory)
        cfg = config_file(root, 'deadline.json', callback_delay_us=300000)
        output = root / 'deadline-run'
        subprocess.run(command(cfg, output), cwd=ROOT, check=True, timeout=30)
        observed_end = time.monotonic_ns()
        rows = list(csv.DictReader((output / 'C01/001/sender.csv').open()))
        latest_deadline = max(int(r['scheduled_ns']) + 1000000000 for r in rows)
        assert observed_end >= latest_deadline, 'Observation stopped while deadlines were still pending'
        resolved = json.loads((output / 'C01/001/resolved.json').read_text())
        assert resolved['effective_drain_seconds'] >= 1
        summary = json.loads((output / 'summary.json').read_text())['results'][0]['metrics']
        assert summary['deadline']['denominator'] == 5
        assert summary['deadline']['violated'] == summary['deadline']['late'] + summary['deadline']['missing']
        print('PASS: short requested drain still observes every configured deadline')

        original_status = (output / 'run-status.json').read_bytes()
        repeat = subprocess.run(command(cfg, output), cwd=ROOT, capture_output=True, timeout=10)
        assert repeat.returncode != 0
        assert (output / 'run-status.json').read_bytes() == original_status
        print('PASS: existing evidence directory is not overwritten')

        cfg = config_file(root, 'minimal.json')
        minimal = json.loads(cfg.read_text()); minimal['sampling_mode'] = 'minimal'
        cfg.write_text(json.dumps(minimal))
        output = root / 'minimal-run'
        subprocess.run(command(cfg, output), cwd=ROOT, check=True, timeout=30)
        summary = json.loads((output / 'summary.json').read_text())['results'][0]
        assert not (output / 'resources.jsonl').exists()
        assert summary['resources']['available'] is False
        assert summary['metrics']['counts']['measured_sent'] == 5
        assert summary['metrics']['distributions']['release_lateness_ns']['n'] == 5
        print('PASS: minimal mode retains events and disables the resource sampler')

        cfg = config_file(root, 'sampler-failure.json', cpu_interference_workers=1)
        output = root / 'sampler-failure-run'
        code = ('import sys; from perfkit.resources import ResourceSampler; '
                'from perfkit.runner import main; '
                'ResourceSampler._snapshot=lambda self, due=None: (_ for _ in ()).throw(OSError("injected sampler failure")); '
                'sys.argv=["runner","--config",sys.argv[1],"--output",sys.argv[2]]; main()')
        failed = subprocess.run([sys.executable, '-c', code, str(cfg), str(output)],
                                cwd=ROOT, capture_output=True, timeout=30)
        assert failed.returncode != 0
        status = json.loads((output / 'run-status.json').read_text())
        assert status['status'] == 'failed' and 'injected sampler failure' in status['resource_sampler_error']
        assert not (output / 'summary.json').exists()
        print('PASS: sampler failure is nonzero and preserves failure evidence')

        cfg = config_file(root, 'interrupt.json', cpu_interference_workers=1)
        config = json.loads(cfg.read_text()); config['measurement_seconds'] = 30
        cfg.write_text(json.dumps(config))
        output = root / 'interrupted-run'
        with (root / 'interrupted.log').open('w') as log:
            process = subprocess.Popen(command(cfg, output), cwd=ROOT, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            children = []
            try:
                ready_until = time.monotonic() + 15
                while time.monotonic() < ready_until:
                    path = Path(f'/proc/{process.pid}/task/{process.pid}/children')
                    if path.exists():
                        children = path.read_text().split()
                    if len(children) >= 3 and (output / 'C01/001/ready').exists(): break
                    if process.poll() is not None: raise RuntimeError('runner exited before interruption probe')
                    time.sleep(0.02)
                else:
                    raise RuntimeError('runner did not start all child processes')
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=15) != 0
                status = json.loads((output / 'run-status.json').read_text())
                assert status['status'] == 'failed' and status['error_type'] == 'KeyboardInterrupt'
                assert all(not Path(f'/proc/{pid}').exists() for pid in children), 'orphan benchmark child survived'
                print('PASS: SIGTERM records failure and reaps publisher, subscriber and CPU-load children')
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()
                for pid in children:
                    try: os.killpg(int(pid), signal.SIGKILL)
                    except ProcessLookupError: pass

        suite = json.loads((ROOT / 'configs/suite-smoke.json').read_text())
        suite['cases'] = [next(c for c in suite['cases'] if c['name'] == 'c01-cpu-interference')]
        suite['sampler_comparisons'] = []
        suite['defaults']['measurement_seconds'] = 30
        cfg = root / 'suite-interrupt.json'; cfg.write_text(json.dumps(suite))
        output = root / 'suite-interrupt-run'
        with (root / 'suite-interrupted.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'perfkit.suite', '--config', str(cfg),
                                        '--output', str(output)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            children = []
            try:
                until = time.monotonic() + 15
                while time.monotonic() < until:
                    path = Path(f'/proc/{process.pid}/task/{process.pid}/children')
                    children = path.read_text().split() if path.exists() else []
                    if len(children) >= 3: break
                    if process.poll() is not None: raise RuntimeError('suite exited before interruption')
                    time.sleep(0.02)
                else: raise RuntimeError('suite did not start all children')
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=15) != 0
                assert json.loads((output / 'suite-status.json').read_text())['status'] == 'failed'
                assert json.loads((output / 'c01-cpu-interference/run-status.json').read_text())['status'] == 'failed'
                assert all(not Path(f'/proc/{pid}').exists() for pid in children)
                print('PASS: suite interruption preserves both statuses and reaps all owned children')
            finally:
                if process.poll() is None: process.kill(); process.wait()
                for pid in children:
                    try: os.killpg(int(pid), signal.SIGKILL)
                    except ProcessLookupError: pass


if __name__ == '__main__':
    main()
