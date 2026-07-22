import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from chapters.segmenter import TranscriptSegment, TranscriptSegmenter


def _config(merging=None):
    return {"project": {
        "transcript_windows": {"target_segment_length_s": 60},
        "chapters": {"merging": merging or {}},
    }}


def _groups(n, group_dur=60.0):
    """n one-segment groups laid back-to-back, group_dur seconds each."""
    out = []
    for i in range(n):
        t = i * group_dur
        out.append([TranscriptSegment(f"text {i}", t, t + group_dur, i)])
    return out


def _cls(types_topics):
    """classification_map from [(type, topic), ...]."""
    return {
        i: {"type": ty, "confidence": 0.9, "summary": "", "topic": topic}
        for i, (ty, topic) in enumerate(types_topics)
    }


class MergeAdjacentGroupsTests(unittest.TestCase):
    def _merge(self, groups, cls_map, merging):
        seg = TranscriptSegmenter(_config(merging), use_llm=False)
        return seg._merge_adjacent_groups(groups, cls_map)

    def test_same_type_same_topic_merges(self):
        proto = self._merge(
            _groups(3),
            _cls([("working", "auth flow")] * 3),
            {"max_chapter_duration_s": 600, "split_on_topic_change": True},
        )
        self.assertEqual(len(proto), 1)

    def test_type_change_splits(self):
        proto = self._merge(
            _groups(4),
            _cls([("working", ""), ("working", ""), ("testing", ""), ("testing", "")]),
            {"max_chapter_duration_s": 600},
        )
        self.assertEqual(len(proto), 2)
        self.assertEqual([p["type"] for p in proto], ["working", "testing"])

    def test_topic_change_splits_same_type(self):
        proto = self._merge(
            _groups(4),
            _cls([("working", "auth flow"), ("working", "auth flow"),
                  ("working", "database layer"), ("working", "database layer")]),
            {"max_chapter_duration_s": 600, "min_chapter_duration_s": 45,
             "split_on_topic_change": True},
        )
        self.assertEqual(len(proto), 2)
        self.assertEqual(proto[0]["end"], 120.0)

    def test_topic_split_disabled(self):
        proto = self._merge(
            _groups(4),
            _cls([("working", "a"), ("working", "a"), ("working", "b"), ("working", "b")]),
            {"max_chapter_duration_s": 600, "split_on_topic_change": False},
        )
        self.assertEqual(len(proto), 1)

    def test_topic_split_respects_min_duration(self):
        # Topic flips every group but min duration forbids sub-90s chapters
        proto = self._merge(
            _groups(4, group_dur=60.0),
            _cls([("working", "a"), ("working", "b"), ("working", "c"), ("working", "d")]),
            {"max_chapter_duration_s": 600, "min_chapter_duration_s": 90,
             "split_on_topic_change": True},
        )
        for pc in proto:
            self.assertGreaterEqual(pc["end"] - pc["start"], 90.0)

    def test_max_duration_forces_split(self):
        proto = self._merge(
            _groups(6, group_dur=60.0),
            _cls([("working", "")] * 6),
            {"max_chapter_duration_s": 180},
        )
        self.assertGreater(len(proto), 1)

    def test_short_trailing_chapter_folds_into_previous(self):
        # 2x60s working then one 20s testing straggler
        groups = _groups(2, group_dur=60.0)
        groups.append([TranscriptSegment("tail", 120.0, 140.0, 2)])
        proto = self._merge(
            groups,
            _cls([("working", ""), ("working", ""), ("testing", "")]),
            {"max_chapter_duration_s": 600, "min_chapter_duration_s": 45},
        )
        self.assertEqual(len(proto), 1)
        self.assertEqual(proto[0]["end"], 140.0)

    def test_short_opening_chapter_folds_forward(self):
        groups = [[TranscriptSegment("intro", 0.0, 20.0, 0)]]
        groups += [[TranscriptSegment(f"g{i}", 20.0 + (i - 1) * 60.0,
                                      20.0 + i * 60.0, i)] for i in (1, 2)]
        proto = self._merge(
            groups,
            _cls([("setup", ""), ("working", ""), ("working", "")]),
            {"max_chapter_duration_s": 600, "min_chapter_duration_s": 45},
        )
        self.assertEqual(len(proto), 1)
        self.assertEqual(proto[0]["start"], 0.0)
        self.assertEqual(proto[0]["end"], 140.0)


class TimingSegmentsTests(unittest.TestCase):
    def test_segments_built_from_whisper_timing(self):
        seg = TranscriptSegmenter(_config(), use_llm=False)
        timing = {"segments": [
            {"start": 0.0, "end": 4.5, "text": "hello there"},
            {"start": 4.5, "end": 9.0, "text": "  "},          # blank: skipped
            {"start": 9.0, "end": 12.0, "text": "more speech"},
        ]}
        out = seg._segments_from_timing(timing)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].start_time, 0.0)
        self.assertEqual(out[1].end_time, 12.0)
        self.assertEqual([s.index for s in out], [0, 1])

    def test_no_timing_returns_empty(self):
        seg = TranscriptSegmenter(_config(), use_llm=False)
        self.assertEqual(seg._segments_from_timing(None), [])
        self.assertEqual(seg._segments_from_timing({"segments": []}), [])


if __name__ == "__main__":
    unittest.main()
