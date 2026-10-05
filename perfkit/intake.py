"""Read-only machine intake: local interface reads, no launched commands."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import signal
import time
import uuid

from .lifecycle import defer_interrupts
from .monitor import _source_record, profile_config, validate_config
from .platform_probe import _Probe, collect_profile


MANUAL_FIELDS = (
    'module_model', 'carrier_board', 'cooling', 'ambient_conditions',
    'workload_version', 'ros_distribution', 'rmw_implementation', 'power_mode',
)
VIEWS = ('unknown', 'native-host', 'container')


def validate_metadata(value):
    if not isinstance(value, dict) or set(value) - {'format_version', *MANUAL_FIELDS}:
        raise ValueError('metadata must contain only supported manual fields')
    if type(value.get('format_version')) is not int or value['format_version'] != 1:
        raise ValueError('metadata format_version must be 1')
    for name in MANUAL_FIELDS:
        item = value.get(name)
        if item is not None and (not isinstance(item, str) or not item.strip()
                                 or len(item) > 512 or any(ord(c) < 32 for c in item)):
            raise ValueError(name + ': nonempty single-line text up to 512 characters required')
    return dict(value)


def _write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')


def _status(path, value):
    # Only update the status in the directory exclusively created by this run.
    with defer_interrupts():
        path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                        encoding='utf-8')


def _fact(probe, source, convert):
    text, status = probe.read(source)
    value = None
    if status['available']:
        try:
            value = convert(text)
        except (ValueError, TypeError):
            status.update(available=False, reason='unrecognized interface contents')
    return {'value': value, 'observation': status}


def _integer(text):
    if not text.isascii() or not text.isdecimal():
        raise ValueError('nonnegative integer required')
    return int(text)


def _memory(text):
    rows = re.findall(r'(?m)^MemTotal:\s+(\d+)\s+kB\s*$', text)
    if len(rows) != 1 or int(rows[0]) <= 0:
        raise ValueError('one positive MemTotal in kB required')
    return int(rows[0]) * 1024


def hardware_facts(fs_root):
    probe = _Probe(fs_root)
    cpus, cpu_reason = probe.children('/sys/devices/system/cpu')
    topology = []
    for cpu in cpus:
        if re.fullmatch(r'cpu[0-9]+', cpu.name):
            base = '/sys/devices/system/cpu/' + cpu.name + '/topology/'
            topology.append({'cpu': int(cpu.name[3:]),
                'core_id': _fact(probe, base + 'core_id', _integer),
                'package_id': _fact(probe, base + 'physical_package_id', _integer),
                'thread_siblings_list': _fact(probe, base + 'thread_siblings_list', str)})
    disks, disk_reason = probe.children('/sys/block')
    storage = [{'name': disk.name,
                # Linux sysfs size is in 512-byte sectors, even for 4KiB disks.
                'capacity_bytes': _fact(probe, '/sys/block/' + disk.name + '/size',
                                       lambda text: _integer(text) * 512)} for disk in disks]
    return {'memory_total_bytes': _fact(probe, '/proc/meminfo', _memory),
            'cpu_topology': {'entries': topology, 'discovery_reason': cpu_reason},
            'visible_block_devices': {'entries': storage, 'discovery_reason': disk_reason},
            'scope': 'current filesystem view; capacity is not storage performance; no serial numbers'}


def _cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def write_report(output, machine, capabilities, status):
    host = machine['observed_platform']
    lines = ['# 机器只读接入报告', '',
             '报告生成时状态：' + status['status'] + '；最终状态以 intake-status.json 为准。', '',
             '机器代号：' + _cell(machine['machine_id']),
             '运行 ID：' + machine['run_id'],
             '平台视图：' + machine['view']['declared'] + '（操作者声明，不是宿主认证）',
             '系统/架构：' + _cell(host['system']) + ' / ' + _cell(host['architecture']),
             '型号：' + _cell(host['board_model']),
             '内核：' + _cell(host['kernel_release']), '',
             '本入口不启动外部命令或性能负载；实际工具选项、权限与采样资格尚未验证。', '',
             '| 能力 | 本次至少一个接口可读 | 原因/限制 |',
             '| --- | --- | --- |']
    for name, item in capabilities['interfaces'].items():
        lines.append('| {} | {} | {} |'.format(name, item['available'],
                     _cell(item.get('reason') or item.get('limitation') or '仅表示接口读取成功')))
    lines += ['', '| 工具 | 找到可执行文件 | 版本/选项 | 实际采样 |',
              '| --- | --- | --- | --- |']
    for name, item in capabilities['tools'].items():
        lines.append('| {} | {} | {} | {} |'.format(name, item['found'],
                     item['version_options']['status'], item['sampling']['status']))
    lines += ['', '## 明确要求', '', '```json',
              json.dumps(status['requirements'], ensure_ascii=False, indent=2), '```', '',
              '## 人工声明', '', '```json',
              json.dumps(machine['manual_declarations'], ensure_ascii=False, indent=2), '```', '',
              '## 下一步', '',
              '- 检查 machine-profile.json 的来源和硬件读取缺口，补充 metadata 人工声明。',
              '- 修改 monitor-config.suggested.json 的 UID/名称/PID/cgroup，先确认业务范围再采集。',
              '- 工具功能验证使用源码仓库的 compare_tools.py 对应预检查和有限运行入口。',
              '- 性能基线单独选择 S01/C01 与固定负载；接入完成不表示性能达标。',
              '- 输出留在设备或受控存储；档案可能包含本地接口路径、部署声明和 namespace。']
    with (output / 'MACHINE_REPORT.md').open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')


def _validate_options(machine_id, view, require_jetson, required_capabilities, skip_temperature):
    if view not in VIEWS:
        raise ValueError('unsupported declared view')
    if machine_id is not None and (not isinstance(machine_id, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', machine_id)):
        raise ValueError('machine_id must be a 1..64 character anonymous identifier')
    if type(require_jetson) is not bool:
        raise ValueError('require_jetson must be boolean')
    if (not isinstance(required_capabilities, (list, tuple))
            or any(not isinstance(item, str) or not item for item in required_capabilities)):
        raise ValueError('required_capabilities must be a list of names')
    if type(skip_temperature) is not bool:
        raise ValueError('skip_temperature must be boolean')
    if skip_temperature and 'thermal' in required_capabilities:
        raise ValueError('required capability thermal conflicts with --skip-temperature')


def run_intake(output, *, machine_id=None, view='unknown', metadata=None,
               require_jetson=False, required_capabilities=(), fs_root=Path('/'),
               skip_temperature=False):
    _validate_options(machine_id, view, require_jetson, required_capabilities, skip_temperature)
    manual = validate_metadata(metadata if metadata is not None else {'format_version': 1})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    begin = time.monotonic_ns()
    status = {'format_version': 1, 'status': 'running', 'error': None, 'completed_files': [],
              'requirements': [], 'started_ns': begin, 'finished_ns': None,
              'probe_options': {'skip_temperature': skip_temperature}}
    _write(output / 'intake-status.json', status)
    try:
        host = collect_profile(fs_root, include_kernel_command_line=False, query_power_mode=False,
                               skip_temperature=skip_temperature)
        unknown = set(required_capabilities) - host['capabilities'].keys()
        if unknown:
            raise ValueError('unknown required capabilities: ' + ', '.join(sorted(unknown)))
        run_id = uuid.uuid4().hex
        source = _source_record(query_git=False)
        source['git_revision_source'] = 'environment_declared_not_verified' if source['git_revision'] else None
        machine = {'format_version': 1, 'kind': 'machine_readonly_intake', 'run_id': run_id,
                   'machine_id': machine_id or 'machine-' + run_id[:12],
                   'machine_id_source': 'operator_supplied' if machine_id else 'anonymous_this_run',
                   'view': {'declared': view, 'source': 'operator_supplied' if view != 'unknown' else 'unspecified',
                            'observation_scope': 'current process/filesystem view; host provenance not verified'},
                   'source': source, 'observed_platform': host,
                   'hardware': hardware_facts(fs_root),
                   'manual_declarations': {name: {'value': manual.get(name),
                       'source': 'operator_supplied' if manual.get(name) is not None else None,
                       'status': 'declared_not_verified' if manual.get(name) is not None else 'not_configured'}
                       for name in MANUAL_FIELDS},
                   'input_metadata_sha256': hashlib.sha256(json.dumps(manual, sort_keys=True,
                       ensure_ascii=False).encode()).hexdigest(),
                   'read_window': {'start_ns': begin, 'end_ns': time.monotonic_ns()},
                   'limitations': ['No kernel command line, process arguments, environment dump, hostname or serial collected.',
                                   'Shell ROS/RMW declarations are not inferred as the business runtime.',
                                   'No tool was executed; missing optional interfaces do not mean performance failure.',
                                   'Git revision is not queried; package version/module hashes identify the running code.']}
        capabilities = {'format_version': 1, 'run_id': run_id, 'interfaces': host['capabilities'],
                        'tools': {name: {'found': found,
                            'version_options': {'status': 'not_evaluated' if found else 'unavailable',
                                                'reason': 'not queried by intake' if found else 'executable missing'},
                            'sampling': {'status': 'not_evaluated', 'reason': 'no measurement was started'}}
                            for name, found in host['checks']['tools'].items()}}
        requirements = [{'name': 'linux', 'met': host['checks']['linux'],
                         'scope': 'observed operating system'}]
        if require_jetson:
            requirements.append({'name': 'jetson', 'met': host['checks']['jetson_detected'],
                                 'scope': 'device evidence in current view, not host/container certification'})
        requirements += [{'name': name, 'met': host['capabilities'][name]['available'],
                          'scope': 'at least one interface read; not event attribution or performance'}
                         for name in dict.fromkeys(required_capabilities)]
        status['requirements'] = requirements
        suggested = validate_config(profile_config('light'))
        suggested['require_jetson'] = require_jetson
        for name, value in (('machine-profile.json', machine), ('capabilities.json', capabilities),
                            ('monitor-config.suggested.json', suggested)):
            _write(output / name, value)
            status['completed_files'].append(name)
        status['status'] = 'complete' if all(item['met'] for item in requirements) else 'failed'
        if status['status'] == 'failed':
            status['error'] = 'required observations unavailable: ' + ', '.join(
                item['name'] for item in requirements if not item['met'])
        write_report(output, machine, capabilities, status)
        status['completed_files'].append('MACHINE_REPORT.md')
        if status['status'] == 'failed':
            raise RuntimeError(status['error'])
    except BaseException as error:
        status.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        status['finished_ns'] = time.monotonic_ns()
        try:
            _status(output / 'intake-status.json', status)
        except KeyboardInterrupt as error:
            status.update(status='interrupted', error=type(error).__name__ + ': ' + str(error),
                          finished_ns=time.monotonic_ns())
            _status(output / 'intake-status.json', status)
            raise
    return machine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--machine-id', help='anonymous local alias; omit for a fresh per-run alias')
    parser.add_argument('--view', choices=VIEWS, default='unknown', help='operator declaration, not automatic host verification')
    parser.add_argument('--metadata', type=Path, help='manual declarations JSON; never treated as observed runtime')
    parser.add_argument('--require-jetson', action='store_true')
    parser.add_argument('--require-capability', action='append', default=[])
    parser.add_argument('--skip-temperature', action='store_true', help='do not discover or read thermal temperature interfaces')
    args = parser.parse_args()
    def terminate(signum, frame):
        raise KeyboardInterrupt('intake interrupted by SIGTERM')
    old = signal.signal(signal.SIGTERM, terminate)
    try:
        _validate_options(args.machine_id, args.view, args.require_jetson,
                          args.require_capability, args.skip_temperature)
        manual = json.loads(args.metadata.read_text(encoding='utf-8')) if args.metadata else None
        run_intake(args.output, machine_id=args.machine_id, view=args.view, metadata=manual,
                   require_jetson=args.require_jetson, required_capabilities=args.require_capability,
                   skip_temperature=args.skip_temperature)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print('Machine intake failed:', error)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old)
    print('Machine intake saved:', args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
