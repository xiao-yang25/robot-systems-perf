"""Read-only host profile; usable before ROS installation or a Docker build."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time


def optional(path):
    try:
        return Path(path).read_text().strip().replace('\x00', '')
    except (OSError, UnicodeError):
        return None


def query(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        return {'available': result.returncode == 0,
                'value': result.stdout.strip() if result.returncode == 0 else None,
                'reason': None if result.returncode == 0 else 'query failed or permission denied'}
    except (OSError, subprocess.TimeoutExpired):
        return {'available': False, 'value': None, 'reason': 'command unavailable or timed out'}


def collect_profile(fs_root=Path('/'), include_kernel_command_line=True):
    def read(path):
        return optional(fs_root / path.lstrip('/'))
    board = read('/proc/device-tree/model') or read('/sys/firmware/devicetree/base/model')
    bsp = read('/etc/nv_tegra_release')
    system, architecture = platform.system(), platform.machine()
    jetson = system == 'Linux' and architecture in ('aarch64', 'arm64') and bool(
        bsp or (board and any(name in board.lower() for name in ('jetson', 'orin', 'thor'))))
    cpu = {}
    for line in (read('/proc/cpuinfo') or '').splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            if key.strip() in ('model name', 'Processor', 'CPU implementer', 'CPU part'):
                cpu.setdefault(key.strip(), value.strip())
    frequency_policies = {}
    for path in (fs_root / 'sys/devices/system/cpu/cpufreq').glob('policy*'):
        frequency_policies[path.name] = {key: optional(path / key) for key in (
            'affected_cpus', 'scaling_governor', 'scaling_min_freq', 'scaling_max_freq')}
    checks = {'linux': system == 'Linux', 'native_arm64': architecture in ('aarch64', 'arm64'),
              'jetson_detected': jetson, 'python': platform.python_version(),
              'tools': {name: shutil.which(name) is not None for name in
                        ('docker', 'cmake', 'c++', 'nvpmodel', 'tegrastats', 'perf')}}
    return {'format_version': 1, 'kind': 'host_readonly_profile',
            'recorded_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'monotonic_ns': time.monotonic_ns(), 'system': system, 'architecture': architecture,
            'kernel_release': platform.release(), 'os_release': read('/etc/os-release'),
            'board_model': board, 'jetson_linux_release': bsp, 'cpu_model': cpu,
            'cpu_online': read('/sys/devices/system/cpu/online'),
            'cpu_affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
            'kernel_command_line': read('/proc/cmdline') if include_kernel_command_line else None,
            'schedstats_enabled': read('/proc/sys/kernel/sched_schedstats'),
            'frequency_policies': frequency_policies,
            'nvpmodel_readonly': query(['nvpmodel', '-q']),
            'checks': checks,
            'manual_fields': ['module/carrier details', 'cooling and ambient conditions',
                              'business deadlines and data-age limits', 'production IRQ/affinity policy'],
            'limitations': ['No system settings were changed.',
                            'Detection is not certification of ROS/BSP compatibility.',
                            'No device serial numbers or host names are collected.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-jetson', action='store_true')
    args = parser.parse_args()
    profile = collect_profile()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(profile, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print('Host profile saved; Jetson detected:', profile['checks']['jetson_detected'])
    if args.require_jetson and not profile['checks']['jetson_detected']:
        raise SystemExit('Expected a native Linux ARM64 Jetson; inspect the saved host profile')


if __name__ == '__main__':
    main()
