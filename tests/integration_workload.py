"""Installed M2a: shared process, restart, ambiguity, failure and cancellation."""
import argparse
from email.parser import Parser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import venv
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.comparison_common import launch
from tests.process_helpers import OwnedProcesses


PROGRAM = """import ctypes,sys,threading,time
ctypes.CDLL(None).prctl(15,sys.argv[1].encode(),0,0,0)
def worker():
 while True: time.sleep(.01)
for _ in range(2): threading.Thread(target=worker,daemon=True).start()
worker()
"""
SECRET = 'm2a-private-argument-sentinel'


def wait_for(check, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.03)
    raise AssertionError('bounded workload observation timed out')


def rows(output):
    path = output / 'business-relations.jsonl'
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines(keepends=True):
        if line.endswith('\n'):
            result.append(json.loads(line))
    return [row for row in result if row['event'] == 'business_relation_scan']


def latest(output):
    records = rows(output)
    return {row['function_id']: row for row in records[-1]['functions']} if records else {}


def verify(wheel, output):
    owner = OwnedProcesses()
    with tempfile.TemporaryDirectory(prefix='m2a-install-') as temporary:
        root = Path(temporary)
        prefix, cwd = root / 'venv', root / 'unrelated'
        cwd.mkdir()
        venv.EnvBuilder(with_pip=False).create(prefix)
        pip_spec = importlib.util.find_spec('pip')
        if pip_spec is None:
            raise RuntimeError('offline integration requires prepared pip bootstrap')
        env = dict(os.environ)
        env.pop('EP_SOURCE_REVISION', None)
        env['PYTHONPATH'] = str(Path(pip_spec.origin).parent.parent)
        install = subprocess.run([str(prefix / 'bin/python'), '-m', 'pip', '--isolated', 'install',
            '--no-index', '--disable-pip-version-check', str(wheel.resolve())], env=env,
            text=True, capture_output=True, timeout=30)
        (output / 'install.log').write_text(install.stdout + install.stderr)
        assert install.returncode == 0
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            assert len(metadata_names) == 1
            expected_version = Parser().parsestr(archive.read(metadata_names[0]).decode())['Version']
        env.pop('PYTHONPATH', None)
        null_workload, rejected = root / 'null.json', output / 'null-rejected'
        null_workload.write_text('null\n')
        invalid = subprocess.run([str(prefix / 'bin/robot-perf-monitor'), '--workload', str(null_workload),
            '--output', str(rejected)], cwd=cwd, env=env, text=True, capture_output=True, timeout=10)
        (output / 'null-rejection.log').write_text(invalid.stdout + invalid.stderr)
        assert invalid.returncode != 0 and 'unknown or invalid workload fields' in invalid.stderr
        assert not rejected.exists(), 'invalid workload must not start an unscoped capture'
        names = {role: 'm2_' + uuid.uuid4().hex[:6] + '_' + role for role in ('cmp', 'ctl', 'out')}
        workload = {'format_version': 1, 'workload_id': 'integration-demo',
            'workload_version': 'fixture-v1', 'functions': [
                {'id': 'detector', 'process_selector': {'include_names': ['^' + names['cmp'] + '$']},
                 'ros_nodes': ['/fixture/detector']},
                {'id': 'tracker', 'process_selector': {'include_names': ['^' + names['cmp'] + '$']},
                 'ros_nodes': ['/fixture/tracker']},
                {'id': 'control', 'process_selector': {'include_names': ['^' + names['ctl'] + '$']},
                 'ros_nodes': ['/fixture/control']}]}
        config = output / 'workload.local.json'
        config.write_text(json.dumps(workload, indent=2) + '\n')
        logs = []
        def spawn(kind):
            process = launch(owner, [sys.executable, '-c', PROGRAM, names[kind], SECRET],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            wait_for(lambda: owner.handle(process).alive() and
                     owner.backend.bound_info(owner.handle(process).directory).comm == names[kind])
            return process
        def monitor(folder, seconds, *, fail=False, workload_path=None, cleanup_fault=False, final_fault=None):
            args = ['--workload', str((workload_path or config).resolve()), '--profile', 'light', '--seconds', str(seconds),
                    '--interval', '.1', '--process-interval', '.1', '--system-interval', '.1',
                    '--discovery-interval', '.2', '--output', str(folder.resolve())]
            command = [str(prefix / 'bin/robot-perf-monitor'), *args]
            if fail:
                code = """import sys
from perfkit import monitor
original=monitor.BusinessRelations.observe
def fault(self,*args):
 if self.scans: raise RuntimeError('controlled relation write failure')
 return original(self,*args)
monitor.BusinessRelations.observe=fault
sys.argv=['robot-perf-monitor',*sys.argv[1:]]
monitor.main()
"""
                command = [str(prefix / 'bin/python'), '-c', code, *args]
            elif cleanup_fault:
                code = """import sys
from perfkit import monitor
def fault(*args): raise OSError('controlled interrupted summary failure')
monitor.summarize_resources=fault
sys.argv=['robot-perf-monitor',*sys.argv[1:]]
monitor.main()
"""
                command = [str(prefix / 'bin/python'), '-c', code, *args]
            elif final_fault:
                code = """import hashlib,json,os,signal,sys
from perfkit import monitor
phase=sys.argv[1]
original_json,original_report=monitor._json,monitor.write_monitor_report
sent=[]
def write(path,value):
 if sent and phase=='summary' and path.name=='monitor-summary.json': raise OSError('controlled summary rewrite failure')
 result=original_json(path,value)
 if path.name=='monitor-status.json' and value['status']=='complete' and not sent:
  sent.append(True)
  marker={'status_on_disk':json.loads(path.read_text())['status'],'phase':phase,'signal':int(signal.SIGTERM),
          'raw_sha256':{name:hashlib.sha256((path.parent/name).read_bytes()).hexdigest()
                        for name in ('resources.jsonl','discovery.jsonl','business-relations.jsonl')}}
  (path.parent/'final-signal-marker.json').write_text(json.dumps(marker)+'\\n')
  os.kill(os.getpid(),signal.SIGTERM)
 return result
def report(*args):
 if sent and phase=='report': raise OSError('controlled report rewrite failure')
 return original_report(*args)
monitor._json,monitor.write_monitor_report=write,report
sys.argv=['robot-perf-monitor',*sys.argv[2:]]
monitor.main()
"""
                command = [str(prefix / 'bin/python'), '-c', code, final_fault, *args]
            log = (output / (folder.name + '.log')).open('x')
            logs.append(log)
            return launch(owner, command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            component, control, outside = spawn('cmp'), spawn('ctl'), spawn('out')
            capture = output / 'capture'
            observer = monitor(capture, 8)
            wait_for(lambda: len(latest(capture)) == 3 and all(row['candidates'] and
                row['candidates'][0]['resource_ref'] for row in latest(capture).values()))
            first = latest(capture)
            first_ref = first['detector']['candidates'][0]['resource_ref']
            assert first_ref == first['tracker']['candidates'][0]['resource_ref']
            duplicate = spawn('cmp')
            wait_for(lambda: latest(capture).get('detector', {}).get('status') == 'ambiguous')
            owner.signal(duplicate, signal.SIGTERM)
            duplicate.wait(timeout=5)
            owner.signal(component, signal.SIGTERM)
            component.wait(timeout=5)
            wait_for(lambda: latest(capture).get('detector', {}).get('status') == 'unresolved')
            replacement = spawn('cmp')
            wait_for(lambda: any(item['pid'] == replacement.pid for item in
                latest(capture).get('detector', {}).get('candidates', [])))
            assert observer.wait(timeout=15) == 0
            summary = json.loads((capture / 'monitor-summary.json').read_text())
            business = summary['workload']
            by_function = {row['function_id']: row for row in business['functions']}
            assert by_function['detector']['resource_refs'] == by_function['tracker']['resource_refs']
            assert by_function['detector']['scan_status_counts']['ambiguous'] > 0
            assert by_function['detector']['scan_status_counts']['unresolved'] > 0
            assert summary['quality']['status'] == 'review_required'
            entities = summary['resources']['registered_entities']
            for key in business['unique_resource_refs']:
                for function in business['functions']:
                    if key not in function['resource_refs']:
                        continue
                    status = function['resource_reference_status'][key]
                    assert status['entity_recorded'] == (key in entities)
                    if key not in entities:
                        assert key in function['unavailable_resource_refs'] and status['reason'], key
            assert all(item['pid'] != outside.pid for item in entities.values())
            assert any(first_ref in row['expired_resource_refs']
                       for record in rows(capture) for row in record['functions'])
            assert any(item['pid'] == replacement.pid for item in entities.values())
            assert all(row['ros_node_evidence'] == 'operator_declared_not_verified'
                       for row in business['functions'])
            assert 'cpu_percent_one_core' not in json.dumps(business['functions'])
            environment = json.loads((capture / 'environment.json').read_text())
            assert environment['source']['package_version'] == expected_version
            with zipfile.ZipFile(wheel) as archive:
                for name, digest in environment['source']['sha256'].items():
                    assert hashlib.sha256(archive.read(name)).hexdigest() == digest
            before = {path.name: path.read_bytes() for path in capture.iterdir()}
            rejected = subprocess.run([str(prefix / 'bin/robot-perf-monitor'), '--workload', str(config),
                '--output', str(capture)], cwd=cwd, env=env, capture_output=True, timeout=10)
            assert rejected.returncode != 0
            assert b'FileExistsError' in rejected.stderr
            assert before == {path.name: path.read_bytes() for path in capture.iterdir()}
            pid_workload = json.loads(json.dumps(workload))
            for function in pid_workload['functions']:
                function['process_selector'] = {'pids': [control.pid if function['id'] == 'control' else replacement.pid]}
            pid_config, pid_capture = output / 'pid-workload.local.json', output / 'explicit-pids'
            pid_config.write_text(json.dumps(pid_workload) + '\n')
            pid_monitor = monitor(pid_capture, 2, workload_path=pid_config)
            assert pid_monitor.wait(timeout=10) == 0
            pid_summary = json.loads((pid_capture / 'monitor-summary.json').read_text())
            assert json.loads((pid_capture / 'workload-profile.json').read_text())['discovery_mode'] == 'explicit_pids'
            pid_scans = [json.loads(line) for line in (pid_capture / 'discovery.jsonl').read_text().splitlines()]
            assert pid_scans and all(row['scan']['mode'] == 'explicit_pids' for row in pid_scans)
            assert all(row['scan']['process_count'] == 2 for row in pid_scans)
            assert all({item['pid'] for item in row['targets']} == {control.pid, replacement.pid} for row in pid_scans)
            pid_functions = {row['function_id']: row for row in pid_summary['workload']['functions']}
            assert pid_functions['detector']['resource_refs'] == pid_functions['tracker']['resource_refs']
            assert len(pid_summary['workload']['unique_resource_refs']) == 2
            interrupted = output / 'interrupted'
            cancelled = monitor(interrupted, 30)
            wait_for(lambda: bool(latest(interrupted).get('detector', {}).get('candidates')))
            owner.signal(cancelled, signal.SIGTERM)
            assert cancelled.wait(timeout=10) == 130
            assert json.loads((interrupted / 'monitor-status.json').read_text())['status'] == 'interrupted'
            assert json.loads((interrupted / 'monitor-summary.json').read_text())['status'] == 'interrupted'
            assert 'interrupted' in (interrupted / 'BUSINESS_MAP_REPORT.md').read_text()
            assert not Path('/proc', str(cancelled.pid)).exists()
            interrupted_fault = output / 'interrupted-summary-fault'
            cancel_fault = monitor(interrupted_fault, 30, cleanup_fault=True)
            wait_for(lambda: bool(latest(interrupted_fault).get('detector', {}).get('candidates')))
            owner.signal(cancel_fault, signal.SIGTERM)
            assert cancel_fault.wait(timeout=10) == 130
            cancel_status = json.loads((interrupted_fault / 'monitor-status.json').read_text())
            assert cancel_status['status'] == 'interrupted' and cancel_status['error_type'] == 'KeyboardInterrupt'
            assert any(row['error'] == 'controlled interrupted summary failure' for row in cancel_status['cleanup_errors'])
            cancel_terminal = json.loads((interrupted_fault / 'business-relations.jsonl').read_text().splitlines()[-1])
            assert cancel_terminal['outcome'] == 'interrupted'
            assert cancel_terminal['monotonic_ns'] == cancel_status['window_end_ns']
            assert not Path('/proc', str(cancel_fault.pid)).exists()
            final_fault_folders = []
            for phase in ('summary', 'report'):
                final_folder = output / ('final-status-' + phase + '-fault')
                final_fault_folders.append(final_folder)
                final_monitor = monitor(final_folder, 1, workload_path=pid_config, final_fault=phase)
                assert final_monitor.wait(timeout=10) == 130
                final_status = json.loads((final_folder / 'monitor-status.json').read_text())
                assert final_status['status'] == 'interrupted' and final_status['error_type'] == 'KeyboardInterrupt'
                assert final_status['window_start_ns'] is not None and final_status['window_end_ns'] is not None
                assert any(row['error_type'] == 'OSError' and row['error'] ==
                           'controlled ' + phase + ' rewrite failure' for row in final_status['cleanup_errors'])
                marker = json.loads((final_folder / 'final-signal-marker.json').read_text())
                assert marker['status_on_disk'] == 'complete' and marker['signal'] == int(signal.SIGTERM)
                assert marker['raw_sha256'] == {name: hashlib.sha256((final_folder / name).read_bytes()).hexdigest()
                                               for name in marker['raw_sha256']}
                assert rows(final_folder), 'late cancellation must preserve prior observations'
                assert all(proc.poll() is None for proc in (control, outside, replacement))
                assert not Path('/proc', str(final_monitor.pid)).exists()
            failed = output / 'failed'
            faulted = monitor(failed, 5, fail=True)
            assert faulted.wait(timeout=10) != 0
            failed_status = json.loads((failed / 'monitor-status.json').read_text())
            assert failed_status['status'] == 'failed'
            assert failed_status['error'] == 'controlled relation write failure'
            assert failed_status['window_start_ns'] is not None and failed_status['window_end_ns'] is not None
            terminal = [json.loads(line) for line in (failed / 'business-relations.jsonl').read_text().splitlines()][-1]
            assert terminal['event'] == 'observation_ended' and terminal['outcome'] == 'failed'
            assert terminal['monotonic_ns'] == failed_status['window_end_ns']
            assert rows(failed), 'failure must preserve prior relation evidence'
            assert all(proc.poll() is None for proc in (control, outside, replacement)), 'monitor affected external fixtures'
            for folder in (capture, pid_capture, interrupted, interrupted_fault, failed, *final_fault_folders):
                for path in folder.iterdir():
                    if path.is_file(): assert SECRET not in path.read_text(), path.name
            (output / 'verification.json').write_text(json.dumps({
                'installed_version': expected_version, 'installed_module_hashes_match_wheel': True,
                'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
                'shared_process_refs': True, 'ambiguity_and_restart': True,
                'unrelated_process_not_collected': True, 'no_overwrite': True,
                'null_workload_rejected_before_output': True,
                'pure_pid_mode_and_shared_refs': True,
                'sigterm_exitcode': 130, 'failure_retains_evidence': True,
                'failure_window_closed': True,
                'sigterm_with_summary_failure_keeps_130': True,
                'final_status_sigterm_summary_failure_consistent': True,
                'final_status_sigterm_report_failure_consistent': True,
                'external_fixtures_alive': True, 'no_argument_collection': True,
                'ros_nodes_are_declarations': True, 'no_target_device_claim': True}, indent=2) + '\n')
        finally:
            try:
                owner.cleanup()
            finally:
                for log in logs: log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    verify(args.wheel, args.output)
    print('Installed M2a integration passed')


if __name__ == '__main__':
    main()
