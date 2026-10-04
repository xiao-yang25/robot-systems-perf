"""Same-scope Linux collector comparison; synthetic data, no business reads."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from perfkit.resources import ResourceSampler, summarize_resources
from tests.integration_resource_profiles import PROGRAM
from tests.process_helpers import OwnedProcesses


def owned_telemetry(output):
    tools = output / 'owned-tools'
    tools.mkdir()
    executable = tools / 'tegrastats'
    executable.write_text('#!' + sys.executable + '\n' + """import time
while True:
    deadline = time.monotonic() + .03
    while time.monotonic() < deadline: pass
    print('GR3D_FREQ 0%@100 EMC_FREQ 20%@200', flush=True)
    time.sleep(.05)
""")
    executable.chmod(0o755)
    sampler = ResourceSampler(output / 'owned-cpu.jsonl', .1,
                              options={'skip_temperatures': True, 'jetson_telemetry': True})
    begin = time.monotonic_ns()
    with patch.dict(os.environ, PATH=str(tools.resolve()) + os.pathsep + os.environ['PATH']):
        with sampler:
            pids = sampler.owned_process_ids()
            assert len(pids) == 1
            time.sleep(1.6)
    end = time.monotonic_ns()
    assert sampler.error is None, sampler.error
    assert not Path('/proc', str(pids[0])).exists(), 'owned child survived collector exit'
    observer = summarize_resources(sampler.output, begin, end)['observer']
    assert observer['owned_child_unavailable_samples'] == 0
    assert observer['total_coverage']['delta']['owned_child_cpu_ns'] > 0
    assert observer['total_cpu_percent_one_core'] > observer['cpu_percent_one_core']
    (output / 'owned-cpu-verification.json').write_text(json.dumps({
        'owned_child_reaped': True, 'observer': observer,
        'scope': 'owned synthetic executable only; not real tegrastats overhead'}, indent=2) + '\n')


def run(output, baseline=None, seconds=1.6):
    variants = [('optimized', ResourceSampler)]
    if baseline:
        spec = importlib.util.spec_from_file_location('perfkit.resources_baseline', baseline)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        variants.insert(0, ('baseline', module.ResourceSampler))
    owner = OwnedProcesses()
    records = []
    try:
        owned = subprocess.Popen([sys.executable, '-c', PROGRAM])
        owner.add(owned)
        deadline = time.monotonic() + 5
        while len(list(Path('/proc', str(owned.pid), 'task').glob('*'))) != 49:
            if owned.poll() is not None or time.monotonic() > deadline:
                raise AssertionError('owned 49-thread fixture did not start')
            time.sleep(.01)
        for batch in range(3):
            for name, cls in variants if batch % 2 == 0 else list(reversed(variants)):
                path = output / (name + '-' + str(batch) + '.jsonl')
                sampler = cls(path, .1, options={'skip_temperatures': True})
                sampler.register(owned.pid, 'owned-test-fixture')
                begin, cpu_begin = time.monotonic_ns(), time.process_time_ns()
                with sampler:
                    time.sleep(seconds)
                cpu_end, end = time.process_time_ns(), time.monotonic_ns()
                assert sampler.error is None, sampler.error
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                assert len(rows) >= 3
                for row in rows:
                    process = row['processes'][0]
                    assert process['stat'] is not None
                    assert len(process['tasks']) == 49
                    assert all(task['stat'] is not None for task in process['tasks'])
                    assert set(row['source_windows']) == {'system', 'process', 'thread', 'cgroup', 'observer'}
                report = summarize_resources(path, begin, end)
                process = next(item for item in report['registered_entities'].values() if item['kind'] == 'process')
                assert process['valid_intervals']['cpu_ticks'] > 0
                assert report['collection_cost']['cycles'] >= 3
                assert owned.poll() is None, 'collector terminated fixture'
                records.append({'variant': name, 'batch': batch, 'samples': len(rows),
                    'threads_per_snapshot': 49,
                    'observer_cpu_percent_one_core': 100 * (cpu_end - cpu_begin) / (end - begin),
                    'phase_costs_ns': report['collection_cost']['phase_costs_ns'],
                    'over_period_cycles': report['collection_cost']['over_period_cycles'],
                    'bytes': path.stat().st_size})
        owned_telemetry(output)
        assert owned.poll() is None, 'collector terminated external fixture'
        (output / 'comparison.json').write_text(json.dumps({
            'architecture': os.uname().machine, 'seconds_per_run': seconds,
            'baseline_sha256': hashlib.sha256(baseline.read_bytes()).hexdigest() if baseline else None,
            'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted((Path(__file__).resolve().parent.parent / 'perfkit').glob('*.py'))},
            'records': records,
            'scope': 'same 49 threads, source schedules and metrics; descriptive repeats, no Jetson or business acceptance'},
            indent=2) + '\n')
    finally:
        owner.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--baseline', type=Path, help='previous perfkit/resources.py; same resource-options API')
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('This integration check requires Linux')
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        run(args.output, args.baseline)
    else:
        with tempfile.TemporaryDirectory() as directory:
            run(Path(directory), args.baseline)
    print('Same-scope collector checks passed')
