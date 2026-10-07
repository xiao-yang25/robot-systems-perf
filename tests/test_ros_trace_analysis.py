"""Explicit synthetic parser fixtures; not onsite traces or business events."""
from argparse import Namespace
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from scripts.ros_trace_analysis import analyze, distribution
from scripts import analyze_ros_trace as cli

BOOT='00000000-0000-0000-0000-000000000001'


def fixture():
    data={'format_version':1,'kind':'robot_ros_trace_events','synthetic':True,
          'source':{'adapter':'test_fixture','tool_version':'unit-data-v1'},
          'clocks':{'c':{'name':'monotonic','frequency':10**9,'offset_seconds':1700000000,
                         'offset_cycles':100,'origin_is_unix_epoch':True,'uuid':BOOT}},'events':[]}
    history={'format_version':1,'kind':'robot_ros_trace_history','identities':[
        {'history_id':'p','vpid':10,'starttime_ticks':900,'boot_id':BOOT,'pid_namespace_inode':101,'procname':'test',
         'scope':{'kind':'capture_reserved_pid'},'evidence':['synthetic unit fixture']} ]}
    def emit(name,cycles,payload=None,**context):
        data['events'].append({'index':len(data['events']),'name':'ros2:'+name,'cycles':cycles,'clock_id':'c',
            'context':dict(vpid=10,vtid=11,pid_ns=101,procname='test',**context), 'payload':payload or {}})
    emit('rcl_publisher_init',1,dict(publisher_handle=2,node_handle=1,topic_name='/data'))
    emit('rcl_node_init',2,dict(node_handle=1,node_name='node',namespace='/'))
    emit('rcl_subscription_init',3,dict(subscription_handle=3,node_handle=1,topic_name='/data'))
    emit('rclcpp_subscription_init',4,dict(subscription_handle=3,subscription=4))
    emit('rclcpp_subscription_callback_added',5,dict(subscription=4,callback=5))
    return data,history,emit


def pair(emit,start=10,end=20,callback=5):
    emit('callback_start',start,{'callback':callback,'is_intra_process':0})
    emit('callback_end',end,{'callback':callback})


def channel_loss(count=0):
    return {'channel_discarded_events':[{'count_type':'channel_discarded_events','count':count,'channel':'ros',
        'domain':'userspace','observation_phase':'after_successful_stop','source':{'path':'test','sha256':'0'*64},
        'coverage':'synthetic test channel buffer'}], 'decoder_discarded_events':{'count':None},
        'business_exporter_dropped_events':None}


class ModelTests(unittest.TestCase):
    def test_nearest_rank_known_values_empty_and_exact_fraction(self):
        result=distribution(range(1,101))
        self.assertEqual([result[k] for k in ('p50_ns','p95_ns','p99_ns','max_ns')],[50,95,99,100])
        self.assertIsNone(distribution([])['p99_ns'])
        self.assertEqual(distribution([Fraction(1,3)])['p99_ns_exact'],{'numerator':1,'denominator':3})

    def test_epoch_offset_never_used_in_interval(self):
        data,history,emit=fixture(); pair(emit,10**18,10**18+9)
        result=analyze(data,history,channel_loss())
        self.assertEqual(result['callback_interval']['p99_ns'],9)
        self.assertIsNone(result['business_e2e']); self.assertIsNone(result['budget'])
        self.assertEqual(result['clock_bridge']['status'],'unconfirmed')
        self.assertEqual(result['business_acceptance'],'not_evaluated')

    def test_out_of_order_parent_registration_valid_only_at_use(self):
        data,history,emit=fixture(); emit('rcl_publish',6,{'publisher_handle':2,'message':99})
        for value in (7,8): emit('rcl_publish',value,{'publisher_handle':2,'message':99})
        result=analyze(data,history)
        self.assertEqual(result['publications'][0]['rcl_publish_count'],3)
        self.assertEqual(result['publications'][0]['distinct_message_addresses'],1)
        self.assertFalse(result['publications'][0]['message_address_is_sample_id'])
        bad=deepcopy(data)
        # Publisher init is observed, parent init occurs after the first use.
        row=bad['events'].pop(1); bad['events'].append(row)
        for i,event in enumerate(bad['events']): event['index']=i
        result=analyze(bad,history)
        self.assertFalse(result['publications'])
        self.assertTrue(any(r['reason']=='missing_initialization_at_use' for r in result['unresolved_events']))

    def test_rclcpp_publish_without_publisher_handle_remains_unclassified(self):
        data,history,emit=fixture(); emit('rclcpp_publish',7,{'message':99})
        result=analyze(data,history)
        self.assertEqual(result['event_counts']['ros2:rclcpp_publish'],1)
        self.assertEqual(result['publications'],[])

    def test_same_handles_and_addresses_in_different_pid_never_join(self):
        data,history,emit=fixture(); pair(emit)
        other=dict(history['identities'][0],history_id='other',vpid=20,starttime_ticks=901)
        history['identities'].append(other)
        # An end from another PID must not match the original start.
        data['events'][-1]['context']['vpid']=20
        result=analyze(data,history)
        self.assertEqual(result['counts']['paired_intervals'],0)
        self.assertEqual(result['counts']['unpaired_events'],2)
        emit('rcl_publish',25,{'publisher_handle':2,'message':99})
        data['events'][-1]['context']['vpid']=20
        self.assertFalse(analyze(data,history)['publications'])

    def test_namespace_starttime_boot_and_procname_mismatch_unresolved(self):
        for key,value in [('pid_ns',102),('starttime_ticks',901),('boot_id','other'),('procname','other')]:
            with self.subTest(key=key):
                data,history,emit=fixture(); pair(emit); data['events'][-1]['context'][key]=value
                result=analyze(data,history)
                self.assertEqual(result['counts']['paired_intervals'],0)
                self.assertTrue(result['unresolved_events'])

    def test_historical_pid_reuse_requires_disjoint_ctf_scopes(self):
        data,history,emit=fixture(); pair(emit)
        first=history['identities'][0]; first['scope']={'kind':'ctf_cycle_range','clock_id':'c','start_cycle':0,'end_cycle':30}
        second=deepcopy(first); second.update(history_id='restart',starttime_ticks=901)
        second['scope'].update(start_cycle=30,end_cycle=50); history['identities'].append(second)
        pair(emit,31,32)
        result=analyze(data,history)
        self.assertEqual(result['counts']['paired_intervals'],1)
        self.assertIn('missing_initialization_at_use',[r['reason'] for r in result['invalid_intervals']])
        second['scope'].update(start_cycle=0)
        self.assertGreater(analyze(data,history)['counts']['unresolved_events'],0)

    def test_missing_and_conflicting_registration(self):
        data,history,emit=fixture(); pair(emit)
        data['events'][4]['payload']['subscription']=999
        self.assertEqual(analyze(data,history)['counts']['paired_intervals'],0)
        data,history,emit=fixture(); pair(emit)
        emit('rclcpp_subscription_callback_added',21,dict(subscription=4,callback=5)); pair(emit,22,23)
        result=analyze(data,history)
        self.assertEqual(result['counts']['paired_intervals'],1)
        self.assertEqual(result['invalid_intervals'][0]['reason'],'handle_conflict_or_reuse_without_retirement')
        self.assertTrue(any(row['status']=='conflict_or_reuse_unresolved' for row in result['objects']))

    def test_registration_timestamp_after_use_invalid(self):
        data,history,emit=fixture(); data['events'][4]['cycles']=50; pair(emit)
        self.assertEqual(analyze(data,history)['invalid_intervals'][0]['reason'],'registration_clock_or_use_time_invalid')

    def test_cross_thread_end_never_repaired(self):
        data,history,emit=fixture(); pair(emit); data['events'][-1]['context']['vtid']=12
        result=analyze(data,history)
        self.assertEqual(result['counts']['paired_intervals'],0)
        self.assertIn('cross_thread_end',[r['reason'] for r in result['unpaired']])

    def test_missing_start_and_end_window_truncation(self):
        data,history,emit=fixture(); emit('callback_end',9,{'callback':5}); emit('callback_start',10,{'callback':5})
        result=analyze(data,history)
        self.assertEqual(result['counts']['unpaired_events'],2)
        self.assertEqual(result['callback_interval']['samples'],0)

    def test_reentrant_same_callback_lifo_inclusive_intervals(self):
        data,history,emit=fixture()
        for name,t in [('callback_start',10),('callback_start',11),('callback_end',13),('callback_end',15)]: emit(name,t,{'callback':5})
        result=analyze(data,history)
        self.assertEqual(sorted(r['duration_ns'] for r in result['intervals']),[2,5])
        self.assertEqual(sorted(r['nested_depth'] for r in result['intervals']),[0,1])

    def test_crossed_end_invalidates_open_stack(self):
        data,history,emit=fixture(); emit('rclcpp_subscription_callback_added',6,dict(subscription=4,callback=6))
        for cb,name,t in [(5,'callback_start',10),(6,'callback_start',11),(5,'callback_end',12),(6,'callback_end',13)]: emit(name,t,{'callback':cb})
        result=analyze(data,history)
        self.assertEqual(result['counts']['paired_intervals'],0)
        self.assertEqual(result['counts']['unpaired_events'],4)

    def test_negative_missing_and_different_clock_rejected_from_distribution(self):
        data,history,emit=fixture(); pair(emit,20,10)
        self.assertEqual(analyze(data,history)['invalid_intervals'][0]['reason'],'negative_interval')
        for cid in (None,'missing','different'):
            data,history,emit=fixture(); pair(emit)
            data['clocks']['different']=dict(data['clocks']['c'])
            data['events'][-1]['clock_id']=cid
            result=analyze(data,history)
            self.assertEqual(result['counts']['paired_intervals'],0)
            self.assertIn(result['invalid_intervals'][0]['reason'],('missing_clock','clock_class_mismatch'))

    def test_integer_schema_never_accepts_equal_float_or_bool(self):
        for value in (10.0,True,-1,1<<64):
            data,history,emit=fixture(); history['identities'][0]['starttime_ticks']=value
            with self.assertRaises(ValueError): analyze(data,history)
        data,history,emit=fixture(); data['synthetic']=False
        with self.assertRaises(ValueError): analyze(data,history)

    def test_missing_nonzero_and_distinct_loss_scopes(self):
        data,history,emit=fixture(); pair(emit)
        unknown=analyze(data,history)
        self.assertIn('channel_loss_evidence_missing',unknown['quality']['reasons'])
        for count in (0,2):
            result=analyze(data,history,channel_loss(count))
            self.assertEqual('nonzero_scoped_loss' in result['quality']['reasons'],count>0)
            self.assertIsNone(result['loss']['business_exporter_dropped_events'])
            self.assertEqual(result['loss']['channel_discarded_events'][0]['observation_phase'],'after_successful_stop')
            self.assertEqual(result['counts']['paired_intervals'],1)


LIST='''Tracing session own: [inactive]
=== Domain: User space ===
Channels:
- ros: [enabled]
    Statistics:
        Discarded events: 0
- other: [enabled]
    Statistics:
        Discarded events: 9
'''


class OfflineCLITests(unittest.TestCase):
    def test_loss_parser_requires_exact_session_stop_domain_and_channel(self):
        rows,reason=cli.parse_channel_list(LIST,'own',{'path':'synthetic','sha256':'0'*64})
        self.assertEqual(rows[0]['count'],0); self.assertIsNone(reason)
        for text,session in [(LIST,'other'),(LIST.replace('[inactive]','[active]'),'own'),
                              (LIST.replace('User space','Kernel'),'own'),(LIST.replace('Discarded events: 0','Discarded packets: 0'),'own')]:
            self.assertEqual(cli.parse_channel_list(text,session,{})[0],[])

    def test_normalized_cli_preserves_inputs_and_refuses_overwrite(self):
        data,history,emit=fixture(); pair(emit)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'events.json'; context=root/'history.json'
            source.write_text(json.dumps(data)); context.write_text(json.dumps(history)); before=source.read_bytes()
            args=Namespace(capture=None,events=source,history=context,output=root/'out',bt2_python='',timeout_seconds=1)
            self.assertEqual(cli.execute(args),0)
            result=json.loads((args.output/'callback-analysis.json').read_text())
            self.assertEqual(result['callback_interval']['samples'],1)
            self.assertTrue(result['synthetic']); self.assertEqual(source.read_bytes(),before)
            self.assertEqual(cli.execute(args),1)
            self.assertEqual(source.read_bytes(),before)

    def test_source_fifo_and_duplicates_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); file=root/'duplicate.json'; file.write_text('{"a":1,"a":2}')
            with self.assertRaises(ValueError): cli.Sources().read(file)
            import os
            os.mkfifo(root/'fifo')
            with self.assertRaises(ValueError): cli.Sources().read(root/'fifo')

    def test_cancel_status_and_original_error_independent_of_final_evidence(self):
        data,history,emit=fixture(); pair(emit)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'events').write_text(json.dumps(data)); (root/'history').write_text(json.dumps(history))
            args=Namespace(capture=None,events=root/'events',history=root/'history',output=root/'out',bt2_python='',timeout_seconds=1)
            original=cli.save
            def fail(path,value):
                if path.name=='source-evidence.json': raise OSError('derived failure')
                original(path,value)
            with patch.object(cli,'save',side_effect=fail), patch.object(cli,'analyze',side_effect=KeyboardInterrupt('cancel')):
                self.assertEqual(cli.execute(args),130)
            state=json.loads((args.output/'analysis-status.json').read_text())
            self.assertEqual(state['status'],'interrupted'); self.assertIn('cancel',state['primary_error'])
            self.assertTrue(state['cleanup_errors'])

    def test_real_sigterm_during_final_state_and_derived_write_failure(self):
        import os
        for target in ('analysis-status.json','source-evidence.json'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                data,history,emit=fixture(); pair(emit)
                root=Path(directory); (root/'events').write_text(json.dumps(data)); (root/'history').write_text(json.dumps(history))
                args=Namespace(capture=None,events=root/'events',history=root/'history',output=root/'out',bt2_python='',timeout_seconds=1)
                original=cli.save; sent=[]
                def fail(path,value):
                    if path.name==target and (target!='analysis-status.json' or value['status']=='complete') and not sent:
                        sent.append(True); os.kill(os.getpid(),signal.SIGTERM); raise OSError('secondary final save fault')
                    original(path,value)
                handler=signal.getsignal(signal.SIGTERM)
                try:
                    cli.install_signal_handler()
                    with patch.object(cli,'save',side_effect=fail): self.assertEqual(cli.execute(args),130)
                finally: signal.signal(signal.SIGTERM,handler)
                state=json.loads((args.output/'analysis-status.json').read_text())
                self.assertEqual(state['status'],'interrupted')
                self.assertTrue(state['cleanup_errors']); self.assertIn('KeyboardInterrupt',state['interruption_error'])

    def test_missing_loss_diagnostic_and_decoder_unknown_or_packet_scope(self):
        data,history,emit=fixture(); pair(emit)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index,op in enumerate(('stop','list')):
                folder=root/('control-%02d'%index); folder.mkdir()
                (folder/'command.json').write_text(json.dumps(['lttng','--no-sessiond',op,'own']))
                (folder/'result.json').write_text(json.dumps({'start_ns':index+1,'end_ns':index+2,'returncode':0,'error':None}))
            loss=cli.loss_evidence(root,{'session_name':'own'},data,cli.Sources())
            self.assertEqual(loss['channel_discarded_events'],[])
            self.assertIn('unavailable',loss['channel_discarded_events_reason'])
        for message in [{'type':'discarded_events','count':None},{'type':'discarded_events','count':4},{'type':'discarded_packets','count':1}]:
            data['decoder_loss_messages']=[message]
            loss=cli.loss_evidence(None,None,data,cli.Sources())
            result=analyze(data,history,loss)
            self.assertEqual(result['quality']['status'],'partial')
            self.assertIsNone(result['loss']['business_exporter_dropped_events'])


if __name__=='__main__': unittest.main()
