import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from silence.refiner import (
    chapter_removal_ranges,
    merge_ranges,
    no_speech_ranges,
    refine_silences,
    speech_gap_ranges,
    speech_spans,
    subtract_spans,
)


def _timing(words=None, segments=None):
    return {"words": words or [], "segments": segments or []}


def _word(start, end, word="w"):
    return {"start": start, "end": end, "word": word}


def _seg(start, end, text="t", no_speech_prob=0.0):
    return {"start": start, "end": end, "text": text, "no_speech_prob": no_speech_prob}


class MergeRangesTests(unittest.TestCase):
    def test_merges_overlapping_and_sorts(self):
        self.assertEqual(merge_ranges([(5.0, 8.0), (1.0, 3.0), (2.0, 4.0)]),
                         [(1.0, 4.0), (5.0, 8.0)])

    def test_drops_empty(self):
        self.assertEqual(merge_ranges([(3.0, 3.0), (5.0, 4.0)]), [])


class SpeechSpansTests(unittest.TestCase):
    def test_words_padded_and_merged(self):
        t = _timing(words=[_word(1.0, 1.5), _word(1.6, 2.0), _word(10.0, 10.5)])
        spans = speech_spans(t, pad_s=0.1)
        # First two words pad into each other and merge
        self.assertEqual(spans, [(0.9, 2.1), (9.9, 10.6)])

    def test_segment_fallback_skips_no_speech(self):
        t = _timing(segments=[_seg(0.0, 5.0), _seg(5.0, 8.0, no_speech_prob=0.95)])
        spans = speech_spans(t, pad_s=0.0, no_speech_prob_threshold=0.85)
        self.assertEqual(spans, [(0.0, 5.0)])


class SubtractSpansTests(unittest.TestCase):
    def test_speech_inside_silence_splits_it(self):
        # A quiet word at 12-13s inside a 10-15s "silence" must be rescued
        pieces = subtract_spans([(10.0, 15.0)], [(12.0, 13.0)])
        self.assertEqual(pieces, [(10.0, 12.0), (13.0, 15.0)])

    def test_speech_overlapping_edge_shrinks_silence(self):
        pieces = subtract_spans([(10.0, 15.0)], [(9.0, 11.0)])
        self.assertEqual(pieces, [(11.0, 15.0)])

    def test_fully_covered_silence_removed(self):
        self.assertEqual(subtract_spans([(10.0, 12.0)], [(9.0, 13.0)]), [])

    def test_min_piece_drops_slivers(self):
        pieces = subtract_spans([(10.0, 15.0)], [(10.4, 14.8)], min_piece_s=0.6)
        self.assertEqual(pieces, [])  # 0.4s and 0.2s leftovers both too short


class NoSpeechRangesTests(unittest.TestCase):
    def test_only_high_prob_long_segments(self):
        t = _timing(segments=[
            _seg(0.0, 5.0, no_speech_prob=0.1),   # speech: keep out
            _seg(5.0, 8.0, no_speech_prob=0.9),   # noise, 3s: candidate
            _seg(8.0, 8.5, no_speech_prob=0.9),   # noise but too short
        ])
        self.assertEqual(no_speech_ranges(t, prob_threshold=0.85, min_duration_s=1.0),
                         [(5.0, 8.0)])


class RefineSilencesTests(unittest.TestCase):
    def test_no_timing_passes_through_merged(self):
        out = refine_silences([(1.0, 2.0), (1.5, 3.0)], {}, [(10.0, 12.0)])
        self.assertEqual(out, [(1.0, 3.0), (10.0, 12.0)])

    def test_rescues_quiet_speech(self):
        timing = _timing(words=[_word(11.0, 12.0)])
        out = refine_silences([(10.0, 14.0)], timing, speech_pad_s=0.1,
                              min_silence_duration_s=0.5)
        self.assertEqual(out, [(10.0, 10.9), (12.1, 14.0)])

    def test_extra_ranges_not_speech_rescued(self):
        # An "irrelevant" chapter window is cut even though it contains speech
        timing = _timing(words=[_word(20.0, 25.0)])
        out = refine_silences([], timing, extra_ranges=[(18.0, 30.0)])
        self.assertEqual(out, [(18.0, 30.0)])

    def test_no_speech_segments_added_when_enabled(self):
        timing = _timing(words=[_word(1.0, 2.0)],
                         segments=[_seg(5.0, 9.0, no_speech_prob=0.95)])
        out = refine_silences([], timing, add_no_speech_segments=True,
                              min_silence_duration_s=0.6)
        self.assertEqual(out, [(5.0, 9.0)])
        out_off = refine_silences([], timing, add_no_speech_segments=False)
        self.assertEqual(out_off, [])


class SpeechGapRangesTests(unittest.TestCase):
    def test_long_gap_between_words_found(self):
        # Speech at 0-5s and 20-25s: the 15s music/idle gap is removable
        t = _timing(words=[_word(0.0, 5.0), _word(20.0, 25.0)])
        out = speech_gap_ranges(t, min_gap_s=4.0, edge_pad_s=0.5)
        self.assertEqual(out, [(5.5, 19.5)])

    def test_short_gaps_ignored(self):
        t = _timing(words=[_word(0.0, 5.0), _word(7.0, 12.0)])
        self.assertEqual(speech_gap_ranges(t, min_gap_s=4.0), [])

    def test_leading_gap_before_first_word(self):
        t = _timing(words=[_word(10.0, 15.0)])
        out = speech_gap_ranges(t, min_gap_s=4.0, edge_pad_s=0.5)
        self.assertEqual(out, [(0.5, 9.5)])

    def test_trailing_gap_needs_total_duration(self):
        t = _timing(words=[_word(0.0, 5.0)])
        self.assertEqual(speech_gap_ranges(t, min_gap_s=4.0), [])
        out = speech_gap_ranges(t, min_gap_s=4.0, edge_pad_s=0.5,
                                total_duration_s=30.0)
        self.assertEqual(out, [(5.5, 29.5)])

    def test_no_speech_returns_nothing(self):
        # An all-music file shouldn't be wholesale deleted by gap logic
        self.assertEqual(speech_gap_ranges(_timing(), min_gap_s=4.0,
                                           total_duration_s=100.0), [])

    def test_refine_silences_includes_long_gaps_when_enabled(self):
        timing = _timing(words=[_word(0.0, 5.0), _word(30.0, 35.0)])
        out = refine_silences([], timing, long_gap_min_s=4.0,
                              long_gap_edge_pad_s=0.5)
        self.assertEqual(out, [(5.5, 29.5)])
        self.assertEqual(refine_silences([], timing, long_gap_min_s=0.0), [])


class _FakeChapter:
    def __init__(self, segment_types):
        self.segment_types = segment_types


class ChapterRemovalRangesTests(unittest.TestCase):
    def test_matches_type_and_confidence(self):
        ch = _FakeChapter([
            {"type": "irrelevant", "confidence": 0.9, "start_time": 10.0, "end_time": 40.0},
            {"type": "irrelevant", "confidence": 0.4, "start_time": 50.0, "end_time": 60.0},
            {"type": "working", "confidence": 0.99, "start_time": 70.0, "end_time": 90.0},
        ])
        out = chapter_removal_ranges([ch], ["irrelevant"], min_confidence=0.7)
        self.assertEqual(out, [(10.0, 40.0)])

    def test_empty_types_disables(self):
        ch = _FakeChapter([{"type": "irrelevant", "confidence": 1.0,
                            "start_time": 0.0, "end_time": 5.0}])
        self.assertEqual(chapter_removal_ranges([ch], []), [])


if __name__ == "__main__":
    unittest.main()
