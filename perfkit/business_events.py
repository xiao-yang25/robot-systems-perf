"""Bounded, offline single-machine business event import; no SDK or process control."""
import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time

from .analysis import _distribution, QUANTILE_METHOD
from .lifecycle import defer_interrupts
from .monitor import _source_record
from .ros_evidence import _read as _read_json, _json, load_monitor as _load_monitor, NODE, UUID


LIMITS = [
    'External exports and operator declarations are not authenticated algorithm/ROS ownership proof.',
    'Only one machine and linux_monotonic clock; no cross-machine clock conversion.',
    'Resource observation envelopes do not prove continuous process/node existence.',
    'E2E is input event to valid output event, conditional on uniquely associated completed samples.',
    'Missing output is unfinished in this window, not a network-loss attribution.',
    'No callback CPU, CTF decoding, automatic business recognition or business acceptance.',
    'Resource references are never copied, divided or summed into function CPU/RSS.',
]


def _int(value, low=0):
    return type(value) is int and low <= value <= 2**63-1


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ValueError(name + ': bounded single-line text required')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError(name + ': valid UTF-8 text required') from error


def _validate_json_tree(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError('nonfinite JSON number')
        if isinstance(item, str):
            try:
                item.encode('utf-8')
            except UnicodeError as error:
                raise ValueError('JSON text must encode as UTF-8') from error
        elif isinstance(item, dict):
            pending.extend(item.keys()); pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _read(path):
    value, raw = _read_json(path)
    _validate_json_tree(value)
    return value, raw


def load_monitor(directory):
    # Strict preflight around the legacy reader leaves its public behavior and
    # old module bytes untouched. Reject a source changed between the two reads.
    names = ('monitor-status.json', 'monitor-summary.json', 'environment.json', 'workload-profile.json')
    hashes = {name: hashlib.sha256(_read(Path(directory)/name)[1]).hexdigest() for name in names}
    monitor = _load_monitor(directory)
    if hashes != monitor['source_hashes']:
        raise ValueError('monitor source changed during import preflight')
    context = monitor['environment'].get('observation_context')
    if context is not None and not isinstance(context, dict):
        raise ValueError('monitor observation_context must be an object or null')
    for role in monitor['summary']['workload']['functions']:
        for ref in role['resource_refs']:
            entity = monitor['entities'].get(ref)
            # Missing/non-process records remain unresolved, as in M3a. A
            # present process record must not bind via numeric coercion.
            if entity is not None and entity.get('kind') == 'process' and not _reference_matches(ref, entity):
                raise ValueError('monitor process identity type/range/reference inconsistent')
    return monitor


def _reference_matches(ref, entity):
    if not _int(entity.get('pid'), 1) or not _int(entity.get('starttime_ticks'), 1):
        return False
    match = re.fullmatch(r'pid=([1-9][0-9]*):registration=([1-9][0-9]*):start=([0-9]+)', ref)
    if not match or len(match[2]) > 19 or not _int(int(match[2]), 1):
        return False
    registration = entity.get('registration_id')
    if 'registration_id' in entity and (not _int(registration, 1) or str(registration) != match[2]):
        return False
    return match[1] == str(entity['pid']) and match[3] == str(entity['starttime_ticks'])


def _fields(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError(name + ': unknown or missing fields')


def validate_chain(value, monitor):
    _fields(value, 'format_version chain_id workload_id deployment_version input output deadline_ns', 'chain')
    if type(value['format_version']) is not int or value['format_version'] != 1:
        raise ValueError('chain format_version must be 1')
    for key in ('chain_id', 'workload_id', 'deployment_version'):
        _text(value[key], key)
    if value['workload_id'] != monitor['workload']['workload_id']:
        raise ValueError('chain workload_id mismatch')
    if value['deadline_ns'] is not None and not _int(value['deadline_ns'], 1):
        raise ValueError('deadline_ns must be positive integer or null')
    declarations = {r['id']: r['ros_nodes'] for r in monitor['workload']['functions']}
    for stage in ('input', 'output'):
        endpoint = value[stage]
        _fields(endpoint, 'function_id node', stage)
        _text(endpoint['function_id'], 'function_id')
        if not isinstance(endpoint['node'], str) or not NODE.fullmatch(endpoint['node']):
            raise ValueError('absolute endpoint node required')
        if endpoint['node'] not in declarations.get(endpoint['function_id'], []):
            raise ValueError(stage + ' endpoint not declared in monitor workload')
    return value


def validate_events(value):
    _fields(value, 'format_version kind source context window events', 'event export')
    if type(value['format_version']) is not int or value['format_version'] != 1 or value['kind'] != 'robot_business_events':
        raise ValueError('unsupported business event export')
    source = value['source']
    _fields(source, 'adapter tool_version raw_sha256 synthetic', 'source')
    if source['adapter'] != 'application_events_v1' or type(source['synthetic']) is not bool:
        raise ValueError('unsupported event adapter or synthetic declaration')
    _text(source['tool_version'], 'tool_version')
    if not isinstance(source['raw_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', source['raw_sha256']):
        raise ValueError('raw source digest required')
    context = value['context']
    _fields(context, 'boot_id pid_namespace clock ros_domain_id', 'context')
    if not UUID.fullmatch(str(context['boot_id'])) or not re.fullmatch(r'pid:\[[0-9]+\]', str(context['pid_namespace'])):
        raise ValueError('invalid boot or PID namespace')
    if context['clock'] != 'linux_monotonic' or not _int(context['ros_domain_id']) or context['ros_domain_id'] > 232:
        raise ValueError('same-machine monotonic clock and ROS domain required')
    window = value['window']
    _fields(window, 'start_ns end_ns expected_inputs dropped_events', 'window')
    if not _int(window['start_ns']) or not _int(window['end_ns']) or window['end_ns'] <= window['start_ns']:
        raise ValueError('nonempty event window required')
    for key in ('expected_inputs', 'dropped_events'):
        if window[key] is not None and not _int(window[key]):
            raise ValueError(key + ': nonnegative count or null required')
    events = value['events']
    if not isinstance(events, list) or len(events) > 100000:
        raise ValueError('bounded event list required')
    for event in events:
        _fields(event, 'sample_id type monotonic_ns pid starttime_ticks function_id node valid reason', 'event')
        for key in ('sample_id', 'function_id'):
            _text(event[key], key)
        if event['type'] not in ('input', 'output', 'drop') or type(event['valid']) is not bool:
            raise ValueError('unsupported event type or validity')
        if event['type'] != 'output' and not event['valid']:
            raise ValueError('input/drop must be valid; invalid output is a separate outcome')
        if not _int(event['pid'], 1) or not _int(event['starttime_ticks'], 1) or not _int(event['monotonic_ns']):
            raise ValueError('event identity/time invalid')
        if not window['start_ns'] <= event['monotonic_ns'] <= window['end_ns']:
            raise ValueError('event outside export window')
        if not isinstance(event['node'], str) or not NODE.fullmatch(event['node']):
            raise ValueError('absolute event node required')
        if event['reason'] is not None:
            _text(event['reason'], 'reason')
        if (event['type'] == 'drop' or not event['valid']) and event['reason'] is None:
            raise ValueError('drop/invalid delivery requires reason')
    return value


def _raw_source(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('raw source must be a regular file')
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('raw source exceeds 16MiB limit')
    return raw


def analyze(monitor, chain, export):
    """Import consistency, never upgrades candidate function or authenticates an exporter."""
    context_reasons = []
    context = monitor['environment'].get('observation_context') or {}
    for key in ('boot_id', 'pid_namespace', 'clock'):
        if context.get(key) != export['context'][key]:
            context_reasons.append(key + '_unrecorded_or_mismatch')
    if monitor['workload']['ros_domain_id'] != export['context']['ros_domain_id']:
        context_reasons.append('domain_unrecorded_or_mismatch')
    if monitor['workload']['workload_version'] != chain['deployment_version']:
        context_reasons.append('deployment_version_unrecorded_or_mismatch')
    window = export['window']
    if not monitor['status']['window_start_ns'] <= window['start_ns'] < window['end_ns'] <= monitor['status']['window_end_ns']:
        context_reasons.append('event_window_outside_monitor')
    roles = {r['function_id']: r for r in monitor['summary']['workload']['functions']}
    associated, groups = [], defaultdict(list)
    for index, event in enumerate(export['events']):
        endpoint = chain['input' if event['type'] == 'input' else 'output']
        reasons = list(context_reasons)
        refs = []
        if any(event[key] != endpoint[key] for key in ('function_id', 'node')):
            reasons.append('endpoint_declaration_mismatch')
        if not reasons:
            role = roles[endpoint['function_id']]
            for ref, observed in role['reference_observations'].items():
                entity = monitor['entities'].get(ref) or {}
                if (entity.get('kind') == 'process' and entity.get('pid') == event['pid'] and
                        entity.get('starttime_ticks') == event['starttime_ticks'] and
                        _reference_matches(ref, entity) and
                        observed['first_observed_ns'] <= event['monotonic_ns'] <= observed['last_observed_ns']):
                    refs.append(ref)
            if len(refs) != 1:
                reasons.append('resource_identity_ambiguous' if refs else 'resource_identity_not_observed')
        row = {'index': index, 'event': event, 'resource_refs': refs,
               'status': 'unresolved' if reasons else 'imported_endpoint_identity_consistent', 'reasons': reasons}
        associated.append(row); groups[event['sample_id']].append(row)
    samples, latencies = [], []
    deadline = chain['deadline_ns']
    for sample_id, rows in groups.items():
        by_type = {kind: [r for r in rows if r['event']['type'] == kind] for kind in ('input', 'output', 'drop')}
        inputs, outputs, drops = (by_type[k] for k in ('input', 'output', 'drop'))
        reasons = []
        if len(inputs) != 1 or len(outputs) > 1 or len(drops) > 1 or (outputs and drops):
            state = 'invalid_sample'; reasons.append('nonunique_input_or_conflicting_terminal_events')
        elif any(r['status'] == 'unresolved' for r in rows):
            state = 'unresolved'; reasons.append('event_identity_unresolved')
        elif (outputs or drops) and (outputs or drops)[0]['event']['monotonic_ns'] < inputs[0]['event']['monotonic_ns']:
            state = 'invalid_delivery'; reasons.append('terminal_precedes_input')
        elif outputs and not outputs[0]['event']['valid']:
            state = 'invalid_delivery'; reasons.append(outputs[0]['event']['reason'])
        elif outputs:
            state = 'completed_valid'
        elif drops:
            state = 'explicit_drop'; reasons.append(drops[0]['event']['reason'])
        else:
            state = 'unfinished'
        latency = (outputs[0]['event']['monotonic_ns'] - inputs[0]['event']['monotonic_ns']) if state == 'completed_valid' else None
        if latency is not None: latencies.append(latency)
        # Fixed mature-input cohort: near-boundary completed and incomplete inputs
        # are both excluded. Unknown/invalid identity is never a measured miss.
        mature = (deadline is not None and len(inputs) == 1 and
                  inputs[0]['status'] != 'unresolved' and
                  inputs[0]['event']['monotonic_ns'] + deadline <= window['end_ns'])
        assessed = mature and state not in ('unresolved', 'invalid_sample')
        samples.append({'sample_id': sample_id, 'status': state, 'reasons': reasons,
                        'event_indexes': [r['index'] for r in rows], 'e2e_ns': latency,
                        'deadline_assessed': assessed,
                        'deadline_missed': (state != 'completed_valid' or latency > deadline) if assessed else None})
    counts = {state: sum(s['status'] == state for s in samples) for state in (
        'completed_valid', 'explicit_drop', 'unfinished', 'invalid_delivery', 'invalid_sample', 'unresolved')}
    counts.update(event_rows=len(associated), observed_input_ids=sum(any(r['event']['type'] == 'input' for r in rows)
                  for rows in groups.values()), duplicate_event_rows=sum(max(0, len([r for r in rows if r['event']['type'] == kind])-1)
                  for rows in groups.values() for kind in ('input', 'output', 'drop')))
    input_total = counts['observed_input_ids']
    input_ids = {sample_id for sample_id, rows in groups.items() if any(r['event']['type'] == 'input' for r in rows)}
    counts['orphan_sample_ids'] = len(groups) - input_total
    quality_reasons = list(context_reasons)
    if window['expected_inputs'] is None: quality_reasons.append('expected_inputs_unrecorded')
    elif window['expected_inputs'] != input_total: quality_reasons.append('expected_input_count_mismatch')
    if window['dropped_events'] is None: quality_reasons.append('event_loss_counter_unrecorded')
    elif window['dropped_events']: quality_reasons.append('exporter_reported_event_loss')
    if any(counts[k] for k in ('unresolved', 'invalid_sample', 'invalid_delivery')):
        quality_reasons.append('unresolved_or_invalid_samples')
    if not input_total: quality_reasons.append('no_observed_inputs')
    assessed = [s for s in samples if s['deadline_assessed']]
    misses = sum(s['deadline_missed'] for s in assessed)
    result = {'format_version': 1, 'kind': 'business_chain_observation', 'chain': chain,
        'source': export['source'], 'context': export['context'], 'window': window,
        'mapping': {'status': 'operator_declared', 'provenance': 'external_export_not_authenticated',
                    'resource_refs': sorted({ref for r in associated for ref in r['resource_refs']})},
        'quality': {'status': 'partial' if quality_reasons else 'reported_complete', 'reasons': quality_reasons,
                    'expected_inputs': window['expected_inputs'], 'dropped_events': window['dropped_events'],
                    'resolved_event_fraction': (sum(r['status'] != 'unresolved' for r in associated)/len(associated)) if associated else None},
        'counts': counts, 'e2e_ns': _distribution(latencies), 'quantile_method': QUANTILE_METHOD,
        'throughput': {'completed_valid_per_second': counts['completed_valid'] * 1e9 / (window['end_ns']-window['start_ns']),
                       'denominator': 'entire declared event window, not completed-sample span'},
        'outcome_fractions': {k: sum(s['status'] == k and s['sample_id'] in input_ids for s in samples)/input_total
                             if input_total else None for k in (
            'completed_valid', 'unfinished', 'explicit_drop', 'invalid_delivery', 'invalid_sample', 'unresolved')},
        'deadline': {'deadline_ns': deadline, 'status': 'not_configured' if deadline is None else
                     'not_evaluated' if quality_reasons or not assessed else 'observed',
                     'assessed_inputs': len(assessed), 'missed_inputs': misses if deadline is not None else None,
                     'miss_fraction': misses/len(assessed) if assessed else None,
                     'unassessed_inputs': input_total-len(assessed),
                     'scope': 'mature unique identity-consistent input cohort; miss means no valid output by deadline'},
        'collector_budget': {'status': 'not_evaluated',
                            'reason': 'consult source monitor budget; no no-collector business baseline in this import'},
        'business_acceptance': 'not_evaluated',
        'limits': LIMITS, 'monitor_source_hashes': monitor['source_hashes']}
    return result, associated, samples


def run_business_events(monitor_run, chain_file, events_file, raw_source, output):
    output, source = Path(output), Path(monitor_run).resolve()
    try:
        output.resolve().relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError('output must be separate from source monitor')
    monitor = load_monitor(source)
    chain, chain_raw = _read(chain_file); validate_chain(chain, monitor)
    export, events_raw = _read(events_file); validate_events(export)
    raw = _raw_source(raw_source)
    if hashlib.sha256(raw).hexdigest() != export['source']['raw_sha256']:
        raise ValueError('raw source SHA256 mismatch')
    status = {'status': 'running', 'error': None, 'error_type': None, 'started_ns': time.monotonic_ns()}
    primary = None
    owns_output = False
    try:
        with defer_interrupts():
            output.mkdir(parents=True, exist_ok=False)
            owns_output = True
            _json(output/'business-status.json', status)
        for name, data in (('chain.json', chain_raw), ('events.json', events_raw), ('raw-source.bin', raw)):
            (output/name).write_bytes(data)
        _json(output/'business-inputs.json', {'tool': _source_record(), 'monitor_source_hashes': monitor['source_hashes'],
            'chain_sha256': hashlib.sha256(chain_raw).hexdigest(), 'events_sha256': hashlib.sha256(events_raw).hexdigest(),
            'raw_sha256': hashlib.sha256(raw).hexdigest()})
        result, events, samples = analyze(monitor, chain, export)
        for name, rows in (('business-event-associations.jsonl', events), ('business-samples.jsonl', samples)):
            with (output/name).open('x') as stream:
                for row in rows: stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
        _json(output/'business-summary.json', result)
        status['status'] = 'complete'
        return result
    except BaseException as error:
        primary = error
        status.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=str(error), error_type=type(error).__name__)
        raise
    finally:
        # A failed mkdir never grants ownership, including an existing result.
        if owns_output:
            try:
                with defer_interrupts():
                    status['finished_ns'] = time.monotonic_ns()
                    _json(output/'business-status.json', status)
            except BaseException as cleanup:
                unhandled = primary is None
                if unhandled:
                    status.update(status='interrupted' if isinstance(cleanup, KeyboardInterrupt) else 'failed',
                                  error=str(cleanup), error_type=type(cleanup).__name__)
                else:
                    status.setdefault('cleanup_errors', []).append({'stage': 'final_status_write',
                        'error_type': type(cleanup).__name__, 'error': str(cleanup)})
                try:
                    with defer_interrupts(): _json(output/'business-status.json', status)
                except BaseException:
                    pass  # Secondary failure cannot replace the original exception/interrupt.
                if unhandled: raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('monitor-run', 'chain', 'events', 'raw-source', 'output'):
        parser.add_argument('--'+flag, type=Path, required=True)
    args = parser.parse_args()
    def terminate(signum, frame): raise KeyboardInterrupt('business event import interrupted by SIGTERM')
    old = signal.signal(signal.SIGTERM, terminate)
    try:
        run_business_events(args.monitor_run, args.chain, args.events, args.raw_source, args.output)
    except KeyboardInterrupt: return 130
    except (OSError, ValueError, RuntimeError) as error:
        print('Business event import failed:', error, file=sys.stderr); return 1
    finally:
        signal.signal(signal.SIGTERM, old)
    print('Business event observation saved:', args.output)
    return 0


if __name__ == '__main__': raise SystemExit(main())
