"""PAE pipeline orchestrator. Run as __main__ to open the UI."""
import os
import shutil

from config import get_config, resolve_paths


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


def process_transcript(config: dict) -> list:
    """Transcribe audio then segment into chapters. Returns chapter list."""
    from ai import client as ai
    from ai.response import ResponseInfo
    from chapters.segmenter import segment_transcript, TranscriptSegmenter

    videos = config.get("project", {}).get("videos", [])
    audio_file = next(
        (v["path"]["file"] for v in videos
         if "audio_source" in (v.get("tags") or []) and v.get("path", {}).get("exists")),
        None,
    )
    if not audio_file or not os.path.exists(audio_file):
        raise RuntimeError("No video with 'audio_source' tag found (or file missing)")

    models = config["project"]["models"]
    t_cfg = models["transcript_model"]

    print(f"Transcribing: {os.path.basename(audio_file)}")
    transcript_text, transcript_info = ai.transcribe(
        audio_file,
        model=t_cfg["name"],
        prompt=t_cfg.get("prompts", ""),
        api_key_env=t_cfg.get("api_key_env", "OPENAI_API_KEY"),
        output_dir=config["project"]["output_directory"]["file"],
        max_workers=config.get("project", {}).get("performance", {}).get("max_workers", 4),
    )

    if not transcript_text or transcript_text.strip().startswith("[Error"):
        raise RuntimeError("Transcription produced invalid output")

    print(f"Transcription complete ({len(transcript_text)} chars)\n")
    print(transcript_info.format())
    print("\nGenerating chapters...")

    chapter_result = segment_transcript(
        transcript_text, audio_file, config=config, return_cost=True)

    if isinstance(chapter_result, tuple):
        chapters, chapter_info = chapter_result
    else:
        chapters, chapter_info = chapter_result, ResponseInfo()

    if not chapters:
        print("Warning: No chapters generated")
        _save_file(config["project"]["output_directory"]["file"],
                   "transcript.txt", transcript_text)
        return []

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
    chapters_file = _save_file(config["project"]["output_directory"]["file"],
                               "chapters.json", chapters_json)
    print(f"\nChapters saved: {chapters_file}")
    return chapters


def process_timeline(config: dict, border_images: list = None, chapters: list = None) -> str:
    from premiere.builder import build_timeline
    return build_timeline(config, border_images, chapters)


def _copy_inputs_to_output(config: dict) -> None:
    """Copy inputs to output dir and mutate path config so all subsequent steps reference the copies."""
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
    """Move original input files into the output dir, skipping files already there."""
    opts = (config.get("project") or {}).get("output_options") or {}
    if not opts.get("move_inputs"):
        return
    output_dir = os.path.abspath(config["project"]["output_directory"]["file"])
    for v in (config.get("project") or {}).get("videos", []):
        src = (v.get("path") or {}).get("file")
        if not src or not os.path.isfile(src):
            continue
        # Skip files already inside the output directory
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


def run_pipeline(config: dict) -> None:
    """Run the full pipeline: copy inputs → borders → transcript → timeline → move inputs."""
    import copy
    cfg = resolve_paths(copy.deepcopy(config))
    _assign_asset_ids(cfg)
    _copy_inputs_to_output(cfg)
    border_images = process_borders(cfg)
    chapters = process_transcript(cfg)
    process_timeline(cfg, border_images, chapters)
    _move_inputs(cfg)


if __name__ == "__main__":
    from app import main
    main()
