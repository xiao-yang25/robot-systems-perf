import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.integration_ros_evidence import fixture_graph_diagnostics, wait_for_fixture_graph
from tests.test_ros_integration_contract import topic


NS = '/controlled_fixture'
MANAGER = NS+'/container'


def snapshot(*, duplicate=False):
    data = topic('KEEP_LAST', 3)
    data.update(name=NS+'/data', types=['std_msgs/msg/String'])
    for role in ('publishers', 'subscriptions'):
        for endpoint, name in zip(data[role], ('first', 'second')):
            endpoint['full_name'] = NS+'/'+name
    names = [NS+'/'+name for name in ('first', 'second', 'standalone')]
    if duplicate: names.append(NS+'/first')
    return {'status': 'observed', 'reason': None, 'nodes': [{'full_name': name} for name in names],
            'topics': [data], 'components': [{'manager': MANAGER, 'status': 'observed',
                'nodes': [{'full_name': NS+'/'+name} for name in ('first', 'second')]}],
            'query_window': {'start_ns': 10, 'end_ns': 20},
            'query_evidence': {'host_window': {'start_ns': 5, 'end_ns': 25}}}


class FixtureReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.now=100.; self.calls=[]

    def query(self, values, *, advance=.25):
        def observe(folder, budget):
            self.calls.append((folder, budget)); self.now += advance
            folder.mkdir()
            value=copy.deepcopy(values[len(self.calls)-1])
            (folder/'graph-query.json').write_text(json.dumps(value))
            return value
        return observe

    def wait(self, query, **kwargs):
        return wait_for_fixture_graph(query,self.root,NS,MANAGER,clock=lambda:self.now,**kwargs)

    def report(self):
        return json.loads((self.root/'graph-readiness.json').read_text())

    def test_empty_graph_with_loaded_components_is_retained_then_complete_selected(self):
        first=snapshot(); first.update(status='empty',nodes=[],topics=[])
        complete=snapshot()
        folder,selected=self.wait(self.query([first,complete]))
        self.assertEqual(selected,complete); self.assertEqual(folder.name,'graph-002')
        self.assertEqual(json.loads((self.root/'graph/graph-query.json').read_text()),first)
        state=self.report()
        self.assertTrue(state['recovered_incomplete']); self.assertEqual(state['status'],'ready')
        self.assertEqual(state['attempts'][0]['status'],'incomplete')
        self.assertIn('query_window',state['attempts'][0]['diagnostic'])
        self.assertEqual(state['selected_output'],'graph-002')

    def test_partial_snapshots_cannot_be_merged_or_claimed_complete(self):
        nodes=snapshot(); nodes['topics']=[]
        endpoints=snapshot(); endpoints['nodes']=[]
        with self.assertRaisesRegex(AssertionError,'bounded attempts'):
            self.wait(self.query([nodes,endpoints,nodes]))
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.report()['status'],'failed')
        self.assertIsNone(self.report()['selected_output'])
        for folder,_ in self.calls: self.assertTrue((folder/'graph-query.json').exists())

    def test_budget_exhaustion_stops_before_another_two_second_wait(self):
        value=snapshot(); value['nodes']=[]
        with self.assertRaisesRegex(AssertionError,'insufficient time'):
            self.wait(self.query([value,value],advance=9.25))
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0][1],10)
        self.assertEqual(self.calls[1][1],10)
        self.assertEqual(self.report()['status'],'failed')

    def test_slow_query_shrinks_next_budget_and_late_result_fails(self):
        value=snapshot(); value['nodes']=[]
        with self.assertRaisesRegex(AssertionError,'deadline exceeded'):
            self.wait(self.query([value,value],advance=10.25))
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0][1],10)
        self.assertEqual(self.calls[1][1],9.75)
        self.assertEqual(self.report()['attempts'][-1]['status'],'late')

    def test_complete_result_at_deadline_is_not_accepted(self):
        with self.assertRaisesRegex(AssertionError,'deadline exceeded'):
            self.wait(self.query([snapshot()],advance=20))
        state=self.report()
        self.assertEqual(state['attempts'][0]['status'],'late')
        self.assertEqual(state['status'],'failed'); self.assertIsNone(state['selected_output'])

    def test_slow_record_write_cannot_launch_query_using_stale_budget(self):
        write=Path.write_text
        def slow_record(path,value,*args,**kwargs):
            self.now += 20
            return write(path,value,*args,**kwargs)
        with patch.object(Path,'write_text',slow_record), self.assertRaisesRegex(AssertionError,'before query launch'):
            self.wait(lambda *args:self.fail('expired query must not launch'))
        self.assertEqual(self.report()['attempts'][0]['status'],'not_started')

    def test_query_error_and_cancellation_are_never_retried_and_keep_original(self):
        for index,error in enumerate((RuntimeError('controlled missing SDK'),KeyboardInterrupt('cancel'))):
            out=self.root/str(index); out.mkdir(); calls=[]
            def fail(folder,budget): calls.append(folder); raise error
            with self.subTest(error=error), self.assertRaises(type(error)) as caught:
                wait_for_fixture_graph(fail,out,NS,MANAGER)
            self.assertIs(caught.exception,error); self.assertEqual(len(calls),1)
            state=json.loads((out/'graph-readiness.json').read_text())
            self.assertEqual(state['error'],str(error))
            self.assertEqual(state['status'],'interrupted' if index else 'failed')

    def test_invalid_qos_identity_duplicate_and_component_results_are_not_retried(self):
        cases=[]
        qos=snapshot(); qos['topics'][0]['publishers'][0]['qos']['depth']=2; cases.append(qos)
        identity=snapshot(); identity['topics'][0]['subscriptions'][0]['full_name']=NS+'/other'; cases.append(identity)
        cases.append(snapshot(duplicate=True))
        components=snapshot(); components['components'][0]['status']='unavailable'; cases.append(components)
        for index,value in enumerate(cases):
            out=self.root/str(index); out.mkdir(); self.calls=[]
            with self.subTest(index=index), self.assertRaisesRegex(AssertionError,'invalid fixture graph'):
                wait_for_fixture_graph(self.query([value]),out,NS,MANAGER,clock=lambda:self.now)
            self.assertEqual(len(self.calls),1)
            state=json.loads((out/'graph-readiness.json').read_text())
            self.assertEqual(state['attempts'][0]['status'],'invalid')
            self.assertIn('expected',state['attempts'][0]['diagnostic'])
            self.assertIn('actual',state['attempts'][0]['diagnostic'])

    def test_partial_role_bad_qos_cannot_be_hidden_by_later_correct_snapshot(self):
        for index,role in enumerate(('publishers','subscriptions')):
            out=self.root/str(index); out.mkdir(); self.calls=[]
            value=snapshot(); value['topics'][0][role].pop()
            value['topics'][0][role][0]['qos']['reliability']['name']='BEST_EFFORT'
            with self.subTest(role=role),self.assertRaisesRegex(AssertionError,'QoS mismatch'):
                wait_for_fixture_graph(self.query([value,snapshot()]),out,NS,MANAGER,clock=lambda:self.now)
            self.assertEqual(len(self.calls),1)
            state=json.loads((out/'graph-readiness.json').read_text())
            self.assertEqual(state['status'],'failed'); self.assertIsNone(state['selected_output'])

    def test_final_record_cost_is_separate_from_observation_deadline(self):
        write=Path.write_text
        def slow_final_record(path,value,*args,**kwargs):
            state=json.loads(value)
            if path.name=='graph-readiness.json' and state['attempts'] and state['attempts'][-1]['status']=='ready': self.now += 25
            return write(path,value,*args,**kwargs)
        with patch.object(Path,'write_text',slow_final_record): self.wait(self.query([snapshot()]))
        state=self.report(); deadline=state['started_monotonic']+state['budget_seconds']
        self.assertEqual(state['status'],'ready')
        self.assertLess(state['attempts'][0]['evaluated_monotonic'],deadline)
        self.assertGreater(state['finished_monotonic'],deadline)

    def test_duplicate_stage_waits_for_two_occurrences_in_single_snapshot(self):
        folder,_=self.wait(self.query([snapshot(),snapshot(duplicate=True)]),duplicate=True)
        self.assertEqual(folder.name,'graph-002')
        self.assertTrue(self.report()['recovered_incomplete'])

    def test_report_reuse_rejected_before_any_query(self):
        self.wait(self.query([snapshot()]))
        before=(self.root/'graph-readiness.json').read_bytes()
        with self.assertRaises(FileExistsError): self.wait(lambda *args:self.fail('query should not run'))
        self.assertEqual((self.root/'graph-readiness.json').read_bytes(),before)

    def test_invalid_budget_rejected_before_record_or_query(self):
        for kwargs in ({'max_attempts':4},{'max_attempts':True},{'timeout_seconds':float('nan')},
                       {'timeout_seconds':21},{'timeout_seconds':2}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                self.wait(lambda *args:self.fail('query should not run'),**kwargs)
        self.assertFalse((self.root/'graph-readiness.json').exists())

    def test_secondary_record_failure_cannot_replace_query_error(self):
        error=RuntimeError('original query failure')
        def query(*args): raise error
        write=Path.write_text
        def failed_record(path,value,*args,**kwargs):
            if json.loads(value)['status']=='failed': raise OSError('secondary readiness record failure')
            return write(path,value,*args,**kwargs)
        with patch.object(Path,'write_text',failed_record),self.assertRaises(RuntimeError) as caught:
            self.wait(query)
        self.assertIs(caught.exception,error)

    def test_incomplete_diagnostic_contains_expected_actual_and_both_windows(self):
        value=snapshot(); value['topics'][0]['subscriptions'].pop()
        original=copy.deepcopy(value)
        diagnostic=fixture_graph_diagnostics(value,NS,MANAGER)
        self.assertFalse(diagnostic['ready']); self.assertEqual(value,original)
        self.assertIn('endpoints incomplete: subscriptions',diagnostic['incomplete'])
        self.assertEqual(diagnostic['query_window'],{'start_ns':10,'end_ns':20})
        self.assertEqual(diagnostic['host_window'],{'start_ns':5,'end_ns':25})
