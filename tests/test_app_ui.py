"""Headless UI tests: instantiate the real Tk app (window never shown) and
exercise the logic-bearing paths — config sync, mode selection, and pipeline
queue-message dispatch. Skipped automatically where no display is available."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tkinter as tk

from main import (
    MSG_LOG, MSG_STAGE_START, MSG_STAGE_DONE, MSG_STAGE_SKIP, MSG_STAGE_ERROR,
    MSG_SILENCE, MSG_BORDERS, MSG_COST,
)


def _make_app():
    from app import PAEApp
    app = PAEApp()
    app.withdraw()  # never show the window
    return app


class ChapterAttrHelperTests(unittest.TestCase):
    """_chapter_attr/_chapter_set must work identically for live Chapter objects
    (this session's run) and plain dicts (chapters loaded from a past run's
    manifest/chapters.json) - this is pure logic, no Tk needed."""

    def test_reads_from_dict_chapter(self):
        from app import _chapter_attr
        ch = {"title": "1. Intro", "start_time": 0.0, "end_time": 30.0,
              "segment_types": [{"type": "setup"}]}
        self.assertEqual(_chapter_attr(ch, "title"), "1. Intro")
        self.assertEqual(_chapter_attr(ch, "start_time", "start"), 0.0)
        self.assertEqual(_chapter_attr(ch, "segment_types"), [{"type": "setup"}])
        self.assertIsNone(_chapter_attr(ch, "nonexistent"))

    def test_reads_from_object_chapter(self):
        from app import _chapter_attr

        class FakeChapter:
            def __init__(self):
                self.title = "1. Intro"
                self.start_time = 0.0
                self.segment_types = [{"type": "setup"}]

        ch = FakeChapter()
        self.assertEqual(_chapter_attr(ch, "title"), "1. Intro")
        self.assertEqual(_chapter_attr(ch, "start_time", "start"), 0.0)
        self.assertEqual(_chapter_attr(ch, "segment_types"), [{"type": "setup"}])

    def test_set_on_dict_and_object(self):
        from app import _chapter_attr, _chapter_set

        d = {"title": "old"}
        _chapter_set(d, "title", "new")
        self.assertEqual(d["title"], "new")

        class FakeChapter:
            title = "old"

        obj = FakeChapter()
        _chapter_set(obj, "title", "new")
        self.assertEqual(obj.title, "new")
        self.assertEqual(_chapter_attr(obj, "title"), "new")


def _tk_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


@unittest.skipUnless(_tk_available(), "no display available for Tk")
class PAEAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        except tk.TclError:
            pass

    def setUp(self):
        self.app.state.reset_run()

    def test_config_loaded_from_repo(self):
        self.assertIn("project", self.app.state.config)

    def test_current_silence_mode_reads_config(self):
        mode = self.app._current_silence_mode()
        self.assertIn(mode, ("off", "mark", "only"))

    def test_mode_change_round_trips_into_config(self):
        original = self.app._current_silence_mode()
        try:
            self.app._mode_var.set("only")
            self.app._on_mode_change()
            self.assertEqual(
                self.app.state.config["project"]["main_profile"]["silence_removal"]["mode"],
                "only")
        finally:
            self.app._mode_var.set(original)
            self.app._on_mode_change()

    def test_stage_dispatch_updates_stage_states(self):
        self.app._dispatch(MSG_STAGE_START, "silence")
        stage = next(s for s in self.app.state.stages if s.id == "silence")
        self.assertEqual(stage.status, "running")

        self.app._dispatch(MSG_STAGE_DONE, "silence")
        self.assertEqual(stage.status, "done")

        self.app._dispatch(MSG_STAGE_SKIP, "borders")
        self.assertEqual(
            next(s for s in self.app.state.stages if s.id == "borders").status,
            "skipped")

        self.app._dispatch(MSG_STAGE_ERROR, ("timeline", "boom"))
        timeline = next(s for s in self.app.state.stages if s.id == "timeline")
        self.assertEqual(timeline.status, "error")
        self.assertEqual(timeline.message, "boom")

    def test_silence_message_stores_intervals(self):
        self.app._dispatch(MSG_SILENCE, [(1.0, 2.0), (5.0, 8.0)])
        self.assertEqual(self.app.state.silent_intervals, [(1.0, 2.0), (5.0, 8.0)])

    def test_borders_message_stores_images(self):
        self.app._dispatch(MSG_BORDERS, ["a.png"])
        self.assertEqual(self.app.state.border_images, ["a.png"])

    def test_dict_chapters_render_with_real_titles_and_types(self):
        # Simulates a past run loaded via "Open Run": chapters come back as
        # plain dicts (json-decoded), not live Chapter objects.
        dict_chapters = [
            {"title": "1. Kickoff", "start_time": 0.0, "end_time": 30.0,
             "duration": 30.0, "text": "hello",
             "segment_types": [{"type": "setup", "confidence": 0.9,
                                "summary": "getting set up",
                                "start_time": 0.0, "end_time": 30.0}]},
            {"title": "2. Deep Dive", "start_time": 30.0, "end_time": 90.0,
             "duration": 60.0, "text": "world",
             "segment_types": [{"type": "working", "confidence": 0.8,
                                "summary": "", "start_time": 30.0, "end_time": 90.0}]},
        ]
        self.app._set_chapters(dict_chapters)

        children = self.app._chapters_tree.get_children("")
        self.assertEqual(len(children), 2)
        titles = [self.app._chapters_tree.item(c, "text") for c in children]
        self.assertEqual(titles, ["1. Kickoff", "2. Deep Dive"])
        # Segment-type sub-rows (the old getattr() bug made these always empty)
        self.assertEqual(len(self.app._chapters_tree.get_children(children[0])), 1)
        first_values = self.app._chapters_tree.item(children[0], "values")
        self.assertEqual(first_values[0], "setup")  # dominant segment type

        # Timeline canvas geometry needs a mapped window/selected sub-tab to have
        # real pixel dimensions, which isn't reliable in a headless test run - so
        # exercise the draw logic directly against a wide fake width instead of
        # asserting on the real (unmapped) canvas's segment lists.
        self.app._timeline_canvas.winfo_width = lambda: 1000
        self.app._timeline_canvas.winfo_height = lambda: 210
        self.app._redraw_timeline()
        self.assertEqual(len(self.app._timeline_ch_segs), 2)
        self.assertEqual(len(self.app._timeline_seg_segs), 2)
        # Segment block colors reflect real classification types, not the
        # "default"/blank fallback the old getattr() bug produced
        _, _, _, _, first_seg = self.app._timeline_seg_segs[0]
        self.assertEqual(first_seg["type"], "setup")

    def test_rename_and_merge_work_on_dict_chapters(self):
        from app import _chapter_attr
        self.app.state.chapters = [
            {"title": "A", "start_time": 0.0, "end_time": 10.0, "duration": 10.0,
             "text": "a", "segment_types": [{"type": "setup"}]},
            {"title": "B", "start_time": 10.0, "end_time": 20.0, "duration": 10.0,
             "text": "b", "segment_types": [{"type": "working"}]},
        ]
        self.app._refresh_chapters_tree()

        _chapter_set = __import__("app")._chapter_set
        _chapter_set(self.app.state.chapters[0], "title", "Renamed")
        self.assertEqual(self.app.state.chapters[0]["title"], "Renamed")

        self.app._merge_chapter_by_index(0)
        self.assertEqual(len(self.app.state.chapters), 1)
        merged = self.app.state.chapters[0]
        self.assertEqual(_chapter_attr(merged, "end_time"), 20.0)
        self.assertEqual(len(_chapter_attr(merged, "segment_types")), 2)

    def test_cost_message_stores_estimate(self):
        est = {"total": 0.42, "whisper": 0.3, "classification": 0.1, "titles": 0.02}
        self.app._dispatch(MSG_COST, {"estimate": est})
        self.assertEqual(self.app.state.cost_estimate, est)

    def test_cost_log_line_lands_in_cost_tab(self):
        self.app._dispatch(MSG_LOG, "Estimated run cost: ~$0.123 (transcription ...)")
        self.assertTrue(any("$0.123" in line for line in self.app.state.cost_lines))

    def test_reset_run_clears_run_state(self):
        self.app._dispatch(MSG_SILENCE, [(1.0, 2.0)])
        self.app._dispatch(MSG_COST, {"estimate": {"total": 1.0}})
        self.app.state.reset_run()
        self.assertEqual(self.app.state.silent_intervals, [])
        self.assertIsNone(self.app.state.cost_estimate)
        self.assertTrue(all(s.status == "pending" for s in self.app.state.stages))


if __name__ == "__main__":
    unittest.main()
