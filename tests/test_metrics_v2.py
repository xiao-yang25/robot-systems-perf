"""Known timestamp fixtures independently check v2 populations and boundaries."""

import json
from pathlib import Path
import tempfile
import unittest

from perfkit.analysis import analyze_c01, analyze_s01, write_report


SENDER_HEADER = 'seq,scheduled_ns,generated_ns,publish_ns,publish_return_ns,measured\n'
RECEIVER_HEADER = 'seq,receive_ns,finish_ns,cpu_ns,payload_valid\n'
SAMPLE_HEADER = 'seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured\n'


class MetricsV2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def csv(self, name, content):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return path

    def c01(self, **kwargs):
        sender = self.csv('sender.csv', SENDER_HEADER +
            '0,0,1,2,3,0\n'
            '1,100,110,120,125,1\n'
            '2,200,220,240,245,1\n'
            '3,300,305,310,315,1\n'
            '4,400,420,550,555,1\n'
            '5,500,560,600,605,1\n')
        receiver = self.csv('receiver.csv', RECEIVER_HEADER +
            '0,4,5,1,1\n'
            '1,150,160,5,1\n'
            '2,250,255,1,0\n'
            '2,260,350,10,1\n'
            '1,360,370,2,1\n'
            '4,570,580,5,1\n'
            '5,600,610,5,1\n')
        return analyze_c01(sender, receiver, 100, **kwargs)

    def test_sender_lateness_call_cost_response_and_signed_intervals(self):
        metrics = self.c01()
        distributions = metrics['distributions']
        self.assertEqual(distributions['release_lateness_ns'],
            {'n': 5, 'min': 5, 'mean': 23, 'p50': 20, 'p95': 60, 'p99': 60, 'max': 60})
        self.assertEqual(distributions['publish_call_time_ns']['mean'], 5)
        self.assertEqual(distributions['publish_period_error_ns'],
            {'n': 4, 'min': -50, 'mean': 20, 'p50': -30, 'p95': 140, 'p99': 140, 'max': 140})
        self.assertEqual(distributions['absolute_publish_period_error_ns']['mean'], 60)
        self.assertEqual(distributions['response_time_ns'],
            {'n': 4, 'min': 60, 'mean': 125, 'p50': 110, 'p95': 180, 'p99': 180, 'max': 180})
        tail = metrics['diagnostics']['tail_events']['publish_period_error_ns'][0]
        self.assertEqual((tail['seq'], tail['previous_seq'], tail['value_ns']), (4, 3, 140))
        self.assertEqual(tail['boundaries']['publish_ns'], 550)

    def test_half_open_throughput_excludes_drain_duplicate_invalid_and_warmup(self):
        metrics = self.c01(payload_bytes=8)
        window = metrics['measurement_window']
        self.assertEqual((window['start_ns'], window['end_ns'], window['period_ns']), (100, 600, 100))
        throughput = metrics['throughput']
        self.assertEqual(throughput['published_messages'], 4)
        self.assertEqual(throughput['valid_received_messages'], 3)
        self.assertEqual(throughput['published_messages_per_second'], 8_000_000)
        self.assertEqual(throughput['valid_received_messages_per_second'], 6_000_000)
        self.assertEqual(throughput['valid_received_payload_bytes_per_second'], 48_000_000)
        self.assertEqual(throughput['drain']['valid_received_messages'], 1)
        self.assertEqual(throughput['drain']['published_messages_after_window'], 1)
        observation = throughput['observed_publish_interval_rate']
        self.assertEqual(observation['intervals'], 4)
        self.assertEqual(observation['span_ns'], 480)
        self.assertAlmostEqual(observation['messages_per_second'], 4 * 1_000_000_000 / 480)
        self.assertEqual(metrics['planned_interval_accounting']['delivered_tasks_per_planned_second'], 8_000_000)
        # Legacy post-window count keeps its strict '>' semantics, v2 uses >=.
        self.assertEqual(metrics['counts']['delivered_after_planned_window_end'], 0)
        self.assertIsNone(self.c01()['throughput']['valid_received_payload_bytes_per_second'])

    def test_response_tail_decomposition_is_nonoverlapping_and_marks_drain(self):
        metrics = self.c01()
        tail = metrics['diagnostics']['response_tail_decomposition']
        self.assertEqual([item['response_time_ns'] for item in tail], [180, 150, 110, 60])
        self.assertEqual(tail[0]['seq'], 4)
        self.assertEqual(tail[0]['callback_cpu_time_ns'], 5)
        for item in tail:
            self.assertEqual(sum(item[key] for key in ('release_lateness_ns', 'generation_to_publish_ns',
                'publish_to_callback_ns', 'callback_wall_time_ns')), item['response_time_ns'])
        self.assertTrue(next(item for item in tail if item['seq'] == 5)['received_in_drain'])
        self.assertFalse(next(item for item in tail if item['seq'] == 4)['received_in_drain'])
        self.assertFalse(next(item for item in tail if item['seq'] == 1)['received_in_drain'])

    def test_deadline_missing_streak_and_completed_overrun_have_distinct_populations(self):
        metrics = self.c01()
        self.assertEqual((metrics['deadline']['late'], metrics['deadline']['missing'],
                          metrics['deadline']['violated']), (3, 1, 4))
        diagnostics = metrics['diagnostics']['deadline']
        self.assertEqual(diagnostics['longest_consecutive_violations'], 4)
        self.assertEqual([event['seq'] for event in diagnostics['violations']], [2, 3, 4, 5])
        missing = diagnostics['violations'][1]
        self.assertEqual(missing, {'seq': 3, 'scheduled_ns': 300, 'deadline_at_ns': 400,
                                  'finish_ns': None, 'overrun_ns': None, 'reason': 'missing'})
        self.assertEqual(metrics['deadline']['completed_overrun_ns'],
            {'n': 3, 'min': 10, 'mean': 140 / 3, 'p50': 50, 'p95': 80, 'p99': 80, 'max': 80})

    def test_data_age_threshold_strict_boundary_and_population(self):
        age = self.c01(max_data_age_ns=40)['data_age_threshold']
        self.assertEqual((age['denominator'], age['expired'], age['expired_fraction']), (4, 1, 0.25))
        self.assertEqual([event['seq'] for event in age['events']], [4])
        self.assertEqual(age['events'][0]['data_age_ns'], 150)
        self.assertEqual(self.c01()['data_age_threshold']['verdict'], 'not_evaluated')
        self.assertIsNone(self.c01()['data_age_threshold']['expired'])

    def test_s01_response_overrun_tail_and_monotonic_buckets(self):
        path = self.csv('samples.csv', SAMPLE_HEADER +
            '1,999999900,999999910,999999930,5,1\n'
            '2,1000000000,1000000020,1000000060,5,1\n'
            '3,1000000100,1000000105,1000000130,5,1\n')
        metrics = analyze_s01(path, 30)
        self.assertEqual(metrics['distributions']['response_time_ns']['mean'], 40)
        self.assertEqual(metrics['deadline']['completed_overrun_ns']['max'], 30)
        self.assertEqual(metrics['diagnostics']['deadline']['longest_consecutive_violations'], 1)
        self.assertEqual([event['seq'] for event in metrics['diagnostics']['deadline']['violations']], [2])
        self.assertEqual(metrics['distributions']['period_error_ns']['min'], -15)
        buckets = metrics['diagnostics']['time_series']['buckets']
        self.assertEqual([bucket['start_ns'] for bucket in buckets], [0, 1_000_000_000])
        self.assertEqual(buckets[0]['distributions']['response_time_ns']['n'], 1)
        self.assertEqual(buckets[1]['distributions']['response_time_ns']['n'], 2)
        tail = metrics['diagnostics']['tail_events']['response_time_ns'][0]
        self.assertEqual((tail['seq'], tail['value_ns'], tail['timestamp_ns']), (2, 60, 1_000_000_060))
        self.assertEqual(metrics['measurement_window']['end_ns'], 1_000_000_200)
        self.assertEqual(metrics['actual_execution_window']['end_ns'], 1_000_000_130)

    def test_fixed_histogram_edge_is_in_next_bucket(self):
        path = self.csv('samples.csv', SAMPLE_HEADER + '1,0,1000,2000,1,1\n')
        metrics = analyze_s01(path, None)
        bins = metrics['diagnostics']['histograms']['start_lateness_ns']['bins']
        nonempty = [item for item in bins if item['count']]
        self.assertEqual(nonempty, [{'lower_ns': 1000, 'upper_ns': 10000, 'count': 1}])
        self.assertEqual(sum(item['count'] for item in bins), 1)
        self.assertIsNone(metrics['diagnostics']['deadline']['longest_consecutive_violations'])

    def test_empty_population_and_unknown_plan_are_not_success(self):
        sender = self.csv('sender.csv', SENDER_HEADER)
        receiver = self.csv('receiver.csv', RECEIVER_HEADER)
        empty = analyze_c01(sender, receiver, 0, 0, 0)
        self.assertIsNone(empty['measurement_window']['start_ns'])
        self.assertIsNone(empty['throughput']['published_messages'])
        self.assertIsNone(empty['throughput']['observed_publish_interval_rate']['messages_per_second'])
        self.assertEqual(empty['data_age_threshold']['verdict'], 'no_delivered_samples')
        self.assertEqual(empty['diagnostics']['time_series']['buckets'], [])
        self.assertEqual(empty['distributions']['response_time_ns']['n'], 0)
        self.assertEqual(empty['diagnostics']['sample_populations']['response_time_ns']['warning'],
                         'empty conditional population')
        for rows in ('1,100,110,120,125,1\n',
                     '1,100,110,120,125,1\n2,200,210,220,225,1\n3,400,410,420,425,1\n'):
            sender = self.csv('sender.csv', SENDER_HEADER + rows)
            metrics = analyze_c01(sender, receiver, None)
            self.assertIsNone(metrics['measurement_window']['end_ns'])
            self.assertIsNone(metrics['throughput']['published_messages_per_second'])

    def test_optional_parameter_validation(self):
        for name in ('payload_bytes', 'max_data_age_ns'):
            for value in (-1, True, 1.5, '8'):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    self.c01(**{name: value})

    def test_report_schema_v2_diagnostics_resources_quality(self):
        metrics = self.c01()
        result = {'scenario': 'C01', 'repetition': 1, 'metrics': metrics,
                  'raw_directory': 'raw/C01/1', 'resources': {'available': False},
                  'quality': {'measurement_overhead': None}}
        write_report(self.root / 'report', {}, {}, [result])
        summary = json.loads((self.root / 'report' / 'summary.json').read_text())
        self.assertEqual(summary['schema_version'], 2)
        self.assertIn('additive', summary['schema_compatibility'])
        text = (self.root / 'report' / 'REPORT.md').read_text()
        for word in ('throughput', 'tail_events', 'completed_overrun_ns', 'available', 'measurement_overhead'):
            self.assertIn(word, text)


if __name__ == '__main__':
    unittest.main()
