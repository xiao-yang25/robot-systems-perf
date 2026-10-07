"""Advertised edges cannot certify delivery, identity or full discovery."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts import export_ros_topology as tool


def fixture():
    def node(name):
        return {'name': name, 'namespace': '/demo', 'full_name': '/demo/' + name}
    def endpoint(name, gid):
        qos = {k: {'name': 'UNKNOWN' if k == 'history' else 'RELIABLE', 'value': 0}
               for k in ('history', 'reliability', 'durability', 'liveliness')}
        qos.update(reported_depth=0, depth=None, depth_reason='RMW graph does not expose queue depth',
                   scope='reported graph', deadline_ns=0, lifespan_ns=0,
                   liveliness_lease_duration_ns=0, avoid_ros_namespace_conventions=False)
        return dict(node_name=name, node_namespace='/demo', full_name='/demo/' + name,
                    endpoint_gid=gid, topic_type='std_msgs/msg/String', qos=qos)
    return {'format_version': 1, 'kind': 'ros_graph_snapshot', 'status': 'observed',
            'reason': None, 'domain_id': 0, 'query_window': {'start_ns': 100, 'end_ns': 200},
            'source': {'adapter': 'rclpy', 'rmw': 'rmw_fastrtps_cpp',
                       'python_version': '3.10', 'ros_distro': 'humble'}, 'limitations': [],
            'nodes': [node('source'), node('sink')], 'components': [],
            'topics': [{'name': '/demo/data', 'types': ['std_msgs/msg/String'],
                        'publishers': [endpoint('source', '01')],
                        'subscriptions': [endpoint('sink', '02')]}]}


class TopologyTests(unittest.TestCase):
    def test_two_advertised_edges_not_one_certified_delivery(self):
        graph = fixture()
        result = tool.topology(graph, 'a' * 64)
        groups = {n['full_name']: n['id'] for n in result['node_groups']}
        self.assertEqual([(e['source'], e['target']) for e in result['links']],
                         [(groups['/demo/source'], 't0'), ('t0', groups['/demo/sink'])])
        self.assertEqual(result['endpoints'][0]['reported'], graph['topics'][0]['publishers'][0])
        self.assertIsNone(result['endpoints'][0]['reported']['qos']['depth'])
        self.assertEqual(result['business_acceptance'], 'not_evaluated')
        self.assertTrue(all(n['process_ownership'] == 'unresolved' for n in result['node_groups']))
        self.assertEqual(graph, fixture())

    def test_duplicate_names_keep_all_endpoint_rows_and_ambiguity(self):
        graph = fixture()
        graph['nodes'].append(copy.deepcopy(graph['nodes'][0]))
        graph['topics'][0]['publishers'].append(copy.deepcopy(graph['topics'][0]['publishers'][0]))
        graph['topics'][0]['publishers'][1]['endpoint_gid'] = '03'
        result = tool.topology(graph, 'a' * 64)
        n = next(n for n in result['node_groups'] if n['full_name'] == '/demo/source')
        self.assertEqual(n['observed_node_count'], 2)
        self.assertEqual(n['name_status'], 'ambiguous')
        self.assertEqual(len(result['endpoints']), 3)
        self.assertEqual({e['reported']['endpoint_gid'] for e in result['endpoints']}, {'01', '02', '03'})

    def test_endpoint_only_name_and_failed_read_do_not_become_complete(self):
        graph = fixture()
        graph['nodes'].pop()
        graph.update(status='failed', reason='partial subscription read')
        graph['topics'].append({'name': '/other', 'types': [], 'publishers': [], 'subscriptions': None})
        result = tool.topology(graph, 'a' * 64)
        self.assertEqual(result['observation_status'], 'failed')
        self.assertEqual(result['observation_reason'], graph['reason'])
        self.assertEqual(result['topics'][1]['endpoint_reads']['subscriptions'], 'unavailable')
        sink = next(n for n in result['node_groups'] if n['full_name'] == '/demo/sink')
        self.assertEqual(sink['name_status'], 'endpoint_only')
        self.assertEqual(sink['process_ownership'], 'unresolved')

    def test_empty_is_observation_not_no_workload(self):
        graph = fixture()
        graph.update(status='empty', nodes=[], topics=[])
        result = tool.topology(graph, 'a' * 64)
        self.assertEqual(result['observation_status'], 'empty')
        self.assertEqual(result['links'], [])
        self.assertIn('discovery', ' '.join(result['limitations']))

    def test_failed_missing_role_is_unavailable_and_dot_preserves_full_evidence(self):
        graph = fixture()
        graph.update(status='failed', reason='subscription query failed')
        del graph['topics'][0]['subscriptions']
        graph['components'] = [{'manager': '/demo/container', 'status': 'failed',
                                'reason': 'ListNodes unavailable', 'nodes': None}]
        result = tool.topology(graph, 'a' * 64)
        self.assertEqual(result['topics'][0]['endpoint_reads']['subscriptions'], 'unavailable')
        self.assertEqual(len(result['links']), 1)
        dot = tool.to_dot(result)
        metadata = json.loads(dot.splitlines()[0].removeprefix('// ros_topic_topology_json: '))
        self.assertEqual(metadata, result)
        self.assertIn('subscription query failed', dot)
        self.assertEqual(metadata['component_observations'], graph['components'])
        self.assertEqual(metadata['endpoints'][0]['reported']['qos'], graph['topics'][0]['publishers'][0]['qos'])

    def test_component_observations_do_not_bind_pid(self):
        graph = fixture()
        graph['components'] = [{'manager': '/demo/container', 'status': 'observed',
                                'reason': None, 'nodes': [{'full_name': '/demo/sink', 'unique_id': 1}]}]
        result = tool.topology(graph, 'a' * 64)
        self.assertEqual(result['component_observations'], graph['components'])
        self.assertNotIn('pid', json.dumps(result))

    def test_invalid_snapshot_and_domain_rejected(self):
        for key, value in (('domain_id', True), ('domain_id', -1), ('domain_id', 233),
                           ('format_version', 2), ('kind', 'ros_preflight')):
            graph = fixture()
            graph[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                tool.topology(graph, 'a' * 64)

    def test_dot_quotes_untrusted_labels_and_exposes_limitations(self):
        graph = fixture()
        graph['topics'][0]['name'] = '/x"; injected -> node; //\n'
        result = tool.topology(graph, 'a' * 64)
        dot = tool.to_dot(result)
        self.assertIn('NOT message delivery', dot)
        self.assertIn(tool.dot_string(graph['topics'][0]['name'] + '\nstd_msgs/msg/String'), dot)
        self.assertIn('depth=None', dot)
        self.assertEqual(len(result['links']), 2)

    def test_real_cli_other_directory_hashes_and_never_overwrites(self):
        script = Path(tool.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / 'source.json'
            raw = json.dumps(fixture()).encode()
            source.write_bytes(raw)
            output = folder / 'topology.json'
            command = [sys.executable, str(script), '--graph-query', str(source), '--output', str(output)]
            run = subprocess.run(command, cwd=folder, capture_output=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(output.read_bytes())
            self.assertEqual(result['source_sha256'], hashlib.sha256(raw).hexdigest())
            before = output.read_bytes()
            self.assertEqual(subprocess.run(command, cwd=folder, capture_output=True, timeout=5).returncode, 1)
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(source.read_bytes(), raw)

    def test_duplicate_json_and_fifo_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'source', Path(tmp) / 'output'
            source.write_text('{"domain_id":0,"domain_id":1}')
            self.assertEqual(tool.main(['--graph-query', str(source), '--output', str(output)]), 1)
            self.assertFalse(output.exists())
            source.unlink()
            if hasattr(os, 'mkfifo'):
                os.mkfifo(source)
                self.assertEqual(tool.main(['--graph-query', str(source), '--output', str(output)]), 1)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
