"""Independent acceptance dimensions; missing requirements never imply success."""
import math


FRACTION_LIMITS = ('max_deadline_miss_fraction', 'max_data_age_expired_fraction')


def validate_limits(limits):
    if not isinstance(limits, dict):
        raise ValueError('acceptance_limits must be an object')
    if set(limits) - set(FRACTION_LIMITS):
        raise ValueError('unknown acceptance limit')
    for key, value in limits.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError(key + ' must be a finite fraction in [0,1] or null')


def input_state(metrics, quality):
    limits = quality.get('limits', {})
    counts = metrics.get('counts', {})
    samples = counts.get('measured_sent', counts.get('measured_samples'))
    fraction = quality.get('release_late_fraction')
    limit = limits.get('max_release_late_fraction')
    minimum = limits.get('min_samples', 1000)
    reasons = []
    if samples is None or fraction is None:
        status = 'unavailable'
        reasons.append('measured input evidence missing')
    elif samples < minimum or (limit is not None and fraction > limit):
        status = 'invalid'
        if samples < minimum:
            reasons.append('insufficient measured samples')
        if limit is not None and fraction > limit:
            reasons.append('release lateness limit exceeded')
    elif limit is None:
        status = 'not_configured'
        reasons.append('release lateness limit not configured')
    else:
        status = 'valid'
    return {'status': status, 'samples': samples, 'release_late_fraction': fraction,
            'limits': {'min_samples': minimum, 'max_release_late_fraction': limit}, 'reasons': reasons}


def delivery_state(metrics):
    if metrics.get('scenario') == 'S01':
        count = metrics.get('counts', {}).get('measured_samples')
        return {'status': 'complete' if count else 'unavailable',
                'scope': 'completed periodic samples; runner checks the planned manifest'}
    counts = metrics.get('counts', {})
    fields = ('missing_delivery', 'invalid_payload_events', 'duplicate_events', 'unexpected_id_events')
    observed = {key: counts.get(key) for key in fields}
    status = ('unavailable' if any(value is None for value in observed.values()) else
              'incomplete' if any(observed.values()) else 'complete')
    return {'status': status, 'observed': observed,
            'scope': 'finite observation and drain; missing is not a network-loss attribution'}


def resource_state(resources):
    coverage = resources.get('source_coverage') or {}
    if not coverage:
        return {'status': 'unavailable', 'reasons': [resources.get('reason', 'no resource source observations')]}
    reasons = [name + ' has fewer than two observations' for name, data in coverage.items()
               if data.get('samples', 0) < 2]
    entities = resources.get('registered_entities') or {}
    processes = [item for item in entities.values() if item.get('kind') == 'process']
    if not processes or any(item.get('cpu_percent_one_core') is None for item in processes):
        reasons.append('some process identities lack a valid CPU interval')
    threads = [item for item in entities.values() if item.get('kind') == 'thread']
    if any(item.get('cpu_percent_one_core') is None for item in threads):
        reasons.append('some thread identities lack a valid CPU interval')
    # Optional sensors are independent capabilities; retain their own reasons.
    mandatory_gaps = [name for name, item in (resources.get('availability') or {}).items()
                      if (name.endswith('.stat') or name.endswith('.tasks') or name == 'system.per_cpu_ticks')
                      and item.get('unavailable_samples', 0)]
    reasons.extend(mandatory_gaps)
    return {'status': 'partial' if reasons else 'observed', 'reasons': reasons,
            'scope': 'sampled resource coverage; optional interfaces evaluated per field'}


def evaluate_run(result, scenario):
    metrics, quality = result.get('metrics', {}), result.get('quality', {})
    source = input_state(metrics, quality)
    delivery = delivery_state(metrics)
    eligible = source['status'] == 'valid' and delivery['status'] == 'complete'
    limits = scenario.get('acceptance_limits', {})
    def budget(limit_key, threshold, observed):
        maximum = limits.get(limit_key)
        if maximum is None or threshold is None:
            state = 'not_configured'
        elif not eligible or observed is None:
            state = 'not_evaluated'
        else:
            state = 'exceeded' if observed > maximum else 'within_observed_scope'
        return {'status': state, 'threshold_ns': threshold, 'maximum_fraction': maximum,
                'observed_fraction': observed,
                'reason': None if state in ('exceeded', 'within_observed_scope') else
                          'threshold and acceptance fraction required' if state == 'not_configured' else
                          'valid input and complete delivery evidence required'}
    deadline = metrics.get('deadline', {})
    age = metrics.get('data_age_threshold', {})
    resources = result.get('resources', {})
    return {'execution': {'status': 'complete'}, 'input': source, 'delivery': delivery,
            'resources': resource_state(resources),
            'deadline': budget('max_deadline_miss_fraction', deadline.get('deadline_ns'),
                               deadline.get('violation_fraction')),
            'data_age': budget('max_data_age_expired_fraction', age.get('max_data_age_ns'),
                               age.get('expired_fraction')),
            'collector_budget': resources.get('overhead_budget', {'status': 'not_evaluated'}),
            'scope': 'synthetic benchmark requirements; no robot business-chain acceptance'}
