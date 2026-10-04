"""Read-only host profile; usable before ROS installation or a Docker build."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time


def optional(path):
    try:
        return Path(path).read_text().strip().replace('\x00', '')
    except (OSError, UnicodeError, TypeError):
        return None


def query(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        return {'available': result.returncode == 0,
                'value': result.stdout.strip() if result.returncode == 0 else None,
                'reason': None if result.returncode == 0 else 'query failed or permission denied'}
    except (OSError, subprocess.TimeoutExpired):
        return {'available': False, 'value': None, 'reason': 'command unavailable or timed out'}


class _Probe:
    """Read files under one root, including when a fixture contains symlinks."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, source):
        path = self.root / source.lstrip('/')
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError('path resolves outside fs_root')
        return resolved

    def read(self, source, validate=None):
        status = {'available': False, 'present': False, 'reason': None, 'source': source}
        try:
            path = self.path(source)
            path.stat()
            status['present'] = True
            value = path.read_text().strip().replace('\x00', '')
            if not value:
                status['reason'] = 'empty interface'
                return value, status
            if validate is not None and not validate(value):
                status['reason'] = 'unrecognized interface contents'
                return None, status
            status['available'] = True
            return value, status
        except PermissionError:
            status['reason'] = 'permission denied'
        except FileNotFoundError:
            status['reason'] = 'interface missing'
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            status['reason'] = 'read failed: ' + str(error)
        return None, status

    def children(self, source):
        try:
            # Check each child's resolved path again before any read or listing.
            return sorted(self.path(source).iterdir(), key=lambda path: path.name), None
        except PermissionError:
            return [], 'permission denied listing ' + source
        except FileNotFoundError:
            return [], 'interface directory missing: ' + source
        except (OSError, ValueError, TypeError) as error:
            return [], 'directory discovery failed: ' + str(error)


def _capability(probe, sources, validate=None, discovery_reason=None):
    interfaces = [probe.read(source, validate)[1] for source in sources]
    available = any(item['available'] for item in interfaces)
    capability = {'available': available, 'present': any(item['present'] for item in interfaces),
            'reason': None if available else (discovery_reason or '; '.join(
                dict.fromkeys(item['reason'] for item in interfaces)) or 'no matching interface'),
            'source': list(sources), 'interfaces': interfaces}
    if discovery_reason:
        capability['discovery_reason'] = discovery_reason
    return capability


def _nonnegative_integer(value):
    return value.isdigit()


def _task_stat(value):
    # comm may contain spaces and parentheses; fields after its final ')' are stable.
    suffix = value.rpartition(')')[2].split()
    return len(suffix) >= 20 and all(suffix[index].isdigit() for index in (11, 12, 19))


def _schedstat(value):
    fields = value.split()
    return len(fields) >= 3 and all(field.isdigit() for field in fields[:3])


def _collect_capabilities(probe, tools):
    capabilities = {
        'proc_cpu': _capability(probe, ['/proc/stat'], lambda text: any(
            line.startswith('cpu ') and len(line.split()) >= 5 and
            all(field.isdigit() for field in line.split()[1:]) for line in text.splitlines())),
        'proc_memory': _capability(probe, ['/proc/meminfo'], lambda text: any(
            re.fullmatch(r'MemTotal:\s+\d+\s+kB', line) for line in text.splitlines()))}
    tasks, task_reason = probe.children('/proc/self/task')
    task_sources = ['/proc/self/task/' + path.name for path in tasks if path.name.isdigit()]
    capabilities['task_stat'] = _capability(
        probe, [source + '/stat' for source in task_sources], _task_stat, task_reason)
    sched = _capability(probe, [source + '/schedstat' for source in task_sources], _schedstat, task_reason)
    enabled, enabled_status = probe.read('/proc/sys/kernel/sched_schedstats', lambda value: value in ('0', '1'))
    sched.update({'schedstats_enabled': enabled == '1' if enabled_status['available'] else None,
                  'setting': enabled_status, 'attribution_available': False,
                  'limitation': 'Readable cumulative counters do not establish scheduler event attribution; '
                                'zero counters do not prove schedstats is enabled.'})
    capabilities['task_schedstat'] = sched

    membership, membership_status = probe.read('/proc/self/cgroup')
    v1, v2 = [], []
    for line in (membership or '').splitlines():
        parts = line.split(':', 2)
        if (len(parts) != 3 or not parts[0].isdigit() or not parts[2].startswith('/') or
                any(part in ('.', '..') for part in parts[2].split('/'))):
            continue
        if parts[0] == '0' and not parts[1]:
            v2.append(parts[2])
        elif parts[1]:
            v1.append((parts[1], parts[2]))
    sources = []
    for group in v2:
        base = '/sys/fs/cgroup' + group.rstrip('/')
        sources.extend(base + '/' + name for name in ('cpu.stat', 'memory.current', 'cgroup.controllers'))
    for controllers, group in v1:
        # Mount names vary; test the combined and individual controller directories.
        for controller in dict.fromkeys([controllers] + controllers.split(',')):
            if controller not in ('cpu', 'cpuacct', 'cpu,cpuacct', 'cpuacct,cpu', 'memory'):
                continue
            base = '/sys/fs/cgroup/' + controller + group.rstrip('/')
            names = ('memory.usage_in_bytes',) if controller == 'memory' else ('cpu.stat', 'cpuacct.usage')
            sources.extend(base + '/' + name for name in names)
    cgroup = _capability(probe, list(dict.fromkeys(sources)))
    cgroup['mode'] = 'hybrid' if v1 and v2 else 'v2' if v2 else 'v1' if v1 else 'unknown'
    cgroup['membership'] = membership_status
    if not membership_status['available']:
        cgroup['reason'] = membership_status['reason']
    elif not v1 and not v2:
        cgroup['reason'] = 'no recognized cgroup membership'
    capabilities['cgroup'] = cgroup

    policies, policies_reason = probe.children('/sys/devices/system/cpu/cpufreq')
    capabilities['cpufreq'] = _capability(probe, [
        '/sys/devices/system/cpu/cpufreq/' + path.name + '/' + name
        for path in policies if path.name.startswith('policy')
        for name in ('scaling_cur_freq', 'cpuinfo_cur_freq')], _nonnegative_integer, policies_reason)
    devices, devices_reason = probe.children('/sys/class/devfreq')
    devfreq = {'gpu': [], 'emc': []}
    identifiers = {'gpu': {'gpu', 'ga10b', 'gv11b', 'gb20b'}, 'emc': {'emc'}}
    identity_interfaces = []
    for path in devices:
        source = '/sys/class/devfreq/' + path.name
        name, identity = probe.read(source + '/name')
        identity_interfaces.append(identity)
        # Hardware names identify devfreq source candidates, not valid metrics.
        # Reading a recognized cur_freq interface remains the availability condition.
        words = set(re.split(r'[^a-z0-9]+', (path.name + ' ' + (name or '')).lower()))
        for kind in devfreq:
            if words & identifiers[kind]:
                devfreq[kind].append(source + '/cur_freq')
    for kind, sources in devfreq.items():
        capabilities[kind + '_devfreq'] = _capability(
            probe, sources, _nonnegative_integer, devices_reason)
        capabilities[kind + '_devfreq']['identity_interfaces'] = identity_interfaces
    for kind, directory, filename in (
            ('hwmon_power', '/sys/class/hwmon', r'power\d+_(?:input|average)'),
            ('thermal', '/sys/class/thermal', r'temp')):
        devices, discovery_reason = probe.children(directory)
        sources = []
        for device in devices:
            if kind == 'thermal' and not device.name.startswith('thermal_zone'):
                continue
            entries, reason = probe.children(directory + '/' + device.name)
            if reason and discovery_reason is None:
                discovery_reason = reason
            sources.extend(directory + '/' + device.name + '/' + path.name
                           for path in entries if re.fullmatch(filename, path.name))
        capabilities[kind] = _capability(probe, sources, lambda value: value.lstrip('-').isdigit(), discovery_reason)
    capabilities['tracefs'] = _capability(probe, [
        '/sys/kernel/tracing/available_events', '/sys/kernel/debug/tracing/available_events'])
    capabilities['tracefs']['functional'] = None
    capabilities['tracefs']['limitation'] = 'Event list visibility only; tracing was not started or tested.'
    capabilities['perf'] = _capability(probe, ['/proc/sys/kernel/perf_event_paranoid'],
                                        lambda value: value.lstrip('-').isdigit())
    capabilities['perf'].update({'found': tools['perf'], 'functional': None,
                                 'limitation': 'Configuration visibility and tool discovery only; '
                                               'perf_event_open permissions and collection were not tested.'})
    return capabilities


def collect_profile(fs_root=Path('/'), include_kernel_command_line=True):
    probe = _Probe(fs_root)
    def read(path):
        return probe.read(path)[0]
    board = read('/proc/device-tree/model') or read('/sys/firmware/devicetree/base/model')
    bsp = read('/etc/nv_tegra_release')
    system, architecture = platform.system(), platform.machine()
    jetson = system == 'Linux' and architecture in ('aarch64', 'arm64') and bool(
        bsp or (board and any(name in board.lower() for name in ('jetson', 'orin', 'thor'))))
    family = None
    if jetson:
        words = set(re.split(r'[^a-z0-9]+', (board or '').lower()))
        family = 'thor' if 'thor' in words else 'orin' if 'orin' in words else 'unknown'
    cpu = {}
    for line in (read('/proc/cpuinfo') or '').splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            if key.strip() in ('model name', 'Processor', 'CPU implementer', 'CPU part'):
                cpu.setdefault(key.strip(), value.strip())
    frequency_policies = {}
    for path in probe.children('/sys/devices/system/cpu/cpufreq')[0]:
        if not path.name.startswith('policy'):
            continue
        frequency_policies[path.name] = {key: read('/sys/devices/system/cpu/cpufreq/' + path.name + '/' + key) for key in (
            'affected_cpus', 'scaling_governor', 'scaling_min_freq', 'scaling_max_freq')}
    checks = {'linux': system == 'Linux', 'native_arm64': architecture in ('aarch64', 'arm64'),
              'jetson_detected': jetson, 'python': platform.python_version(),
              'tools': {name: shutil.which(name) is not None for name in
                        ('docker', 'cmake', 'c++', 'nvpmodel', 'tegrastats', 'perf',
                         'pidstat', 'mpstat', 'cyclictest', 'rtla', 'nsys', 'ros2', 'lttng')}}
    return {'format_version': 1, 'kind': 'host_readonly_profile',
            'recorded_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'monotonic_ns': time.monotonic_ns(), 'system': system, 'architecture': architecture,
            'kernel_release': platform.release(), 'os_release': read('/etc/os-release'),
            'board_model': board, 'jetson_linux_release': bsp, 'jetson_family': family, 'cpu_model': cpu,
            'cpu_online': read('/sys/devices/system/cpu/online'),
            'cpu_affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
            'kernel_command_line': read('/proc/cmdline') if include_kernel_command_line else None,
            'schedstats_enabled': read('/proc/sys/kernel/sched_schedstats'),
            'frequency_policies': frequency_policies,
            'nvpmodel_readonly': query(['nvpmodel', '-q']),
            'checks': checks,
            'capabilities': _collect_capabilities(probe, checks['tools']),
            'manual_fields': ['module/carrier details', 'cooling and ambient conditions',
                              'business deadlines and data-age limits', 'production IRQ/affinity policy'],
            'limitations': ['No system settings were changed.',
                            'Detection is not certification of ROS/BSP compatibility.',
                            'Capabilities describe interface reads, not performance or full attribution support.',
                            'available means at least one successful interface read; per-interface failures are retained.',
                            'checks.tools reports found != functional; tools were not executed except nvpmodel -q.',
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
