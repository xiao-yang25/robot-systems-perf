#!/usr/bin/env python3
"""Offline callback intervals from existing CTF or explicit normalized test exports.

Never enables tracing, reads current business identities, or produces E2E events.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

try:
    from .collect_ros_trace import command, digest, inventory, save, defer_interrupts
    from .ros_trace_analysis import analyze, integer, UUID
except ImportError:
    from collect_ros_trace import command, digest, inventory, save, defer_interrupts
    from ros_trace_analysis import analyze, integer, UUID
from perfkit.ros_evidence import _read
from perfkit.runner import install_signal_handler


class Sources:
    def __init__(self): self.files = {}
    def read(self, path, maximum=64*1024*1024):
        value,raw = _read(path,maximum)
        self.files[str(path.resolve())] = {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        return value
    def raw(self, path):
        # NONBLOCK+regular-file guard also applies to diagnostics, not just JSON.
        import stat
        fd = os.open(path,os.O_RDONLY|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError('diagnostic must be a regular file')
            raw=stream.read(4*1024*1024+1)
        if len(raw)>4*1024*1024: raise ValueError('diagnostic exceeds 4 MiB')
        self.files[str(path.resolve())]={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        return raw.decode()
    def unchanged(self):
        for path,record in self.files.items():
            if Path(path).stat().st_size!=record['bytes'] or digest(Path(path))!=record['sha256']:
                raise ValueError('source changed during offline analysis: '+path)


def capture_history(root, sources):
    host=sources.read(root/'host.json')
    ids=sources.read(root/'identities.json')
    status=sources.read(root/'trace-status.json')
    request=sources.read(root/'request.json')
    if status.get('kind')!='ros_trace_capture' or type(status.get('synthetic')) is not bool:
        raise ValueError('capture status and explicit synthetic flag required')
    if not UUID.fullmatch(str(host.get('boot_id'))) or not isinstance(ids,dict) or not ids:
        raise ValueError('capture historical boot/identities required')
    guard=status.get('fixture_binary',{}).get('procname_guard')
    if not isinstance(guard,str) or not re.fullmatch(r'rsp-[0-9a-f]{11}',guard):
        raise ValueError('capture reservation needs recorded random procname guard')
    if set(ids)!={'publisher','subscriber'}:
        raise ValueError('only fixed test-owned publisher/subscriber capture roles supported; otherwise supply explicit --history')
    identities=[]
    for role,row in ids.items():
        if not isinstance(row,dict) or not integer(row.get('pid'),1,2**31-1) or not integer(row.get('starttime_ticks'),1) or not integer(row.get('pid_namespace_inode'),1):
            raise ValueError('capture historical identity invalid')
        if row.get('pid_namespace')!='pid:['+str(row['pid_namespace_inode'])+']' or row['pid_namespace']!=host.get('pid_namespace'):
            raise ValueError('capture namespace context inconsistent')
        runtime=sources.read(root/(role+'-start.json'))
        if type(runtime.get('pid')) is not int or runtime['pid']!=row['pid'] or runtime.get('rmw_identifier',{}).get('value')!=request.get('rmw'):
            raise ValueError('runtime/history PID or RMW inconsistent')
        sources.raw(root/(role+'-start.maps'))
        identities.append({'history_id':str(role),'vpid':row['pid'],'starttime_ticks':row['starttime_ticks'],
            'boot_id':host['boot_id'],'pid_namespace_inode':row['pid_namespace_inode'],'procname':guard,
            'scope':{'kind':'capture_reserved_pid','clock_uuid':host['boot_id']},
            'evidence':['identities.json','host.json','trace-status.json',role+'-start.json',role+'-start.maps']})
    return {'format_version':1,'kind':'robot_ros_trace_history','identities':identities,
        'basis':'test-owned exec gate, capture reservation and saved runtime context; no current /proc lookup',
        'capture_status':status['status'],'domain_requested':request.get('domain_id'),
        'clock_bridge':'unconfirmed'},status


def parse_channel_list(text, session, source):
    """Only an exact stopped session/user-space channel statistic, no global zero."""
    header=re.search(r'^Tracing session\s+([^\s:]+):\s*\[([^\]]+)\]',text,re.M)
    if not header or header[1]!=session or header[2]!='inactive':
        return [],'session identity/stopped state not confirmed in list output'
    domains=re.split(r'^=== Domain:\s*(.*?)\s*===\s*$',text,flags=re.M)
    user_sections=[domains[i+1] for i in range(1,len(domains)-1,2) if domains[i].strip().lower()=='user space']
    if len(user_sections)!=1:
        return [],'exact user-space domain unavailable'
    section=user_sections[0]
    channels=list(re.finditer(r'^\s*-?\s*([A-Za-z0-9_-]+):\s*\[(?:enabled|disabled)\]\s*$',section,re.M))
    target=[(i,row) for i,row in enumerate(channels) if row[1]=='ros']
    if len(target)!=1: return [],'ros channel statistic unavailable or ambiguous'
    i,row=target[0]
    body=section[row.end():channels[i+1].start() if i+1<len(channels) else len(section)]
    values=re.findall(r'^\s*Discarded events:\s*([0-9]+)\s*$',body,re.M)
    if len(values)!=1: return [],'discarded-events statistic missing or ambiguous'
    return [{'count_type':'channel_discarded_events','count':int(values[0]),'channel':'ros','domain':'userspace',
        'session':session,'source':source,'observation_phase':'after_successful_stop',
        'coverage':'this stopped user-space ros channel buffer statistic; excludes filters, untraced threads/processes and business exporters'}],None


def loss_evidence(root, status, data, sources):
    rows=[]; missing='no matching successful stop then list diagnostic'
    session=status.get('session_name') if status else None
    stop_end=None
    if root and isinstance(session,str):
        for folder in sorted(root.glob('control-*')):
            try:
                argv=sources.read(folder/'command.json')
                result=sources.read(folder/'result.json')
            except (OSError,ValueError,UnicodeError) as error:
                missing='control diagnostic unavailable: '+str(error)
                continue
            if not isinstance(argv,list) or len(argv)!=4 or argv[0]!='lttng' or argv[1]!='--no-sessiond' or argv[3]!=session:
                continue
            if argv[2]=='stop' and type(result.get('returncode')) is int and result['returncode']==0 and result.get('error') is None:
                stop_end=result.get('end_ns') if integer(result.get('end_ns')) else None
            elif argv[2]=='list' and stop_end is not None and integer(result.get('start_ns')) and result['start_ns']>=stop_end and type(result.get('returncode')) is int and result['returncode']==0 and result.get('error') is None:
                try:
                    text=sources.raw(folder/'stdout.bin')
                except (OSError,ValueError,UnicodeError) as error:
                    missing='list statistic unavailable: '+str(error)
                    break
                rows,missing=parse_channel_list(text,session,{'path':str(folder/'stdout.bin'),**sources.files[str((folder/'stdout.bin').resolve())]})
                # Never combine independent list snapshots into a single zero.
                break
    decoder=data.get('decoder_loss_messages',[])
    discarded=[row for row in decoder if row.get('type')=='discarded_events']
    valid=bool(discarded) and all(integer(row.get('count')) for row in discarded)
    return {'channel_discarded_events':rows,'channel_discarded_events_reason':missing,
        'decoder_discarded_events':{'count':sum(row['count'] for row in discarded) if valid else None,
           'messages':discarded,'count_type':'bt2_discarded_events_message','source':'official bt2 CTF messages',
           'channel':None,'channel_reason':'decoder messages identify streams, not a verified LTTng channel',
           'observation_phase':'offline_decode','coverage':'reported stream decoder messages, not business-exporter loss',
           'reason':None if valid else 'absence of decoder discarded messages is not a zero-loss proof; count may be unavailable'},
        'decoder_discarded_packets':{'count':None,'messages':[r for r in decoder if r.get('type')=='discarded_packets'],
           'count_type':'bt2_discarded_packets_message','source':'official bt2 CTF messages',
           'channel':None,'channel_reason':'decoder messages identify streams, not a verified LTTng channel',
           'observation_phase':'offline_decode','coverage':'reported packet messages for their streams; excludes business exporters',
           'reason':'no whole-source packet-loss total inferred; individual counts retained separately from event count'},
        'business_exporter_dropped_events':None,'business_exporter_dropped_events_reason':'no business exporter evidence in framework trace',
        'whole_chain_loss':None,'whole_chain_loss_reason':'channel/decoder evidence does not cover filters or untraced execution'}


def execute(args):
    output=args.output.resolve()
    sources=Sources()
    data=history=status=None
    ctf_files=None
    capture_metadata={}
    if args.capture:
        root=args.capture.resolve()
        if output==root or root in output.parents: raise ValueError('output must be outside source capture')
        history,status=capture_history(root,sources) if not args.history else (sources.read(args.history),sources.read(root/'trace-status.json'))
        ctf_files=inventory(root/'ctf')
        recorded=sources.read(root/'ctf-manifest.json')
        if recorded.get('files')!=ctf_files: raise ValueError('CTF manifest/bytes/hash mismatch')
        if not ctf_files: raise ValueError('empty CTF source')
        for name in ('host.json','request.json','preflight.json'):
            path=root/name
            capture_metadata[name]={'value':sources.read(path),'reason':None} if path.exists() else {'value':None,'reason':'not supplied in source capture'}
    else:
        root=None
        if not args.history: raise ValueError('--events requires explicit historical --history')
        history=sources.read(args.history)
        data=sources.read(args.events)
    owned=False; code=0
    state={'kind':'ros_trace_analysis_status','format_version':1,'status':'running','started_ns':time.monotonic_ns(),
           'ended_ns':None,'error':None,'primary_error':None,'interruption_error':None,'cleanup_errors':[],'business_acceptance':'not_evaluated'}
    try:
        with defer_interrupts():
            output.mkdir(); owned=True; save(output/'analysis-status.json',state)
        if args.capture:
            if not isinstance(args.bt2_python,str) or not args.bt2_python.strip(): raise ValueError('explicit bt2 interpreter required')
            synthetic=status.get('synthetic')
            if type(synthetic) is not bool: raise ValueError('capture synthetic flag unavailable')
            command([args.bt2_python,str(Path(__file__).with_name('ros_trace_bt2.py')), '--ctf',str(root/'ctf'),
                     '--output',str(output/'decoded.json'),'--synthetic','true' if synthetic else 'false'],
                    output/'decoder',dict(os.environ),timeout=args.timeout_seconds)
            data=sources.read(output/'decoded.json')
        loss=loss_evidence(root,status,data,sources)
        result=analyze(data,history,loss)
        if args.capture and inventory(root/'ctf')!=ctf_files: raise ValueError('CTF changed while decoding')
        sources.unchanged()
        result['capture_metadata']=capture_metadata
        result['source_evidence']={'files':sources.files,'ctf_files':ctf_files,'ctf_verification':'size/SHA256 before and after decode' if ctf_files else 'no CTF supplied',
                                   'analysis_script_sha256':digest(Path(__file__)), 'analysis_model_sha256':digest(Path(__file__).with_name('ros_trace_analysis.py')),
                                   'bt2_adapter_sha256':digest(Path(__file__).with_name('ros_trace_bt2.py'))}
        save(output/'callback-analysis.json',result)
        state.update(status='complete',quality=result['quality'],counts=result['counts'])
    except BaseException as error:
        code=130 if isinstance(error,KeyboardInterrupt) else 1
        state.update(status='interrupted' if code==130 else 'failed',error=repr(error),primary_error=repr(error))
        if code==130: state['interruption_error']=repr(error)
    finally:
        if owned:
            def write_final(path, value):
                nonlocal code
                try:
                    save(path,value)
                    return True
                except OSError as error:
                    state['cleanup_errors'].append(path.name+': '+repr(error))
                    if not code:
                        code=1; state.update(status='failed',error=repr(error))
                    print('final save failed: '+repr(error),file=sys.stderr)
                    return False
            try:
                with defer_interrupts():
                    state['ended_ns']=time.monotonic_ns()
                    # Catch write errors inside the deferral so a pending signal
                    # still propagates; derived evidence never gates terminal save.
                    write_final(output/'source-evidence.json',{'files':sources.files,'ctf_files':ctf_files})
                    if not write_final(output/'analysis-status.json',state):
                        write_final(output/'analysis-status.json',state)
            except KeyboardInterrupt as error:
                code=130
                state.update(status='interrupted',error=repr(error),interruption_error=repr(error),ended_ns=time.monotonic_ns())
                if state['primary_error'] is None: state['primary_error']=repr(error)
                with defer_interrupts(): write_final(output/'analysis-status.json',state)
    if state['error']: print(state['error'],file=sys.stderr)
    return code


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--capture',type=Path)
    source.add_argument('--events',type=Path)
    parser.add_argument('--history',type=Path)
    parser.add_argument('--bt2-python',default='/usr/bin/python3')
    parser.add_argument('--timeout-seconds',type=int,default=30)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.timeout_seconds<=60: parser.error('timeout must be 1..60 seconds')
    install_signal_handler()
    return execute(args)


if __name__=='__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: sys.exit(130)
    except (OSError,ValueError,RuntimeError) as error:
        print(str(error),file=sys.stderr); sys.exit(1)
