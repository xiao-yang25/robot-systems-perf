"""Run by the selected ROS Python as a file, never importing perfkit."""
import argparse
import json
import os
import platform
import time
import uuid


def _full_name(name, namespace):
    return namespace.rstrip('/') + '/' + name


def _policy(value):
    return {'name': getattr(value, 'name', str(value)), 'value': int(value)}


def _endpoint(info):
    profile = info.qos_profile
    qos = {key: _policy(getattr(profile, key)) for key in
           ('history', 'reliability', 'durability', 'liveliness')}
    qos['reported_depth'] = int(profile.depth)
    unknown_history = qos['history']['name'] == 'UNKNOWN'
    qos['depth'] = None if unknown_history else qos['reported_depth']
    qos['depth_reason'] = 'RMW graph does not expose queue depth' if unknown_history else None
    qos['scope'] = 'Reported RMW endpoint graph information; not complete local endpoint configuration.'
    for key in ('deadline', 'lifespan', 'liveliness_lease_duration'):
        qos[key + '_ns'] = int(getattr(profile, key).nanoseconds)
    qos['avoid_ros_namespace_conventions'] = bool(profile.avoid_ros_namespace_conventions)
    return {'node_name': info.node_name, 'node_namespace': info.node_namespace,
            'full_name': _full_name(info.node_name, info.node_namespace),
            'endpoint_gid': bytes(info.endpoint_gid).hex(), 'qos': qos,
            'topic_type': info.topic_type}


def _component(node, rclpy, manager, service_type, deadline):
    result = {'manager': manager, 'status': 'failed', 'reason': None, 'nodes': None}
    client = None
    try:
        client = node.create_client(service_type, manager + '/_container/list_nodes')
        remaining = max(0, deadline - time.monotonic())
        if not client.wait_for_service(timeout_sec=remaining):
            result.update(status='unavailable', reason='ListNodes service unavailable before deadline')
            return result
        remaining = max(0, deadline - time.monotonic())
        if not remaining:
            result['reason'] = 'component query deadline exhausted'
            return result
        future = client.call_async(service_type.Request())
        rclpy.spin_until_future_complete(node, future, timeout_sec=remaining)
        if not future.done():
            future.cancel()
            result['reason'] = 'ListNodes response timed out'
            return result
        response = future.result()
        if response is None or len(response.full_node_names) != len(response.unique_ids):
            result['reason'] = 'invalid ListNodes response'
            return result
        result.update(status='observed', nodes=[{'full_name': name, 'unique_id': int(uid)}
            for name, uid in zip(response.full_node_names, response.unique_ids)])
    except Exception as error:
        result['reason'] = 'ListNodes query failed: ' + type(error).__name__ + ': ' + str(error)
    finally:
        if client is not None:
            node.destroy_client(client)
    return result


def snapshot(domain_id, wait_seconds, timeout_seconds, managers, deadline_ns=None):
    start = time.monotonic_ns()
    deadline = time.monotonic() + timeout_seconds
    if deadline_ns is not None:
        # Leave a small part of the parent's total budget for node shutdown and
        # JSON output; the parent still enforces the hard deadline independently.
        deadline = min(deadline, deadline_ns / 1000000000 - .1)
    result = {'format_version': 1, 'kind': 'ros_graph_snapshot', 'status': 'unavailable',
        'reason': None, 'domain_id': domain_id, 'query_window': {'start_ns': start, 'end_ns': start},
        'source': {'adapter': 'rclpy', 'ros_distro': os.environ.get('ROS_DISTRO'),
                   'rmw': None, 'python_version': platform.python_version()},
        'nodes': [], 'topics': [], 'components': [], 'limitations': [
            'Visible ROS graph entities may belong to remote hosts.',
            'ROS graph names and endpoint GIDs do not establish a local PID mapping.',
            'This is a point query; graph reads are not atomic and discovery may be incomplete.',
            'An empty observation does not establish that no business workload exists.']}
    node = None
    initialized = False
    errors = []
    try:
        try:
            import rclpy
            from rclpy.utilities import get_rmw_implementation_identifier
        except ImportError as error:
            result['reason'] = 'rclpy unavailable: ' + str(error)
            result['components'] = [{'manager': manager, 'status': 'unavailable',
                'reason': 'rclpy unavailable', 'nodes': None} for manager in managers]
            return result
        rclpy.init(args=[])
        initialized = True
        result['source']['rmw'] = get_rmw_implementation_identifier()
        result['source']['rmw_source'] = 'rclpy.utilities.get_rmw_implementation_identifier'
        result['source']['rmw_environment_declaration'] = os.environ.get('RMW_IMPLEMENTATION')
        observer = '_perfkit_graph_' + uuid.uuid4().hex
        node = rclpy.create_node(observer, namespace='/', enable_rosout=False,
                                start_parameter_services=False, use_global_arguments=False)
        until = min(deadline, time.monotonic() + wait_seconds)
        while time.monotonic() < until:
            rclpy.spin_once(node, timeout_sec=min(.05, max(0, until - time.monotonic())))
        try:
            result['nodes'] = [{'name': name, 'namespace': namespace,
                'full_name': _full_name(name, namespace)}
                for name, namespace in node.get_node_names_and_namespaces()
                if (name, namespace) != (observer, '/')]
        except Exception as error:
            errors.append('node query failed: ' + type(error).__name__ + ': ' + str(error))
        try:
            topics = node.get_topic_names_and_types()
        except Exception as error:
            topics = []
            errors.append('topic query failed: ' + type(error).__name__ + ': ' + str(error))
        for name, types in topics:
            topic = {'name': name, 'types': list(types), 'publishers': None, 'subscriptions': None}
            observer_endpoints = 0
            for role, method in (('publishers', node.get_publishers_info_by_topic),
                                 ('subscriptions', node.get_subscriptions_info_by_topic)):
                try:
                    infos = method(name)
                    observer_endpoints += sum((info.node_name, info.node_namespace) ==
                                              (observer, '/') for info in infos)
                    topic[role] = [_endpoint(info) for info in infos
                        if (info.node_name, info.node_namespace) != (observer, '/')]
                except Exception as error:
                    errors.append(role + ' query failed for ' + name + ': ' +
                                  type(error).__name__ + ': ' + str(error))
            if observer_endpoints and topic['publishers'] == [] and topic['subscriptions'] == []:
                continue
            result['topics'].append(topic)
        if managers:
            try:
                from composition_interfaces.srv import ListNodes
            except ImportError as error:
                result['components'] = [{'manager': manager, 'status': 'unavailable',
                    'reason': 'composition_interfaces unavailable: ' + str(error), 'nodes': None}
                    for manager in managers]
            else:
                for index, manager in enumerate(managers):
                    # Divide the remaining budget so one missing manager cannot
                    # consume every later manager's opportunity to respond.
                    component_deadline = time.monotonic() + max(0, deadline - time.monotonic()) / (len(managers) - index)
                    result['components'].append(_component(node, rclpy, manager, ListNodes,
                                                           component_deadline))
            errors.extend(row['manager'] + ': ' + row['reason']
                          for row in result['components'] if row['status'] != 'observed')
        if errors:
            result.update(status='failed', reason='partial graph query: ' + '; '.join(errors),
                          partial=True, query_errors=errors)
        else:
            observed = result['nodes'] or result['topics'] or any(row['nodes'] for row in result['components'])
            result.update(status='observed' if observed else 'empty', reason=None)
    except Exception as error:
        result.update(status='failed', reason='adapter failed: ' + type(error).__name__ + ': ' + str(error),
                      partial=bool(result['nodes'] or result['topics']))
        if not result['components']:
            result['components'] = [{'manager': manager, 'status': 'failed',
                'reason': 'adapter failed before component query', 'nodes': None} for manager in managers]
    finally:
        try:
            if node is not None:
                node.destroy_node()
            if initialized:
                rclpy.shutdown()
        except Exception as error:
            result.update(status='failed', reason='adapter cleanup failed: ' + str(error), partial=True)
        result['query_window']['end_ns'] = time.monotonic_ns()
        result['query_duration_ns'] = result['query_window']['end_ns'] - start
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain-id', type=int, required=True)
    parser.add_argument('--wait-seconds', type=float, required=True)
    parser.add_argument('--timeout-seconds', type=float, required=True)
    parser.add_argument('--deadline-ns', type=int)
    parser.add_argument('--component-manager', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(snapshot(args.domain_id, args.wait_seconds, args.timeout_seconds,
                              args.component_manager, args.deadline_ns), allow_nan=False))


if __name__ == '__main__':
    main()
