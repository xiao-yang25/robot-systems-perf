"""Known geometry, blocked maps, strict inventories and CLI bounds."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import signal
from scenarios.robot_reference.algorithms import astar, inverse, forward, centroid, apply, step, OBSTACLES, ROLES
from scenarios.robot_reference.run import distribution, main, verify_graph, verify_records


class Algorithms(unittest.TestCase):
    def test_ik_matches_independent_target(self):
        for target in ([.65,.4],[.6,-.2],[1.1,.3]):
            actual = forward(inverse(target))
            for a,b in zip(actual,target): self.assertAlmostEqual(a,b,places=12)
        with self.assertRaises(ValueError): inverse([2,0])

    def test_astar_route_avoids_wall_and_is_connected(self):
        path = astar([.3,.3],[1.8,1.8])
        cells = [tuple(round(v/.1) for v in p) for p in path]
        self.assertEqual(cells[0],(3,3)); self.assertEqual(cells[-1],(18,18))
        self.assertFalse(set(cells)&OBSTACLES)
        for a,b in zip(cells,cells[1:]): self.assertEqual(sum(abs(x-y) for x,y in zip(a,b)),1)
        self.assertEqual(len(cells),31)  # Manhattan lower bound is achieved around the barrier.
        with self.assertRaises(ValueError): astar([1.2,.5],[1.8,1.8])
        with self.assertRaises(ValueError): astar([.2,.2],[.4,.4],{(x,3) for x in range(24)})

    def test_perception_rejects_background(self):
        self.assertEqual(centroid([[1,2,.5],[1,2,.5],[1,2,.5],[1,2,.5],[999,999,1]]),[1,2])
        with self.assertRaises(ValueError): centroid([[1,2,.5]])

    def test_closed_loops_progress(self):
        for scenario in ROLES:
            simulation={'pose':[.3,.3],'target':[.65,.4] if scenario=='arm' else [1.8,1.8], 'joints':[-.4,.8]}
            if scenario=='arm': simulation['pose']=forward(simulation['joints'])
            initial=math.dist(simulation['pose'],simulation['target']); states={r:{} for r in ROLES[scenario]}
            for _ in range(200):
                data=dict(simulation,odometry=simulation['pose'],position_measurement=simulation['pose'],
                          points=[[*simulation['target'],.5] for _ in range(4)])
                for role in ROLES[scenario][1:]: data=step(scenario,role,data,states[role])
                distance=apply(scenario,simulation,data['command'],.05)
            self.assertLess(distance,.05); self.assertLess(distance,initial)

    def test_actuator_rejects_bad_command(self):
        for command in ([float('nan'),0],[10,0],[0]):
            with self.assertRaises(ValueError): apply('navigation',{'pose':[.3,.3],'target':[1,1]},command,.05)


class Measurements(unittest.TestCase):
    def test_known_quantiles(self):
        self.assertEqual(distribution([3,1,2]),{'n':3,'p50':2,'p95':3,'p99':3,'max':3})
        self.assertIsNone(distribution([])['p99'])

    def test_graph_exact_topology(self):
        graph={'status':'observed','topics':[]}
        roles=ROLES['navigation']; namespace='/test'
        for i,role in enumerate(roles):
            endpoint=lambda r:{'node_name':r,'node_namespace':namespace,'qos':{'reliability':{'name':'RELIABLE'}}}
            graph['topics'].append({'name':namespace+'/hop_'+str(i),'publishers':[endpoint(role)],
                                    'subscriptions':[endpoint(roles[(i+1)%len(roles)])]})
        verify_graph(graph,namespace,roles)
        for mutation in ('qos','duplicate','missing','identity'):
            bad=copy.deepcopy(graph)
            if mutation=='qos': bad['topics'][0]['publishers'][0]['qos']['reliability']['name']='BEST_EFFORT'
            if mutation=='duplicate': bad['topics'][0]['publishers']*=2
            if mutation=='missing': bad['topics'].pop()
            if mutation=='identity': bad['topics'][0]['subscriptions'][0]['node_name']='other'
            with self.assertRaises(ValueError): verify_graph(bad,namespace,roles)

    def test_duplicate_or_missing_events_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'simulator-events.jsonl'
            for records in ([],[{'sample_id':'a','type':'input'}]*2):
                p.write_text(''.join(json.dumps(r)+'\n' for r in records))
                with self.assertRaises(ValueError): verify_records(Path(tmp),['a'],{},['simulator'])

    def test_invalid_config_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'run'
            for flag,value in (('--seconds','nan'),('--hz','0'),('--points','3'),('--domain-id','233'),('--monitor-interval','inf')):
                with self.assertRaises(SystemExit) as result: main(['--output',str(path),flag,value])
                self.assertEqual(result.exception.code,2); self.assertFalse(path.exists())

    def test_existing_output_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp); (path/'keep').write_text('original')
            self.assertEqual(main(['--output',str(path)]),1)
            self.assertEqual(list(path.iterdir()),[path/'keep'])
            self.assertEqual((path/'keep').read_text(),'original')

    def test_real_cancel_at_creation_leaves_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=(Path(tmp)/'run').resolve(); original=Path.mkdir
            def interrupt(path,*args,**kwargs):
                value=original(path,*args,**kwargs)
                if path==output: os.kill(os.getpid(),signal.SIGTERM)
                return value
            with patch.object(Path,'mkdir',interrupt):
                self.assertEqual(main(['--output',str(output)]),130)
            status=json.loads((output/'reference-status.json').read_text())
            self.assertEqual(status['status'],'interrupted'); self.assertGreater(status['finished_ns'],status['started_ns'])

    def test_initial_state_write_error_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'run'; original=Path.write_text; calls=0
            def fail(path,*args,**kwargs):
                nonlocal calls
                if path.name=='reference-status.json':
                    calls+=1
                    if calls==1: raise OSError('initial status write failed')
                return original(path,*args,**kwargs)
            with patch.object(Path,'write_text',fail):
                self.assertEqual(main(['--output',str(output)]),1)
            status=json.loads((output/'reference-status.json').read_text())
            self.assertEqual(status['status'],'failed'); self.assertEqual(status['error'],'initial status write failed')


if __name__ == '__main__': unittest.main()
