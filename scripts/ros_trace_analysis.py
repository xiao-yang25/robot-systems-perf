"""Strict offline ROS callback intervals; no CTF decoder or live /proc access.

Normalized decoder output is descriptive framework evidence, never business events.
"""
from collections import Counter, defaultdict
from fractions import Fraction
import re

MAX_EVENTS = 100000
MAX_HANDLE = (1 << 64)-1
UUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')
REGISTRATIONS = {
    'ros2:rcl_node_init': ('node', 'node_handle'),
    'ros2:rcl_publisher_init': ('publisher', 'publisher_handle'),
    'ros2:rcl_subscription_init': ('subscription', 'subscription_handle'),
    'ros2:rclcpp_subscription_init': ('cpp_subscription', 'subscription'),
    'ros2:rclcpp_subscription_callback_added': ('callback', 'callback'),
}


def integer(value, low=0, high=MAX_HANDLE):
    return type(value) is int and low <= value <= high


def validate_inputs(data, history):
    if not isinstance(data, dict) or data.get('format_version') != 1 or type(data['format_version']) is not int or data.get('kind') != 'robot_ros_trace_events':
        raise ValueError('unsupported normalized trace format')
    if type(data.get('synthetic')) is not bool:
        raise ValueError('explicit synthetic flag required')
    source = data.get('source', {})
    if not isinstance(source,dict) or source.get('adapter') not in ('bt2', 'test_fixture') or not isinstance(source.get('tool_version'), str) or not source['tool_version'].strip():
        raise ValueError('decoder adapter/version required')
    if source['adapter'] == 'test_fixture' and not data['synthetic']:
        raise ValueError('test fixture must be synthetic')
    clocks = data.get('clocks')
    if not isinstance(clocks, dict) or len(clocks)>1024:
        raise ValueError('bounded clock class map required')
    for cid, clock in clocks.items():
        if not isinstance(cid, str) or not isinstance(clock, dict) or not integer(clock.get('frequency'), 1, 10**12):
            raise ValueError('clock class/frequency invalid')
        if not isinstance(clock.get('name'), (str, type(None))) or not integer(clock.get('offset_cycles')) or type(clock.get('offset_seconds')) is not int or type(clock.get('origin_is_unix_epoch')) is not bool:
            raise ValueError('clock metadata incomplete')
        if clock.get('uuid') is not None and not UUID.fullmatch(str(clock['uuid'])):
            raise ValueError('clock UUID invalid')
    messages=data.get('decoder_loss_messages',[])
    if not isinstance(messages,list) or len(messages)>MAX_EVENTS or any(not isinstance(row,dict) or row.get('type') not in ('discarded_events','discarded_packets') or row.get('count') is not None and not integer(row['count']) for row in messages):
        raise ValueError('decoder loss messages invalid')
    events = data.get('events')
    if not isinstance(events, list) or len(events)>MAX_EVENTS:
        raise ValueError('bounded decoded event list required')
    for index, row in enumerate(events):
        if not isinstance(row, dict) or row.get('index') != index or type(row.get('index')) is not int or not isinstance(row.get('name'), str) or not row['name'].startswith('ros2:'):
            raise ValueError('event index/name invalid')
        if not isinstance(row.get('context'), dict) or not isinstance(row.get('payload'), dict):
            raise ValueError('event context/payload required')
        if row.get('cycles') is not None and not integer(row['cycles']):
            raise ValueError('clock cycles must be unsigned integers')
        if row.get('clock_id') is not None and not isinstance(row['clock_id'], str):
            raise ValueError('clock_id must be string or null')
    if not isinstance(history, dict) or history.get('kind') != 'robot_ros_trace_history' or type(history.get('format_version')) is not int or history['format_version'] != 1:
        raise ValueError('unsupported historical identity format')
    identities = history.get('identities')
    if not isinstance(identities, list) or not identities or len(identities)>256:
        raise ValueError('1..256 historical identities required')
    ids = set()
    for row in identities:
        if not isinstance(row, dict) or not isinstance(row.get('history_id'), str) or not row['history_id'] or row['history_id'] in ids:
            raise ValueError('distinct history_id required')
        ids.add(row['history_id'])
        if not UUID.fullmatch(str(row.get('boot_id'))) or not integer(row.get('vpid'), 1, 2**31-1) or not integer(row.get('starttime_ticks'), 1) or not integer(row.get('pid_namespace_inode'), 1):
            raise ValueError('historical PID/starttime/boot/namespace invalid')
        if not isinstance(row.get('procname'), str) or not row['procname'] or len(row['procname'].encode())>15:
            raise ValueError('historical procname required (Linux comm limit)')
        scope = row.get('scope', {})
        if not isinstance(scope,dict): raise ValueError('historical scope must be an object')
        if scope.get('kind') == 'ctf_cycle_range':
            if not isinstance(scope.get('clock_id'), str) or not integer(scope.get('start_cycle')) or not integer(scope.get('end_cycle')) or scope['start_cycle']>=scope['end_cycle']:
                raise ValueError('historical CTF scope must be a nonempty half-open cycle range')
        elif scope.get('kind') != 'capture_reserved_pid':
            raise ValueError('historical identity needs capture reservation or explicit CTF range')
        if scope.get('clock_uuid') is not None and not UUID.fullmatch(str(scope['clock_uuid'])):
            raise ValueError('historical clock UUID constraint invalid')
        if not isinstance(row.get('evidence'), list) or not row['evidence']:
            raise ValueError('historical identity evidence references required')
    return data, history


def distribution(values):
    """Exact nearest rank before optional fractional-ns JSON conversion."""
    ordered = sorted(Fraction(value) for value in values)
    def number(value):
        return value.numerator if value.denominator == 1 else float(value)
    def rank(num, den):
        return ordered[(num*len(ordered)+den-1)//den-1]
    result = {'samples': len(ordered), 'unit': 'ns', 'method': 'nearest-rank: sorted[ceil(p*n)-1]; no interpolation'}
    for key, value in [('p50', rank(1,2) if ordered else None), ('p95', rank(95,100) if ordered else None),
                       ('p99', rank(99,100) if ordered else None), ('max', ordered[-1] if ordered else None)]:
        result[key+'_ns'] = number(value) if value is not None else None
        result[key+'_ns_exact'] = {'numerator': value.numerator, 'denominator': value.denominator} if value is not None else None
    return result


class Model:
    def __init__(self, data, history):
        self.clocks = data['clocks']
        self.identities = history['identities']
        self.registry = defaultdict(list)
        self.bound = {}
        self.issues = []
        self.events = data['events']
        for row in self.events:
            self.bound[row['index']] = self.bind(row)
            registration = REGISTRATIONS.get(row['name'])
            if registration and self.bound[row['index']] is not None:
                kind, field = registration
                handle = row['payload'].get(field)
                if not integer(handle, 1):
                    self.issue(row, 'invalid_registration_handle')
                else:
                    self.registry[(self.bound[row['index']], kind, handle)].append(row)

    def issue(self, row, reason):
        self.issues.append({'event_index': row['index'], 'event': row['name'], 'reason': reason})

    def bind(self, row):
        ctx = row['context']
        if not all(integer(ctx.get(k), 1, 2**31-1 if k in ('vpid','vtid') else MAX_HANDLE) for k in ('vpid','vtid','pid_ns')) or not isinstance(ctx.get('procname'), str):
            self.issue(row, 'missing_or_invalid_process_context')
            return None
        candidates = []
        for identity in self.identities:
            if (ctx['vpid'], ctx['pid_ns'], ctx['procname']) != (identity['vpid'], identity['pid_namespace_inode'], identity['procname']):
                continue
            if 'starttime_ticks' in ctx and (not integer(ctx['starttime_ticks'],1) or ctx['starttime_ticks'] != identity['starttime_ticks']):
                continue
            if 'boot_id' in ctx and ctx['boot_id'] != identity['boot_id']:
                continue
            scope = identity['scope']
            if scope['kind']=='ctf_cycle_range' and (row['clock_id'] != scope['clock_id'] or row['cycles'] is None or not scope['start_cycle']<=row['cycles']<scope['end_cycle']):
                continue
            expected_uuid = scope.get('clock_uuid')
            if expected_uuid is not None and self.clocks.get(row['clock_id'], {}).get('uuid') != expected_uuid:
                continue
            candidates.append(identity['history_id'])
        if len(candidates) != 1:
            self.issue(row, 'historical_identity_unresolved' if not candidates else 'historical_identity_ambiguous')
            return None
        return candidates[0]

    def object_at(self, identity, kind, handle, use):
        if not integer(handle, 1) or use['clock_id'] not in self.clocks or use['cycles'] is None:
            return None, 'missing_handle_or_clock'
        rows = self.registry.get((identity,kind,handle), [])
        # Register references first, evaluate graph edges only at a use. A later
        # node registration can complete publisher metadata, never an earlier use.
        previous = [row for row in rows if row['index']<=use['index']]
        if not previous:
            return None, 'missing_initialization_at_use'
        if len(previous)>1:
            return None, 'handle_conflict_or_reuse_without_retirement'
        row = previous[0]
        if row['clock_id']!=use['clock_id'] or row['cycles'] is None or row['cycles']>use['cycles']:
            return None, 'registration_clock_or_use_time_invalid'
        return row['payload'], None

    def resolve(self, identity, kind, handle, use):
        payload, reason = self.object_at(identity,kind,handle,use)
        if reason:
            return None, reason
        if kind=='node':
            name, ns = payload.get('node_name'), payload.get('namespace')
            if not isinstance(name,str) or not name or not isinstance(ns,str) or not ns.startswith('/'):
                return None, 'invalid_node_metadata'
            return {'node_handle':handle, 'node_name':name, 'namespace':ns}, None
        if kind in ('publisher','subscription'):
            node, reason = self.resolve(identity,'node',payload.get('node_handle'),use)
            if reason: return None, reason
            topic = payload.get('topic_name')
            if not isinstance(topic,str) or not topic.startswith('/'):
                return None, 'invalid_topic_metadata'
            return dict(node, **{kind+'_handle':handle}, topic=topic), None
        if kind=='cpp_subscription':
            sub, reason = self.resolve(identity,'subscription',payload.get('subscription_handle'),use)
            return (dict(sub, cpp_subscription=handle), None) if sub is not None else (None, reason)
        if kind=='callback':
            sub, reason = self.resolve(identity,'cpp_subscription',payload.get('subscription'),use)
            return (dict(sub, callback=handle), None) if sub is not None else (None, reason)
        return None, 'unsupported_object_kind'


def analyze(data, history, loss=None):
    validate_inputs(data,history)
    model = Model(data,history)
    stacks = defaultdict(list)
    intervals, invalid, unpaired = [], [], []
    publications = defaultdict(list)
    names = Counter(row['name'] for row in data['events'])
    def unpair(row, reason):
        unpaired.append({'event_index':row['index'], 'event':row['name'], 'reason':reason})
    for row in data['events']:
        identity = model.bound[row['index']]
        if identity is None:
            if row['name'] in ('ros2:callback_start','ros2:callback_end'):
                unpair(row,'historical_identity_unresolved')
            continue
        if row['name']=='ros2:rcl_publish':
            mapping, reason = model.resolve(identity,'publisher',row['payload'].get('publisher_handle'),row)
            if reason:
                model.issue(row,reason)
            else:
                key = (identity,mapping['publisher_handle'],mapping['topic'])
                publications[key].append(row)
            continue
        if row['name'] not in ('ros2:callback_start','ros2:callback_end'):
            continue
        callback = row['payload'].get('callback')
        if not integer(callback,1):
            model.issue(row,'invalid_callback_handle'); unpair(row,'invalid_callback_handle'); continue
        key = (identity,row['context']['vtid'])
        stack = stacks[key]
        if row['name']=='ros2:callback_start':
            mapping, reason = model.resolve(identity,'callback',callback,row)
            if reason: model.issue(row,reason)
            stack.append({'row':row,'mapping':mapping,'reason':reason,'depth':len(stack)})
            continue
        if not stack:
            other_thread = any(k[0]==identity and k!=key and any(f['row']['payload'].get('callback')==callback for f in frames) for k,frames in stacks.items())
            unpair(row,'cross_thread_end' if other_thread else 'missing_start_or_window_truncation')
            continue
        if stack[-1]['row']['payload']['callback'] != callback:
            for frame in stack: unpair(frame['row'],'crossed_callback_end')
            stack.clear(); unpair(row,'crossed_callback_end'); continue
        frame = stack.pop(); start = frame['row']
        record = {'history_id':identity,'vtid':key[1],'callback':callback,
                  'start_event_index':start['index'],'end_event_index':row['index'],
                  'start_cycles':start['cycles'],'end_cycles':row['cycles'],
                  'clock_id':start['clock_id'],'nested_depth':frame['depth']}
        reason = None
        if start['clock_id'] not in data['clocks'] or row['clock_id'] not in data['clocks'] or start['cycles'] is None or row['cycles'] is None:
            reason = 'missing_clock'
        elif start['clock_id'] != row['clock_id']:
            reason = 'clock_class_mismatch'
        elif row['cycles'] < start['cycles']:
            reason = 'negative_interval'
        end_mapping, end_reason = model.resolve(identity,'callback',callback,row)
        if reason is None:
            reason = frame['reason'] or end_reason
            if reason is None and frame['mapping'] != end_mapping:
                reason = 'callback_lifecycle_changed_during_interval'
        if reason:
            invalid.append(dict(record,reason=reason))
        else:
            duration = Fraction((row['cycles']-start['cycles'])*10**9, data['clocks'][row['clock_id']]['frequency'])
            intervals.append(dict(record, **frame['mapping'], duration_ns_exact={'numerator':duration.numerator,'denominator':duration.denominator},
                                  duration_ns=duration.numerator if duration.denominator==1 else float(duration)))
    for stack in stacks.values():
        for frame in stack: unpair(frame['row'],'missing_end_or_window_truncation')
    grouped = defaultdict(list)
    for interval in intervals:
        grouped[(interval['history_id'],interval['callback'],interval['clock_id'])].append(interval)
    by_callback = []
    for (identity,callback,cid), rows in sorted(grouped.items()):
        by_callback.append({'history_id':identity,'callback':callback,'clock_id':cid,
            'topic':rows[0]['topic'],'node_name':rows[0]['node_name'],'namespace':rows[0]['namespace'],
            'callback_interval':distribution(Fraction(r['duration_ns_exact']['numerator'],r['duration_ns_exact']['denominator']) for r in rows)})
    pub_rows = [{'history_id':key[0],'publisher_handle':key[1],'topic':key[2],'rcl_publish_count':len(rows),
                 'distinct_message_addresses':len({r['payload']['message'] for r in rows if integer(r['payload'].get('message'),1)}),
                 'missing_message_addresses':sum(not integer(r['payload'].get('message'),1) for r in rows),
                 'message_address_is_sample_id':False} for key,rows in sorted(publications.items())]
    loss = loss or {'channel_discarded_events': [],'channel_discarded_events_reason':'no validated stop/list evidence supplied',
                    'decoder_discarded_events': {'count':None,'reason':'not supplied'}, 'business_exporter_dropped_events':None}
    for entry in loss.get('channel_discarded_events',[]):
        if not isinstance(entry,dict) or entry.get('count_type')!='channel_discarded_events' or not integer(entry.get('count')) or entry.get('channel')!='ros' or entry.get('domain')!='userspace' or entry.get('observation_phase')!='after_successful_stop' or not isinstance(entry.get('source'),dict) or not entry.get('coverage'):
            raise ValueError('invalid scoped channel loss evidence')
    loss_unknown = not loss.get('channel_discarded_events')
    loss_nonzero = any(row['count'] != 0 for row in loss.get('channel_discarded_events',[])) or bool(loss.get('decoder_discarded_events',{}).get('count'))
    reasons = []
    if not intervals: reasons.append('no_valid_callback_intervals')
    if model.issues: reasons.append('unresolved_framework_events')
    if invalid: reasons.append('invalid_or_unresolved_intervals')
    if unpaired: reasons.append('unpaired_callback_events')
    if loss_unknown: reasons.append('channel_loss_evidence_missing')
    if loss_nonzero: reasons.append('nonzero_scoped_loss')
    if loss.get('decoder_discarded_events',{}).get('messages') and loss['decoder_discarded_events'].get('count') is None:
        reasons.append('decoder_discard_count_unavailable')
    if loss.get('decoder_discarded_packets',{}).get('messages'):
        reasons.append('decoder_reports_packet_loss')
    if any(len(rows)>1 for rows in model.registry.values()):
        reasons.append('object_handle_conflicts')
    objects = [{'history_id':identity,'kind':kind,'handle':handle,'registration_event_indexes':[r['index'] for r in rows],
                'registrations':[{'event_index':r['index'],'clock_id':r['clock_id'],'cycles':r['cycles'],'payload':r['payload']} for r in rows],
                'status':'registered' if len(rows)==1 else 'conflict_or_reuse_unresolved',
                'lifetime_end':'unknown; no supported retirement event'} for (identity,kind,handle),rows in sorted(model.registry.items())]
    return {'format_version':1,'kind':'robot_ros_callback_analysis','synthetic':data['synthetic'],
        'source':data['source'],'history':history,'clocks':data['clocks'], 'event_counts':dict(names),
        'objects':objects,'publications':pub_rows,'callbacks':by_callback,'intervals':intervals,
        'callback_interval':distribution(Fraction(r['duration_ns_exact']['numerator'],r['duration_ns_exact']['denominator']) for r in intervals),
        'unpaired':unpaired,'invalid_intervals':invalid,'unresolved_events':model.issues,
        'counts':{'paired_intervals':len(intervals),'invalid_intervals':len(invalid),'unresolved_intervals':sum(r['reason'] not in ('missing_clock','clock_class_mismatch','negative_interval') for r in invalid),'unpaired_events':len(unpaired),'unresolved_events':len(model.issues)},
        'loss':loss, 'quality':{'status':'partial' if reasons else 'observed','reasons':reasons},
        'clock_bridge':{'status':'unconfirmed','resource_join':'not_evaluated','reason':'CTF origin/UUID is not a CLOCK_MONOTONIC bridge'},
        'temperature':{'status':'skipped','reason':'offline analysis reads no thermal interfaces'},
        'deadline':None,'budget':None,'business_acceptance':'not_evaluated','business_e2e':None,
        'limitations':['callback_interval includes waiting/preemption, not CPU time or E2E',
                       'message pointers are reused process-local addresses, never sample IDs',
                       'initialization precedes each resolved use; missing retirement cannot prove continuous lifetime',
                       'channel discard is scoped buffer evidence, not all-chain or business-exporter loss',
                       'historical declarations are consistency-checked, not authenticated by hashes']}
