"""Run bounded same-host experiments; preserve failed runs rather than hiding them."""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import csv
from contextlib import nullcontext
import time
import uuid
from .lifecycle import defer_interrupts
from .resources import validate_resource_options


def read_optional(path):
    try:
        return Path(path).read_text().strip().replace('\x00', '')
    except (OSError, UnicodeError, TypeError):
        return None


def command_optional(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def number(value, label, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label}: finite number required')
    if not low <= value <= high or (integer and not isinstance(value, int)):
        raise ValueError(f'{label}: outside supported range [{low}, {high}]')
    return value


def validate(config):
    if config.get('format_version') != 1:
        raise ValueError('unsupported configuration format')
    if 'resource_options' in config and not isinstance(config['resource_options'], dict):
        raise ValueError('resource_options must be an object')
    validate_resource_options(config.get('resource_options'))
    number(config['warmup_seconds'], 'warmup_seconds', 0, 3600)
    number(config['measurement_seconds'], 'measurement_seconds', 0.001, 3600)
    number(config['repetitions'], 'repetitions', 1, 100, integer=True)
    number(config['drain_seconds'], 'drain_seconds', 0.01, 60)
    number(config['resource_sampling_seconds'], 'resource_sampling_seconds', 0.1, 60)
    if config.get('sampling_mode', 'basic') not in ('basic', 'minimal'):
        raise ValueError('sampling_mode must be basic or minimal')
    for key in ('min_samples',):
        if key in config.get('quality_limits', {}):
            number(config['quality_limits'][key], key, 2, 1000000, integer=True)
    for key in ('max_release_late_fraction',):
        if key in config.get('quality_limits', {}):
            number(config['quality_limits'][key], key, 0, 1)
    if not isinstance(config.get('scenarios'), list) or not config['scenarios']:
        raise ValueError('at least one scenario required')
    seen = set()
    for s in config['scenarios']:
        if s['id'] not in ('C01', 'S01') or s['id'] in seen:
            raise ValueError('only one configuration per C01/S01; compare in separate runs')
        seen.add(s['id'])
        hz = number(s['frequency_hz'], 'frequency_hz', 1, 10000)
        if int(config['measurement_seconds'] * hz) < 2:
            raise ValueError('each scenario needs at least two measured samples')
        if int((config['warmup_seconds'] + config['measurement_seconds']) * hz) > 1000000:
            raise ValueError('at most 1,000,000 total samples per scenario')
        number(s.get('cpu_interference_workers', 0), 'cpu_interference_workers', 0, 64, integer=True)
        if s.get('deadline_us') is not None:
            number(s['deadline_us'], 'deadline_us', 1, 1000000, integer=True)
        if s['id'] == 'C01':
            number(s['payload_bytes'], 'payload_bytes', 0, 16777216, integer=True)
            number(s['qos_depth'], 'qos_depth', 1, 100000, integer=True)
            number(s['callback_delay_us'], 'callback_delay_us', 0, 1000000, integer=True)
            if s.get('max_data_age_us') is not None:
                number(s['max_data_age_us'], 'max_data_age_us', 0, 1000000, integer=True)
            if s['reliability'] not in ('reliable', 'best_effort'):
                raise ValueError('unsupported reliability')
        else:
            number(s['work_us'], 'work_us', 0, 1000000, integer=True)
    return config


def environment(root):
    from .platform_probe import collect_profile
    profile_file = os.environ.get('EP_HOST_PROFILE')
    packages = command_optional(['dpkg-query', '-W', '-f=${Package}=${Version}\n',
                                 'ros-*-rclcpp', 'ros-*-rmw*', 'ros-*-std-msgs'])
    source_files = list((root / 'src').glob('*')) + list((root / 'perfkit').glob('*.py'))
    source_files += list((root / 'scripts').glob('*.sh')) + [root / 'CMakeLists.txt', root / 'Dockerfile']
    manifest = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in source_files if p.is_file()}
    return {
        'format_version': 1,
        'recorded_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'environment_kind': os.environ.get('EP_ENVIRONMENT_KIND', 'native-linux-unclassified'),
        'architecture': platform.machine(), 'kernel_release': platform.release(),
        'os_release': read_optional('/etc/os-release'),
        'jetson_model': read_optional('/proc/device-tree/model'),
        'jetson_linux_release': read_optional('/etc/nv_tegra_release'),
        'cpu_online': read_optional('/sys/devices/system/cpu/online'),
        'process_affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
        'source_revision': os.environ.get('EP_SOURCE_REVISION') or command_optional(['git', '-C', str(root), 'rev-parse', 'HEAD']),
        'source_sha256': manifest,
        'base_image_id': os.environ.get('EP_BASE_IMAGE_ID'),
        'image_id': os.environ.get('EP_IMAGE_ID'),
        'docker_engine_os': os.environ.get('EP_DOCKER_ENGINE_OS'),
        'docker_network': os.environ.get('EP_DOCKER_NETWORK'),
        'docker_ipc': os.environ.get('EP_DOCKER_IPC'),
        'external_host_kernel': os.environ.get('EP_HOST_KERNEL'),
        'external_host_architecture': os.environ.get('EP_HOST_ARCH'),
        'external_host_jetson_linux_release': os.environ.get('EP_HOST_BSP') or None,
        'external_host_board_model': os.environ.get('EP_HOST_BOARD') or None,
        'ros_distribution': os.environ.get('ROS_DISTRO'),
        'rmw_selected': os.environ.get('RMW_IMPLEMENTATION', 'rmw_fastrtps_cpp'),
        'ros_package_versions': packages,
        'clock': 'CLOCK_MONOTONIC; same-host only',
        'cgroup': {name: read_optional('/sys/fs/cgroup/' + name) for name in
                   ('cpu.max', 'cpu.stat', 'cpuset.cpus.effective', 'memory.max', 'memory.current')},
        'kernel_schedstat': read_optional('/proc/sys/kernel/sched_schedstats'),
        'host_profile': json.loads(Path(profile_file).read_text()) if profile_file else
                        collect_profile(include_kernel_command_line=False),
        'host_profile_source': 'supplied_file' if profile_file else 'local_process_view_at_startup',
        'compiler': command_optional(['c++', '--version']),
        'power_mode_readonly': command_optional(['nvpmodel', '-q']),
        'limitations': [
            'Not a cross-host or GPU benchmark; C01 includes middleware plus callback dispatch.',
            'Per-thread schedstat counters are sampled when available; per-event scheduler/executor/queue waits are not traced.',
            'CPU usage and timing are observed in the executing Linux environment, possibly a VM.',
            'Container views may not expose the host BSP, power mode or device statistics.'
        ]
    }


def start(command, log, env=None):
    with log.open('x') as f:
        return subprocess.Popen([str(x) for x in command], stdout=f, stderr=subprocess.STDOUT,
                                start_new_session=True, env=env)


def stop(proc, graceful=signal.SIGTERM):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, graceful)
        except ProcessLookupError:
            proc.wait()
            return
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            raise RuntimeError('process did not stop gracefully')


def run_one(root, folder, config, scenario, sampler=None):
    from .analysis import analyze_c01, analyze_s01
    folder.mkdir(parents=True)
    hz = scenario['frequency_hz']
    count = int(config['measurement_seconds'] * hz)
    warmup = int(config['warmup_seconds'] * hz)
    period = round(1000000000 / hz)
    deadline = scenario.get('deadline_us')
    deadline = deadline * 1000 if deadline is not None else None
    effective_drain = max(config['drain_seconds'], deadline / 1000000000 if deadline is not None else 0)
    common = ['--count', count, '--warmup', warmup, '--period-ns', period]
    child_env = dict(os.environ)
    # The random topic isolates runs; one domain keeps discovery local to this run.
    child_env['ROS_DOMAIN_ID'] = str(10 + uuid.uuid4().int % 180)
    child_env['ROS_LOCALHOST_ONLY'] = '1'
    child_env.setdefault('RMW_IMPLEMENTATION', 'rmw_fastrtps_cpp')
    (folder / 'resolved.json').write_text(json.dumps({
        'measured_count': count, 'warmup_count': warmup, 'period_ns': period,
        'deadline_ns': deadline, 'ros_domain_id': child_env['ROS_DOMAIN_ID'],
        'effective_drain_seconds': effective_drain if scenario['id'] == 'C01' else None,
        'ros_localhost_only': child_env['ROS_LOCALHOST_ONLY'],
        'rmw_implementation': child_env['RMW_IMPLEMENTATION'],
        'release_policy': 'absolute_schedule_with_catch_up_no_skipping'
    }, indent=2) + '\n')
    processes = []
    loads = []
    def launch(command, log, env=None, role='benchmark'):
        proc = start(command, log, env)
        processes.append(proc)
        if sampler is not None:
            sampler.register(proc.pid, role)
        return proc
    try:
        for i in range(scenario.get('cpu_interference_workers', 0)):
            load = launch([root / 'build/periodic_bench', '--load', 'cpu'], folder / f'load-{i}.log', role=f'cpu-load-{i}')
            loads.append(load)
        timeout = config['warmup_seconds'] + config['measurement_seconds'] + 30
        if scenario['id'] == 'C01':
            topic = '/embodied_perf/run_' + uuid.uuid4().hex
            shared = common + ['--topic', topic, '--payload-bytes', scenario['payload_bytes'],
                               '--depth', scenario['qos_depth'], '--reliability', scenario['reliability']]
            subscriber = launch([root / 'build/ros_bench', '--role', 'subscriber', *shared,
                                '--callback-delay-ns', scenario['callback_delay_us'] * 1000,
                                '--ready-file', folder / 'ready', '--output', folder / 'receiver.csv'],
                               folder / 'subscriber.log', child_env, role='subscriber')
            ready_end = time.monotonic() + 15
            while not (folder / 'ready').exists():
                if subscriber.poll() is not None or time.monotonic() >= ready_end:
                    raise RuntimeError('subscriber startup failed; see subscriber.log')
                time.sleep(0.02)
            publisher = launch([root / 'build/ros_bench', '--role', 'publisher', *shared,
                               '--output', folder / 'sender.csv'], folder / 'publisher.log', child_env, role='publisher')
            if publisher.wait(timeout=timeout) != 0:
                raise RuntimeError('publisher failed; see publisher.log')
            # After publishing ends, this guarantees even the latest planned
            # deadline has passed before missing tasks count as violations.
            if sampler is not None:
                sampler.set_window(scenario['id'], int(folder.name), 'drain')
            drain_end = time.monotonic() + effective_drain
            while time.monotonic() < drain_end:
                if subscriber.poll() is not None:
                    raise RuntimeError('subscriber stopped before drain completed')
                time.sleep(min(0.05, max(0, drain_end - time.monotonic())))
            stop(subscriber, signal.SIGINT)
            if subscriber.returncode != 0:
                raise RuntimeError('subscriber failed during shutdown; see subscriber.log')
            metrics = analyze_c01(folder / 'sender.csv', folder / 'receiver.csv', deadline,
                                  payload_bytes=scenario['payload_bytes'],
                                  max_data_age_ns=scenario['max_data_age_us'] * 1000 if scenario.get('max_data_age_us') is not None else None)
            if metrics['counts']['measured_sent'] != count or metrics['counts']['warmup_sent'] != warmup:
                raise RuntimeError('publisher manifest does not match planned sample count')
        else:
            proc = launch([root / 'build/periodic_bench', *common, '--work-ns', scenario['work_us'] * 1000,
                          '--output', folder / 'samples.csv'], folder / 'periodic.log', role='periodic-worker')
            if proc.wait(timeout=timeout) != 0:
                raise RuntimeError('periodic test failed; see periodic.log')
            metrics = analyze_s01(folder / 'samples.csv', deadline)
            if metrics['counts']['measured_samples'] != count or metrics['counts']['warmup_samples'] != warmup:
                raise RuntimeError('periodic manifest does not match planned sample count')
        if any(load.poll() is not None for load in loads):
            raise RuntimeError('CPU interference worker exited unexpectedly')
        return metrics
    finally:
        errors = []
        with defer_interrupts():
            for proc in reversed(processes):
                try:
                    stop(proc)
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    if sampler is not None:
                        sampler.unregister(proc.pid)
        if errors:
            raise RuntimeError('cleanup failed: ' + '; '.join(errors))



def measurement_quality(metrics, folder, config, scenario):
    filename = 'sender.csv' if scenario['id'] == 'C01' else 'samples.csv'
    field = 'generated_ns' if scenario['id'] == 'C01' else 'start_ns'
    with (folder / filename).open() as stream:
        rows = [row for row in csv.DictReader(stream) if row['measured'] == '1']
    period = round(1000000000 / scenario['frequency_hz'])
    missed = sum(int(row[field]) - int(row['scheduled_ns']) > period for row in rows)
    fraction = missed / len(rows) if rows else None
    limits = config.get('quality_limits', {})
    warnings = []
    if len(rows) < limits.get('min_samples', 1000):
        warnings.append('Short sample population; tail estimates are exploratory.')
    if 'max_release_late_fraction' in limits and fraction is not None and fraction > limits['max_release_late_fraction']:
        warnings.append('Configured release-lateness limit exceeded; planned input was not maintained.')
    if scenario.get('deadline_us') is None:
        warnings.append('Business deadline not configured; no real-time acceptance verdict.')
    if config.get('sampling_mode', 'basic') == 'minimal':
        warnings.append('Resource sampler disabled; timestamp instrumentation remains enabled.')
    return {'status': 'review_required' if warnings else 'measurement_complete',
            'business_acceptance': 'not_evaluated_by_quality_check',
            'release_late_more_than_one_period': missed, 'release_late_fraction': fraction,
            'limits': limits, 'sampling_mode': config.get('sampling_mode', 'basic'),
            'warnings': warnings,
            'unobserved': ['middleware/executor queue wait', 'per-event kernel dispatch wait',
                           'full instrumentation overhead', 'robot business chain and GPU interference']}


def run_experiment(config, output, root=None):
    from .resources import ResourceSampler, summarize_resources
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    config = validate(config)
    output = Path(output)
    required = set()
    for scenario in config['scenarios']:
        if scenario['id'] == 'C01':
            required.add('ros_bench')
        if scenario['id'] == 'S01' or scenario.get('cpu_interference_workers', 0):
            required.add('periodic_bench')
    for name in required:
        if not (root / 'build' / name).is_file():
            raise RuntimeError('benchmark binary missing; build with CMake or use Docker')
    output.mkdir(parents=True, exist_ok=False)
    status = {'status': 'running', 'completed_runs': 0, 'error': None}
    results = []
    sampler = None
    try:
        (output / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
        env = environment(root)
        env['required_binaries'] = sorted(required)
        env['binary_sha256'] = {name: hashlib.sha256((root / 'build' / name).read_bytes()).hexdigest()
                                for name in sorted(required)}
        (output / 'environment.json').write_text(json.dumps(env, indent=2) + '\n')
        if config.get('sampling_mode', 'basic') == 'basic':
            sampler_kwargs = {'options': config['resource_options']} if config.get('resource_options') else {}
            sampler = ResourceSampler(output / 'resources.jsonl', config['resource_sampling_seconds'], **sampler_kwargs)
        with sampler if sampler is not None else nullcontext():
            for scenario in config['scenarios']:
                for rep in range(1, config['repetitions'] + 1):
                    folder = output / scenario['id'] / f'{rep:03}'
                    if sampler is not None:
                        sampler.set_window(scenario['id'], rep, 'active_including_warmup')
                    metrics = run_one(root, folder, config, scenario, sampler)
                    if sampler is not None:
                        sampler.set_window(scenario['id'], rep, 'analysis')
                    results.append({'scenario': scenario['id'], 'repetition': rep, 'metrics': metrics,
                                    'quality': measurement_quality(metrics, folder, config, scenario),
                                    'raw_directory': str(folder.relative_to(output))})
                    status['completed_runs'] += 1
                    print(f"Completed {scenario['id']} repetition {rep}", flush=True)
        if sampler is not None and sampler.error:
            raise RuntimeError('resource sampling failed: ' + sampler.error)
        for result in results:
            window = result['metrics']['measurement_window']
            result['resources'] = (summarize_resources(output / 'resources.jsonl', window['start_ns'], window['end_ns'])
                                   if sampler is not None and window['start_ns'] is not None and window['end_ns'] is not None
                                   else {'available': False, 'reason': 'sampler disabled or measurement window unavailable'})
        from .analysis import write_report
        write_report(output, config, env, results)
        status['status'] = 'complete'
        print(f'Report: {output / "REPORT.md"}', flush=True)
        return {'config': config, 'environment': env, 'results': results}
    except BaseException as exc:
        status['status'] = 'failed'; status['error'] = str(exc)
        status['error_type'] = type(exc).__name__
        raise
    finally:
        status['resource_sampler_error'] = sampler.error if sampler is not None else None
        (output / 'run-status.json').write_text(json.dumps(status, indent=2) + '\n')


def install_signal_handler():
    def terminate(signum, frame):
        raise KeyboardInterrupt(f'interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, terminate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    install_signal_handler()
    run_experiment(json.loads(args.config.read_text()), args.output)


if __name__ == '__main__':
    main()
