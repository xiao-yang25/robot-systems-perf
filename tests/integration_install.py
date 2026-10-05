"""Offline wheel installation and multi-process monitoring from an unrelated cwd."""
import argparse
from email.parser import Parser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.process_helpers import OwnedProcesses
from tests.integration_monitor import cleanup, spawn


def verify(wheel, output, pip_root=None):
    owned = OwnedProcesses()
    if pip_root is None:
        spec = importlib.util.find_spec('pip')
        if spec is None:
            raise RuntimeError('Offline check needs an installed pip module or explicit --pip-root')
        pip_root = Path(spec.origin).parent.parent
    with tempfile.TemporaryDirectory(prefix='perfkit-installed-') as directory:
        prefix = Path(directory)/'venv'
        venv.EnvBuilder(with_pip=False).create(prefix)
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env.pop('EP_SOURCE_REVISION', None)
        if pip_root:
            env['PYTHONPATH'] = str(pip_root.resolve())
        install = subprocess.run([str(prefix/'bin/python'), '-m', 'pip', '--isolated',
                    'install', '--no-index', '--disable-pip-version-check', str(wheel)],
                    env=env, text=True, capture_output=True)
        (output/'install.log').write_text(install.stdout + install.stderr)
        assert install.returncode == 0, install.stdout + install.stderr
        env.pop('PYTHONPATH', None)
        cwd = output/'unrelated-workdir'
        cwd.mkdir()
        executable = prefix/'bin/robot-perf-monitor'
        help_run = subprocess.run([str(executable), '--help'], cwd=cwd, env=env,
                                   text=True, capture_output=True)
        assert help_run.returncode == 0 and '--pid' in help_run.stdout
        try:
            first = spawn('rk_busy_fixture', owned)
            second = spawn('rk_idle_fixture', owned)
            command = [str(executable), '--seconds', '2', '--interval', '.1',
                       '--discovery-interval', '.2', '--active-cpu-percent', '0',
                       '--pid', str(first.pid), '--pid', str(second.pid), '--output', 'capture']
            run = subprocess.run(command, cwd=cwd, env=env, text=True,
                                 capture_output=True, timeout=30)
            (output/'monitor.log').write_text(run.stdout + run.stderr)
            assert run.returncode == 0, run.stdout + run.stderr
            assert first.poll() is None and second.poll() is None
            captured = cwd/'capture'
            summary = json.loads((captured/'monitor-summary.json').read_text())
            status = json.loads((captured/'monitor-status.json').read_text())
            environment = json.loads((captured/'environment.json').read_text())
            assert summary['status'] == status['status'] == 'complete'
            entities = summary['resources']['registered_entities'] or {}
            process_pids = {item['pid'] for item in entities.values() if item['kind']=='process'}
            assert process_pids == {first.pid, second.pid}
            threads = [item for item in entities.values() if item['kind']=='thread' and item['pid']==first.pid]
            assert {'rk_thread_a','rk_thread_b'} <= {item['comm'] for item in threads}
            assert summary['quality']['peak_registered_targets'] == 2
            with zipfile.ZipFile(wheel) as archive:
                names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
                assert len(names) == 1
                expected_version = Parser().parsestr(archive.read(names[0]).decode())['Version']
            assert environment['source']['package_version'] == expected_version
            assert environment['source']['git_revision'] is None
            with zipfile.ZipFile(wheel) as archive:
                for name, digest in environment['source']['sha256'].items():
                    assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
            for profile in ('light', 'full'):
                profile_run = subprocess.run([str(executable), '--profile', profile,
                    '--seconds', '2', '--active-cpu-percent', '0',
                    '--pid', str(first.pid), '--pid', str(second.pid),
                    '--output', 'capture-' + profile], cwd=cwd, env=env,
                    text=True, capture_output=True, timeout=30)
                (output/('profile-' + profile + '.log')).write_text(profile_run.stdout + profile_run.stderr)
                assert profile_run.returncode == 0, profile_run.stdout + profile_run.stderr
                profile_root = cwd/('capture-' + profile)
                profile_config = json.loads((profile_root/'monitor-config.json').read_text())
                profile_summary = json.loads((profile_root/'monitor-summary.json').read_text())
                enabled = profile == 'full'
                assert profile_config['resource_options']['collect_threads'] == enabled
                assert ('thread' in profile_summary['resources']['source_coverage']) == enabled
                assert profile_config['resource_options']['max_cycle_fraction'] is None
                assert profile_config['resource_options']['max_observer_cpu_percent_one_core'] is None
                assert profile_summary['quality']['peak_registered_targets'] == 2
                assert first.poll() is None and second.poll() is None
            (output/'verification.json').write_text(json.dumps({
                'status': 'passed', 'installed_package_version': environment['source']['package_version'],
                'selected_process_count': len(process_pids), 'relative_output_created_in_cwd': True,
                'dynamic_threads_observed': True,
                'source_pythonpath_removed': True, 'wheel_hashes_match': True,
                'external_targets_remain_alive': True,
                'installed_light_full_coverage_verified': True}, indent=2) + '\n')
            print('PASS offline wheel installation, console entry from arbitrary cwd, two explicit PIDs, relative output, package provenance and external target survival')
        finally:
            cleanup(owned)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--pip-root', type=Path, help='Optional offline pip module/zip for test bootstrap')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('This integration check requires Linux')
    wheel = args.wheel.resolve()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        verify(wheel, args.output.resolve(), args.pip_root)
    else:
        with tempfile.TemporaryDirectory(prefix='perfkit-install-check-') as directory:
            verify(wheel, Path(directory), args.pip_root)


if __name__ == '__main__':
    main()
