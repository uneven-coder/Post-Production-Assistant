import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ai.pricing import estimate_run_cost, format_estimate


def _cfg(**overrides):
    project = {
        "models": {
            "transcript_model": {"name": "whisper-1", "speed": 1.0},
            "semantic_segmentation_model": {"name": "gpt-4.1-nano"},
            "chapter_title_model": {"name": "gpt-4.1"},
        },
        "transcript_windows": {"target_segment_length_s": 60},
        "chapters": {"merging": {"max_chapter_duration_s": 300}},
    }
    project.update(overrides)
    return {"project": project}


class EstimateRunCostTests(unittest.TestCase):
    def test_basic_estimate_structure(self):
        est = estimate_run_cost(_cfg(), audio_duration_s=1800.0)
        for key in ("whisper", "classification", "titles", "total", "detail"):
            self.assertIn(key, est)
        self.assertGreater(est["total"], 0.0)
        self.assertAlmostEqual(
            est["total"], est["whisper"] + est["classification"] + est["titles"])

    def test_longer_audio_costs_more(self):
        short = estimate_run_cost(_cfg(), 600.0)
        long = estimate_run_cost(_cfg(), 3600.0)
        self.assertGreater(long["total"], short["total"])
        self.assertGreater(long["detail"]["windows"], short["detail"]["windows"])

    def test_billable_duration_reduces_whisper_only(self):
        full = estimate_run_cost(_cfg(), 1800.0)
        trimmed = estimate_run_cost(_cfg(), 1800.0, billable_duration_s=1200.0)
        self.assertLess(trimmed["whisper"], full["whisper"])
        self.assertEqual(trimmed["classification"], full["classification"])

    def test_speed_reduces_whisper_cost(self):
        base = estimate_run_cost(_cfg(), 1800.0)
        cfg = _cfg()
        cfg["project"]["models"]["transcript_model"]["speed"] = 2.0
        fast = estimate_run_cost(cfg, 1800.0)
        self.assertAlmostEqual(fast["whisper"], base["whisper"] / 2.0, places=6)

    def test_pricing_overrides_used(self):
        cfg = _cfg()
        cfg["project"]["models"]["transcript_model"]["pricing"] = {"per_minute": 0.6}
        est = estimate_run_cost(cfg, 600.0)  # 10 minutes
        self.assertAlmostEqual(est["whisper"], 6.0, places=6)

    def test_smaller_windows_mean_more_windows(self):
        cfg = _cfg()
        cfg["project"]["transcript_windows"]["target_segment_length_s"] = 30
        more = estimate_run_cost(cfg, 1800.0)
        base = estimate_run_cost(_cfg(), 1800.0)
        self.assertGreater(more["detail"]["windows"], base["detail"]["windows"])

    def test_format_estimate_readable(self):
        est = estimate_run_cost(_cfg(), 1800.0)
        text = format_estimate(est)
        self.assertIn("$", text)
        self.assertIn("classification", text)


if __name__ == "__main__":
    unittest.main()
