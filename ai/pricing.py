"""Pre-run cost estimation for the transcription + chapter pipeline.

Actual costs are tracked after the fact by ResponseInfo; this module predicts
them up front from the audio duration and config so the user can see roughly
what a run will spend before any API call is made.
"""
import math

# Fallback per-model rates (USD) used when litellm's pricing DB is unavailable.
# Chat rates are per 1M tokens (input, output); audio rate is per minute.
_FALLBACK_CHAT_RATES = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}
_DEFAULT_CHAT_RATE = (2.00, 8.00)  # unknown models: assume mid-tier pricing
_WHISPER_PER_MINUTE = 0.006

# Speech heuristics for sizing the transcript before it exists
_WORDS_PER_MINUTE = 150
_CHARS_PER_WORD = 6.6      # includes the trailing space
_CHARS_PER_TOKEN = 4.0


def _chat_rates(model: str, overrides: dict | None = None) -> tuple[float, float]:
    """Return (input, output) USD per 1M tokens for a chat model."""
    if overrides and "input_per_1m" in overrides and "output_per_1m" in overrides:
        return float(overrides["input_per_1m"]), float(overrides["output_per_1m"])
    try:
        import litellm
        entry = litellm.model_cost.get(model) or {}
        cin = entry.get("input_cost_per_token")
        cout = entry.get("output_cost_per_token")
        if cin and cout:
            return cin * 1e6, cout * 1e6
    except Exception:
        pass
    return _FALLBACK_CHAT_RATES.get(model, _DEFAULT_CHAT_RATE)


def _whisper_rate(overrides: dict | None = None) -> float:
    if overrides and "per_minute" in overrides:
        return float(overrides["per_minute"])
    try:
        import litellm
        per_s = litellm.model_cost.get("whisper-1", {}).get("input_cost_per_second", 0.0)
        if per_s > 0:
            return per_s * 60.0
    except Exception:
        pass
    return _WHISPER_PER_MINUTE


def _chat_cost(model: str, prompt_tokens: float, completion_tokens: float,
               overrides: dict | None = None) -> float:
    cin, cout = _chat_rates(model, overrides)
    return (prompt_tokens * cin + completion_tokens * cout) / 1e6


def estimate_run_cost(config: dict, audio_duration_s: float,
                      billable_duration_s: float | None = None) -> dict:
    """Estimate the API cost of one pipeline run.

    `audio_duration_s` is the full source duration (drives window/chapter counts);
    `billable_duration_s` is what actually gets sent to Whisper (silence-trimmed
    and/or sped-up) when known — defaults to the full duration.

    Returns {"whisper": x, "classification": y, "titles": z, "total": t,
             "detail": {...}} — all USD.
    """
    project = (config or {}).get("project") or {}
    models = project.get("models") or {}

    t_cfg = models.get("transcript_model") or {}
    speed = float(t_cfg.get("speed") or 1.0) or 1.0
    billable = billable_duration_s if billable_duration_s is not None else audio_duration_s
    whisper_minutes = max(0.0, billable / speed) / 60.0
    whisper_cost = whisper_minutes * _whisper_rate((t_cfg.get("pricing") or None))

    # Transcript size heuristic (drives every chat prompt below)
    est_words = audio_duration_s / 60.0 * _WORDS_PER_MINUTE
    transcript_tokens = est_words * _CHARS_PER_WORD / _CHARS_PER_TOKEN

    window_s = (project.get("transcript_windows") or {}).get("target_segment_length_s", 60)
    if not isinstance(window_s, (int, float)) or window_s <= 0:
        window_s = 60
    n_windows = max(1, math.ceil(audio_duration_s / window_s))

    # Classification: one request carrying the whole transcript plus per-window
    # framing, answering with ~50 tokens of JSON per window.
    seg_cfg = models.get("semantic_segmentation_model") or {}
    seg_model = seg_cfg.get("name", "gpt-4.1-nano")
    cls_prompt_tokens = transcript_tokens + n_windows * 20 + 400
    cls_completion_tokens = n_windows * 50
    classification_cost = _chat_cost(seg_model, cls_prompt_tokens, cls_completion_tokens,
                                     (seg_cfg.get("pricing") or None))

    # Titles: summaries condense each chapter to ~1 sentence; short JSON reply.
    max_chapter_s = ((project.get("chapters") or {}).get("merging") or {}) \
        .get("max_chapter_duration_s", 300)
    if not isinstance(max_chapter_s, (int, float)) or max_chapter_s <= 0:
        max_chapter_s = 300
    # Chapters land between the max-duration floor and one-per-window; assume
    # merging fuses roughly 2.5 windows per chapter on typical footage.
    n_chapters = max(1, min(n_windows, math.ceil(n_windows / 2.5)))
    title_cfg = models.get("chapter_title_model") or {}
    title_model = title_cfg.get("name", "gpt-4.1")
    title_prompt_tokens = n_chapters * 60 + 150
    title_completion_tokens = n_chapters * 12
    titles_cost = _chat_cost(title_model, title_prompt_tokens, title_completion_tokens,
                             (title_cfg.get("pricing") or None))

    total = whisper_cost + classification_cost + titles_cost
    return {
        "whisper": whisper_cost,
        "classification": classification_cost,
        "titles": titles_cost,
        "total": total,
        "detail": {
            "audio_duration_s": audio_duration_s,
            "billable_duration_s": billable,
            "whisper_minutes": whisper_minutes,
            "windows": n_windows,
            "chapters_estimate": n_chapters,
            "models": {"transcript": t_cfg.get("name", "whisper-1"),
                       "classification": seg_model, "titles": title_model},
        },
    }


def format_estimate(est: dict) -> str:
    d = est.get("detail", {})
    return (f"Estimated run cost: ~${est['total']:.3f} "
            f"(transcription ${est['whisper']:.3f} for {d.get('whisper_minutes', 0):.1f} min, "
            f"classification ${est['classification']:.3f} over {d.get('windows', 0)} window(s), "
            f"titles ${est['titles']:.3f} for ~{d.get('chapters_estimate', 0)} chapter(s))")
