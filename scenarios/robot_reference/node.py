"""One owned reference node. JSON envelopes keep sample IDs across every ROS hop."""
import argparse
import json
import os
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scenarios.robot_reference.algorithms import ROLES, step, apply, forward


def write(path, value):
    path.write_text(json.dumps(value, allow_nan=False)+'\n')


def run(args):
    import rclpy
    from std_msgs.msg import String
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    rclpy.init(args=[])
    node = rclpy.create_node(args.role, namespace=args.namespace, enable_rosout=False)
    root = args.output
    config = json.loads((root/'config.json').read_text())
    planned = json.loads((root/'input-inventory.json').read_text())['sample_ids']
    roles = ROLES[args.scenario]
    index = roles.index(args.role)
    topic_out = 'hop_'+str(index)
    topic_in = 'hop_'+str((index-1) % len(roles))
    qos = QoSProfile(depth=config['depth'], reliability=ReliabilityPolicy.RELIABLE)
    pub = node.create_publisher(String, topic_out, qos)
    identity = {'pid':os.getpid(), 'starttime_ticks':int(
        Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])}
    context = {'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
               'pid_namespace':os.readlink('/proc/self/ns/pid'), 'clock':'linux_monotonic',
               'ros_domain_id':node.context.get_domain_id()}
    write(root/(args.role+'-identity.json'), {'identity':identity, 'context':context,
        'node':node.get_fully_qualified_name(), 'rmw':rclpy.get_rmw_implementation_identifier(),
        'python':str(Path(sys.executable).resolve()), 'rclpy_module':rclpy.__file__})
    raw = (root/(args.role+'-events.jsonl')).open('x')
    stats = (root/(args.role+'-stages.jsonl')).open('x')
    state, seen = {}, set()
    simulator = {'joints':[-.4,.8], 'pose':[.3,.3],
                 'target':[.65,.4] if args.scenario == 'arm' else [1.8,1.8]}
    if args.scenario == 'arm': simulator['pose'] = forward(simulator['joints'])
    initial_distance = __import__('math').dist(simulator['pose'], simulator['target'])
    randomizer = random.Random(config['seed'])
    inflight = None
    sent, received, start_ns = 0, 0, None

    def event(sid, kind, timestamp):
        raw.write(json.dumps({'sample_id':sid, 'type':kind, 'monotonic_ns':timestamp,
            'function_id':args.role, 'node':node.get_fully_qualified_name(),
            'valid':True, 'reason':None, **identity})+'\n')
        raw.flush()

    def publish(data):
        data['send_ns'] = time.monotonic_ns()
        message = String(); message.data = json.dumps(data, allow_nan=False)
        begin = time.monotonic_ns()
        pub.publish(message)
        end = time.monotonic_ns()
        return len(message.data.encode()), end-begin

    def callback(message):
        nonlocal inflight, received
        begin, cpu = time.monotonic_ns(), time.thread_time_ns()
        envelope = json.loads(message.data)
        sid = envelope['sample_id']
        if sid not in planned or sid in seen:
            raise ValueError('unexpected/duplicate sample delivery')
        seen.add(sid)
        sent_ns = envelope['send_ns']
        if begin < sent_ns: raise ValueError('reversed reference clock')
        algorithm_begin, algorithm_cpu = time.monotonic_ns(), time.thread_time_ns()
        if args.role == 'simulator':
            if sid != inflight: raise ValueError('actuator input differs from outstanding frame')
            distance = apply(args.scenario, simulator, envelope['data']['command'], 1/config['hz'])
            end = time.monotonic_ns()
            algorithm_end, algorithm_cpu_end = end, time.thread_time_ns()
            callback_cpu_end = algorithm_cpu_end
            event(sid, 'output', end)
            inflight = None
            size, publish_ns = 0, None
        else:
            result = step(args.scenario, args.role, envelope['data'], state)
            algorithm_end, algorithm_cpu_end = time.monotonic_ns(), time.thread_time_ns()
            size, publish_ns = publish({'sample_id':sid, 'data':result,
                                       'planned_release_ns':envelope['planned_release_ns']})
            end = time.monotonic_ns()
            callback_cpu_end = time.thread_time_ns()
        row = {'sample_id':sid, 'role':args.role, 'received_ns':begin,
            'source_send_ns':sent_ns, 'edge_age_ns':begin-sent_ns,
            'algorithm_wall_ns':algorithm_end-algorithm_begin,
            'algorithm_cpu_ns':algorithm_cpu_end-algorithm_cpu,
            'callback_wall_ns':end-begin, 'callback_cpu_ns':callback_cpu_end-cpu,
            'publish_call_ns':publish_ns, 'outgoing_payload_bytes':size, **identity}
        if args.role == 'simulator': row['goal_error'] = distance
        stats.write(json.dumps(row)+'\n'); stats.flush()
        received += 1
        if received == len(planned):
            write(root/(args.role+'-done.json'), {'received':received,
                'initial_goal_error':initial_distance if args.role == 'simulator' else None,
                'final_goal_error':distance if args.role == 'simulator' else None})

    sub = node.create_subscription(String, topic_in, callback, qos)
    ready = False
    lifetime = time.monotonic()+90
    try:
        while not (root/'stop').exists():
            now = time.monotonic_ns()
            if time.monotonic() > lifetime: raise TimeoutError('reference node lifetime exceeded 90 seconds')
            rclpy.spin_once(node, timeout_sec=.002)
            if not ready and pub.get_subscription_count() == 1 and node.count_publishers(args.namespace+'/'+topic_in) == 1:
                write(root/(args.role+'-ready.json'), {'ready_ns':time.monotonic_ns()}); ready = True
            if args.role != 'simulator' or not ready or not (root/'go').exists(): continue
            if start_ns is None: start_ns = time.monotonic_ns()
            if inflight is not None:
                if now-last_sent_ns > 2_000_000_000: raise TimeoutError('reference frame did not return within 2 seconds')
                continue
            if sent == len(planned): continue
            release = start_ns+round(sent*1e9/config['hz'])
            if time.monotonic_ns() < release: continue
            sid = planned[sent]
            data = {'pose':simulator['pose'][:], 'target':simulator['target'][:],
                    'odometry':simulator['pose'][:], 'joints':simulator['joints'][:]}
            if args.scenario == 'navigation':
                data['position_measurement'] = [v+randomizer.uniform(-.001,.001) for v in simulator['pose']]
            else:
                target = simulator['target']
                # Foreground object + background points, deterministic seeded sensor noise.
                data['points'] = [[target[0]+randomizer.uniform(-.005,.005),
                    target[1]+randomizer.uniform(-.005,.005), .5+randomizer.uniform(-.01,.01)]
                    if i % 2 else [randomizer.uniform(0,2), randomizer.uniform(0,2), 1.]
                    for i in range(config['points'])]
            timestamp = time.monotonic_ns()
            event(sid, 'input', timestamp)
            size, duration = publish({'sample_id':sid, 'data':data, 'planned_release_ns':release})
            stats.write(json.dumps({'sample_id':sid, 'role':'sensor_release', 'input_ns':timestamp,
                'planned_release_ns':release, 'release_lateness_ns':timestamp-release,
                'publish_call_ns':duration, 'outgoing_payload_bytes':size, **identity})+'\n'); stats.flush()
            sent += 1; inflight = sid; last_sent_ns = timestamp
    finally:
        raw.close(); stats.close()
        node.destroy_node(); rclpy.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=ROLES, required=True)
    parser.add_argument('--role', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())
