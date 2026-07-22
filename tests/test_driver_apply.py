import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from youtube_automation.driver import _apply_cuts_with_progress, _APPLY_ALL_CUTS_JS


class _FakePage:
    """Stands in for a Playwright page: records evaluate() calls and returns a
    scripted outcome for the apply-cuts script."""

    def __init__(self, outcome=None, fail_with=None):
        self.calls = []  # (script, arg)
        self._outcome = outcome
        self._fail_with = fail_with

    def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        if script == _APPLY_ALL_CUTS_JS:
            if self._fail_with:
                raise self._fail_with
            return self._outcome
        return None  # progress-bar updates etc.

    def cut_call_args(self):
        return [arg for script, arg in self.calls if script == _APPLY_ALL_CUTS_JS]


def _ok_outcome(n):
    return {"results": [{"ok": True} for _ in range(n)]}


class ApplyCutsTests(unittest.TestCase):
    def test_no_cuts_makes_no_apply_call(self):
        page = _FakePage()
        msg = _apply_cuts_with_progress(page, [], 100.0, lambda *a: None)
        self.assertIn("No silence cuts", msg)
        self.assertEqual(page.cut_call_args(), [])

    def test_cuts_sent_as_ms_pairs_in_order(self):
        page = _FakePage(outcome=_ok_outcome(2))
        cuts = [(1.5, 2.25), (10.0, 12.5)]
        msg = _apply_cuts_with_progress(page, cuts, 100.0, lambda *a: None)
        self.assertEqual(page.cut_call_args(), [[(1500, 2250), (10000, 12500)]])
        self.assertIn("2 cuts applied", msg)

    def test_cuts_clamped_to_duration_and_zero(self):
        page = _FakePage(outcome=_ok_outcome(2))
        cuts = [(-0.5, 2.0), (95.0, 130.0)]
        _apply_cuts_with_progress(page, cuts, 100.0, lambda *a: None)
        self.assertEqual(page.cut_call_args(), [[(0, 2000), (95000, 100000)]])

    def test_partial_failure_reported(self):
        outcome = {"results": [{"ok": True}, {"ok": False, "error": "boom"},
                               {"ok": True}]}
        page = _FakePage(outcome=outcome)
        reports = []
        msg = _apply_cuts_with_progress(page, [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)],
                                        100.0, lambda step, m: reports.append((step, m)))
        self.assertIn("2/3 cuts applied", msg)
        self.assertIn("boom", msg)
        self.assertTrue(any("cuts" == step for step, _ in reports))

    def test_panel_missing_reported(self):
        page = _FakePage(outcome={"panelMissing": True})
        msg = _apply_cuts_with_progress(page, [(1.0, 2.0)], 100.0, lambda *a: None)
        self.assertIn("Trim & cut panel", msg)

    def test_evaluate_exception_is_contained(self):
        page = _FakePage(fail_with=RuntimeError("page crashed"))
        msg = _apply_cuts_with_progress(page, [(1.0, 2.0)], 100.0, lambda *a: None)
        self.assertIn("Failed to apply cuts", msg)
        self.assertIn("page crashed", msg)


if __name__ == "__main__":
    unittest.main()
