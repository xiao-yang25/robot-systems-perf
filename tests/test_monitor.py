import json
import os
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit import monitor as m


class IdleSampler:
    instances = []
    def __init__(self, path, interval, window_source):
        self.path, self.error, self.closed = path, None, False
        self.instances.append(self)
    def set_window(self, *args):
        pass
    def __enter__(self):
        self.path.write_text('')
        return self
    def __exit__(self, *args):
        self.closed = True
    def register(self, *args, **kwargs):
        raise AssertionError('empty inventory must not register')


class FakeClock:
    def __init__(self):
        self.now = 1_000_000_000
    def monotonic_ns(self):
        return self.now
    def sleep(self, seconds):
        self.now += round(seconds * 1e9)


def resource_summary(**updates):
    result = {'registered_entities': {}, 'coverage': {'samples': 2},
              'source_coverage': {name: {'samples': 2} for name in ('system', 'process', 'thread')},
              'observer': {'cpu_percent_one_core': 5},
              'collection_cost': {'over_period_cycles': 0},
              'overhead_budget': {'status': 'not_configured'},
              'thread_scope': {'enabled': True}, 'jetson_telemetry': {'enabled': False}}
    result.update(updates)
    return result


class MonitorTests(unittest.TestCase):
    def test_resource_options_validate_without_expanding_default_config(self):
        self.assertEqual(m.validate_config({})['resource_options'], {})
        options = {'collect_threads': False, 'thread_names': ['worker'], 'thread_ids': [42],
                   'system_sampling_seconds': 2, 'jetson_telemetry': True}
        validated = m.validate_config({'resource_options': options})
        self.assertEqual(validated['resource_options'], options)
        validated['resource_options']['thread_ids'].append(43)
        self.assertEqual(options['thread_ids'], [42])
        for options in (None, [], {'unknown': True}, {'collect_threads': 1},
                        {'thread_names': ['[']}, {'thread_ids': [0]},
                        {'process_sampling_seconds': float('nan')}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                m.validate_config({'resource_options': options})

    def test_cli_resource_options_merge_and_pass_through(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'resource_options': {'thread_names': ['existing'], 'thread_ids': [11]}}))
            argv = ['monitor', '--config', str(config), '--output', str(root / 'output'),
                    '--system-interval', '2', '--process-interval', '3', '--thread-interval', '4',
                    '--no-threads', '--thread-name', 'worker', '--tid', '42', '--skip-temperatures',
                    '--jetson-telemetry', '--jetson-interval', '5', '--max-cycle-fraction', '.2',
                    '--max-observer-cpu-percent', '10']
            with patch.object(m.platform, 'system', return_value='Linux'), \
                 patch('sys.argv', argv), patch.object(m, 'run_monitor') as run, \
                 patch.object(m.signal, 'signal'):
                m.main()
            options = run.call_args[0][0]['resource_options']
            self.assertEqual(options, {'system_sampling_seconds': 2, 'process_sampling_seconds': 3,
                'thread_sampling_seconds': 4, 'collect_threads': False,
                'thread_names': ['existing', 'worker'], 'thread_ids': [11, 42],
                'skip_temperatures': True, 'jetson_telemetry': True, 'jetson_sampling_seconds': 5,
                'max_cycle_fraction': .2, 'max_observer_cpu_percent_one_core': 10})

    def test_discovery_absolute_deadlines_cost_intervals_and_no_catch_up(self):
        for cost, expected_starts, overruns in (
                (25_000_000, [i * 100_000_000 for i in range(10)], 0),
                (150_000_000, [i * 200_000_000 for i in range(5)], 5)):
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as directory:
                clock, starts = FakeClock(), []
                def scan(*args, **kwargs):
                    starts.append(clock.now - 1_000_000_000)
                    clock.now += cost
                    return {'processes': [], 'scan': {'skipped_count': 0}}
                with patch.object(m, 'time', clock), patch.object(m, 'ResourceSampler', IdleSampler), \
                     patch.object(m, 'collect_profile', return_value={}), patch.object(m, '_source_record', return_value={}), \
                     patch.object(m, 'scan_processes', side_effect=scan), \
                     patch.object(m, 'summarize_resources', return_value=resource_summary()):
                    summary = m.run_monitor({'duration_seconds': 1, 'discovery_interval_seconds': .1},
                                            Path(directory) / 'capture')
                self.assertEqual(starts, expected_starts)
                discovery = summary['quality']['discovery']
                self.assertEqual(discovery['duration_ns']['mean_ns'], cost)
                self.assertEqual(discovery['duration_ns']['max_ns'], cost)
                self.assertEqual(discovery['over_period_scans'], overruns)
                self.assertEqual(discovery['skipped_deadlines'], overruns)
                self.assertEqual(discovery['scan_start_interval_ns']['mean_ns'], expected_starts[1])

    def test_quality_budget_source_coverage_disabled_threads_and_report(self):
        class OptionsSampler(IdleSampler):
            def __init__(self, path, interval, window_source, options):
                super().__init__(path, interval, window_source)
                self.options = options
        for budget_status in ('exceeded', 'not_evaluated', 'within_observed_scope'):
            with self.subTest(status=budget_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'capture'
                options = {'collect_threads': False, 'max_cycle_fraction': .2}
                resources = resource_summary(source_coverage={'system': {'samples': 2},
                    'process': {'samples': 1}, 'thread': {'samples': 0}},
                    thread_scope={'enabled': False}, collection_cost={'over_period_cycles': 1},
                    overhead_budget={'status': budget_status})
                with patch.object(m, 'time', FakeClock()), patch.object(m, 'ResourceSampler', OptionsSampler), \
                     patch.object(m, 'collect_profile', return_value={}), patch.object(m, '_source_record', return_value={}), \
                     patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {}}), \
                     patch.object(m, 'summarize_resources', return_value=resources):
                    summary = m.run_monitor({'duration_seconds': 1, 'resource_options': options}, root)
                self.assertEqual(OptionsSampler.instances[-1].options, options)
                warnings = summary['quality']['warnings']
                self.assertTrue(any('process samples' in warning for warning in warnings))
                self.assertFalse(any('thread samples' in warning for warning in warnings))
                self.assertTrue(any('cycles exceeded' in warning for warning in warnings))
                self.assertEqual(any('overhead budget' in warning for warning in warnings),
                                 budget_status in ('exceeded', 'not_evaluated'))
                report = (root / 'MONITOR_REPORT.md').read_text()
                for name in ('source_coverage', 'observer', 'collection_cost', 'overhead_budget',
                             'thread_scope', 'jetson_telemetry'):
                    self.assertIn(name, report)

    def test_unconfigured_budget_is_not_warned(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'time', FakeClock()), patch.object(m, 'ResourceSampler', IdleSampler), \
             patch.object(m, 'collect_profile', return_value={}), patch.object(m, '_source_record', return_value={}), \
             patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {}}), \
             patch.object(m, 'summarize_resources', return_value=resource_summary(overhead_budget={'status': 'not_evaluated'})):
            summary = m.run_monitor({'duration_seconds': 1}, Path(directory) / 'capture')
        self.assertFalse(any('overhead budget' in warning for warning in summary['quality']['warnings']))

    def test_runner_validates_and_passes_explicit_resource_options(self):
        from perfkit import runner
        config = json.loads((Path(__file__).resolve().parents[1] / 'configs/smoke.json').read_text())
        config['resource_options'] = {'collect_threads': False, 'skip_temperatures': True}
        self.assertIs(runner.validate(config), config)
        for options in (None, {'unknown': 1}, {'thread_ids': [True]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                runner.validate(dict(config, resource_options=options))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            for binary in ('ros_bench', 'periodic_bench'):
                (root / 'build' / binary).touch()
            from unittest.mock import MagicMock
            context = MagicMock()
            context.error = None
            with patch('perfkit.resources.ResourceSampler', return_value=context) as constructor, \
                 patch('perfkit.resources.summarize_resources', return_value={}), \
                 patch.object(runner, 'environment', return_value={}), \
                 patch.object(runner, 'run_one', return_value={'measurement_window': {'start_ns': 1, 'end_ns': 2}}), \
                 patch.object(runner, 'measurement_quality', return_value={}), \
                 patch('perfkit.analysis.write_report'):
                runner.run_experiment(config, root / 'capture', root)
            self.assertEqual(constructor.call_args.kwargs, {'options': config['resource_options']})

    def test_installed_source_records_version_and_hashes_without_parent_git_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root/'perfkit'
            package.mkdir()
            source = package/'monitor.py'
            source.write_text('fixture source\n')
            with patch.object(m, '__file__', str(source)), \
                 patch.object(m.metadata, 'version', return_value='0.3.0'), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(m.subprocess, 'run') as git:
                record = m._source_record()
            git.assert_not_called()
            self.assertIsNone(record['git_revision'])
            self.assertEqual(record['package_version'], '0.3.0')
            import hashlib
            self.assertEqual(record['sha256']['perfkit/monitor.py'], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_scope_defaults_to_current_uid_and_cli_patterns_are_validated(self):
        self.assertEqual(m.validate_config({})['uids'], [os.getuid()])
        self.assertIsNone(m.validate_config({'uids': None})['uids'])
        for config in ({'duration_seconds': float('nan')}, {'duration_seconds': 0},
                       {'max_targets': True}, {'max_targets': 1.5}, {'uids': [True]},
                       {'include_names': ['[']}, {'include_names': 'python'},
                       {'unknown': 1}, {'require_jetson': 'yes'}, {'format_version': True},
                       {'active_cpu_percent': 0}):
            with self.subTest(config=config), self.assertRaises((ValueError, TypeError)):
                m.validate_config(config)
        self.assertEqual(m.validate_config({'active_cpu_percent': 0, 'include_names': ['worker']})['active_cpu_percent'], 0)

    def test_empty_capture_is_review_required_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'capture'
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {'skipped_count': 0}}):
                summary = m.run_monitor({'duration_seconds': 1, 'discovery_interval_seconds': .1}, output)
            self.assertEqual(summary['status'], 'complete')
            self.assertEqual(summary['quality']['status'], 'review_required')
            self.assertEqual(summary['quality']['registrations'], 0)
            self.assertTrue(IdleSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'complete')
            with self.assertRaises(FileExistsError):
                m.run_monitor({}, output)

    def test_discovery_failure_closes_sampler_and_records_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', side_effect=PermissionError('proc root denied')):
                with self.assertRaises(PermissionError):
                    m.run_monitor({}, output)
            self.assertTrue(IdleSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'failed')
            self.assertFalse((output/'monitor-summary.json').exists())

    def test_interrupt_keeps_partial_report_and_closes_sampler(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', side_effect=KeyboardInterrupt('fixture interrupt')):
                with self.assertRaises(KeyboardInterrupt):
                    m.run_monitor({}, output)
            self.assertTrue(IdleSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'interrupted')
            self.assertEqual(json.loads((output/'monitor-summary.json').read_text())['status'], 'interrupted')

    def test_require_jetson_fails_before_observing_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'collect_profile', return_value={'checks': {'jetson_detected': False}}), \
                 patch.object(m, 'scan_processes') as scan:
                with self.assertRaisesRegex(RuntimeError, 'Jetson'):
                    m.run_monitor({'require_jetson': True}, output)
            scan.assert_not_called()
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'failed')

    def test_late_signal_during_summary_or_report_keeps_consistent_interrupted_evidence(self):
        for phase in ('summarize_resources', 'write_monitor_report'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'capture'
                original = getattr(m, phase)
                sent = []
                def late_signal(*args, **kwargs):
                    value = original(*args, **kwargs)
                    if not sent:
                        sent.append(True)
                        os.kill(os.getpid(), signal.SIGINT)
                    return value
                with patch.object(m, 'ResourceSampler', IdleSampler), \
                     patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {'skipped_count': 0}}), \
                     patch.object(m, phase, side_effect=late_signal):
                    with self.assertRaises(KeyboardInterrupt):
                        m.run_monitor({'duration_seconds': 1}, output)
                self.assertEqual(json.loads((output/'monitor-summary.json').read_text())['status'], 'interrupted')
                self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'interrupted')
                self.assertIn('采集状态：interrupted', (output/'MONITOR_REPORT.md').read_text())

    def test_sampler_failure_is_non_success_and_closes_owned_sampler(self):
        class FailedSampler(IdleSampler):
            def __enter__(self):
                super().__enter__()
                self.error = 'fixture sampler failure'
                return self
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            with patch.object(m, 'ResourceSampler', FailedSampler):
                with self.assertRaisesRegex(RuntimeError, 'sampling failed'):
                    m.run_monitor({}, output)
            self.assertTrue(FailedSampler.instances[-1].closed)
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'failed')
            self.assertFalse((output/'monitor-summary.json').exists())

    def test_signal_during_final_status_write_reconciles_all_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'capture'
            original = m._json
            writes = []
            def final_signal(path, value):
                if path.name == 'monitor-status.json':
                    writes.append(True)
                    if len(writes) == 2:
                        os.kill(os.getpid(), signal.SIGINT)
                return original(path, value)
            with patch.object(m, 'ResourceSampler', IdleSampler), \
                 patch.object(m, 'scan_processes', return_value={'processes': [], 'scan': {'skipped_count': 0}}), \
                 patch.object(m, '_json', side_effect=final_signal):
                with self.assertRaises(KeyboardInterrupt):
                    m.run_monitor({'duration_seconds': 1}, output)
            self.assertEqual(json.loads((output/'monitor-summary.json').read_text())['status'], 'interrupted')
            self.assertEqual(json.loads((output/'monitor-status.json').read_text())['status'], 'interrupted')
            self.assertIn('采集状态：interrupted', (output/'MONITOR_REPORT.md').read_text())


if __name__ == '__main__':
    unittest.main()
