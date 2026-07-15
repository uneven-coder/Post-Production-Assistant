import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from silence.detector import invert_intervals, map_trimmed_to_original


class InvertIntervalsTests(unittest.TestCase):
    # invert_intervals just complements — padding/merging happens upstream in silent_intervals().
    def test_no_silence_keeps_whole_range(self):
        self.assertEqual(invert_intervals([], 100.0), [(0.0, 100.0)])

    def test_single_silence_in_middle(self):
        keep = invert_intervals([(40.0, 60.0)], 100.0)
        self.assertEqual(keep, [(0.0, 40.0), (60.0, 100.0)])

    def test_silence_at_start_and_end(self):
        keep = invert_intervals([(0.0, 10.0), (90.0, 100.0)], 100.0)
        self.assertEqual(keep, [(10.0, 90.0)])

    def test_clamps_to_total_duration(self):
        keep = invert_intervals([(40.0, 160.0)], 100.0)
        self.assertEqual(keep, [(0.0, 40.0)])

    def test_unsorted_input_is_sorted(self):
        keep = invert_intervals([(60.0, 70.0), (10.0, 20.0)], 100.0)
        self.assertEqual(keep, [(0.0, 10.0), (20.0, 60.0), (70.0, 100.0)])

    def test_overlapping_silence_windows_merge(self):
        keep = invert_intervals([(10.0, 30.0), (20.0, 40.0)], 100.0)
        self.assertEqual(keep, [(0.0, 10.0), (40.0, 100.0)])


class MapTrimmedToOriginalTests(unittest.TestCase):
    def test_empty_keep_intervals_returns_input(self):
        self.assertEqual(map_trimmed_to_original(5.0, []), 5.0)

    def test_maps_within_first_interval(self):
        keep = [(10.0, 40.0), (60.0, 100.0)]
        self.assertEqual(map_trimmed_to_original(5.0, keep), 15.0)

    def test_maps_within_second_interval(self):
        keep = [(10.0, 40.0), (60.0, 100.0)]
        # first interval contributes 30s of trimmed time; t=35 is 5s into the second interval
        self.assertEqual(map_trimmed_to_original(35.0, keep), 65.0)

    def test_boundary_at_interval_edge(self):
        keep = [(10.0, 40.0), (60.0, 100.0)]
        self.assertEqual(map_trimmed_to_original(30.0, keep), 40.0)

    def test_past_end_clamps_to_last_interval_end(self):
        keep = [(10.0, 40.0), (60.0, 100.0)]
        self.assertEqual(map_trimmed_to_original(1000.0, keep), 100.0)


if __name__ == "__main__":
    unittest.main()
