"""Real child-process regression: cancellation arriving during normal/failure cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CleanupTests(unittest.TestCase):
    def test_cleanup_phase_sigterm_reaps_owned_process_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'build').mkdir()
                marker = root / 'load-term'
                pidfile = root / 'load-pid'
                binary = root / 'build/periodic_bench'
                binary.write_text(f'''#!{sys.executable}
import csv,os,signal,sys,time
from pathlib import Path
if '--load' in sys.argv:
    def term(sig,frame): Path({str(marker)!r}).write_text('cleanup begun')
    signal.signal(signal.SIGTERM,term)
    Path({str(pidfile)!r}).write_text(str(os.getpid()))
    while True: time.sleep(0.01)
while not Path({str(pidfile)!r}).exists(): time.sleep(0.01)
if {fail!r}: raise SystemExit(2)
args=dict(zip(sys.argv[1::2],sys.argv[2::2]))
base=time.monotonic_ns()
with open(args['--output'],'w') as stream:
    w=csv.writer(stream);w.writerow(['seq','scheduled_ns','start_ns','finish_ns','cpu_ns','measured'])
    for i in range(int(args['--count'])):
        scheduled=base+i*int(args['--period-ns'])
        w.writerow([i,scheduled,scheduled,scheduled+100,0,1])
''')
                binary.chmod(0o755)
                (root / 'build/ros_bench').touch()
                config = json.loads((ROOT / 'configs/smoke.json').read_text())
                config.update(warmup_seconds=0, measurement_seconds=0.02, repetitions=1, sampling_mode='minimal')
                scenario = config['scenarios'][1]
                scenario.update(frequency_hz=100, cpu_interference_workers=1)
                config['scenarios'] = [scenario]
                cfg = root / 'config.json'; cfg.write_text(json.dumps(config))
                output = root / 'output'
                code = ('import json,sys; from pathlib import Path; '
                        'from perfkit.runner import install_signal_handler,run_experiment; '
                        'install_signal_handler(); run_experiment(json.loads(Path(sys.argv[1]).read_text()), '
                        'Path(sys.argv[2]),Path(sys.argv[3]))')
                child = None
                with (root / 'runner.log').open('w') as log:
                    proc = subprocess.Popen([sys.executable, '-c', code, str(cfg), str(output), str(root)],
                                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        until = time.monotonic() + 15
                        while not marker.exists() and time.monotonic() < until:
                            self.assertIsNone(proc.poll(), (root / 'runner.log').read_text())
                            time.sleep(0.02)
                        self.assertTrue(marker.exists(), 'fixture did not enter cleanup')
                        child = int(pidfile.read_text())
                        proc.send_signal(signal.SIGTERM)
                        self.assertNotEqual(proc.wait(timeout=20), 0)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(child, 0)
                        status = json.loads((output / 'run-status.json').read_text())
                        self.assertEqual(status['status'], 'failed')
                        self.assertEqual(status['error_type'], 'KeyboardInterrupt')
                    finally:
                        if proc.poll() is None:
                            proc.kill(); proc.wait()
                        if pidfile.exists():
                            try: os.kill(int(pidfile.read_text()), signal.SIGKILL)
                            except ProcessLookupError: pass


if __name__ == '__main__':
    unittest.main()
