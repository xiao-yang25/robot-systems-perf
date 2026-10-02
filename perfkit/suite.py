"""Run a bounded, explicit collection of experiments and sampler ABBA comparisons."""
import argparse
import copy
import json
from pathlib import Path
import re
from statistics import median

from .runner import install_signal_handler, number, run_experiment, validate


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
    for comparison in suite.get('sampler_comparisons', []):
        order = comparison['abba_cases']
        if len(order) != 4 or any(name not in names for name in order):
            raise ValueError('sampler comparison needs four existing ABBA cases')
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
    return expanded


def sampler_comparisons(suite, runs):
    by_name = dict(runs)
    output = []
    for comparison in suite.get('sampler_comparisons', []):
        order = comparison['abba_cases']
        grouped = {'minimal': {}, 'basic': {}}
        validity = []
        for name in order:
            run = by_name[name]
            mode = run['config'].get('sampling_mode', 'basic')
            for result in run['results']:
                scenario = result['scenario']
                metric = 'response_time_ns'
                value = result['metrics']['distributions'][metric]['p99']
                grouped[mode].setdefault(scenario, []).append(value)
                counts = result['metrics'].get('counts', {})
                quality = result.get('quality', {})
                fraction = quality.get('release_late_fraction')
                limit = quality.get('limits', {}).get('max_release_late_fraction')
                validity.append({'case': name, 'scenario': scenario,
                                 'repetition': result.get('repetition'),
                                 'missing_delivery': counts.get('missing_delivery'),
                                 'release_late_fraction': fraction,
                                 'load_limit_exceeded': fraction is not None and limit is not None and fraction > limit})
        for scenario, observed in grouped['basic'].items():
            reference = grouped['minimal'][scenario]
            populated = all(value is not None for value in reference + observed)
            a = median(reference) if populated else None
            b = median(observed) if populated else None
            change = (b - a) * 100 / a if a else None
            checks = [item for item in validity if item['scenario'] == scenario]
            status = ('unavailable' if not populated else 'review_required' if any(
                item['missing_delivery'] or item['load_limit_exceeded'] for item in checks) else 'observed')
            budget = comparison.get('max_p99_increase_percent')
            output.append({'name': comparison.get('name', 'resource-sampler'), 'scenario': scenario,
                           'abba_cases': order, 'metric': 'response_time_ns.p99',
                           'minimal_per_run_values': reference, 'basic_per_run_values': observed,
                           'minimal_median_of_run_values_ns': a, 'basic_median_of_run_values_ns': b,
                           'increase_percent': change, 'budget_percent': budget,
                           'comparison_status': status, 'validity': checks,
                           'reason': 'One or more runs have no completed response samples' if not populated else None,
                           'budget_status': 'not_evaluated' if budget is None or change is None or status != 'observed' else
                                            'exceeded' if change > budget else 'within_budget',
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
                 '| 场景 | 轮次 | 响应 P99 (ns) | 违约率 | 测量质量 | 详细报告 |',
                 '| --- | ---: | ---: | ---: | --- | --- |']
        for name, run in runs:
            for result in run['results']:
                metrics = result['metrics']
                lines.append(f"| {name}/{result['scenario']} | {result['repetition']} | "
                             f"{metrics['distributions']['response_time_ns']['p99']} | "
                             f"{metrics['deadline']['violation_fraction']} | {result['quality']['status']} | "
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
