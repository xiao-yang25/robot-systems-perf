"""Reference calculations, not Nav2/MoveIt or production robot controllers."""
import heapq
import math

ROLES = {
    'navigation': ('simulator', 'localizer', 'planner', 'controller'),
    'perception': ('simulator', 'perception', 'fusion', 'planner', 'controller'),
    'arm': ('simulator', 'perception', 'ik', 'trajectory', 'controller'),
}
LENGTHS = (.75, .55)
OBSTACLES = {(12, y) for y in range(4, 15)}
RESOLUTION = .1
GRID_SIZE = 24


def forward(q):
    a, b = LENGTHS
    return [a*math.cos(q[0])+b*math.cos(q[0]+q[1]),
            a*math.sin(q[0])+b*math.sin(q[0]+q[1])]


def inverse(target):
    a, b = LENGTHS
    x, y = target
    c = (x*x+y*y-a*a-b*b)/(2*a*b)
    if abs(c) > 1:
        raise ValueError('target outside two-link workspace')
    elbow = math.acos(c)
    shoulder = math.atan2(y, x)-math.atan2(b*math.sin(elbow), a+b*math.cos(elbow))
    return [shoulder, elbow]


def astar(start, goal, blocked=OBSTACLES):
    """4-connected A*, metric world coordinates, finite grid, blocked-cell rejection."""
    def cell(point):
        return tuple(int(round(v/RESOLUTION)) for v in point)
    first, last = cell(start), cell(goal)
    def free(p):
        return all(0 <= v < GRID_SIZE for v in p) and p not in blocked
    if not free(first) or not free(last):
        raise ValueError('start/goal outside free reference map')
    queue = [(0, first)]
    costs, previous = {first:0}, {}
    while queue:
        _, current = heapq.heappop(queue)
        if current == last:
            path = [last]
            while path[-1] != first:
                path.append(previous[path[-1]])
            return [[x*RESOLUTION, y*RESOLUTION] for x,y in reversed(path)]
        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
            nxt = (current[0]+dx, current[1]+dy)
            cost = costs[current]+1
            if free(nxt) and cost < costs.get(nxt, float('inf')):
                costs[nxt], previous[nxt] = cost, current
                heuristic = abs(nxt[0]-last[0])+abs(nxt[1]-last[1])
                heapq.heappush(queue, (cost+heuristic, nxt))
    raise ValueError('no reference path')


def centroid(points):
    """Segment the known foreground depth band; do not average background points."""
    foreground = [p for p in points if .45 <= p[2] <= .55]
    if len(foreground) < 4:
        raise ValueError('insufficient foreground points')
    return [sum(p[i] for p in foreground)/len(foreground) for i in (0,1)]


def step(scenario, role, data, state):
    result = dict(data)
    if role == 'perception':
        result['target'] = centroid(data['points'])
        result.pop('points')
    elif role == 'localizer':
        # Known absolute-position observation + odometry prediction; scalar Kalman updates.
        old, variance = state.get('estimate', data['odometry']), state.get('variance', .01)
        last_odom = state.get('odometry', data['odometry'])
        predicted = [old[i]+data['odometry'][i]-last_odom[i] for i in (0,1)]
        variance += .0001
        gain = variance/(variance+.0004)
        estimate = [predicted[i]+gain*(data['position_measurement'][i]-predicted[i]) for i in (0,1)]
        state.update(estimate=estimate, variance=(1-gain)*variance, odometry=data['odometry'])
        result['pose'] = estimate
    elif role == 'fusion':
        old = state.get('target', data['target'])
        estimate = [.6*data['target'][i]+.4*old[i] for i in (0,1)]
        state['target'] = estimate
        result['target'] = estimate
    elif role == 'planner':
        result['path'] = astar(data['pose'], data['target'])
    elif role == 'ik':
        result['desired_joints'] = inverse(data['target'])
    elif role == 'trajectory':
        current, desired = data['joints'], data['desired_joints']
        # Cubic rest-to-rest joint path; first short horizon waypoint is tracked each frame.
        s = .25
        blend = 3*s*s-2*s*s*s
        result['joint_waypoint'] = [q+blend*(d-q) for q,d in zip(current, desired)]
    elif role == 'controller':
        if scenario == 'arm':
            result['command'] = [max(-2., min(2., 10*(d-q)))
                                 for q,d in zip(data['joints'], data['joint_waypoint'])]
        else:
            path = data['path']
            waypoint = path[1] if len(path)>1 else data['target']
            delta = [waypoint[i]-data['pose'][i] for i in (0,1)]
            distance = math.hypot(*delta)
            speed = min(.7, 4*distance)
            result['command'] = [speed*v/distance if distance else 0. for v in delta]
    else:
        raise ValueError('unsupported scenario role')
    return result


def apply(scenario, state, command, dt):
    if len(command) != 2 or any(not math.isfinite(v) for v in command):
        raise ValueError('invalid actuator command')
    limit = 2. if scenario == 'arm' else .7
    if any(abs(v)>limit+1e-9 for v in command):
        raise ValueError('actuator command exceeds reference limit')
    if scenario == 'arm':
        state['joints'] = [q+dt*v for q,v in zip(state['joints'], command)]
        state['pose'] = forward(state['joints'])
    else:
        state['pose'] = [q+dt*v for q,v in zip(state['pose'], command)]
        cell = tuple(round(v/RESOLUTION) for v in state['pose'])
        if cell in OBSTACLES or any(not 0 <= v < GRID_SIZE for v in cell):
            raise ValueError('reference mobile robot collided or left map')
    return math.dist(state['pose'], state['target'])
