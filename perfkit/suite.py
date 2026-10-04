"""Run a bounded, explicit collection of experiments and sampler ABBA comparisons."""
import argparse
import copy
import json
from pathlib import Path
import re
from statistics import median

from .runner import install_signal_handler, number, run_experiment, validate
from .acceptance import delivery_state, input_state


def comparison_orders(comparison):
    blocks = number(comparison.get('repeat_blocks', 1), 'repeat_blocks', 1, 16, integer=True)
    order = comparison['abba_cases']
    return [order if index == 1 else [name + '-block-' + str(index).zfill(3) for name in order]
            for index in range(1, blocks + 1)]


def expand_suite(suite):
    if suite.get('format_version') != 1:
        raise ValueError('unsupported suite format')
    cases = suite.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 32:
        raise ValueError('suite needs 1..32 explicit cases')
    defaults = suite['defaults']
    expanded, names = [], set()
    for case in cases:
        name = case['name']
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name) or name in names:
            raise ValueError('case names must be unique, safe directory components')
        names.add(name)
        config = copy.deepcopy(defaults)
        config.update(copy.deepcopy({k: v for k, v in case.items() if k != 'name'}))
        validate(config)
        expanded.append((name, config))
    additions, used = {}, set()
    for comparison in suite.get('sampler_comparisons', []):
        order = comparison['abba_cases']
        if len(order) != 4 or any(name not in names for name in order):
            raise ValueError('sampler comparison needs four existing ABBA cases')
        if used.intersection(order):
            raise ValueError('ABBA cases cannot belong to multiple comparisons')
        used.update(order)
        positions = [next(i for i, (n, _) in enumerate(expanded) if n == name) for name in order]
        if positions != list(range(positions[0], positions[0] + 4)):
            raise ValueError('ABBA cases must be consecutive in execution order')
        configs = [expanded[i][1] for i in positions]
        if [c.get('sampling_mode', 'basic') for c in configs] != ['minimal', 'basic', 'basic', 'minimal']:
            raise ValueError('sampler comparison order must be minimal/basic/basic/minimal')
        normalized = [{k: v for k, v in c.items() if k != 'sampling_mode'} for c in configs]
        if any(c != normalized[0] for c in normalized[1:]):
            raise ValueError('sampler comparisons must retain identical workloads and requirements')
        if comparison.get('max_p99_increase_percent') is not None:
            number(comparison['max_p99_increase_percent'], 'max_p99_increase_percent', 0, 10000)
        additions[positions[-1]] = [(name, copy.deepcopy(config))
            for block in comparison_orders(comparison)[1:] for name, config in zip(block, configs)]
    result = []
    for index, item in enumerate(expanded):
        result.append(item)
        result.extend(additions.get(index, []))
    if len(result) > 128 or len({name for name, _ in result}) != len(result) or any(
            len(name) > 96 for name, _ in result):
        raise ValueError('expanded suite needs unique names and at most 128 cases')
    return result


def _compare_block(order, by_name, planned, budget, index):
    grouped, validity = {'minimal': {}, 'basic': {}}, []
    scenarios = {result['scenario'] for name in order if name in by_name
                 for result in by_name[name]['results']}
    scenarios.update(item['id'] for name in order for item in planned[name]['scenarios'])
    manifest = {}
    for name in order:
        if name not in by_name:
            manifest[name] = False
            continue
        run = by_name[name]
        mode = run['config'].get('sampling_mode', 'basic')
        expected = planned[name]['repetitions']
        expected_scenarios = {item['id'] for item in planned[name]['scenarios']}
        actual = [(result['scenario'], result.get('repetition')) for result in run['results']]
        manifest[name] = (run['config'] == planned[name]
                          and len(actual) == expected * len(expected_scenarios) and len(set(actual)) == len(actual)
                          and set(actual) == {(scenario, repetition) for scenario in expected_scenarios
                                             for repetition in range(1, expected + 1)})
        for result in run['results']:
            scenario = result['scenario']
            value = result['metrics']['distributions']['response_time_ns']['p99']
            grouped[mode].setdefault(scenario, []).append(value)
            source = input_state(result['metrics'], result.get('quality', {}))
            delivery = delivery_state(result['metrics'])
            validity.append({'case': name, 'scenario': scenario, 'repetition': result.get('repetition'),
                             'input_status': source['status'], 'delivery_status': delivery['status'],
                             'missing_delivery': result['metrics'].get('counts', {}).get('missing_delivery'),
                             'release_late_fraction': source['release_late_fraction'],
                             'load_limit_exceeded': 'release lateness limit exceeded' in source['reasons']})
    output = {}
    for scenario in sorted(scenarios):
        observed = grouped['basic'].get(scenario, [])
        reference = grouped['minimal'].get(scenario, [])
        populated = bool(reference and observed) and all(value is not None for value in reference + observed)
        a, b = (median(reference), median(observed)) if populated else (None, None)
        change = (b - a) * 100 / a if a else None
        checks = [item for item in validity if item['scenario'] == scenario]
        status = ('unavailable' if not populated or change is None else
                  'review_required' if not all(manifest.values()) or any(item['input_status'] != 'valid' or
                    item['delivery_status'] != 'complete' for item in checks) else 'observed')
        output[scenario] = {'block': index, 'abba_cases': order, 'minimal_per_run_values': reference,
            'basic_per_run_values': observed, 'minimal_median_of_run_values_ns': a,
            'basic_median_of_run_values_ns': b, 'increase_percent': change,
            'comparison_status': status, 'validity': checks, 'case_manifest_complete': manifest,
            'budget_status': 'not_evaluated' if budget is None or status != 'observed' else
                             'exceeded' if change > budget else 'within_budget'}
    return output


def sampler_comparisons(suite, runs):
    by_name = dict(runs)
    if len(by_name) != len(runs):
        raise ValueError('duplicate suite run names')
    planned = dict(expand_suite(suite))
    output = []
    for comparison in suite.get('sampler_comparisons', []):
        budget = comparison.get('max_p99_increase_percent')
        orders = comparison_orders(comparison)
        blocks = [_compare_block(order, by_name, planned, budget, index) for index, order in enumerate(orders, 1)]
        for scenario in sorted({key for block in blocks for key in block}):
            # Missing an entire population in a block cannot be repaired by other blocks.
            for index, block in enumerate(blocks, 1):
                block.setdefault(scenario, {'block': index, 'abba_cases': orders[index-1],
                    'minimal_per_run_values': [], 'basic_per_run_values': [],
                    'increase_percent': None, 'comparison_status': 'unavailable',
                    'validity': [], 'budget_status': 'not_evaluated'})
            items = [block[scenario] for block in blocks]
            reference = [value for block in items for value in block['minimal_per_run_values']]
            observed = [value for block in items for value in block['basic_per_run_values']]
            populated = bool(reference and observed) and all(value is not None for value in reference + observed)
            a, b = (median(reference), median(observed)) if populated else (None, None)
            change = (b - a) * 100 / a if a else None
            changes = [block['increase_percent'] for block in items]
            all_valid = all(block['comparison_status'] == 'observed' for block in items)
            worst = max(changes) if all_valid else None
            status = 'observed' if all_valid else 'unavailable' if any(
                block['comparison_status'] == 'unavailable' for block in items) else 'review_required'
            output.append({'name': comparison.get('name', 'resource-sampler'), 'scenario': scenario,
                           'abba_cases': comparison['abba_cases'], 'metric': 'response_time_ns.p99',
                           'minimal_per_run_values': reference, 'basic_per_run_values': observed,
                           'minimal_median_of_run_values_ns': a, 'basic_median_of_run_values_ns': b,
                           'increase_percent': change, 'budget_percent': budget,
                           'comparison_status': status, 'validity': [check for block in items for check in block['validity']],
                           'blocks': items, 'repeat_blocks': len(items),
                           'block_increase_percent': changes, 'worst_block_increase_percent': worst,
                           'block_increase_range_percent': {'min': min(changes), 'max': max(changes)}
                               if all(value is not None for value in changes) else None,
                           'block_increase_median_percent': median(changes)
                               if all(value is not None for value in changes) else None,
                           'budget_rule': 'every configured ABBA block must be valid and within budget',
                           'reason': 'Missing population, incomplete case manifest, invalid input/delivery or zero reference; inspect blocks'
                               if status != 'observed' else None,
                           'budget_status': 'not_evaluated' if budget is None or change is None or status != 'observed' else
                                            'exceeded' if worst > budget else 'within_budget',
                           'limits': ['Median of run-level P99 values is not a pooled percentile.',
                                      'Timestamp recording remains enabled in both modes.',
                                      'Observed difference includes run-to-run noise; inspect all four runs.',
                                      'This measures incremental resource sampling, not full instrumentation overhead.']})
    return output


def run_suite(suite, output):
    expanded = expand_suite(suite)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    status = {'status': 'running', 'completed_cases': [], 'error': None}
    runs = []
    try:
        (output / 'suite-config.json').write_text(json.dumps(suite, indent=2) + '\n')
        for name, config in expanded:
            print('Starting case:', name, flush=True)
            run = run_experiment(config, output / name)
            runs.append((name, run))
            status['completed_cases'].append(name)
        comparison = sampler_comparisons(suite, runs)
        summary = {'schema_version': 1, 'suite': suite,
                   'cases': [{'name': name, 'results': run['results']} for name, run in runs],
                   'sampler_comparisons': comparison,
                   'limits': ['Complete means collection succeeded; business acceptance is separate.',
                              'No automatic cross-platform hardware attribution or optimization claim.']}
        (output / 'suite-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        lines = ['# 平台测试套件报告', '',
                 '每轮分别展示；业务截止期与测量质量应结合各运行 REPORT.md 查看。', '',
                 '| 场景 | 轮次 | 响应 P99 (ns) | 违约率 | 输入 | 交付 | 期限验收 | 详细报告 |',
                 '| --- | ---: | ---: | ---: | --- | --- | --- | --- |']
        for name, run in runs:
            for result in run['results']:
                metrics = result['metrics']
                lines.append(f"| {name}/{result['scenario']} | {result['repetition']} | "
                             f"{metrics['distributions']['response_time_ns']['p99']} | "
                             f"{metrics['deadline']['violation_fraction']} | {result['acceptance']['input']['status']} | "
                             f"{result['acceptance']['delivery']['status']} | {result['acceptance']['deadline']['status']} | "
                             f"[REPORT]({name}/REPORT.md) |")
        lines += ['', '## 资源采集开销对照', '', '```json',
                  json.dumps(comparison, ensure_ascii=False, indent=2), '```', '',
                  '该对照保留时间戳埋点；不能解释为全部测量开销或稳定优化收益。']
        (output / 'SUITE_REPORT.md').write_text('\n'.join(lines) + '\n')
        status['status'] = 'complete'
        print('Suite report:', output / 'SUITE_REPORT.md', flush=True)
    except BaseException as exc:
        status.update(status='failed', error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        (output / 'suite-status.json').write_text(json.dumps(status, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    install_signal_handler()
    suite = json.loads(args.config.read_text())
    if args.dry_run:
        cases = expand_suite(suite)
        seconds = sum((c['warmup_seconds'] + c['measurement_seconds'] + c['drain_seconds']) *
                      c['repetitions'] * len(c['scenarios']) for _, c in cases)
        print(json.dumps({'cases': [name for name, _ in cases],
                          'nominal_seconds_excluding_build_discovery_and_analysis': seconds}, indent=2))
    else:
        run_suite(suite, args.output)


if __name__ == '__main__':
    main()
