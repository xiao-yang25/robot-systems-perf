"""Installed-wheel machine intake from an unrelated cwd; controlled cancellation."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import venv
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.comparison_common import launch
from tests.process_helpers import OwnedProcesses


def verify(wheel, output):
    with tempfile.TemporaryDirectory(prefix='intake-installed-') as directory:
        root = Path(directory)
        prefix = root / 'venv'
        venv.EnvBuilder(with_pip=False).create(prefix)
        pip_spec = importlib.util.find_spec('pip')
        if pip_spec is None:
            raise RuntimeError('offline wheel check requires installed pip')
        env = dict(os.environ)
        env.pop('EP_SOURCE_REVISION', None)
        env['PYTHONPATH'] = str(Path(pip_spec.origin).parent.parent)
        installed = subprocess.run([str(prefix / 'bin/python'), '-m', 'pip', '--isolated',
            'install', '--no-index', '--disable-pip-version-check', str(wheel.resolve())],
            env=env, capture_output=True, text=True, timeout=30)
        (output / 'install.log').write_text(installed.stdout + installed.stderr)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        env.pop('PYTHONPATH', None)
        cwd = root / 'unrelated'
        cwd.mkdir()
        executable = prefix / 'bin/robot-perf-intake'
        marker = cwd / 'external-command-invoked'
        fake_bin = root / 'tools'
        fake_bin.mkdir()
        for name in ('nvpmodel', 'git', 'ros2', 'cyclictest', 'nsys', 'tegrastats'):
            tool = fake_bin / name
            tool.write_text('#!' + str(prefix / 'bin/python') + '\nfrom pathlib import Path\n'
                            + 'Path(' + repr(str(marker)) + ").write_text('unexpected invocation')\n")
            tool.chmod(0o755)
        env['PATH'] = str(fake_bin) + os.pathsep + env['PATH']
        def command(args, *, fs_root=None, guard_thermal=False):
            argv = [str(executable), *args]
            if fs_root is not None or guard_thermal:
                # Only the test supplies fs_root; --view does not change physical visibility.
                code = """import sys
from pathlib import Path
from perfkit import intake
from perfkit.platform_probe import _Probe
fixture=Path(sys.argv[1]) if sys.argv[1] else None
guard=sys.argv[2]=='guard'
original=intake.run_intake
def controlled(output, **kwargs):
 if fixture is not None: kwargs['fs_root']=fixture
 return original(output, **kwargs)
intake.run_intake=controlled
if guard:
 read,children=_Probe.read,_Probe.children
 def checked_read(probe,source,*args):
  if source.startswith('/sys/class/thermal') or Path(source).name.startswith('temp'): raise AssertionError('thermal read attempted')
  return read(probe,source,*args)
 def checked_children(probe,source):
  if source.startswith('/sys/class/thermal'): raise AssertionError('thermal discovery attempted')
  return children(probe,source)
 _Probe.read,_Probe.children=checked_read,checked_children
sys.argv=['robot-perf-intake',*sys.argv[3:]]
raise SystemExit(intake.main())
"""
                argv = [str(prefix / 'bin/python'), '-c', code,
                        str(fs_root) if fs_root is not None else '',
                        'guard' if guard_thermal else '', *args]
            result = subprocess.run(argv, cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=15)
            return result
        help_result = command(['--help'])
        assert help_result.returncode == 0 and '--require-capability' in help_result.stdout
        run = command(['--output', 'capture', '--machine-id', 'integration-demo', '--view', 'container'])
        assert run.returncode == 0, run.stdout + run.stderr
        capture = cwd / 'capture'
        status = json.loads((capture / 'intake-status.json').read_text())
        profile = json.loads((capture / 'machine-profile.json').read_text())
        caps = json.loads((capture / 'capabilities.json').read_text())
        assert status['status'] == 'complete'
        assert profile['source']['package_version'] == '0.5.1'
        assert profile['source']['git_revision'] is None
        assert profile['source']['sha256']['perfkit/intake.py']
        with zipfile.ZipFile(wheel) as archive:
            for name, digest in profile['source']['sha256'].items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
        assert profile['view']['declared'] == 'container'
        assert profile['observed_platform']['kernel_command_line'] is None
        assert caps['tools']['ros2']['found']
        assert caps['tools']['ros2']['version_options']['status'] == 'not_evaluated'
        assert caps['tools']['ros2']['sampling']['status'] == 'not_evaluated'
        assert not marker.exists(), 'intake unexpectedly launched an external command'
        before = {path.name: path.read_bytes() for path in capture.iterdir()}
        overwrite = command(['--output', 'capture'])
        assert overwrite.returncode != 0
        assert before == {path.name: path.read_bytes() for path in capture.iterdir()}
        # Explicit negative fixture works on physical Jetson too; declaration is irrelevant.
        nonjetson = root / 'nonjetson-fixture'
        nonjetson.mkdir()
        failed = command(['--output', 'required-failure', '--require-jetson', '--view', 'container'],
                         fs_root=nonjetson)
        assert failed.returncode != 0
        assert json.loads((cwd / 'required-failure/intake-status.json').read_text())['status'] == 'failed'
        assert (cwd / 'required-failure/MACHINE_REPORT.md').exists()
        failed_profile = json.loads((cwd / 'required-failure/machine-profile.json').read_text())
        assert not failed_profile['observed_platform']['checks']['jetson_detected']
        # Explicit positive fixture uses the same installed main, not a mocked detection result.
        jetson = root / 'jetson-fixture'
        for name, text in (('proc/device-tree/model', 'NVIDIA Jetson AGX Thor\0'),
                           ('etc/nv_tegra_release', '# R38 controlled fixture'),
                           ('sys/class/thermal/thermal_zone0/temp', '42000')):
            path = jetson / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        positive = command(['--output', 'fixture-jetson', '--require-jetson', '--view', 'container',
                            '--skip-temperature'], fs_root=jetson, guard_thermal=True)
        assert positive.returncode == 0, positive.stdout + positive.stderr
        positive_profile = json.loads((cwd / 'fixture-jetson/machine-profile.json').read_text())
        assert positive_profile['observed_platform']['checks']['jetson_detected']
        assert positive_profile['observed_platform']['jetson_family'] == 'thor'
        skipped = command(['--output', 'skip-temperature', '--skip-temperature'], guard_thermal=True)
        assert skipped.returncode == 0, skipped.stdout + skipped.stderr
        thermal = json.loads((cwd / 'skip-temperature/capabilities.json').read_text())['interfaces']['thermal']
        assert thermal['status'] == 'skipped' and thermal['available'] is None
        conflict = command(['--output', 'conflict', '--skip-temperature', '--require-capability', 'thermal'],
                           guard_thermal=True)
        assert conflict.returncode != 0 and not (cwd / 'conflict').exists()
        (output / 'controlled-runs.log').write_text(positive.stdout + positive.stderr + failed.stdout
            + failed.stderr + skipped.stdout + skipped.stderr + conflict.stdout + conflict.stderr)
        # Exercise actual installed main's SIGTERM handler after its status exists.
        owner = OwnedProcesses()
        cancellation = cwd / 'cancellation'
        ready = cwd / 'cancel-ready'
        code = """import sys,time
from pathlib import Path
from perfkit import intake
ready=Path(sys.argv[2])
def pause(root):
 ready.write_text('ready')
 time.sleep(30)
 raise AssertionError('cancellation was not delivered')
intake.hardware_facts=pause
sys.argv=['robot-perf-intake','--output',sys.argv[1]]
raise SystemExit(intake.main())
"""
        log = (output / 'cancel-child.log').open('x')
        try:
            process = launch(owner, [str(prefix / 'bin/python'), '-c', code,
                                    str(cancellation), str(ready)], cwd=cwd, env=env,
                             stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert process.poll() is None, 'controlled cancellation child exited too early'
                assert time.monotonic() < deadline, 'controlled cancellation child did not initialize'
                time.sleep(.02)
            assert owner.handle(process).send_signal(signal.SIGTERM)
            assert process.wait(timeout=10) == 130
            assert not Path('/proc', str(process.pid)).exists()
            cancelled = json.loads((cancellation / 'intake-status.json').read_text())
            assert cancelled['status'] == 'interrupted'
        finally:
            try:
                owner.cleanup()
            finally:
                log.close()
        assert not marker.exists()
        for name, source in (('capture', capture), ('required-failure', cwd / 'required-failure'),
                             ('cancellation', cancellation), ('fixture-jetson', cwd / 'fixture-jetson'),
                             ('skip-temperature', cwd / 'skip-temperature')):
            shutil.copytree(source, output / name)
        evidence = {'installed_version': profile['source']['package_version'],
                    'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    'installed_module_hashes_match_wheel': True,
                    'unrelated_cwd': True, 'no_external_commands': True,
                    'no_overwrite': True, 'require_jetson_failure': True,
                    'jetson_requirement_uses_explicit_fixtures': True,
                    'skip_temperature_read_sentinel_passed': True,
                    'skipped_required_conflict_rejected': True,
                    'sigterm_status': cancelled['status'], 'sigterm_exitcode': 130,
                    'owned_child_reaped': True, 'no_jetson_performance_claim': True}
        (output / 'verification.json').write_text(json.dumps(evidence, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    verify(args.wheel, args.output)
    print('Installed intake checks passed')


if __name__ == '__main__':
    main()
