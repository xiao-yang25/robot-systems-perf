"""Independent requested/observed data for the owned-thread qualification gate."""
import unittest

from scripts.wakeup_conditions import ThreadConditions


def thread(tid, **values):
    return dict({'tid': tid, 'starttime_ticks': 12, 'affinity_cpu_list': '3',
                 'policy': 0, 'rt_priority': 0, 'nice': 0}, **values)


class ThreadConditionsTests(unittest.TestCase):
    def test_actual_worker_and_main_conditions_are_required(self):
        probe = ThreadConditions('cyclictest', ['cyclictest'], 3, 0)
        for now in (1000000000, 1250000000):
            probe.check(100, [thread(100), thread(101)], now)
        probe.qualify()
        self.assertEqual(probe.result()['last']['measurement_tid'], 101)
        self.assertEqual(probe.result()['observations'], 2)
        probe.check(100, [thread(100)], 1500000000)
        self.assertTrue(probe.finished)

    def test_changed_mask_scheduler_priority_nice_and_identity_are_rejected(self):
        for values in ({'affinity_cpu_list': '0-11'}, {'policy': 3}, {'rt_priority': 1},
                       {'nice': 5}, {'starttime_ticks': 13}):
            probe = ThreadConditions('cyclictest', ['cyclictest'], 3, 0)
            probe.check(100, [thread(100), thread(101)], 1000000000)
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                probe.check(100, [thread(100), thread(101, **values)], 1250000000)

    def test_missing_or_ambiguous_worker_cannot_qualify(self):
        probe = ThreadConditions('cyclictest', ['cyclictest'], 3, 0)
        probe.check(100, [thread(100)], 1000000000)
        with self.assertRaises(RuntimeError):
            probe.qualify()
        self.assertFalse(probe.result()['validated'])
        for tool, threads in [('cyclictest', [thread(100), thread(101), thread(102)]),
                              ('s01', [thread(100), thread(101)])]:
            with self.subTest(tool=tool), self.assertRaises(RuntimeError):
                ThreadConditions(tool, [tool], 3, 0).check(100, threads, 1000000000)


if __name__ == '__main__':
    unittest.main()
