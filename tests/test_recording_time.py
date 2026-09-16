import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import unittest
from recording_time import recover_times, ClockSamples
from unittest.mock import patch


class ClockRecoveryTests(unittest.TestCase):
    def test_clock_jump_preserves_elapsed_time(self):
        data = {"segments": ["r--0", "r--1", "r--2"], "segmentTimes": {
            "r--0": {"startEpoch": 1000, "endEpoch": 1060},
            "r--1": {"startEpoch": 1060, "endEpoch": 50120},
            "r--2": {"startEpoch": 50120, "endEpoch": 50180}}}
        fixed = recover_times(data, {"r--0": [(10, 1000, False), (70, 1060, False)],
                                     "r--2": [(130, 50120, True), (190, 50180, True)]})
        self.assertEqual(fixed["routeStartEpoch"], 50000)
        self.assertEqual(fixed["routeEndEpoch"], 50180)
        self.assertEqual(fixed["segmentTimes"]["r--1"], {"startEpoch": 50060, "endEpoch": 50120})
        self.assertEqual(fixed["unresolvedSegments"], 0)

    def test_never_invent_without_valid_anchor(self):
        with self.assertRaises(ValueError):
            recover_times({"segments": ["r--0"]}, {"r--0": [(10, 1000, False)]})

    def test_log_only_segment_and_unknown_tail(self):
        fixed = recover_times({"segments": ["r--0", "r--1"], "segmentTimes": {}},
                              {"r--0": [(10, 50000, True), (70, 50060, True)], "r--1": []})
        self.assertEqual(fixed["routeEndEpoch"], 50060)
        self.assertEqual(fixed["unresolvedSegments"], 1)
        self.assertNotIn("r--1", fixed["segmentTimes"])

    def test_conflicting_valid_clocks_rejected(self):
        with self.assertRaises(ValueError):
            recover_times({"segments": ["r--0"]}, {"r--0": [(10, 50000, True), (70, 90000, True)]})

    def test_short_adjacent_session_requires_matching_clock(self):
        group = ClockSamples()
        group.start_mono, group.end_mono, group.init_offset = 100, 105, 50000
        data = {"segments": ["r--0"], "segmentTimes": {}}
        with patch('recording_time._valid_windows', [(50000, 106, 166)]):
            fixed = recover_times(data, {"r--0": group})
            self.assertEqual(fixed['routeEndEpoch'] - fixed['routeStartEpoch'], 5)
        with patch('recording_time._valid_windows', [(90000, 106, 166)]):
            with self.assertRaises(ValueError):
                recover_times(data, {"r--0": group})


if __name__ == "__main__":
    unittest.main()
