"""Test-owned real ROS transport; synchronous recording is not production instrumentation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def write(path, value):
    path.write_text(json.dumps(value, allow_nan=False)+'\n')


def run(args):
    import rclpy
    from std_msgs.msg import String
    from rclpy.qos import QoSProfile, ReliabilityPolicy

    rclpy.init(args=[])
    node = rclpy.create_node(args.role, namespace=args.namespace, enable_rosout=False)
    root = args.directory
    identity = {'pid': os.getpid(), 'starttime_ticks': int(
        Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])}
    context = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
               'pid_namespace': os.readlink('/proc/self/ns/pid'), 'clock': 'linux_monotonic',
               'ros_domain_id': node.context.get_domain_id()}
    source = {'python': str(Path(sys.executable).resolve()), 'rclpy_module': rclpy.__file__,
              'rmw': rclpy.get_rmw_implementation_identifier(),
              'fixture_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    qos = QoSProfile(depth=40, reliability=ReliabilityPolicy.RELIABLE)
    publisher = None
    if args.role in ('source', 'processor'):
        publisher = node.create_publisher(String, 'input' if args.role == 'source' else 'output', qos)
    planned = json.loads((root/'input-inventory.json').read_text())['sample_ids']
    seen, emitted = set(), 0
    stream = (root/(args.role+'-raw.jsonl')).open('x')
    end = time.monotonic()+30

    def emit(sample_id, kind, valid=True, reason=None):
        nonlocal emitted
        row = dict(sample_id=sample_id, type=kind, monotonic_ns=time.monotonic_ns(),
                   function_id=args.role, node=node.get_fully_qualified_name(),
                   valid=valid, reason=reason, **identity)
        stream.write(json.dumps(row)+'\n'); stream.flush()
        emitted += 1

    def callback(message):
        sample = json.loads(message.data)
        index = planned.index(sample['sample_id'])
        if sample['sample_id'] in seen:
            raise RuntimeError('duplicate fixture delivery')
        seen.add(sample['sample_id'])
        if args.role == 'processor':
            if index % 6 == 1:
                emit(sample['sample_id'], 'drop', reason='controlled rejection')
            else:
                valid = index % 6 != 2
                result = String(); result.data = json.dumps({'sample_id': sample['sample_id'], 'valid': valid})
                publisher.publish(result)
                # Output boundary: publish call returned, not downstream delivery.
                emit(sample['sample_id'], 'output', valid,
                     None if valid else 'controlled invalid result')
        else:
            expected_valid = index % 6 != 2
            if type(sample.get('valid')) is not bool or sample['valid'] != expected_valid or index % 6 == 1:
                raise RuntimeError('unexpected sink payload')
            stream.write(json.dumps({'sample_id': sample['sample_id'], 'valid': sample['valid'],
                                     'received_ns': time.monotonic_ns(), **identity})+'\n')
            stream.flush()

    subscription = None
    if args.role != 'source':
        subscription = node.create_subscription(String, 'input' if args.role == 'processor' else 'output', callback, qos)
    try:
        write(root/(args.role+'-identity.json'), dict(identity=identity, context=context, source=source,
                                                    node=node.get_fully_qualified_name()))
        ready = False; started = False
        next_index = 0; next_send = 0
        while not (root/'stop').exists():
            if time.monotonic() >= end:
                raise TimeoutError('fixture total lifetime exceeded 30 seconds')
            rclpy.spin_once(node, timeout_sec=.01)
            if not ready and (publisher is None or publisher.get_subscription_count() == 1):
                write(root/(args.role+'-ready.json'), {'ready_ns': time.monotonic_ns()}); ready = True
            if args.role == 'source' and ready and (root/'go').exists() and time.monotonic() >= next_send:
                if next_index < len(planned):
                    emit(planned[next_index], 'input')
                    message = String(); message.data = json.dumps({'sample_id': planned[next_index]})
                    publisher.publish(message)
                    next_index += 1; next_send = time.monotonic()+.05
                    if next_index == len(planned): write(root/'source-done.json', {'sent': next_index})
            if args.role != 'source' and not started:
                expected = len(planned) if args.role == 'processor' else sum(i % 6 != 1 for i in range(len(planned)))
                if len(seen) == expected:
                    write(root/(args.role+'-done.json'), {'received': len(seen)}); started = True
        write(root/(args.role+'-recording.json'), {'emitted_events': emitted, 'write_errors': 0,
                                                'dropped_events': 0, 'synchronous_test_writer': True})
    finally:
        stream.close()
        node.destroy_node()
        rclpy.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('source', 'processor', 'sink'), required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__': main()
