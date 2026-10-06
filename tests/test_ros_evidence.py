import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from perfkit import ros_evidence as r
from perfkit.monitor import _observation_context
from perfkit.workload import validate_workload


BOOT = '00000000-0000-0000-0000-000000000001'
REF = 'pid=123:registration=1:start=50'
CONTEXT = {'boot_id': BOOT, 'pid_namespace': 'pid:[1]', 'clock': 'linux_monotonic'}


def monitor_fixture(path):
    path.mkdir()
    workload = validate_workload({'format_version': 1, 'workload_id': 'demo', 'ros_domain_id': 37,
        'functions': [{'id': name, 'process_selector': {'pids': [123]}, 'ros_nodes': ['/demo/' + name]}
                      for name in ('a', 'b')]})
    roles = [{'function_id': name, 'ros_nodes': ['/demo/' + name], 'resource_refs': [REF],
              'reference_observations': {REF: {'first_observed_ns': 110, 'last_observed_ns': 190}},
              'last_scan': {'status': 'candidate'}} for name in ('a', 'b')]
    values = {'monitor-status.json': {'status': 'complete', 'window_start_ns': 100, 'window_end_ns': 200},
        'environment.json': {'observation_context': CONTEXT},
        'workload-profile.json': {'workload': workload, 'workload_sha256': hashlib.sha256(json.dumps(
            workload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()},
        'monitor-summary.json': {'workload': {'workload_id': 'demo', 'ros_domain_id': 37, 'functions': roles},
                                'resources': {'registered_entities': {REF: {'kind': 'process', 'pid': 123,
                                    'starttime_ticks': 50, 'cpu_percent_one_core': 25, 'rss_peak_bytes': 4096}}}}}
    for name, value in values.items():
        (path / name).write_text(json.dumps(value))
    return r.load_monitor(path)


def trace_fixture():
    return {'format_version': 1, 'kind': 'ros2_node_init_metadata',
        'source': {'adapter': 'ros2_tracing_normalized_v1', 'tool_version': 'fixture-export-1', 'raw_sha256': '0' * 64},
        'context': dict(CONTEXT, ros_domain_id=37),
        'events': [{'event': 'ros2:rcl_node_init', 'monotonic_ns': 150, 'pid': 123,
                    'starttime_ticks': 50, 'node_handle': i, 'node_name': name, 'namespace': '/demo'}
                   for i, name in enumerate(('a', 'b'), 1)]}


def graph_fixture():
    return {'status': 'observed', 'nodes': [{'full_name': '/demo/' + name} for name in ('a', 'b')],
            'reason': None, 'query_window': {'start_ns': 300, 'end_ns': 310}}


class RosEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.monitor = monitor_fixture(self.root / 'monitor')

    def test_graph_presence_does_not_upgrade_process_declaration(self):
        result = r.associate(self.monitor, graph_fixture())
        row = result['functions'][0]['nodes'][0]
        self.assertEqual(row['graph']['status'], 'present')
        self.assertFalse(row['graph']['local_pid_evidence'])
        self.assertEqual(row['process_relation']['status'], 'unresolved')
        self.assertEqual(row['process_relation']['resource_refs'], [])

    def test_duplicate_and_remote_graph_nodes_never_choose_first_pid(self):
        graph = graph_fixture()
        graph['nodes'].extend([{'full_name': '/demo/a'}, {'full_name': '/remote/unknown'}])
        result = r.associate(self.monitor, graph)
        self.assertEqual(result['functions'][0]['nodes'][0]['graph']['status'], 'ambiguous')
        self.assertNotIn('/remote/unknown', json.dumps(result['functions']))

    def test_shared_trace_initializations_reference_one_table_without_metrics(self):
        result = r.associate(self.monitor, graph_fixture(), r.validate_trace(trace_fixture()))
        for role in result['functions']:
            relation = role['nodes'][0]['process_relation']
            self.assertEqual(relation['status'], 'imported_trace_identity_consistent')
            self.assertEqual(relation['resource_refs'], [REF])
            self.assertEqual(relation['provenance'], 'external_normalized_export_not_authenticated')
        self.assertNotIn('cpu_percent_one_core', json.dumps(result))
        self.assertNotIn('rss_peak_bytes', json.dumps(result))
        self.assertEqual(result['business_acceptance'], 'not_evaluated')

    def test_pid_reuse_window_or_scope_mismatch_does_not_bind(self):
        for field, value in [('pid', 124), ('starttime_ticks', 51), ('monotonic_ns', 109), ('monotonic_ns', 191)]:
            with self.subTest(field=field, value=value):
                trace = trace_fixture()
                trace['events'][0][field] = value
                row = r.associate(self.monitor, trace=trace)['functions'][0]['nodes'][0]
                self.assertEqual(row['process_relation']['status'], 'unresolved')
                self.assertEqual(row['process_relation']['resource_refs'], [])

    def test_namespace_boot_clock_and_domain_must_match_recorded_context(self):
        for field, value in [('boot_id', BOOT[:-1] + '2'), ('pid_namespace', 'pid:[2]'),
                             ('clock', 'other'), ('ros_domain_id', 38)]:
            with self.subTest(field=field):
                trace = trace_fixture()
                trace['context'][field] = value
                self.assertEqual(r.associate(self.monitor, trace=trace)['functions'][0]['nodes'][0]
                                 ['process_relation']['status'], 'unresolved')
        self.monitor['environment'].pop('observation_context')
        row = r.associate(self.monitor, trace=trace_fixture())['functions'][0]['nodes'][0]
        self.assertIn('monitor_boot_id_unrecorded', row['process_relation']['reasons'])

    def test_empty_trace_and_missing_resource_record_remain_unknown(self):
        trace = trace_fixture()
        trace['events'] = []
        relation = r.associate(self.monitor, trace=trace)['functions'][0]['nodes'][0]['process_relation']
        self.assertIn('no_node_init_metadata', relation['reasons'])
        self.monitor['entities'] = {}
        relation = r.associate(self.monitor, trace=trace_fixture())['functions'][0]['nodes'][0]['process_relation']
        self.assertEqual(relation['status'], 'unresolved')
        self.assertIn('no_matching_process_resource_record', relation['reasons'])

    def test_multiple_matching_registrations_are_ambiguous(self):
        extra = 'pid=123:registration=2:start=50'
        role = self.monitor['summary']['workload']['functions'][0]
        role['reference_observations'][extra] = dict(role['reference_observations'][REF])
        self.monitor['entities'][extra] = dict(self.monitor['entities'][REF])
        row = r.associate(self.monitor, trace=trace_fixture())['functions'][0]['nodes'][0]
        self.assertEqual(row['process_relation']['status'], 'ambiguous')

    def test_trace_format_and_context_rejection(self):
        cases = []
        for key, value in [('format_version', True), ('kind', 'arbitrary_ctf'), ('events', None)]:
            trace = trace_fixture(); trace[key] = value; cases.append(trace)
        trace = trace_fixture(); trace['context']['clock'] = 'unix_epoch'; cases.append(trace)
        trace = trace_fixture(); trace['events'].append(copy.deepcopy(trace['events'][0])); cases.append(trace)
        trace = trace_fixture(); trace['events'][0]['event'] = 'callback_start'; cases.append(trace)
        trace = trace_fixture(); trace['events'][0]['pid'] = True; cases.append(trace)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                r.validate_trace(case)

    def test_duplicate_json_fields_and_nonfinite_evidence_rejected(self):
        for text in ('{"events":[],"events":[]}', '{"timestamp":NaN}'):
            path = self.root/'invalid.json'; path.write_text(text)
            with self.assertRaises(ValueError): r._read(path)

    def test_source_status_is_authoritative_and_workload_digest_is_checked(self):
        status = self.root / 'monitor/monitor-status.json'
        status.write_text(json.dumps({'status': 'interrupted', 'window_start_ns': 100, 'window_end_ns': 200}))
        self.assertEqual(r.load_monitor(status.parent)['status']['status'], 'interrupted')
        source = status.parent / 'workload-profile.json'
        value = json.loads(source.read_text()); value['workload']['workload_id'] = 'changed'
        source.write_text(json.dumps(value))
        with self.assertRaises(ValueError): r.load_monitor(status.parent)

    def test_fifo_and_oversize_inputs_are_rejected_without_output(self):
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        with self.assertRaises(ValueError): r._read(fifo)
        path = self.root / 'large'; path.write_bytes(b' ' * 65)
        with self.assertRaises(ValueError): r._read(path, 64)

    def test_invalid_options_do_not_start_query_or_create_output(self):
        for kwargs in ({}, {'graph': True}, {'graph': True, 'domain_id': 38},
                       {'graph': True, 'domain_id': 37, 'wait_seconds': float('nan')},
                       {'graph': True, 'domain_id': 37, 'component_managers': ['bad']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                r.run_ros_evidence(self.root/'monitor', self.root/'output', **kwargs)
            self.assertFalse((self.root/'output').exists())

    def test_nested_or_symlinked_output_cannot_mutate_source_directory(self):
        trace = self.root/'trace.json'; trace.write_text(json.dumps(trace_fixture()))
        alias = self.root/'alias'; alias.symlink_to(self.root/'monitor', target_is_directory=True)
        for output in (self.root/'monitor/new-output', alias/'new-output'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                r.run_ros_evidence(self.root/'monitor', output, trace_metadata=trace)
            self.assertFalse(output.exists())

    def test_trace_only_output_hashes_and_no_overwrite(self):
        trace = self.root/'trace.json'; trace.write_text(json.dumps(trace_fixture()))
        before = {p.name: p.read_bytes() for p in (self.root/'monitor').iterdir()}
        result = r.run_ros_evidence(self.root/'monitor', self.root/'output', trace_metadata=trace)
        self.assertEqual(result['trace_status'], 'imported')
        self.assertEqual((self.root/'output/trace-metadata.json').read_bytes(), trace.read_bytes())
        self.assertEqual(json.loads((self.root/'output/ros-status.json').read_text())['status'], 'complete')
        with self.assertRaises(FileExistsError):
            r.run_ros_evidence(self.root/'monitor', self.root/'output', trace_metadata=trace)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root/'monitor').iterdir()})

    def test_report_failure_keeps_failed_status_and_existing_evidence(self):
        trace = self.root/'trace.json'; trace.write_text(json.dumps(trace_fixture()))
        with patch.object(r, '_report', side_effect=OSError('controlled report failure')):
            with self.assertRaises(OSError):
                r.run_ros_evidence(self.root/'monitor', self.root/'output', trace_metadata=trace)
        status = json.loads((self.root/'output/ros-status.json').read_text())
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['error'], 'controlled report failure')
        self.assertTrue((self.root/'output/ros-relations.json').exists())

    def test_final_status_real_sigterm_retains_interrupted_state(self):
        trace = self.root/'trace.json'; trace.write_text(json.dumps(trace_fixture()))
        original = r._json; sent = []
        def write(path, value):
            original(path, value)
            if path.name == 'ros-status.json' and value['status'] == 'complete' and not sent:
                sent.append(True); os.kill(os.getpid(), signal.SIGTERM)
        with patch('sys.argv', ['ros', '--monitor-run', str(self.root/'monitor'), '--output', str(self.root/'output'),
                                '--trace-metadata', str(trace)]), patch.object(r, '_json', side_effect=write):
            self.assertEqual(r.main(), 130)
        self.assertEqual(json.loads((self.root/'output/ros-status.json').read_text())['status'], 'interrupted')

    def test_final_status_post_write_error_saves_failed_and_preserves_error(self):
        trace = self.root/'trace.json'; trace.write_text(json.dumps(trace_fixture()))
        original = r._json; fault = OSError('controlled final status failure'); sent = []
        def write(path, value):
            original(path, value)
            if path.name == 'ros-status.json' and value['status'] == 'complete' and not sent:
                sent.append(True)
                raise fault
        with patch.object(r, '_json', side_effect=write):
            with self.assertRaises(OSError) as caught:
                r.run_ros_evidence(self.root/'monitor', self.root/'output', trace_metadata=trace)
        self.assertIs(caught.exception, fault)
        status = json.loads((self.root/'output/ros-status.json').read_text())
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['error'], str(fault))
        self.assertEqual(status['error_type'], 'OSError')

    def test_primary_real_sigterm_survives_final_status_error_and_retry(self):
        trace = self.root/'trace.json'; trace.write_text(json.dumps(trace_fixture()))
        original = r._json; sent = []
        def write(path, value):
            if path.name == 'ros-status.json' and value['status'] == 'interrupted' and not sent:
                sent.append(True); raise OSError('controlled status cleanup failure')
            original(path, value)
        def report(*args):
            os.kill(os.getpid(), signal.SIGTERM)
        with patch('sys.argv', ['ros', '--monitor-run', str(self.root/'monitor'), '--output', str(self.root/'output'),
                                '--trace-metadata', str(trace)]), patch.object(r, '_report', side_effect=report), \
                patch.object(r, '_json', side_effect=write):
            self.assertEqual(r.main(), 130)
        status = json.loads((self.root/'output/ros-status.json').read_text())
        self.assertEqual(status['status'], 'interrupted')
        self.assertEqual(status['error_type'], 'KeyboardInterrupt')
        self.assertIn('SIGTERM', status['error'])
        self.assertEqual(status['cleanup_errors'][0]['error'], 'controlled status cleanup failure')

    def test_monitor_observation_context_missing_and_real_identity(self):
        result = _observation_context(self.root/'missing')
        self.assertIsNone(result['boot_id'])
        self.assertIn('boot_id', result['missing_reasons'])
        if Path('/proc/sys/kernel/random/boot_id').exists():
            result = _observation_context(Path('/proc'))
            self.assertTrue(r.UUID.fullmatch(result['boot_id']))
            self.assertEqual(result['pid_namespace'], os.readlink('/proc/self/ns/pid'))


if __name__ == '__main__': unittest.main()
