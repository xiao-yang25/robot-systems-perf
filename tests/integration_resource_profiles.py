"""Linux real-process multirate validation and repeated, scoped overhead observations."""
import argparse
import importlib.util
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from perfkit.resources import ResourceSampler, summarize_resources

PROGRAM = '''
import ctypes, threading, time
lib = ctypes.CDLL(None)
lib.prctl(15, b'rp_worker', 0, 0, 0)
def worker(index):
    lib.prctl(15, ('rp_%d' % index).encode(), 0, 0, 0)
    while True: time.sleep(.005)
for i in range(48): threading.Thread(target=worker,args=(i,),daemon=True).start()
while True:
    for _ in range(10000): pass
    time.sleep(.001)
'''


def run(output, seconds=1.6, baseline=None):
    owned = subprocess.Popen([sys.executable, '-c', PROGRAM])
    try:
        deadline = time.monotonic() + 5
        while len(list(Path('/proc', str(owned.pid), 'task').glob('*'))) < 49:
            if time.monotonic() > deadline:
                raise AssertionError('fixture threads did not start')
            time.sleep(.01)
        profiles = [('full', {}), ('slow_threads', {'thread_sampling_seconds': .5}),
                    ('selected_threads', {'thread_names': ['^rp_0$']}), ('no_threads', {'collect_threads': False})]
        if baseline:
            spec = importlib.util.spec_from_file_location('perfkit.resources_baseline', baseline)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            profiles.insert(0, ('baseline', None))
        measurements = []
        # Alternate profile order to reduce monotonic ordering bias; results are descriptive.
        for repeat in range(3):
            for name, options in profiles if repeat % 2 == 0 else list(reversed(profiles)):
                directory = output / (name + '-' + str(repeat))
                directory.mkdir()
                cls = module.ResourceSampler if name == 'baseline' else ResourceSampler
                arguments = {} if options is None else {'options': options}
                sampler = cls(directory / 'resources.jsonl', .1, **arguments)
                sampler.register(owned.pid, 'owned-test-fixture')
                begin, cpu_begin = time.monotonic_ns(), time.process_time_ns()
                with sampler:
                    time.sleep(seconds)
                cpu_end, end = time.process_time_ns(), time.monotonic_ns()
                if sampler.error:
                    raise AssertionError(sampler.error)
                rows = [json.loads(line) for line in sampler.output.read_text().splitlines()]
                assert len(rows) >= 3
                if name == 'baseline':
                    coverage = {'system': len(rows), 'process': len(rows), 'thread': len(rows)}
                else:
                    report = summarize_resources(sampler.output, begin, end)
                    coverage = {key: state['samples'] for key, state in report['source_coverage'].items()}
                    process = next(value for value in report['registered_entities'].values() if value['kind'] == 'process')
                    assert process['valid_intervals']['cpu_ticks'] >= 1
                    assert process['cpu_percent_one_core'] is not None
                    assert report['observer']['cpu_percent_one_core'] is not None
                    assert report['collection_cost']['cycles'] >= 3
                    if name == 'slow_threads':
                        assert 2 <= coverage['thread'] < coverage['process']
                    if name == 'selected_threads':
                        threads = [value for value in report['registered_entities'].values() if value['kind'] == 'thread']
                        assert len(threads) == 1 and threads[0]['comm'] == 'rp_0'
                        assert report['thread_scope']['selection']['omitted'] > 0
                    if name == 'no_threads':
                        assert 'thread' not in coverage
                measurements.append({'profile': name, 'repeat': repeat,
                    'observer_cpu_percent_one_core': 100 * (cpu_end - cpu_begin) / (end - begin),
                    'collection_duration_ns_max': max(row['sample_end_ns'] - row['monotonic_ns'] for row in rows),
                    'source_samples': coverage,
                    'scope': 'parent CPU; same 49-thread fixture; reduced profiles change thread coverage'})
                assert owned.poll() is None, 'observer terminated fixture'
        (output / 'profile-observations.json').write_text(json.dumps({'architecture': os.uname().machine,
            'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted((Path(__file__).resolve().parent.parent / 'perfkit').glob('*.py'))},
            'baseline_sha256': hashlib.sha256(baseline.read_bytes()).hexdigest() if baseline else None,
            'measurements': measurements, 'interpretation': 'descriptive repeats; no business acceptance or Jetson claim'}, indent=2) + '\n')
    finally:
        owned.terminate()
        owned.wait(timeout=5)


def owned_telemetry(output):
    """The monitor excludes its live sampler child and reaps only that child."""
    tools = output / 'fake-tools'
    tools.mkdir()
    executable = tools / 'tegrastats'
    executable.write_text('#!' + sys.executable + '\n' + """import ctypes, time
ctypes.CDLL(None).prctl(15, b'rp_tegra', 0, 0, 0)
while True:
    deadline = time.monotonic() + .03
    while time.monotonic() < deadline: pass
    print('GR3D_FREQ 0%@100 EMC_FREQ 20%@200', flush=True)
    time.sleep(.05)
""")
    executable.chmod(0o755)
    target = subprocess.Popen([sys.executable, '-c', PROGRAM])
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'])
    capture = output / 'owned-telemetry'
    observer = subprocess.Popen([sys.executable, '-m', 'perfkit.monitor', '--seconds', '30',
        '--interval', '.1', '--discovery-interval', '.1', '--include-name', '^rp_(worker|tegra)$',
        '--active-cpu-percent', '0', '--jetson-telemetry', '--output', str(capture)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    child = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = capture / 'resources.jsonl'
            raw = capture / 'resources-tegrastats.jsonl'
            if rows.exists() and raw.exists() and 'GR3D' in raw.read_text():
                children = Path('/proc', str(observer.pid), 'task', str(observer.pid), 'children').read_text().split()
                if children:
                    child = int(children[0])
                    break
            if observer.poll() is not None:
                raise AssertionError(observer.stderr.read().decode())
            time.sleep(.05)
        assert child is not None
        time.sleep(.7)
        observer.send_signal(signal.SIGTERM)
        assert observer.wait(timeout=10) == 130
        summary = json.loads((capture / 'monitor-summary.json').read_text())
        telemetry = summary['resources']['jetson_telemetry']
        assert telemetry['unique_samples'] >= 2 and telemetry['available']
        assert telemetry['value_ranges']['gpu_utilization_percent']['max'] == 0
        assert summary['quality']['peak_registered_targets'] == 1
        for line in (capture / 'discovery.jsonl').read_text().splitlines():
            assert child not in json.loads(line)['registered_pids']
        assert not Path('/proc', str(child)).exists(), 'owned telemetry child survived cancellation'
        assert target.poll() is None, 'external target was signalled'
        (output / 'owned-telemetry-verification.json').write_text(json.dumps({
            'owned_child_excluded': True, 'owned_child_reaped': True, 'external_target_survives': True,
            'unique_samples': telemetry['unique_samples'], 'status': summary['status']}, indent=2) + '\n')
    finally:
        if observer.poll() is None:
            observer.terminate()
            observer.wait(timeout=5)
        observer.stderr.close()
        target.terminate()
        target.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        run(args.output, baseline=args.baseline)
        owned_telemetry(args.output)
    else:
        with tempfile.TemporaryDirectory() as directory:
            run(Path(directory), baseline=args.baseline)
            owned_telemetry(Path(directory))
    print('Real-process resource profile checks passed')
