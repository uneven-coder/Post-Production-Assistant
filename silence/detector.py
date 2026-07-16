# Silence detection using unsilence's Interval/Intervals model for merging/padding.
# Reimplements the ffmpeg stderr parsing step (unsilence's own regex targets an older
# ffmpeg log format and finds nothing against modern builds).
import json
import re
import subprocess

from unsilence.lib.intervals.Interval import Interval
from unsilence.lib.intervals.Intervals import Intervals

_SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[\d.]+)")


def _probe_duration(file_path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "json", file_path]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


def _detect_raw_intervals(file_path: str, threshold_db: float, min_duration_s: float) -> Intervals:
    cmd = ["ffmpeg", "-i", file_path, "-vn", "-af",
           f"silencedetect=noise={threshold_db}dB:d={min_duration_s}", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)

    intervals = Intervals()
    current = Interval(start=0, end=0, is_silent=False)
    for event, time_str in _SILENCE_RE.findall(r.stderr):
        time = float(time_str)
        if event == "start":
            if current.start != time:
                current.end = time
                intervals.add_interval(current)
            current = Interval(start=time, is_silent=True)
        else:
            current.end = time
            intervals.add_interval(current)
            current = Interval(start=time, is_silent=False)

    current.end = _probe_duration(file_path)
    intervals.add_interval(current)
    return intervals


def silent_intervals(
    file_path: str, threshold_db: float = -35, min_duration_s: float = 0.6,
    padding_s: float = 0.12,
) -> list[tuple[float, float]]:
    # Main entrypoint: detects silent windows, already merged/padded via unsilence's optimizer.
    raw = _detect_raw_intervals(file_path, threshold_db, min_duration_s)
    intervals = raw.copy()
    try:
        intervals.optimize(short_interval_threshold=min_duration_s, stretch_time=padding_s * 2)
    except Exception as e:
        print(f"[warn] Silence padding step failed ({e}) - retrying without padding.")
        intervals = raw.copy()
        try:
            intervals.optimize(short_interval_threshold=min_duration_s, stretch_time=0)
        except Exception as e2:
            # unsilence's own combine step can itself produce a zero-duration interval
            # (a merge artifact from clustered start/end events), which then blows up
            # enlarge_audible_interval() even with stretch_time=0 - optimize() offers no
            # further fallback at that point. Falling back to the raw, un-merged detect
            # output keeps the pipeline running instead of failing the whole run; it just
            # means silence boundaries won't be padded/merged as tightly this time.
            print(f"[warn] Silence merge step also failed ({e2}) - using raw, "
                  f"un-merged intervals instead.")
            intervals = raw

    return [(iv.start, iv.end) for iv in intervals.intervals if iv.is_silent and iv.end > iv.start]


def cap_intervals(
    intervals: list[tuple[float, float]], max_count: int,
) -> list[tuple[float, float]]:
    # Long/noisy sources can produce far more silence cuts than is practical to apply
    # anywhere downstream (YouTube Studio's editor bogs down past a few hundred manual
    # edits; a Premiere timeline with hundreds of hatched-out silence clips is unusable
    # to scrub). max_count caps that - shared by every consumer of silence intervals
    # (transcript trimming, the Premiere/FCPXML timeline, previews, and YouTube Studio
    # automation) so they all agree on the same filtered cut list rather than each
    # independently deciding which silences "count".
    #
    # A plain top-N-by-duration cut would cluster wherever silence happens to run
    # longest (e.g. one rambly intro), leaving the rest of the video untouched. A
    # plain evenly-spaced sample would waste edits on tiny/unimportant gaps just to
    # keep spacing uniform. This splits the timeline into max_count buckets (spread
    # across the full duration) and keeps each bucket's single longest interval - so
    # edits track wherever the source actually has silence worth trimming, not raw
    # chronological order. Buckets with no interval in them free up their slot for
    # whichever remaining intervals (from other buckets) are individually longest, so
    # the cap still prioritizes the most worthwhile edits instead of leaving slots
    # unused.
    if max_count < 0 or len(intervals) <= max_count:
        return list(intervals)
    if max_count == 0:
        return []

    ordered = sorted(intervals)
    start = ordered[0][0]
    span = ordered[-1][1] - start
    if span <= 0:
        return sorted(sorted(ordered, key=lambda iv: iv[1] - iv[0], reverse=True)[:max_count])

    bucket_width = span / max_count
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(max_count)]
    for s, e in ordered:
        idx = int(((s + e) / 2 - start) / bucket_width)
        idx = max(0, min(max_count - 1, idx))
        buckets[idx].append((s, e))

    selected: list[tuple[float, float]] = []
    leftover: list[tuple[float, float]] = []
    for bucket in buckets:
        if not bucket:
            continue
        bucket.sort(key=lambda iv: iv[1] - iv[0], reverse=True)
        selected.append(bucket[0])
        leftover.extend(bucket[1:])

    deficit = max_count - len(selected)
    if deficit > 0 and leftover:
        leftover.sort(key=lambda iv: iv[1] - iv[0], reverse=True)
        selected.extend(leftover[:deficit])

    return sorted(selected)


def invert_intervals(
    silence: list[tuple[float, float]], total_duration: float,
) -> list[tuple[float, float]]:
    # Returns the complementary (non-silent) intervals to keep; `silence` is expected to
    # already be padded.
    cleaned = sorted((max(0.0, s), min(total_duration, e)) for s, e in silence if e > s)

    keep = []
    cursor = 0.0
    for s, e in cleaned:
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total_duration:
        keep.append((cursor, total_duration))
    return keep


def map_trimmed_to_original(t: float, keep_intervals: list[tuple[float, float]]) -> float:
    # Maps a timestamp inside the concatenated trimmed audio back to the original timeline.
    if not keep_intervals:
        return t
    cursor = 0.0
    for s, e in keep_intervals:
        seg_len = e - s
        if t <= cursor + seg_len:
            return s + (t - cursor)
        cursor += seg_len
    return keep_intervals[-1][1]


def build_trimmed_audio(
    src_path: str, keep_intervals: list[tuple[float, float]], output_path: str,
) -> None:
    # Concatenates the keep_intervals from src_path into a single trimmed audio file.
    if not keep_intervals:
        raise RuntimeError("No non-silent audio to transcribe")

    filter_parts = [
        f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]"
        for i, (s, e) in enumerate(keep_intervals)
    ]
    concat_inputs = "".join(f"[a{i}]" for i in range(len(keep_intervals)))
    filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={len(keep_intervals)}:v=0:a=1[out]"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path,
           "-filter_complex", filter_complex, "-map", "[out]",
           "-ar", "16000", "-ac", "1", output_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Failed to build trimmed audio: {r.stderr}")
