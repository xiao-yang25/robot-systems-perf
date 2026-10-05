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
        def command(args):
            result = subprocess.run([str(executable), *args], cwd=cwd, env=env,
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
        assert profile['source']['package_version'] == '0.5.0'
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
        # Container fixture has no Jetson board/BSP; required observations fail.
        failed = command(['--output', 'required-failure', '--require-jetson'])
        assert failed.returncode != 0
        assert json.loads((cwd / 'required-failure/intake-status.json').read_text())['status'] == 'failed'
        assert (cwd / 'required-failure/MACHINE_REPORT.md').exists()
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
                             ('cancellation', cancellation)):
            shutil.copytree(source, output / name)
        evidence = {'installed_version': profile['source']['package_version'],
                    'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    'installed_module_hashes_match_wheel': True,
                    'unrelated_cwd': True, 'no_external_commands': True,
                    'no_overwrite': True, 'require_jetson_failure': True,
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
