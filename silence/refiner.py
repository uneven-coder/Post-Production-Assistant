"""Transcript-assisted refinement of audio-detected silence intervals.

The dB-threshold detector in silence/detector.py is blind to *what* is quiet:
it cuts soft-spoken words and keeps loud non-speech (keyboard clatter, fans).
Given Whisper timing data ({'segments': [...], 'words': [...]}, timestamps in
the source timebase), this module:

  - rescues speech: spoken-word spans (padded) are subtracted from silence
    intervals, which also snaps the remaining cut edges to natural word
    boundaries instead of raw dB crossings;
  - adds non-speech: transcript segments Whisper itself marks as unlikely to
    contain speech (high no_speech_prob) become extra removal candidates even
    when they were too loud for the audio threshold;
  - merges in external removal ranges (e.g. chapter windows classified as
    irrelevant content).
"""


def merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort and merge overlapping/touching ranges; drops empty ones."""
    cleaned = sorted((s, e) for s, e in ranges if e > s)
    merged: list[list[float]] = []
    for s, e in cleaned:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def speech_spans(timing: dict, pad_s: float = 0.08,
                 no_speech_prob_threshold: float = 0.85) -> list[tuple[float, float]]:
    """Padded spans that must be protected from cutting.

    Prefers word timestamps; falls back to segment spans (excluding segments
    Whisper flags as probably-not-speech) when words are unavailable."""
    words = (timing or {}).get("words") or []
    if words:
        raw = [(w["start"] - pad_s, w["end"] + pad_s) for w in words]
    else:
        raw = [(s["start"] - pad_s, s["end"] + pad_s)
               for s in (timing or {}).get("segments") or []
               if s.get("no_speech_prob", 0.0) < no_speech_prob_threshold]
    return merge_ranges(raw)


def subtract_spans(intervals: list[tuple[float, float]],
                   protected: list[tuple[float, float]],
                   min_piece_s: float = 0.0) -> list[tuple[float, float]]:
    """Remove `protected` spans from `intervals`; pieces shorter than
    `min_piece_s` are dropped (they're no longer worth an edit)."""
    if not protected:
        return [(s, e) for s, e in intervals if e - s >= min_piece_s]

    result: list[tuple[float, float]] = []
    for s, e in intervals:
        cursor = s
        for ps, pe in protected:
            if pe <= cursor or ps >= e:
                continue
            if ps > cursor:
                result.append((cursor, ps))
            cursor = max(cursor, pe)
            if cursor >= e:
                break
        if cursor < e:
            result.append((cursor, e))
    return [(s, e) for s, e in result if e - s >= min_piece_s]


def speech_gap_ranges(timing: dict, min_gap_s: float = 4.0,
                      edge_pad_s: float = 0.5,
                      total_duration_s: float = None) -> list[tuple[float, float]]:
    """Long stretches with no speech at all — music beds, idle screen time,
    waiting on builds. Unlike the dB detector these are found purely from the
    transcript, so they're caught even when the audio is loud.

    Gaps between consecutive speech spans longer than `min_gap_s` become
    removal candidates, shrunk by `edge_pad_s` per side so the edit doesn't
    feel abrupt. Pass `total_duration_s` to also catch a trailing gap after
    the last spoken word."""
    spans = speech_spans(timing, pad_s=0.0)
    if not spans:
        return []

    gaps = []
    prev_end = 0.0
    for s, e in spans:
        gaps.append((prev_end, s))
        prev_end = e
    if total_duration_s and total_duration_s > prev_end:
        gaps.append((prev_end, total_duration_s))

    return merge_ranges([
        (gs + edge_pad_s, ge - edge_pad_s)
        for gs, ge in gaps
        if ge - gs >= min_gap_s
    ])


def no_speech_ranges(timing: dict, prob_threshold: float = 0.85,
                     min_duration_s: float = 1.0) -> list[tuple[float, float]]:
    """Transcript segments Whisper marks as probably-not-speech — removal
    candidates the dB detector may have missed (loud but content-free)."""
    return merge_ranges([
        (s["start"], s["end"])
        for s in (timing or {}).get("segments") or []
        if s.get("no_speech_prob", 0.0) >= prob_threshold
        and s["end"] - s["start"] >= min_duration_s
    ])


def refine_silences(
    silence: list[tuple[float, float]],
    timing: dict,
    extra_ranges: list[tuple[float, float]] = (),
    *,
    speech_pad_s: float = 0.08,
    min_silence_duration_s: float = 0.6,
    no_speech_prob_threshold: float = 0.85,
    add_no_speech_segments: bool = False,
    long_gap_min_s: float = 0.0,
    long_gap_edge_pad_s: float = 0.5,
    total_duration_s: float = None,
) -> list[tuple[float, float]]:
    """Produce the transcript-refined removal list.

    `extra_ranges` (e.g. irrelevant-chapter windows) are removed as-is — they
    intentionally contain speech, so speech rescue doesn't apply to them."""
    has_timing = bool((timing or {}).get("words") or (timing or {}).get("segments"))
    if not has_timing:
        return merge_ranges(list(silence) + list(extra_ranges))

    protected = speech_spans(timing, pad_s=speech_pad_s,
                             no_speech_prob_threshold=no_speech_prob_threshold)
    refined = subtract_spans(silence, protected, min_piece_s=min_silence_duration_s)

    candidates = refined + list(extra_ranges)
    if add_no_speech_segments:
        candidates += no_speech_ranges(timing, prob_threshold=no_speech_prob_threshold,
                                       min_duration_s=min_silence_duration_s)
    if long_gap_min_s and long_gap_min_s > 0:
        candidates += speech_gap_ranges(timing, min_gap_s=long_gap_min_s,
                                        edge_pad_s=long_gap_edge_pad_s,
                                        total_duration_s=total_duration_s)
    return merge_ranges(candidates)


def chapter_removal_ranges(chapters: list, remove_types: list[str],
                           min_confidence: float = 0.7) -> list[tuple[float, float]]:
    """Removal ranges from chapter windows whose LLM classification matches
    `remove_types` at or above `min_confidence`. Works on the Chapter objects
    from chapters/segmenter.py (uses their per-window segment_types)."""
    if not remove_types:
        return []
    wanted = {t.strip().lower() for t in remove_types}
    ranges = []
    for ch in chapters or []:
        for seg in getattr(ch, "segment_types", None) or []:
            if (seg.get("type") or "").lower() in wanted \
                    and float(seg.get("confidence") or 0.0) >= min_confidence:
                ranges.append((float(seg["start_time"]), float(seg["end_time"])))
    return merge_ranges(ranges)
