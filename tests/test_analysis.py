"""Handwritten fixtures exercising metric semantics, not internal helpers."""

import json
from pathlib import Path
import tempfile
import unittest

from perfkit.analysis import analyze_c01, analyze_s01, write_report


SENDER_HEADER = "seq,scheduled_ns,generated_ns,publish_ns,publish_return_ns,measured\n"
RECEIVER_HEADER = "seq,receive_ns,finish_ns,cpu_ns,payload_valid\n"
SAMPLE_HEADER = "seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured\n"


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def csv(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def mixed_deliveries(self):
        sender = self.csv("sender.csv", SENDER_HEADER +
            "0,0,10,20,25,0\n"
            "1,100,110,120,125,1\n"
            "2,200,210,220,225,1\n"
            "3,300,310,320,325,1\n"
            "4,400,410,420,425,1\n"
            "5,500,510,520,525,1\n")
        receiver = self.csv("receiver.csv", RECEIVER_HEADER +
            "0,30,35,3,1\n"
            "1,150,200,30,1\n"
            "2,260,270,5,0\n"
            "2,280,350,40,1\n"
            "1,360,380,10,1\n"
            "4,450,460,7,1\n"
            "5,550,560,7,0\n"
            "99,600,601,1,1\n")
        return sender, receiver

    def test_missing_duplicate_invalid_and_conditional_quantiles(self):
        result = analyze_c01(*self.mixed_deliveries(), deadline_ns=100)
        counts = result["counts"]
        self.assertEqual(counts["measured_sent"], 5)
        self.assertEqual(counts["valid_delivered"], 3)
        self.assertEqual(counts["missing_delivery"], 2)
        self.assertEqual(counts["invalid_payload_events"], 2)
        self.assertEqual(counts["duplicate_events"], 2)
        self.assertEqual(counts["unexpected_id_events"], 1)
        self.assertEqual(counts["warmup_receiver_events"], 1)
        self.assertEqual(result["distributions"]["publish_to_callback_ns"],
            {"n": 3, "min": 30, "mean": 40, "p50": 30, "p95": 60, "p99": 60, "max": 60})
        self.assertEqual(result["distributions"]["callback_cpu_time_ns"]["p50"], 30)

    def test_deadline_uses_schedule_and_all_sent_denominator(self):
        deadline = analyze_c01(*self.mixed_deliveries(), deadline_ns=100)["deadline"]
        # seq 1 finishes exactly at scheduled + deadline; seq 2 is late.
        # seq 3 is absent and seq 5 only has an invalid payload: both missing.
        self.assertEqual(deadline["denominator"], 5)
        self.assertEqual(deadline["completed"], 3)
        self.assertEqual(deadline["late"], 1)
        self.assertEqual(deadline["missing"], 2)
        self.assertEqual(deadline["violated"], 3)
        self.assertEqual(deadline["violation_fraction"], 0.6)

    def test_no_deadline_does_not_turn_missing_into_business_verdict(self):
        result = analyze_c01(*self.mixed_deliveries(), deadline_ns=None)
        self.assertEqual(result["deadline"]["missing"], 2)
        self.assertIsNone(result["deadline"]["late"])
        self.assertIsNone(result["deadline"]["violated"])
        self.assertEqual(result["deadline"]["verdict"], "not_evaluated")

    def test_nearest_rank_quantiles_are_not_interpolated(self):
        samples = self.csv("samples.csv", SAMPLE_HEADER +
            "1,100,101,106,2,1\n2,200,202,207,2,1\n3,300,303,308,2,1\n"
            "4,400,404,409,2,1\n5,500,505,510,2,1\n6,600,606,611,2,1\n"
            "7,700,707,712,2,1\n8,800,808,813,2,1\n9,900,909,914,2,1\n"
            "10,1000,1010,1015,2,1\n11,1100,1111,1116,2,1\n12,1200,1212,1217,2,1\n"
            "13,1300,1313,1318,2,1\n14,1400,1414,1419,2,1\n15,1500,1515,1520,2,1\n"
            "16,1600,1616,1621,2,1\n17,1700,1717,1722,2,1\n18,1800,1818,1823,2,1\n"
            "19,1900,1919,1924,2,1\n20,2000,2020,2025,2,1\n")
        result = analyze_s01(samples, None)
        self.assertEqual(result["distributions"]["start_lateness_ns"],
            {"n": 20, "min": 1, "mean": 10.5, "p50": 10, "p95": 19, "p99": 20, "max": 20})

    def test_signed_period_errors_use_actual_corresponding_intervals(self):
        samples = self.csv("samples.csv", SAMPLE_HEADER +
            "1,100,120,150,9,1\n"
            "2,200,240,260,8,0\n"
            "3,300,310,330,7,1\n"
            "4,600,640,680,10,1\n")
        result = analyze_s01(samples, 35)
        self.assertEqual(result["distributions"]["period_error_ns"],
            {"n": 2, "min": -10, "mean": 10, "p50": -10, "p95": 30, "p99": 30, "max": 30})
        self.assertEqual(result["distributions"]["absolute_period_error_ns"]["mean"], 20)
        self.assertEqual(result["counts"]["measured_samples"], 3)
        self.assertEqual(result["deadline"]["late"], 2)

    def test_receiver_callback_can_begin_before_publish_returns(self):
        sender = self.csv("sender.csv", SENDER_HEADER + "1,100,110,120,180,1\n")
        receiver = self.csv("receiver.csv", RECEIVER_HEADER + "1,130,140,5,1\n")
        result = analyze_c01(sender, receiver, None)
        self.assertEqual(result["distributions"]["publish_to_callback_ns"]["min"], 10)

    def test_negative_intervals_rejected_even_for_invalid_and_warmup_events(self):
        for measured, valid in ((1, 1), (1, 0), (0, 1)):
            with self.subTest(measured=measured, valid=valid):
                sender = self.csv("sender.csv", SENDER_HEADER + f"1,100,110,120,125,{measured}\n")
                receiver = self.csv("receiver.csv", RECEIVER_HEADER + f"1,119,140,5,{valid}\n")
                with self.assertRaisesRegex(ValueError, "precedes publish_ns"):
                    analyze_c01(sender, receiver, None)
        for row in ("1,100,99,130,2,1\n", "1,100,120,119,2,1\n"):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    analyze_s01(self.csv("samples.csv", SAMPLE_HEADER + row), None)

    def test_nonmonotonic_receiver_order_and_sender_order_rejected(self):
        sender = self.csv("sender.csv", SENDER_HEADER +
            "1,100,110,120,125,1\n2,200,210,220,225,1\n")
        receiver = self.csv("receiver.csv", RECEIVER_HEADER +
            "2,230,240,3,1\n1,150,160,3,1\n")
        with self.assertRaisesRegex(ValueError, "receive_ns is not monotonic"):
            analyze_c01(sender, receiver, None)
        sender = self.csv("sender.csv", SENDER_HEADER + "1,100,119,118,125,1\n")
        with self.assertRaisesRegex(ValueError, "chronological"):
            analyze_c01(sender, self.csv("empty.csv", RECEIVER_HEADER), None)

    def test_out_of_order_first_valid_and_planned_accounting_rate(self):
        sender = self.csv("sender.csv", SENDER_HEADER +
            "7,100,110,120,125,1\n3,200,210,220,225,1\n9,300,310,320,325,1\n")
        receiver = self.csv("receiver.csv", RECEIVER_HEADER +
            "3,230,240,3,1\n7,350,360,3,1\n9,450,460,3,1\n")
        result = analyze_c01(sender, receiver, None)
        self.assertEqual(result["counts"]["out_of_order_first_valid_deliveries"], 1)
        self.assertEqual(result["counts"]["delivered_after_last_planned_release"], 2)
        self.assertEqual(result["counts"]["delivered_after_planned_window_end"], 1)
        accounting = result["planned_interval_accounting"]
        self.assertEqual(accounting["planned_measurement_interval_ns"], 300)
        self.assertEqual(accounting["delivered_tasks_per_planned_second"], 10_000_000)

    def test_nonuniform_or_single_release_cannot_infer_accounting_interval(self):
        for rows in (
            "1,100,110,120,125,1\n",
            "1,100,110,120,125,1\n2,200,210,220,225,1\n3,500,510,520,525,1\n",
        ):
            with self.subTest(rows=rows):
                result = analyze_c01(self.csv("sender.csv", SENDER_HEADER + rows),
                                     self.csv("receiver.csv", RECEIVER_HEADER), None)
                self.assertIsNone(result["planned_interval_accounting"]["planned_measurement_interval_ns"])
                self.assertIsNone(result["planned_interval_accounting"]["delivered_tasks_per_planned_second"])

    def test_empty_populations_have_null_distribution_and_no_deadline_success(self):
        result = analyze_c01(self.csv("sender.csv", SENDER_HEADER),
                             self.csv("receiver.csv", RECEIVER_HEADER), 100)
        self.assertEqual(result["distributions"]["chain_latency_ns"],
            {"n": 0, "min": None, "mean": None, "p50": None, "p95": None, "p99": None, "max": None})
        self.assertEqual(result["deadline"]["verdict"], "no_measured_samples")
        self.assertIsNone(result["deadline"]["violation_fraction"])
        samples = self.csv("samples.csv", SAMPLE_HEADER + "1,100,110,120,2,1\n")
        self.assertEqual(analyze_s01(samples, None)["distributions"]["period_error_ns"]["n"], 0)

    def test_malformed_csv_is_not_silently_skipped(self):
        bad_csvs = [
            '"seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured\n',
            "seq,scheduled_ns,start_ns,finish_ns,cpu_ns\n1,100,120,130,5\n",
            SAMPLE_HEADER + "1,100,120,130,5\n",
            SAMPLE_HEADER + "1,100,120,130,5,1,extra\n",
            SAMPLE_HEADER + "1,100,120,130,5,2\n",
            SAMPLE_HEADER + "1,100,120.0,130,5,1\n",
            SAMPLE_HEADER + "1,100,-120,130,5,1\n",
            SAMPLE_HEADER + "1,100,120,130,NaN,1\n",
            SAMPLE_HEADER + '1,100,"120,130,5,1\n',
            SAMPLE_HEADER + "1,100,120,130,5,1\n1,200,220,230,5,1\n",
        ]
        for text in bad_csvs:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    analyze_s01(self.csv("bad.csv", text), None)

    def test_deadline_type_and_range_validation(self):
        samples = self.csv("samples.csv", SAMPLE_HEADER)
        for value in (-1, True, 1.5, "100"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    analyze_s01(samples, value)
        self.assertEqual(analyze_s01(samples, 0)["deadline"]["deadline_ns"], 0)

    def test_single_thread_intervals_reject_overlap_and_impossible_cpu(self):
        sender = self.csv('sender.csv', SENDER_HEADER +
                          '1,100,110,120,125,1\n2,200,210,220,225,1\n')
        receiver = self.csv('receiver.csv', RECEIVER_HEADER +
                            '1,130,240,10,1\n2,230,250,10,1\n')
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            analyze_c01(sender, receiver, None)
        receiver = self.csv('receiver.csv', RECEIVER_HEADER + '1,130,140,11,1\n')
        with self.assertRaisesRegex(ValueError, 'CPU duration'):
            analyze_c01(sender, receiver, None)
        for row, pattern in (
            ('1,100,110,230,10,1\n2,200,210,250,10,1\n', 'overlapping'),
            ('1,100,110,120,11,1\n', 'CPU duration')
        ):
            with self.assertRaisesRegex(ValueError, pattern):
                analyze_s01(self.csv('samples.csv', SAMPLE_HEADER + row), None)

    def test_report_retains_repetitions_and_honest_limits(self):
        c01 = analyze_c01(*self.mixed_deliveries(), deadline_ns=100)
        s01 = analyze_s01(self.csv("samples.csv", SAMPLE_HEADER + "1,100,110,120,3,1\n"), None)
        results = [{"scenario": "C01", "repetition": 1, "metrics": c01, "raw_directory": "raw/C01/1"},
                   {"scenario": "S01", "repetition": 2, "metrics": s01, "raw_directory": "raw/S01/2"}]
        output = self.root / "report"
        write_report(output, {"drain_ms": 100}, {"host": "Docker"}, results)
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["results"], results)
        self.assertNotIn("aggregate_percentiles", summary)
        text = (output / "REPORT.md").read_text()
        for expected in ("ROS", "网络丢包", "runnable_wait", "Jetson", "nearest-rank", "排空", "缺失"):
            self.assertIn(expected, text)
        self.assertIn("| publish_to_callback_ns | 3 | 30 | 40 | 30 | 60 | 60 | 60 |", text)
        self.assertIn("planned_interval_accounting", text)

    def test_report_rejects_inconsistent_scenario_before_writing(self):
        s01 = analyze_s01(self.csv("samples.csv", SAMPLE_HEADER), None)
        output = self.root / "bad_report"
        with self.assertRaises(ValueError):
            write_report(output, {}, {}, [{"scenario": "C01", "repetition": 1,
                "metrics": s01, "raw_directory": "raw"}])
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
