# ffmpeg-based rendering for the in-app timeline preview: composites overlays/borders onto
# the main video and applies silence cuts by trimming each stream first, then compositing.
import concurrent.futures
import os
import re
import subprocess
import uuid
from typing import Callable, Optional

from silence.detector import _probe_duration

DEFAULT_PREVIEW_MAX_WIDTH = 960
FAST_PRESET = "ultrafast"

ProgressCallback = Optional[Callable[[float, str], None]]

_OUT_TIME_RE = re.compile(rb"out_time_ms=(-?\d+)")


def _run_ffmpeg_tracked(cmd: list[str], duration: float,
                        progress_callback: ProgressCallback, base: float, span: float) -> None:
    # Reports real progress via ffmpeg's own `-progress pipe:1` output, not a simulated bar.
    if not progress_callback or duration <= 0:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return

    tracked_cmd = cmd[:2] + ["-progress", "pipe:1", "-nostats"] + cmd[2:]
    proc = subprocess.Popen(tracked_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_chunks = []
    try:
        for line in proc.stdout:
            m = _OUT_TIME_RE.match(line)
            if m:
                out_time_s = max(0, int(m.group(1))) / 1_000_000.0
                frac = min(1.0, out_time_s / duration)
                progress_callback(base + span * frac, "Compositing")
    finally:
        stderr_chunks.append(proc.stderr.read())
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(b"".join(stderr_chunks).decode(errors="replace"))


def _resolve_path(vcfg: dict) -> str | None:
    pi = (vcfg or {}).get("path", {})
    f = pi.get("file")
    return f if f and os.path.exists(f) else None


def _find_main_video(config: dict) -> dict | None:
    videos = config.get("project", {}).get("videos", [])
    return (next((v for v in videos if "main" in (v.get("tags") or [])), None)
            or next((v for v in videos if "audio_source" in (v.get("tags") or [])), None))


def _scaled_frame_size(frame_width: int, frame_height: int,
                       max_width: int = DEFAULT_PREVIEW_MAX_WIDTH) -> tuple[int, int]:
    if frame_width <= max_width:
        return frame_width, frame_height
    scale = max_width / frame_width
    # even dimensions - libx264 rejects odd width/height
    return max_width - (max_width % 2), int(round(frame_height * scale)) & ~1


def _overlay_placement(overlay: dict, frame_width: int, frame_height: int) -> tuple[float, float, float, float]:
    # Mirrors premiere/builder.py's _normalized_position + _apply_clip_motion.
    w_pct = float(overlay.get("width", 0.3))
    h_pct = float(overlay.get("height", w_pct))
    tw = w_pct * frame_width
    th = h_pct * frame_height
    nx = float(overlay.get("x", 0.0)) * 0.5
    ny = float(overlay.get("y", 0.0)) * 0.5
    cnx, cny = nx * 0.35, ny * 0.35
    center_x = frame_width / 2 + cnx * frame_width
    center_y = frame_height / 2 + cny * frame_height
    return center_x, center_y, tw, th


def _border_placement(
    center_x: float, center_y: float, target_w: float, content_w: float,
    border_w: float, border_h: float, offset_x: float, offset_y: float,
) -> tuple[float, float, float, float]:
    # Border scales as a rigid unit with the content; its center is offset from the
    # content-placeholder's center (see border/processor.py's _detect_content_offset).
    sf = target_w / float(content_w) if content_w else 1.0
    border_center_x = center_x - offset_x * sf
    border_center_y = center_y - offset_y * sf
    bw, bh = border_w * sf, border_h * sf
    return border_center_x - bw / 2, border_center_y - bh / 2, bw, bh


def _overlay_target_size(overlay_cfg: dict, frame_width: int, frame_height: int) -> tuple[int, int]:
    # Final on-screen pixel size for an overlay - used to downscale during extraction
    # (not just at composite time) since overlays are typically small webcam/chat corners.
    _, _, tw, th = _overlay_placement(overlay_cfg, frame_width, frame_height)
    w = max(2, int(round(tw)) & ~1)
    h = max(2, int(round(th)) & ~1)
    return w, h


def _build_composite_filtergraph_ex(
    main_path: str, overlay_entries: list, border_images: list,
    frame_width: int, frame_height: int,
):
    # Returns (paths, filter_complex, out_video_label), or None if nothing to composite.
    if not overlay_entries:
        return None

    border_by_asset = {b["asset_id"]: b for b in border_images}
    paths = [main_path]
    filter_parts = []
    current_label = "0:v"

    for vcfg, ov_path in overlay_entries:
        asset_id = vcfg.get("asset_id")
        overlay_cfg = vcfg.get("overlay") or {}
        center_x, center_y, tw, th = _overlay_placement(overlay_cfg, frame_width, frame_height)

        border = border_by_asset.get(asset_id)
        if border:
            paths.append(border["border_image_path"])
            b_idx = len(paths) - 1
            bx, by, bfw, bfh = _border_placement(
                center_x, center_y, tw,
                border["content_width"], border["border_width"], border["border_height"],
                border["content_offset_x"], border["content_offset_y"])
            filter_parts.append(f"[{b_idx}:v]scale={bfw:.1f}:{bfh:.1f}[b{b_idx}]")
            next_label = f"tmp{b_idx}a"
            filter_parts.append(f"[{current_label}][b{b_idx}]overlay={bx:.1f}:{by:.1f}[{next_label}]")
            current_label = next_label

        paths.append(ov_path)
        ov_idx = len(paths) - 1
        filter_parts.append(f"[{ov_idx}:v]scale={tw:.1f}:{th:.1f}[ov{ov_idx}]")
        next_label = f"tmp{ov_idx}b"
        ox_px, oy_px = center_x - tw / 2, center_y - th / 2
        filter_parts.append(f"[{current_label}][ov{ov_idx}]overlay={ox_px:.1f}:{oy_px:.1f}[{next_label}]")
        current_label = next_label

    return paths, ";".join(filter_parts), current_label


def _build_composite_filtergraph(config: dict, border_images: list):
    # Convenience wrapper over _build_composite_filtergraph_ex using the config's own
    # (full-resolution, untrimmed) video paths - used by render_thumbnail.
    project = config["project"]
    frame_width = int(project.get("frame_width", 1920))
    frame_height = int(project.get("frame_height", 1080))
    videos = project.get("videos", [])

    main_vcfg = _find_main_video(config)
    main_path = _resolve_path(main_vcfg) if main_vcfg else None
    if not main_path:
        return None

    overlay_entries = [(v, _resolve_path(v)) for v in videos
                       if "overlay" in (v.get("tags") or []) and _resolve_path(v)]
    return _build_composite_filtergraph_ex(main_path, overlay_entries, border_images,
                                           frame_width, frame_height)


def _input_args(paths: list[str]) -> list[str]:
    args = []
    for p in paths:
        args += ["-i", p]
    return args


def render_thumbnail(config: dict, border_images: list, output_path: str, timestamp: float = None) -> None:
    # Grabs a single composited frame for the Timeline tab thumbnail. Seeking must be an
    # *output* option (after -map) - per-input seeking breaks ffmpeg's overlay sync.
    graph = _build_composite_filtergraph(config, border_images)

    if graph is None:
        main_path = _resolve_path(_find_main_video(config))
        if not main_path:
            raise RuntimeError("No main video found to render a thumbnail from")
        ts = timestamp if timestamp is not None else _probe_duration(main_path) / 2
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", main_path,
               "-ss", f"{ts:.3f}", "-frames:v", "1", "-update", "1", output_path]
    else:
        paths, filter_complex, out_label = graph
        ts = timestamp if timestamp is not None else _probe_duration(paths[0]) / 2
        cmd = (["ffmpeg", "-y", "-loglevel", "error"] + _input_args(paths)
               + ["-filter_complex", filter_complex, "-map", f"[{out_label}]",
                  "-ss", f"{ts:.3f}", "-frames:v", "1", "-update", "1", output_path])

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"Failed to render thumbnail: {r.stderr}")


def _extract_segment(
    src_path: str, start: float, end: float, output_path: str,
    with_audio: bool, width: int = None, height: int = None,
) -> None:
    # Input-side seeking (safe with a single input stream) skips decoding everything
    # before `start`, and running many of these concurrently uses far more CPU than one
    # long decode+encode would.
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", src_path, "-t", f"{max(0.0, end - start):.3f}"]
    if width and height:
        cmd += ["-vf", f"scale={width}:{height}"]
    cmd += ["-c:v", "libx264", "-preset", FAST_PRESET]
    cmd += ["-c:a", "aac"] if with_audio else ["-an"]
    cmd.append(output_path)

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"Failed to extract segment {start:.1f}-{end:.1f} of {src_path}: {r.stderr}")


def _concat_copy(file_list: list[str], output_path: str) -> None:
    # Stream-copy concat of same-codec segments - a fast remux, no re-encoding.
    list_path = output_path + ".concat.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in file_list:
            f.write(f"file '{os.path.abspath(p).replace(chr(92), '/')}'\n")
    try:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", list_path, "-c", "copy", output_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(output_path):
            raise RuntimeError(f"Failed to concat segments: {r.stderr}")
    finally:
        if os.path.exists(list_path):
            try:
                os.remove(list_path)
            except OSError:
                pass


def _clamp_intervals_to_duration(
    intervals: list[tuple[float, float]], duration: float,
) -> list[tuple[float, float]]:
    # Overlay clips are often shorter than the main recording - clip each keep-interval to
    # what the stream actually has, since seeking past EOF writes an empty/invalid segment.
    clamped = []
    for s, e in intervals:
        if s >= duration:
            break
        clamped.append((s, min(e, duration)))
    return clamped


def render_preview(
    config: dict, border_images: list, keep_intervals: list, output_path: str,
    max_width: int = DEFAULT_PREVIEW_MAX_WIDTH,
    progress_callback: ProgressCallback = None,
) -> str:
    # Renders the Timeline tab's scrub/play preview: each stream's kept intervals are
    # extracted and concatenated concurrently, then composited in one final pass - the
    # full-length timeline is never decoded/encoded as one continuous pass.
    project = config["project"]
    frame_width = int(project.get("frame_width", 1920))
    frame_height = int(project.get("frame_height", 1080))
    scaled_w, scaled_h = _scaled_frame_size(frame_width, frame_height, max_width)

    main_vcfg = _find_main_video(config)
    main_path = _resolve_path(main_vcfg) if main_vcfg else None
    if not main_path:
        raise RuntimeError("No main video found to render a preview from")

    overlays = [v for v in project.get("videos", [])
               if "overlay" in (v.get("tags") or []) and _resolve_path(v)]

    tmp_dir = os.path.dirname(output_path) or "."
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_files: list[str] = []

    def _tmp_path() -> str:
        p = os.path.join(tmp_dir, f"_preview_tmp_{uuid.uuid4().hex}.mp4")
        tmp_files.append(p)
        return p

    try:
        main_tmp = _tmp_path()
        overlay_tmps = [(vcfg, _tmp_path()) for vcfg in overlays]

        # Two phases: extract every kept interval of every stream concurrently, then
        # concat each stream's segments back together (fast stream-copy).
        streams = [(main_path, main_tmp, True, scaled_w, scaled_h)]
        for vcfg, ov_tmp in overlay_tmps:
            ow, oh = _overlay_target_size(vcfg.get("overlay") or {}, scaled_w, scaled_h)
            streams.append((_resolve_path(vcfg), ov_tmp, False, ow, oh))

        worker_count = max(4, (os.cpu_count() or 4))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            stream_segments: list[list[str]] = []
            extract_futures = []

            for src_path, _out, with_audio, width, height in streams:
                stream_duration = _probe_duration(src_path)
                raw_intervals = keep_intervals or [(0.0, stream_duration)]
                intervals = _clamp_intervals_to_duration(raw_intervals, stream_duration)
                seg_paths = []
                for s, e in intervals:
                    if e <= s:
                        continue
                    seg_path = os.path.join(tmp_dir, f"_preview_seg_{uuid.uuid4().hex}.mp4")
                    seg_paths.append(seg_path)
                    tmp_files.append(seg_path)
                    extract_futures.append(pool.submit(
                        _extract_segment, src_path, s, e, seg_path, with_audio, width, height))
                stream_segments.append(seg_paths)

            done_jobs = 0
            if progress_callback:
                progress_callback(0.0, "Extracting segments")
            for fut in concurrent.futures.as_completed(extract_futures):
                fut.result()
                done_jobs += 1
                if progress_callback:
                    progress_callback(0.75 * done_jobs / max(1, len(extract_futures)), "Extracting segments")

            concat_futures = []
            skipped_overlay_indices = set()
            for i, ((_, out_path, *_rest), seg_paths) in enumerate(zip(streams, stream_segments)):
                if not seg_paths:
                    skipped_overlay_indices.add(i)
                elif len(seg_paths) == 1:
                    os.replace(seg_paths[0], out_path)
                else:
                    concat_futures.append(pool.submit(_concat_copy, seg_paths, out_path))
            for fut in concurrent.futures.as_completed(concat_futures) if concat_futures else []:
                fut.result()
            if progress_callback:
                progress_callback(0.85, "Stitching segments")

        if not overlay_tmps:
            os.replace(main_tmp, output_path)
            if progress_callback:
                progress_callback(1.0, "Done")
            return output_path

        # An overlay with zero usable segments is dropped from the composite rather than
        # fed in as a nonexistent/empty file.
        overlay_entries = [(vcfg, ov_tmp) for i, (vcfg, ov_tmp) in enumerate(overlay_tmps, start=1)
                           if i not in skipped_overlay_indices]
        if not overlay_entries:
            os.replace(main_tmp, output_path)
            if progress_callback:
                progress_callback(1.0, "Done")
            return output_path

        graph = _build_composite_filtergraph_ex(
            main_tmp, overlay_entries, border_images, scaled_w, scaled_h)
        paths, filter_complex, out_label = graph

        cmd = (["ffmpeg", "-y", "-loglevel", "error"] + _input_args(paths)
               + ["-filter_complex", filter_complex, "-map", f"[{out_label}]", "-map", "0:a",
                  "-c:v", "libx264", "-preset", FAST_PRESET, "-c:a", "aac",
                  "-movflags", "+faststart", output_path])
        composite_duration = _probe_duration(main_tmp)
        _run_ffmpeg_tracked(cmd, composite_duration, progress_callback, base=0.85, span=0.15)
        if not os.path.exists(output_path):
            raise RuntimeError("Failed to composite preview: no output produced")
        if progress_callback:
            progress_callback(1.0, "Done")
    finally:
        for f in tmp_files:
            if os.path.exists(f) and os.path.abspath(f) != os.path.abspath(output_path):
                try:
                    os.remove(f)
                except OSError:
                    pass

    return output_path
