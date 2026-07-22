import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import apply_profile
from youtube_automation.driver import reduce_cuts_for_studio


class ReduceCutsForStudioTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(reduce_cuts_for_studio([]), [])

    def test_drops_short_cuts(self):
        cuts = [(10.0, 10.5), (20.0, 22.0)]
        self.assertEqual(reduce_cuts_for_studio(cuts, min_duration_s=1.0), [(20.0, 22.0)])

    def test_merges_close_cuts(self):
        cuts = [(10.0, 12.0), (12.2, 14.0)]
        self.assertEqual(reduce_cuts_for_studio(cuts, max_merge_gap_s=0.35), [(10.0, 14.0)])

    def test_does_not_merge_distant_cuts(self):
        cuts = [(10.0, 12.0), (15.0, 17.0)]
        self.assertEqual(reduce_cuts_for_studio(cuts, max_merge_gap_s=0.35),
                         [(10.0, 12.0), (15.0, 17.0)])

    def test_merge_can_rescue_short_cuts(self):
        # Two sub-minimum cuts that merge into one long enough to keep
        cuts = [(10.0, 10.6), (10.7, 11.3)]
        self.assertEqual(reduce_cuts_for_studio(cuts, min_duration_s=1.0,
                                                max_merge_gap_s=0.35),
                         [(10.0, 11.3)])

    def test_unsorted_input(self):
        cuts = [(20.0, 22.0), (10.0, 12.0)]
        self.assertEqual(reduce_cuts_for_studio(cuts), [(10.0, 12.0), (20.0, 22.0)])

    def test_max_edits_cap(self):
        cuts = [(float(i * 10), float(i * 10 + 2)) for i in range(10)]
        result = reduce_cuts_for_studio(cuts, max_edits=3)
        self.assertEqual(len(result), 3)
        self.assertEqual(result, sorted(result))


class ManifestRoundTripTests(unittest.TestCase):
    def test_save_and_load(self):
        from main import save_run_manifest, load_run_manifest, RUN_MANIFEST_FILENAME
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "project": {"name": "T", "output_directory": {"file": tmp}},
                "env": {"SECRET": "x"},
            }
            intervals = [(1.0, 2.5), (10.0, 12.0)]
            path = save_run_manifest(config, intervals)
            self.assertEqual(path, os.path.join(tmp, RUN_MANIFEST_FILENAME))

            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.assertNotIn("env", raw["config"])  # env must not be persisted

            loaded_config, loaded_intervals = load_run_manifest(path)
            self.assertEqual(loaded_intervals, intervals)
            self.assertEqual(loaded_config["project"]["name"], "T")
            self.assertIn("env", loaded_config)

    def test_save_without_output_dir_returns_none(self):
        from main import save_run_manifest
        self.assertIsNone(save_run_manifest({"project": {}}, []))


class RunSummaryTests(unittest.TestCase):
    def test_summary_counts_silence_and_chapters(self):
        from main import build_run_summary, format_run_summary
        config = {"project": {"videos": []}}  # no audio source: duration 0
        intervals = [(10.0, 15.0), (60.0, 70.0)]
        summary = build_run_summary(config, intervals, chapters=[object(), object()],
                                    cost_estimate={"total": 0.5})
        self.assertEqual(summary["cuts"], 2)
        self.assertEqual(summary["silence_removed_s"], 15.0)
        self.assertEqual(summary["longest_cut_s"], 10.0)
        self.assertEqual(summary["chapters"], 2)
        self.assertEqual(summary["estimated_cost_usd"], 0.5)

        lines = format_run_summary(summary)
        text = "\n".join(lines)
        self.assertIn("Silence removed", text)
        self.assertIn("2 cut(s)", text)
        self.assertIn("$0.5000", text)

    def test_summary_with_no_cuts(self):
        from main import build_run_summary, format_run_summary
        summary = build_run_summary({"project": {"videos": []}}, [])
        self.assertEqual(summary["cuts"], 0)
        self.assertEqual(summary["silence_removed_s"], 0.0)
        # Renders without raising even with everything empty
        self.assertTrue(format_run_summary(summary))

    def test_summary_includes_stage_times_and_total_duration(self):
        from main import build_run_summary, format_run_summary
        stage_times = {
            "silence": {"status": "done", "duration_s": 3.2},
            "borders": {"status": "skipped"},
            "transcript": {"status": "error", "duration_s": 1.1, "error": "boom"},
        }
        summary = build_run_summary({"project": {"videos": []}}, [],
                                    stage_times=stage_times, run_duration_s=125.0)
        self.assertEqual(summary["stage_times"], stage_times)
        self.assertEqual(summary["run_duration_s"], 125.0)

        text = "\n".join(format_run_summary(summary))
        self.assertIn("silence: 3.2s", text)
        self.assertIn("borders: skipped", text)
        self.assertIn("transcript: 1.1s (failed)", text)
        self.assertIn("Total processing time: 2:05", text)

    def test_summary_includes_test_results(self):
        from main import build_run_summary, format_run_summary
        passing = build_run_summary({"project": {"videos": []}}, [],
                                    test_results={"ran": 88, "failures": 0, "errors": 0})
        self.assertIn("Tests: 88/88 passed", "\n".join(format_run_summary(passing)))

        failing = build_run_summary({"project": {"videos": []}}, [], test_results={
            "ran": 88, "failures": 1, "errors": 1,
            "failed_names": ["tests.test_x.FooTests.test_bar",
                             "tests.test_y.BazTests.test_qux"],
        })
        text = "\n".join(format_run_summary(failing))
        self.assertIn("Tests: 86/88 passed (1 failure(s), 1 error(s))", text)
        self.assertIn("FAILED: tests.test_x.FooTests.test_bar", text)

        crashed = build_run_summary({"project": {"videos": []}}, [],
                                    test_results={"crashed": True, "error": "no tests dir"})
        self.assertIn("Tests: could not run (no tests dir)",
                      "\n".join(format_run_summary(crashed)))

    def test_manifest_stores_summary(self):
        from main import save_run_manifest
        with tempfile.TemporaryDirectory() as tmp:
            config = {"project": {"output_directory": {"file": tmp}}}
            path = save_run_manifest(config, [], summary={"cuts": 3})
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["summary"], {"cuts": 3})

    def test_manifest_stores_extra_run_data(self):
        from main import save_run_manifest
        with tempfile.TemporaryDirectory() as tmp:
            config = {"project": {"output_directory": {"file": tmp}}}
            extra = {
                "chapters": [{"title": "1. Intro", "start_time": 0.0}],
                "costs": {"total_usd": 0.12},
                "stage_times": {"silence": {"status": "done", "duration_s": 3.2}},
                "run_duration_s": 95.4,
                "skipped_none": None,  # None values must be dropped
            }
            path = save_run_manifest(config, [], extra=extra)
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["chapters"][0]["title"], "1. Intro")
            self.assertEqual(manifest["costs"], {"total_usd": 0.12})
            self.assertEqual(manifest["stage_times"]["silence"]["duration_s"], 3.2)
            self.assertNotIn("skipped_none", manifest)


class StageTimesTests(unittest.TestCase):
    def test_worker_records_stage_lifecycle(self):
        import queue as q
        from main import (ProcessingWorker, MSG_STAGE_START, MSG_STAGE_DONE,
                          MSG_STAGE_SKIP, MSG_STAGE_ERROR)
        w = ProcessingWorker(q.Queue(), {})
        w._emit(MSG_STAGE_START, "silence")
        w._emit(MSG_STAGE_DONE, "silence")
        w._emit(MSG_STAGE_SKIP, "borders")
        w._emit(MSG_STAGE_START, "transcript")
        w._emit(MSG_STAGE_ERROR, ("transcript", "boom"))

        times = w.stage_times()
        self.assertEqual(times["silence"]["status"], "done")
        self.assertIn("duration_s", times["silence"])
        self.assertIn("started_at", times["silence"])
        self.assertEqual(times["borders"]["status"], "skipped")
        self.assertEqual(times["transcript"]["status"], "error")
        self.assertEqual(times["transcript"]["error"], "boom")
        # Internal monotonic anchors must not leak into the manifest
        for rec in times.values():
            self.assertFalse(any(k.startswith("_") for k in rec))


class ApplyProfileTests(unittest.TestCase):
    def _config(self):
        return {"project": {
            "main_profile": {
                "name": "Base",
                "silence_removal": {"mode": "mark", "max_edits": 30},
            },
            "silence_only_profile": {
                "name": "Silence Only",
                "silence_removal": {"mode": "only", "max_edits": 10},
            },
        }}

    def test_main_profile_is_the_default(self):
        derived = apply_profile(self._config())  # profile_name defaults to "main"
        self.assertEqual(derived["project"]["name"], "Base")
        self.assertEqual(derived["project"]["silence_removal"]["mode"], "mark")

    def test_profile_overrides_project_keys(self):
        config = self._config()
        derived = apply_profile(config, "silence_only")
        self.assertEqual(derived["project"]["name"], "Silence Only")
        self.assertEqual(derived["project"]["silence_removal"]["mode"], "only")
        self.assertEqual(derived["project"]["silence_removal"]["max_edits"], 10)
        # Original untouched
        self.assertEqual(config["project"]["main_profile"]["silence_removal"]["mode"], "mark")

    def test_missing_profile_falls_back_to_main(self):
        config = self._config()
        derived = apply_profile(config, "nonexistent")
        self.assertEqual(derived["project"]["name"], "Base")

    def test_has_profile(self):
        from config import has_profile
        config = self._config()
        self.assertTrue(has_profile(config, "silence_only"))
        self.assertFalse(has_profile(config, "youtube_automation"))


if __name__ == "__main__":
    unittest.main()
