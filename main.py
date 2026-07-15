"""PAE pipeline orchestrator. Run as __main__ to open the UI (or see --help for CLI flags)."""
import json
import os
import queue
import shutil

from config import get_config, resolve_paths, apply_profile

MSG_LOG         = "log"
MSG_STAGE_START = "stage_start"
MSG_STAGE_DONE  = "stage_done"
MSG_STAGE_SKIP  = "stage_skip"
MSG_STAGE_ERROR = "stage_error"
MSG_CHAPTERS    = "chapters"
MSG_SILENCE     = "silence_intervals"
MSG_BORDERS     = "border_images"
MSG_COST        = "cost"
MSG_DONE        = "done"
MSG_ERROR       = "error"


def _assign_asset_ids(config: dict) -> None:
    videos = config.get("project", {}).get("videos", [])
    used: set[str] = set()
    for i, v in enumerate(videos, 1):
        tags = v.get("tags") or []
        prefix = "main" if "main" in tags else ("overlay" if "overlay" in tags else "clip")
        base = f"{prefix}_{i:03d}"
        asset_id, n = base, 2
        while asset_id in used:
            asset_id = f"{base}_{n}"
            n += 1
        v["asset_id"] = asset_id
        used.add(asset_id)


def process_borders(config: dict) -> list:
    from border.processor import BorderProcessor
    return BorderProcessor(config).process_borders()


def _find_audio_source(config: dict) -> str:
    videos = config.get("project", {}).get("videos", [])
    audio_file = next(
        (v["path"]["file"] for v in videos
         if "audio_source" in (v.get("tags") or []) and v.get("path", {}).get("exists")),
        None,
    )
    if not audio_file or not os.path.exists(audio_file):
        raise RuntimeError("No video with 'audio_source' tag found (or file missing)")
    return audio_file


def detect_silence_intervals(config: dict) -> list[tuple[float, float]]:
    from silence.detector import silent_intervals

    audio_file = _find_audio_source(config)
    silence_cfg = config.get("project", {}).get("silence_removal", {}) or {}
    threshold_db = silence_cfg.get("threshold_db", -35)
    min_duration_s = silence_cfg.get("min_silence_duration_s", 0.6)
    padding_s = silence_cfg.get("padding_s", 0.12)

    print(f"Detecting silence in: {os.path.basename(audio_file)}")
    intervals = silent_intervals(audio_file, threshold_db, min_duration_s, padding_s)
    print(f"Found {len(intervals)} silent section(s)")
    for s, e in intervals:
        print(f"  [{s:.1f}s - {e:.1f}s] ({e - s:.1f}s)")
    return intervals


def _remap_chapters_to_original(chapters: list, keep_intervals: list[tuple[float, float]]) -> None:
    from silence.detector import map_trimmed_to_original as _map
    for ch in chapters:
        ch.start_time = _map(ch.start_time, keep_intervals)
        ch.end_time = _map(ch.end_time, keep_intervals)
        ch.duration = ch.end_time - ch.start_time
        for seg in ch.segment_types:
            seg["start_time"] = _map(seg["start_time"], keep_intervals)
            seg["end_time"] = _map(seg["end_time"], keep_intervals)


def process_transcript(config: dict, silent_intervals: list = None) -> list:
    from ai import client as ai
    from ai.response import ResponseInfo
    from chapters.segmenter import segment_transcript, TranscriptSegmenter

    audio_file = _find_audio_source(config)
    models = config["project"]["models"]
    t_cfg = models["transcript_model"]
    output_dir = config["project"]["output_directory"]["file"]

    mode = config.get("project", {}).get("silence_removal", {}).get("mode", "off")
    transcribe_path = audio_file
    keep_intervals = None
    trimmed_path = None

    if mode == "mark" and silent_intervals:
        # Transcribe a silence-trimmed copy (cheaper, cleaner), then remap timestamps back
        from silence.detector import _probe_duration, invert_intervals, build_trimmed_audio

        total_dur = _probe_duration(audio_file)
        keep_intervals = invert_intervals(silent_intervals, total_dur)

        base = os.path.splitext(os.path.basename(audio_file))[0]
        trimmed_path = os.path.join(output_dir, f"{base}_silence_trimmed.wav")
        print(f"Removing {len(silent_intervals)} silent section(s) before transcription...")
        build_trimmed_audio(audio_file, keep_intervals, trimmed_path)
        transcribe_path = trimmed_path

    try:
        print(f"Transcribing: {os.path.basename(transcribe_path)}")
        transcript_text, transcript_info = ai.transcribe(
            transcribe_path,
            model=t_cfg["name"],
            prompt=t_cfg.get("prompts", ""),
            api_key_env=t_cfg.get("api_key_env", "OPENAI_API_KEY"),
            output_dir=output_dir,
            max_workers=config.get("project", {}).get("performance", {}).get("max_workers", 4),
            speed=t_cfg.get("speed", 1.0),
        )

        if not transcript_text or transcript_text.strip().startswith("[Error"):
            raise RuntimeError("Transcription produced invalid output")

        print(f"Transcription complete ({len(transcript_text)} chars)\n")
        print(transcript_info.format())
        print("\nGenerating chapters...")

        chapter_result = segment_transcript(
            transcript_text, transcribe_path, config=config, return_cost=True)

        if isinstance(chapter_result, tuple):
            chapters, chapter_info = chapter_result
        else:
            chapters, chapter_info = chapter_result, ResponseInfo()

        if not chapters:
            print("Warning: No chapters generated")
            _save_file(output_dir, "transcript.txt", transcript_text)
            return []

        if keep_intervals:
            _remap_chapters_to_original(chapters, keep_intervals)

        print(f"Generated {len(chapters)} chapters:")
        for ch in chapters:
            print(f"  {ch}")

        total_info = transcript_info + chapter_info
        print(f"\n--- Chapter Generation Cost ---")
        print(chapter_info.format())
        print(f"\n--- Total Project Cost ---")
        print(f"Transcription: ${transcript_info.total_cost:.6f}")
        print(f"Chapter Generation: ${chapter_info.total_cost:.6f}")
        print(f"Total: ${total_info.total_cost:.6f}")
        print(f"Total Time: {total_info.generation_time:.2f}s")

        segmenter = TranscriptSegmenter(config)
        chapters_json = segmenter.export_chapters(chapters, format="json")
        chapters_file = _save_file(output_dir, "chapters.json", chapters_json)
        print(f"\nChapters saved: {chapters_file}")
        return chapters
    finally:
        if trimmed_path and os.path.exists(trimmed_path):
            try:
                os.remove(trimmed_path)
            except OSError:
                pass


def process_timeline(config: dict, border_images: list = None, chapters: list = None,
                      silent_intervals: list = None) -> str:
    from premiere.builder import build_timeline
    return build_timeline(config, border_images, chapters, silent_intervals)


def _preview_keep_intervals(config: dict, silent_intervals: list) -> list[tuple[float, float]] | None:
    if not silent_intervals:
        return None
    from silence.detector import _probe_duration, invert_intervals
    audio_file = _find_audio_source(config)
    total_dur = _probe_duration(audio_file)
    return invert_intervals(silent_intervals, total_dur)


def generate_thumbnail(config: dict, border_images: list, output_path: str = None) -> str:
    from preview.renderer import render_thumbnail
    output_dir = config["project"]["output_directory"]["file"]
    output_path = output_path or os.path.join(output_dir, "thumbnail.png")
    os.makedirs(output_dir, exist_ok=True)
    render_thumbnail(config, border_images or [], output_path)
    return output_path


def generate_preview(config: dict, border_images: list, silent_intervals: list,
                     output_path: str = None, progress_callback=None) -> str:
    # progress_callback runs on the calling thread; UI callers must hop threads themselves
    from preview.renderer import render_preview
    output_dir = config["project"]["output_directory"]["file"]
    output_path = output_path or os.path.join(output_dir, "preview.mp4")
    os.makedirs(output_dir, exist_ok=True)

    keep_intervals = _preview_keep_intervals(config, silent_intervals) or []
    return render_preview(config, border_images or [], keep_intervals, output_path,
                          progress_callback=progress_callback)


def youtube_automation_enabled(config: dict) -> bool:
    return bool((config.get("project") or {}).get("youtube_automation", {}).get("enabled"))


def run_youtube_automation(config: dict, silent_intervals: list, progress_callback=None) -> None:
    from silence.detector import _probe_duration
    from youtube_automation.driver import reduce_cuts_for_studio, run_automation

    yt_cfg = (config.get("project") or {}).get("youtube_automation", {}) or {}
    video_path = _find_audio_source(config)
    duration = _probe_duration(video_path)

    kwargs = {}
    if yt_cfg.get("profile_root"):
        kwargs["profile_root"] = yt_cfg["profile_root"]
    if yt_cfg.get("profile_name"):
        kwargs["profile_name"] = yt_cfg["profile_name"]
    if yt_cfg.get("browser_channel"):
        kwargs["browser_channel"] = yt_cfg["browser_channel"]

    cuts = reduce_cuts_for_studio(
        silent_intervals or [],
        min_duration_s=yt_cfg.get("min_cut_duration_s", 1.0),
        max_merge_gap_s=yt_cfg.get("max_merge_gap_s", 0.35))
    if progress_callback and len(cuts) != len(silent_intervals or []):
        progress_callback("cuts", f"Reduced {len(silent_intervals or [])} detected "
                                   f"silences to {len(cuts)} cuts for YouTube Studio "
                                   f"(merged close-together ones, dropped ones too short "
                                   f"to bother with).")

    run_automation(video_path, cuts, duration,
                   progress_callback=progress_callback, **kwargs)


def _copy_inputs_to_output(config: dict) -> None:
    opts = (config.get("project") or {}).get("output_options") or {}
    if not opts.get("copy_inputs"):
        return
    output_dir = config["project"]["output_directory"]["file"]
    os.makedirs(output_dir, exist_ok=True)
    for v in (config.get("project") or {}).get("videos", []):
        path_info = v.get("path") or {}
        src = path_info.get("file")
        if not src or not os.path.isfile(src):
            continue
        dst = os.path.join(output_dir, os.path.basename(src))
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        try:
            shutil.copy2(src, dst)
            print(f"Copied: {os.path.basename(src)} -> output/")
            path_info["file"] = dst
            path_info["exists"] = True
        except Exception as e:
            print(f"[warn] Could not copy {os.path.basename(src)}: {e}")


def _move_inputs(config: dict) -> None:
    opts = (config.get("project") or {}).get("output_options") or {}
    if not opts.get("move_inputs"):
        return
    output_dir = os.path.abspath(config["project"]["output_directory"]["file"])
    for v in (config.get("project") or {}).get("videos", []):
        src = (v.get("path") or {}).get("file")
        if not src or not os.path.isfile(src):
            continue
        if os.path.abspath(src).startswith(output_dir + os.sep):
            continue
        dst = os.path.join(output_dir, os.path.basename(src))
        try:
            shutil.move(src, dst)
            print(f"Moved: {os.path.basename(src)} -> output/")
        except Exception as e:
            print(f"[warn] Could not move {os.path.basename(src)}: {e}")


def _save_file(output_dir: str, filename: str, content: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class ProcessingWorker:
    # Shared by the Tk UI and the headless CLI so both run the identical pipeline.
    def __init__(self, q: "queue.Queue", config: dict):
        self._q = q
        self._config = config

    def _emit(self, kind, data=None):
        self._q.put((kind, data))

    def run(self):
        config = self._config
        silence_cfg = (config.get("project") or {}).get("silence_removal") or {}
        mode = silence_cfg.get("mode", "off")

        silent_intervals: list = []
        if mode in ("mark", "only"):
            self._emit(MSG_STAGE_START, "silence")
            try:
                silent_intervals = detect_silence_intervals(config)
                self._emit(MSG_SILENCE, silent_intervals)
                self._emit(MSG_STAGE_DONE, "silence")
            except Exception as e:
                self._emit(MSG_STAGE_ERROR, ("silence", str(e)))
                self._emit(MSG_ERROR, f"Silence detection failed: {e}")
                return
        else:
            self._emit(MSG_STAGE_SKIP, "silence")

        _copy_inputs_to_output(config)

        border_images = None
        chapters = None

        if mode == "only":
            self._emit(MSG_STAGE_SKIP, "borders")
            self._emit(MSG_STAGE_SKIP, "transcript")
            self._emit(MSG_STAGE_SKIP, "chapters")
        else:
            try:
                self._emit(MSG_STAGE_START, "borders")
                border_images = process_borders(config)
                self._emit(MSG_BORDERS, border_images)
                self._emit(MSG_STAGE_DONE, "borders")
            except Exception as e:
                self._emit(MSG_STAGE_ERROR, ("borders", str(e)))
                self._emit(MSG_ERROR, f"Border processing failed: {e}")
                return

            try:
                self._emit(MSG_STAGE_START, "transcript")
                chapters = process_transcript(
                    config, silent_intervals=silent_intervals if mode == "mark" else None)
                self._emit(MSG_STAGE_DONE, "transcript")
            except Exception as e:
                self._emit(MSG_STAGE_ERROR, ("transcript", str(e)))
                self._emit(MSG_ERROR, f"Transcript failed: {e}")
                return

            if chapters:
                self._emit(MSG_STAGE_START, "chapters")
                self._emit(MSG_CHAPTERS, chapters)
                self._emit(MSG_STAGE_DONE, "chapters")
            else:
                self._emit(MSG_STAGE_ERROR, ("chapters", "none generated"))

        try:
            self._emit(MSG_STAGE_START, "timeline")
            process_timeline(config, border_images, chapters,
                             silent_intervals=silent_intervals if mode in ("mark", "only") else None)
            self._emit(MSG_STAGE_DONE, "timeline")
        except Exception as e:
            self._emit(MSG_STAGE_ERROR, ("timeline", str(e)))
            self._emit(MSG_ERROR, f"Timeline failed: {e}")
            return

        _move_inputs(config)

        try:
            self._emit(MSG_STAGE_START, "tests")
            import unittest
            suite = unittest.TestLoader().discover("tests")
            result = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
            if result.wasSuccessful():
                self._emit(MSG_STAGE_DONE, "tests")
            else:
                fails = len(result.failures) + len(result.errors)
                self._emit(MSG_STAGE_ERROR, ("tests", f"{fails} failure(s)"))
        except Exception as e:
            self._emit(MSG_STAGE_ERROR, ("tests", str(e)))

        self._emit(MSG_DONE, None)


def _print_stage_event(kind, data) -> None:
    # ASCII-only: Windows' default console codepage can't encode arrows/checkmarks.
    if kind == MSG_LOG:
        print(data)
    elif kind == MSG_STAGE_START:
        print(f"\n-> {data}...")
    elif kind == MSG_STAGE_DONE:
        print(f"[done] {data}")
    elif kind == MSG_STAGE_SKIP:
        print(f"[skip] {data}")
    elif kind == MSG_STAGE_ERROR:
        stage_id, msg = data if isinstance(data, tuple) else (data, "")
        print(f"[FAIL] {stage_id}: {msg}")
    elif kind == MSG_ERROR:
        print(f"[ERROR] {data}")
    elif kind == MSG_DONE:
        print("\nDone.")


def run_headless(config_path: str, force_silence_only: bool = False) -> None:
    with open(config_path, "r", encoding="utf-8") as f:
        raw_config = json.load(f)

    if force_silence_only:
        raw_config = apply_profile(raw_config, "silence_only")
        raw_config.setdefault("project", {}).setdefault("silence_removal", {})["mode"] = "only"

    print(f"Using config: {config_path}")
    config = resolve_paths(raw_config)
    _assign_asset_ids(config)

    q: "queue.Queue" = queue.Queue()
    worker = ProcessingWorker(q, config)
    worker.run()

    silent_intervals: list = []
    ok = True
    while not q.empty():
        kind, data = q.get_nowait()
        if kind == MSG_SILENCE:
            silent_intervals = data or []
        elif kind == MSG_ERROR:
            ok = False
        _print_stage_event(kind, data)

    if ok and force_silence_only and youtube_automation_enabled(config):
        yt_cfg = config["project"]["youtube_automation"]
        if yt_cfg.get("auto_launch_on_silence_only", True):
            print("\n-> youtube_automation...")

            def _print_progress(step, message):
                print(f"  [{step}] {message}")

            try:
                run_youtube_automation(config, silent_intervals, progress_callback=_print_progress)
            except Exception as e:
                print(f"[FAIL] youtube_automation: {e}")
            input("\nBrowser window left open for you to review/finish publishing - "
                  "press Enter here to close this automation session.\n")
            from youtube_automation.driver import close_all_sessions
            close_all_sessions()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PAE - Post Production Assistant")
    parser.add_argument("--config", help="Path to a config JSON file (defaults to config.json)")
    parser.add_argument("--run", action="store_true",
                         help="Run the pipeline headlessly instead of opening the UI")
    parser.add_argument("--silence-only", action="store_true",
                         help="Shortcut: applies the config's project.silence_only_profile "
                              "override (if present) and forces silence_removal.mode to 'only'")
    args = parser.parse_args()

    if args.run or args.silence_only:
        here = os.path.dirname(os.path.abspath(__file__))
        cfg_path = args.config or os.path.join(here, "config.json")
        run_headless(cfg_path, force_silence_only=args.silence_only)
    else:
        from app import main
        main()
