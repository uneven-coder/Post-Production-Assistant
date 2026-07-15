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
    intervals = _detect_raw_intervals(file_path, threshold_db, min_duration_s)
    intervals.optimize(short_interval_threshold=min_duration_s, stretch_time=padding_s * 2)
    return [(iv.start, iv.end) for iv in intervals.intervals if iv.is_silent and iv.end > iv.start]


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
