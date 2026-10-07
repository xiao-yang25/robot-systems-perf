#!/usr/bin/env python3
"""Export one saved ROS graph as name groups and endpoint edges, never causality.

Source-checkout tool: no ROS SDK, process inspection, network or Graphviz needed.
"""
import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from perfkit.ros_evidence import _read
from perfkit.ros_graph import _validate_snapshot


LIMITATIONS = [
    'Edges are advertised publisher/subscription endpoints, not observed message delivery.',
    'Name groups are not node identities; duplicate names remain ambiguous.',
    'Local process ownership, QoS compatibility and business causality remain unresolved.',
    'One non-atomic discovery query cannot prove complete or continuous topology.',
    'Services, actions, callback paths and non-ROS communication are not collected here.',
    'Component names and IDs do not establish a local PID or split process resources.',
    'Parent ros-status.json is not read here; export success is not query or run success.',
]


def topology(snapshot, digest):
    """Keep endpoint GIDs/QoS and observation gaps without pairing pub to sub."""
    if not isinstance(snapshot, dict):
        raise ValueError('graph snapshot must be an object')
    domain = snapshot.get('domain_id')
    if type(domain) is not int or not 0 <= domain <= 232:
        raise ValueError('invalid graph domain_id')
    components = snapshot.get('components')
    if not isinstance(components, list) or not all(isinstance(c, dict) for c in components):
        raise ValueError('invalid graph components')
    _validate_snapshot(snapshot, domain, [c.get('manager') for c in components])
    counts = Counter(n['full_name'] for n in snapshot['nodes'])
    endpoint_counts = Counter()
    for topic in snapshot['topics']:
        for role in ('publishers', 'subscriptions'):
            endpoint_counts.update(e['full_name'] for e in topic.get(role) or [])
    names = sorted(set(counts) | set(endpoint_counts))
    groups = [{'id': 'n' + str(i), 'full_name': name,
               'observed_node_count': counts[name], 'endpoint_count': endpoint_counts[name],
               'name_status': ('ambiguous' if counts[name] > 1 else
                               'observed' if counts[name] == 1 else 'endpoint_only'),
               'process_ownership': 'unresolved'} for i, name in enumerate(names)]
    name_ids = {n['full_name']: n['id'] for n in groups}
    topics, endpoints, links = [], [], []
    for i, topic in enumerate(snapshot['topics']):
        tid = 't' + str(i)
        topics.append({'id': tid, 'name': topic['name'], 'types': list(topic['types']),
                       'endpoint_reads': {role: ('unavailable' if topic.get(role) is None else 'observed')
                                          for role in ('publishers', 'subscriptions')}})
        for role in ('publishers', 'subscriptions'):
            for row in topic.get(role) or []:
                eid = 'e' + str(len(endpoints))
                endpoints.append({'id': eid, 'topic_id': tid, 'role': role,
                                  'node_group_id': name_ids[row['full_name']],
                                  'reported': copy.deepcopy(row)})
                source, target = ((name_ids[row['full_name']], tid) if role == 'publishers'
                                  else (tid, name_ids[row['full_name']]))
                links.append({'source': source, 'target': target, 'endpoint_id': eid,
                              'meaning': 'advertised_endpoint'})
    # Failed/unavailable input may still be useful for diagnosis; never complete it.
    return {'format_version': 1, 'kind': 'ros_topic_topology',
            'source_sha256': digest, 'domain_id': domain,
            'observation_status': snapshot['status'], 'observation_reason': snapshot['reason'],
            'query_window': copy.deepcopy(snapshot['query_window']),
            'runtime_source': copy.deepcopy(snapshot['source']),
            'snapshot_limitations': list(snapshot['limitations']), 'limitations': LIMITATIONS,
            'node_groups': groups, 'topics': topics, 'endpoints': endpoints, 'links': links,
            'component_observations': copy.deepcopy(components),
            'business_acceptance': 'not_evaluated'}


def dot_string(value):
    # DOT quoted-string escaping; IDs are generated, never taken from ROS names.
    return json.dumps(value, ensure_ascii=False)


def to_dot(value):
    legend = ('Advertised topic endpoints; NOT message delivery or business causality.\n'
              'status=' + value['observation_status'] + '; domain=' + str(value['domain_id']) +
              '; reason=' + str(value['observation_reason']) +
              '\nquery_window=' + str(value['query_window']) + '\nsource_sha256=' + value['source_sha256'] +
              '\nName groups may be ambiguous/remote; PID unresolved. Services/actions not collected.')
    # Preserve complete evidence in a single ASCII JSON comment. Newlines and
    # untrusted labels are escaped, so they cannot introduce DOT statements.
    metadata = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    lines = ['// ros_topic_topology_json: ' + metadata,
             'digraph ros_topic_topology {', '  rankdir=LR;',
             '  graph [label=' + dot_string(legend) + ', labelloc="t"];']
    for node in value['node_groups']:
        label = (node['full_name'] + '\n' + node['name_status'] +
                 '; node rows=' + str(node['observed_node_count']) + '; PID unresolved')
        color = 'red' if node['name_status'] != 'observed' else 'black'
        lines.append('  ' + node['id'] + ' [shape=ellipse, color=' + color +
                     ', label=' + dot_string(label) + '];')
    for topic in value['topics']:
        label = topic['name'] + '\n' + ', '.join(topic['types'])
        missing = [r for r, s in topic['endpoint_reads'].items() if s == 'unavailable']
        if missing:
            label += '\nunavailable: ' + ', '.join(missing)
        lines.append('  ' + topic['id'] + ' [shape=box, label=' + dot_string(label) + '];')
    for edge, endpoint in zip(value['links'], value['endpoints']):
        row = endpoint['reported']
        qos = row['qos']
        label = (endpoint['id'] + ': ' + qos['reliability']['name'] +
                 '; depth=' + str(qos['depth']) + '\ngid=' + row['endpoint_gid'])
        lines.append('  ' + edge['source'] + ' -> ' + edge['target'] +
                     ' [label=' + dot_string(label) + '];')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph-query', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--format', choices=('json', 'dot'), default='json')
    args = parser.parse_args(argv)
    def terminate(signum, frame):
        raise KeyboardInterrupt('topology export interrupted')
    old = signal.signal(signal.SIGTERM, terminate)
    try:
        snapshot, raw = _read(args.graph_query, maximum=4 * 1024 * 1024)
        result = topology(snapshot, hashlib.sha256(raw).hexdigest())
        text = (to_dot(result) if args.format == 'dot' else
                json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        # No directory creation, no source modifications, no overwrite/symlink following.
        # A failed/interrupted write can leave a partial derived file; source evidence
        # remains authoritative and the nonzero return code must be checked.
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, RecursionError) as error:
        print('Topology export failed: ' + str(error), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old)


if __name__ == '__main__':
    raise SystemExit(main())
