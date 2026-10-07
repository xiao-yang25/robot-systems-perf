#!/usr/bin/env python3
"""Optional official bt2 CTF reader. No ROS, live tracing, or business conversion."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

MAX_OUTPUT = 64*1024*1024


def native(field, depth=0):
    if depth>8:
        raise ValueError('decoded field nesting exceeds limit')
    if field is None: return None
    if type(field) in (int,float,str,bool): return field
    # bt2 fields expose different interfaces. Structs are mappings; arrays expose
    # an iterator; scalar strings and integers are converted without string parsing.
    if hasattr(field,'items'):
        return {str(key):native(value,depth+1) for key,value in field.items()}
    name = type(field).__name__
    if 'StringField' in name or 'StringValue' in name: return str(field)
    if 'BoolField' in name or 'BoolValue' in name: return bool(field)
    if 'IntegerField' in name or 'EnumerationField' in name or 'IntegerValue' in name: return int(field)
    if 'RealField' in name or 'RealValue' in name: return float(field)
    if 'OptionField' in name or 'VariantField' in name:
        return native(field.field,depth+1)
    if hasattr(field,'__iter__'):
        return [native(value,depth+1) for value in field]
    raise ValueError('unsupported bt2 field type: '+name)


def decode(root, bt2, synthetic, max_events=100000):
    paths = sorted({str(path.parent) for path in root.rglob('metadata')})
    if not paths: raise ValueError('no CTF metadata directories')
    plugin = bt2.find_plugin('ctf')
    if plugin is None: raise RuntimeError('bt2 CTF plugin unavailable')
    specs = [bt2.ComponentSpec(plugin.source_component_classes['fs'], {'inputs':[path]}) for path in paths]
    iterator = bt2.TraceCollectionMessageIterator(specs)
    clocks, streams, packets, losses, events = {}, {}, [], [], []
    clock_keys, stream_keys = {}, {}
    messages = Counter()
    count = 0
    def stream_key(stream):
        key = (stream.trace.addr,stream.addr)
        if key not in stream_keys:
            sid = 'stream-'+str(len(stream_keys))
            stream_keys[key] = sid
            streams[sid] = {'stream_id':stream.id,'trace_name':stream.trace.name,'trace_environment':native(stream.trace.environment)}
        return stream_keys[key]
    def clock(snapshot, stream):
        if snapshot is None: return None, None
        cc = snapshot.clock_class
        key = (stream.trace.addr,cc.addr)
        if key not in clock_keys:
            cid = 'clock-'+str(len(clock_keys)); clock_keys[key] = cid
            clocks[cid] = {'name':cc.name,'frequency':cc.frequency,'offset_seconds':cc.offset.seconds,
                          'offset_cycles':cc.offset.cycles,'origin_is_unix_epoch':cc.origin_is_unix_epoch,
                          'uuid':str(cc.uuid) if cc.uuid is not None else None,
                          'class_scope':'same bt2 trace/clock class within this decode; not merged by UUID/name'}
        return clock_keys[key],snapshot.value
    for message in iterator:
        count += 1
        if count>max_events*8+10000: raise ValueError('bt2 message count limit exceeded')
        messages[type(message).__name__] += 1
        if isinstance(message,bt2._EventMessageConst):
            event=message.event
            if not event.name.startswith('ros2:'): continue
            if len(events)>=max_events: raise ValueError('ROS event count limit exceeded')
            try: snapshot=message.default_clock_snapshot
            except (ValueError,RuntimeError): snapshot=None
            cid,cycles=clock(snapshot,event.stream)
            events.append({'index':len(events),'name':event.name,'context':native(event.common_context_field) or {},
                           'payload':native(event.payload_field) or {},'clock_id':cid,'cycles':cycles,
                           'stream_id':stream_key(event.stream)})
        elif isinstance(message,bt2._PacketBeginningMessageConst):
            packet=message.packet
            packets.append({'stream_id':stream_key(packet.stream),'context':native(packet.context_field)})
        elif isinstance(message,(bt2._DiscardedEventsMessageConst,bt2._DiscardedPacketsMessageConst)):
            losses.append({'type':'discarded_events' if isinstance(message,bt2._DiscardedEventsMessageConst) else 'discarded_packets',
                           'count':message.count,'stream_id':stream_key(message.stream),
                           'scope':'decoder message for this stream; not business-exporter loss'})
    module=Path(bt2.__file__).resolve()
    return {'format_version':1,'kind':'robot_ros_trace_events','synthetic':synthetic,
        'source':{'adapter':'bt2','tool_version':bt2.__version__,'python':sys.executable,'bt2_module':str(module),
                  'bt2_module_sha256':hashlib.sha256(module.read_bytes()).hexdigest()},
        'events':events,'clocks':clocks,'streams':streams,'packets':packets,
        'decoder_messages':dict(messages),'decoder_loss_messages':losses}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ctf',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--synthetic',choices=('true','false'),required=True)
    args=parser.parse_args()
    # Only this optional subprocess imports the external decoder dependency.
    import bt2
    result=decode(args.ctf,bt2,args.synthetic=='true')
    encoder=json.JSONEncoder(allow_nan=False,separators=(',',':'))
    with args.output.open('x') as stream:
        size=0
        for chunk in encoder.iterencode(result):
            size+=len(chunk.encode())
            if size>MAX_OUTPUT: raise ValueError('decoded export exceeds 64 MiB')
            stream.write(chunk)
        stream.write('\n')
    print(json.dumps({'adapter':'bt2','version':bt2.__version__,'events':len(result['events']), 'output_bytes':size+1}))


if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: sys.exit(130)
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr); sys.exit(1)
