import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perfkit import business_events as b
from tests.test_ros_evidence import monitor_fixture, CONTEXT, REF


def chain():
    return {'format_version': 1, 'chain_id': 'fixture-chain', 'workload_id': 'demo',
            'deployment_version': 'fixture-v1', 'input': {'function_id': 'a', 'node': '/demo/a'},
            'output': {'function_id': 'b', 'node': '/demo/b'}, 'deadline_ns': None}


def event(sample, kind, timestamp, **kwargs):
    return dict(sample_id=sample, type=kind, monotonic_ns=timestamp, pid=123, starttime_ticks=50,
                function_id='a' if kind == 'input' else 'b', node='/demo/a' if kind == 'input' else '/demo/b',
                valid=True, reason=None, **kwargs)


def export(events):
    return {'format_version': 1, 'kind': 'robot_business_events',
            'source': {'adapter': 'application_events_v1', 'tool_version': 'unit-fixture-1',
                       'raw_sha256': hashlib.sha256(b'raw fixture').hexdigest(), 'synthetic': True},
            'context': dict(CONTEXT, ros_domain_id=37),
            'window': {'start_ns': 100, 'end_ns': 200, 'expected_inputs': len({e['sample_id'] for e in events if e['type']=='input'}),
                       'dropped_events': 0}, 'events': events}


class BusinessEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        monitor_fixture(self.root/'monitor')
        p = self.root/'monitor/workload-profile.json'
        value = json.loads(p.read_text()); value['workload']['workload_version'] = 'fixture-v1'
        value['workload_sha256'] = hashlib.sha256(json.dumps(value['workload'],sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        p.write_text(json.dumps(value)); self.monitor = b.load_monitor(self.root/'monitor')
        self.events = export([event('one','input',120), event('one','output',130)])
        self.config = chain()

    def analyze(self, events=None, config=None):
        value = events if events is not None else self.events
        config = config if config is not None else self.config
        b.validate_events(value); b.validate_chain(config,self.monitor)
        return b.analyze(self.monitor,config,value)

    def import_files(self, output=None):
        for name,value in (('chain.json',self.config),('events.json',self.events)):
            (self.root/name).write_text(json.dumps(value))
        (self.root/'raw.bin').write_bytes(b'raw fixture')
        return b.run_business_events(self.root/'monitor',self.root/'chain.json',self.root/'events.json',
                                     self.root/'raw.bin',output or self.root/'import')

    def test_known_quantiles_throughput_and_shared_resource_reference(self):
        rows=[]
        for i,delay in enumerate((1,2,3,10,20)):
            rows += [event(str(i),'input',120+i),event(str(i),'output',120+i+delay)]
        result,associated,_=self.analyze(export(rows))
        self.assertEqual([result['e2e_ns'][k] for k in ('p50','p95','p99','max')],[3,20,20,20])
        self.assertEqual(result['throughput']['completed_valid_per_second'],50_000_000)
        self.assertEqual(result['mapping']['resource_refs'],[REF])
        self.assertTrue(all(r['resource_refs']==[REF] for r in associated))
        self.assertNotIn('cpu_percent_one_core',json.dumps(result)); self.assertNotIn('rss_peak_bytes',json.dumps(result))
        self.assertEqual(result['business_acceptance'],'not_evaluated')
        self.assertEqual(result['mapping']['status'],'operator_declared')

    def test_unfinished_drop_invalid_delivery_and_orphan_stay_separate(self):
        dropped=event('drop','drop',140); dropped['reason']='queue rejected'
        invalid=event('invalid','output',140); invalid.update(valid=False,reason='bad payload')
        rows=[event('unfinished','input',120), event('drop','input',120), dropped,
              event('invalid','input',120),invalid,event('orphan','output',150)]
        result,_,samples=self.analyze(export(rows))
        for key in ('unfinished','explicit_drop','invalid_delivery','invalid_sample'):
            self.assertEqual(result['counts'][key],1)
        self.assertIsNone(result['e2e_ns']['p99'])
        self.assertEqual(result['counts']['observed_input_ids'],3)
        self.assertEqual(result['outcome_fractions']['unfinished'],1/3)
        self.assertEqual(result['counts']['orphan_sample_ids'],1)
        self.assertEqual(result['outcome_fractions']['invalid_sample'],0)
        self.assertEqual(sum(result['outcome_fractions'].values()),1)
        self.assertEqual(len(samples),4)

    def test_duplicate_and_conflicting_events_quarantine_entire_sample(self):
        for extra in (event('one','input',120),event('one','output',131),dict(event('one','drop',135),reason='drop')):
            with self.subTest(extra=extra):
                result,_,_=self.analyze(export(self.events['events']+[extra]))
                self.assertEqual(result['counts']['invalid_sample'],1)
                self.assertEqual(result['e2e_ns']['n'],0)
                self.assertEqual(result['quality']['status'],'partial')

    def test_unsorted_arrival_is_allowed_but_reversed_time_is_invalid(self):
        result,_,_=self.analyze(export(list(reversed(self.events['events']))))
        self.assertEqual(result['e2e_ns']['p99'],10)
        value=copy.deepcopy(self.events); value['events'][1]['monotonic_ns']=119
        self.assertEqual(self.analyze(value)[0]['counts']['invalid_delivery'],1)

    def test_boot_namespace_domain_version_and_monitor_window_mismatch_are_unresolved(self):
        for key,value in (('boot_id',CONTEXT['boot_id'][:-1]+'2'),('pid_namespace','pid:[2]'),('ros_domain_id',38)):
            with self.subTest(key=key):
                evidence=copy.deepcopy(self.events); evidence['context'][key]=value
                result,associated,_=self.analyze(evidence)
                self.assertEqual(result['e2e_ns']['n'],0); self.assertEqual(result['counts']['unresolved'],1)
                self.assertTrue(all(r['status']=='unresolved' for r in associated))
        self.monitor['workload']['workload_version']='other'
        self.assertIn('deployment_version_unrecorded_or_mismatch',self.analyze()[0]['quality']['reasons'])
        self.monitor['workload']['workload_version']='fixture-v1'
        self.monitor['status']['window_end_ns']=199
        self.assertIn('event_window_outside_monitor',self.analyze()[0]['quality']['reasons'])

    def test_pid_reuse_scope_reference_window_missing_and_ambiguous_resource_do_not_bind(self):
        for key,value in (('pid',124),('starttime_ticks',51),('monotonic_ns',109),('function_id','b')):
            evidence=copy.deepcopy(self.events); evidence['events'][0][key]=value
            with self.subTest(key=key): self.assertEqual(self.analyze(evidence)[0]['counts']['unresolved'],1)
        self.monitor['entities']={}
        self.assertEqual(self.analyze()[0]['counts']['unresolved'],1)
        self.monitor=b.load_monitor(self.root/'monitor')
        other='pid=123:registration=2:start=50'
        self.monitor['entities'][other]=dict(self.monitor['entities'][REF])
        for role in self.monitor['summary']['workload']['functions']:
            role['reference_observations'][other]=dict(role['reference_observations'][REF])
        self.assertIn('resource_identity_ambiguous',self.analyze()[1][0]['reasons'])

    def test_resource_key_and_entity_identity_must_agree(self):
        wrong='pid=999:registration=1:start=999'
        self.monitor['entities'][wrong]=self.monitor['entities'].pop(REF)
        for role in self.monitor['summary']['workload']['functions']:
            role['reference_observations'][wrong]=role['reference_observations'].pop(REF)
        self.assertEqual(self.analyze()[0]['counts']['unresolved'],1)

    def test_fixed_mature_deadline_cohort_includes_missing_and_excludes_boundary_pairs_equally(self):
        self.config['deadline_ns']=20
        rows=[event('on-time','input',120),event('on-time','output',140),
              event('late','input',120),event('late','output',141),event('missing','input',150),
              event('boundary-complete','input',185),event('boundary-complete','output',190),
              event('boundary-missing','input',185)]
        result,_,samples=self.analyze(export(rows))
        self.assertEqual(result['deadline']['assessed_inputs'],3)
        self.assertEqual(result['deadline']['missed_inputs'],2)
        self.assertEqual(result['deadline']['miss_fraction'],2/3)
        self.assertEqual(result['deadline']['unassessed_inputs'],2)
        self.assertEqual(result['deadline']['status'],'observed')
        self.assertTrue(all(not s['deadline_assessed'] for s in samples if s['sample_id'].startswith('boundary')))

    def test_unknown_event_coverage_does_not_enable_deadline_evaluation(self):
        self.config['deadline_ns']=5
        for key,value in (('expected_inputs',None),('expected_inputs',2),('dropped_events',None),('dropped_events',1)):
            evidence=copy.deepcopy(self.events); evidence['window'][key]=value
            with self.subTest(key=key,value=value):
                result,_,_=self.analyze(evidence)
                self.assertEqual(result['deadline']['status'],'not_evaluated')
                self.assertEqual(result['e2e_ns']['p99'],10)
        self.assertEqual(self.analyze()[0]['deadline']['missed_inputs'],1)

    def test_empty_window_has_null_distributions_and_no_success(self):
        result,_,_=self.analyze(export([]))
        self.assertEqual(result['e2e_ns']['n'],0); self.assertIsNone(result['e2e_ns']['p99'])
        self.assertIsNone(result['outcome_fractions']['unfinished'])
        self.assertIn('no_observed_inputs',result['quality']['reasons'])

    def test_protocol_rejects_unknown_fields_bool_integer_unbounded_or_nonmonotonic_context(self):
        for target,key,value in (('','format_version',True),('context','clock','realtime'),
                 ('window','expected_inputs',True),('window','end_ns',100),('source','synthetic','false')):
            evidence=copy.deepcopy(self.events); (evidence[target] if target else evidence)[key]=value
            with self.subTest(target=target,key=key),self.assertRaises(ValueError): b.validate_events(evidence)
        for key,value in (('pid',True),('pid',2**63),('sample_id','x'*129),('monotonic_ns',201),('type','callback'),('extra',1)):
            evidence=copy.deepcopy(self.events); evidence['events'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): b.validate_events(evidence)
        for key,value in (('deadline_ns',0),('deadline_ns',2**63),('deployment_version',''),('extra',1)):
            config=copy.deepcopy(self.config); config[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): b.validate_chain(config,self.monitor)

    def test_import_preserves_all_input_bytes_hashes_and_refuses_source_mutation_or_overwrite(self):
        before={p.name:p.read_bytes() for p in (self.root/'monitor').iterdir()}
        self.import_files()
        output=self.root/'import'; original={p.name:p.read_bytes() for p in output.iterdir()}
        self.assertEqual((output/'raw-source.bin').read_bytes(),b'raw fixture')
        self.assertEqual((output/'events.json').read_bytes(),(self.root/'events.json').read_bytes())
        self.assertEqual(json.loads((output/'business-status.json').read_text())['status'],'complete')
        with self.assertRaises(FileExistsError): self.import_files()
        self.assertEqual(original,{p.name:p.read_bytes() for p in output.iterdir()})
        with self.assertRaisesRegex(ValueError,'separate'): self.import_files(self.root/'monitor/nested')
        self.assertEqual(before,{p.name:p.read_bytes() for p in (self.root/'monitor').iterdir()})

    def test_wrong_raw_digest_and_invalid_json_rejected_before_output_creation(self):
        self.events['source']['raw_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'SHA256'): self.import_files()
        self.assertFalse((self.root/'import').exists())
        p=self.root/'duplicate.json'; p.write_text('{"format_version":1,"format_version":1}')
        with self.assertRaisesRegex(ValueError,'duplicate'): b._read(p)

    def test_invalid_context_unicode_and_overflow_numbers_rejected_before_output_creation(self):
        p=self.root/'monitor/environment.json'; original=p.read_bytes()
        for context in (['unexpected'],[],False,'bad'):
            value=json.loads(original); value['observation_context']=context
            p.write_text(json.dumps(value))
            with self.subTest(context=context),self.assertRaisesRegex(ValueError,'observation_context'): self.import_files()
            self.assertFalse((self.root/'import').exists())
        p.write_bytes(original)
        self.config['chain_id']='\ud800'
        with self.assertRaisesRegex(ValueError,'UTF-8'): self.import_files()
        self.assertFalse((self.root/'import').exists())
        with self.assertRaisesRegex(ValueError,'UTF-8'): b.validate_chain(self.config,self.monitor)
        self.config=chain()
        p=self.root/'monitor/monitor-summary.json'; text=p.read_text()
        p.write_text(text.replace('25','1e309'))
        with self.assertRaisesRegex(ValueError,'nonfinite'): self.import_files()
        self.assertFalse((self.root/'import').exists())
        p.write_text(text)
        for name in ('chain.json','events.json'):
            p=self.root/name; p.write_text('{"unused":1e309}')
            with self.assertRaisesRegex(ValueError,'nonfinite'): b._read(p)

    def test_changed_source_between_strict_and_legacy_reads_is_rejected(self):
        original=b._load_monitor
        def changed(directory):
            p=Path(directory)/'environment.json'
            value=json.loads(p.read_text()); value['added']='changed'; p.write_text(json.dumps(value))
            return original(directory)
        with patch.object(b,'_load_monitor',side_effect=changed),self.assertRaisesRegex(ValueError,'changed'): self.import_files()
        self.assertFalse((self.root/'import').exists())

    def test_fifo_and_directory_inputs_rejected_without_reading_or_waiting(self):
        fifo=self.root/'fifo'; os.mkfifo(fifo)
        for read in (b._raw_source,b._read):
            for path in (fifo,self.root):
                with self.subTest(path=path,read=read),self.assertRaises((ValueError,OSError)): read(path)

    def test_error_interrupt_and_secondary_status_failure_preserve_original_and_raw_evidence(self):
        for index,error in enumerate((RuntimeError('original failure'),KeyboardInterrupt('cancel'))):
            output=self.root/str(index); write=b._json
            def fail_final(path,value):
                if path.name=='business-status.json' and value['status']!='running': raise OSError('secondary status failure')
                return write(path,value)
            with patch.object(b,'analyze',side_effect=error),patch.object(b,'_json',side_effect=fail_final):
                with self.assertRaises(type(error)) as caught: self.import_files(output)
            self.assertIs(caught.exception,error)
            self.assertEqual((output/'raw-source.bin').read_bytes(),b'raw fixture')

    def test_final_status_post_write_fault_corrected_to_failed(self):
        write=b._json; sent=[]
        def fault(path,value):
            write(path,value)
            if path.name=='business-status.json' and value['status']=='complete' and not sent:
                sent.append(True); raise OSError('post-complete write fault')
        with patch.object(b,'_json',side_effect=fault),self.assertRaises(OSError): self.import_files()
        status=json.loads((self.root/'import/business-status.json').read_text())
        self.assertEqual(status['status'],'failed'); self.assertEqual(status['error'],'post-complete write fault')
