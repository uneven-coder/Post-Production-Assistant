"""OpenAI API client — transcription and chat completions."""
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

from .response import ResponseInfo


def _client(api_key_env: str = "OPENAI_API_KEY") -> OpenAI:
    key = os.environ.get(api_key_env)
    if not key:
        raise RuntimeError(f"Environment variable {api_key_env!r} is not set")
    return OpenAI(api_key=key)


def _calc_chat_cost(response, model: str) -> float:
    """Return the USD cost of a chat completion using litellm's pricing database."""
    try:
        import litellm
        cost = litellm.completion_cost(completion_response=response, model=model)
        if cost is not None and cost >= 0:
            return float(cost)
    except Exception:
        pass
    return 0.0


def _calc_whisper_cost(duration_seconds: float) -> float:
    """Return the USD cost of a Whisper transcription using litellm's pricing database."""
    try:
        import litellm
        cost_per_s = litellm.model_cost.get("whisper-1", {}).get("input_cost_per_second", 0.0)
        if cost_per_s > 0:
            return duration_seconds * cost_per_s
    except Exception:
        pass
    # OpenAI's published rate: $0.006/min
    return duration_seconds / 60.0 * 0.006


def _audio_duration(file_path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "json", file_path]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


def _atempo_filter(speed: float) -> str:
    """Build an ffmpeg atempo filter chain for an arbitrary speed (atempo only accepts 0.5-2.0 per stage)."""
    stages = []
    remaining = speed
    while remaining > 2.0:
        stages.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        stages.append(0.5)
        remaining /= 0.5
    stages.append(remaining)
    return ",".join(f"atempo={s}" for s in stages)


def _extract_audio(video_path: str, audio_path: str, speed: float = 1.0) -> None:
    print("Extracting audio from video...")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
           "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1"]
    if speed != 1.0:
        cmd += ["-filter:a", _atempo_filter(speed)]
    cmd.append(audio_path)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {r.stderr}")
    if not os.path.exists(audio_path):
        raise RuntimeError("Audio extraction: output file was not created")


def _compress_audio(src: str, dst: str) -> None:
    print("Compressing audio...")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
           "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k", dst]
    subprocess.run(cmd, capture_output=True, text=True)


def _split_audio(audio_path: str, output_dir: str, chunk_duration_s: float) -> list[tuple[str, int]]:
    total = _audio_duration(audio_path)
    base = os.path.splitext(os.path.basename(audio_path))[0]
    chunks: list[tuple[str, int]] = []
    t, idx = 0.0, 0

    print(f"Splitting audio into chunks (~{int(chunk_duration_s)}s each)...")
    while t < total:
        dur = min(chunk_duration_s, total - t)
        chunk_path = os.path.join(output_dir, f"{base}_chunk_{idx}.mp3")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", audio_path,
               "-t", str(dur), "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k", chunk_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(chunk_path):
            break
        if os.path.getsize(chunk_path) / (1024 * 1024) >= 25:
            os.remove(chunk_path)
            chunk_duration_s *= 0.8
            continue
        chunks.append((chunk_path, idx))
        t += dur
        idx += 1

    return chunks


def _transcribe_one_chunk(args: tuple) -> tuple[int, str, float]:
    index, chunk_path, model, prompt, api_key_env = args
    c = _client(api_key_env)
    with open(chunk_path, "rb") as f:
        resp = c.audio.transcriptions.create(model=model, file=f, prompt=prompt)
    return index, resp.text, _audio_duration(chunk_path)


def transcribe(
    file_path: str,
    model: str,
    prompt: str = "",
    api_key_env: str = "OPENAI_API_KEY",
    output_dir: str = "./output/",
    max_workers: int = 4,
    speed: float = 1.0,
) -> tuple[str, ResponseInfo]:
    """Transcribe an audio or video file. Extracts audio from video; splits large files.

    `speed` speeds up the extracted audio before sending it to Whisper (e.g. 1.5x), which
    reduces both duration and transcription cost proportionally.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    os.makedirs(output_dir, exist_ok=True)
    audio_path = file_path
    tmp_files: list[str] = []

    try:
        import filetype
        kind = filetype.guess(file_path)
        if kind and kind.mime.startswith("video/"):
            base = os.path.splitext(os.path.basename(file_path))[0]
            audio_path = os.path.join(output_dir, f"{base}_extracted_audio.wav")
            _extract_audio(file_path, audio_path, speed=speed)
            tmp_files.append(audio_path)

            size_mb = os.path.getsize(audio_path) / (1024 * 1024)
            if size_mb > 25:
                mp3_path = os.path.join(output_dir, f"{base}_audio.mp3")
                _compress_audio(audio_path, mp3_path)
                if os.path.exists(mp3_path):
                    tmp_files.append(mp3_path)
                    audio_path = mp3_path

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)

        if size_mb > 24:
            return _transcribe_chunked(audio_path, model, prompt, api_key_env,
                                       output_dir, max_workers)

        c = _client(api_key_env)
        start = time.time()
        with open(audio_path, "rb") as f:
            resp = c.audio.transcriptions.create(model=model, file=f, prompt=prompt)
        elapsed = time.time() - start

        duration = _audio_duration(audio_path)
        info = ResponseInfo(total_cost=_calc_whisper_cost(duration),
                            generation_time=elapsed,
                            model=model, provider="openai")
        return resp.text, info

    finally:
        for tmp in tmp_files:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def _transcribe_chunked(
    audio_path: str, model: str, prompt: str,
    api_key_env: str, output_dir: str, max_workers: int,
) -> tuple[str, ResponseInfo]:
    size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    chunk_s = _audio_duration(audio_path) / max(1, int(size_mb / 24) + 1)
    chunks = _split_audio(audio_path, output_dir, chunk_s)
    if not chunks:
        raise RuntimeError("Failed to split audio into chunks")

    print(f"Transcribing {len(chunks)} chunk(s) in parallel...")
    args = [(i, p, model, prompt, api_key_env) for p, i in chunks]
    results: dict[int, str] = {}
    total_dur = 0.0
    start = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_transcribe_one_chunk, a): a[0] for a in args}
        for fut in as_completed(futures):
            i, text, dur = fut.result()
            results[i] = text
            total_dur += dur
            print(f"  Chunk {i + 1}/{len(chunks)} done")

    elapsed = time.time() - start
    for p, _ in chunks:
        try:
            os.remove(p)
        except OSError:
            pass

    text = " ".join(results[i] for i in sorted(results))
    info = ResponseInfo(total_cost=_calc_whisper_cost(total_dur),
                        generation_time=elapsed,
                        model=model, provider="openai",
                        extra={"chunks": len(chunks)})
    return text, info


def chat(
    messages: list[dict],
    model: str,
    api_key_env: str = "OPENAI_API_KEY",
    **kwargs,
) -> tuple[str, ResponseInfo]:
    """Chat completion. Returns (reply_text, ResponseInfo)."""
    c = _client(api_key_env)
    start = time.time()
    resp = c.chat.completions.create(model=model, messages=messages, **kwargs)
    elapsed = time.time() - start

    text = resp.choices[0].message.content or ""
    usage = resp.usage
    pt = usage.prompt_tokens if usage else 0
    ct = usage.completion_tokens if usage else 0
    info = ResponseInfo(
        total_cost=_calc_chat_cost(resp, model),
        generation_time=elapsed,
        model=model, provider="openai",
        prompt_tokens=pt, completion_tokens=ct,
        total_tokens=(usage.total_tokens if usage else 0),
    )
    return text, info


def semantic_analysis(
    segments: list[dict],
    prompt_template: str,
    model: str,
    api_key_env: str = "OPENAI_API_KEY",
    system_prompt: str = "You are an expert semantic analysis assistant.",
    **kwargs,
) -> tuple[dict, ResponseInfo]:
    """Run a semantic-analysis prompt over transcript segments. `prompt_template` must contain {segments_text}."""
    segments_text = "\n\n".join(
        f"[Segment {i + 1}] ({s['start_time']:.1f}s - {s['end_time']:.1f}s):\n{s['text']}"
        for i, s in enumerate(segments)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": prompt_template.format(segments_text=segments_text)},
    ]

    c = _client(api_key_env)
    start = time.time()
    resp = c.chat.completions.create(
        model=model, messages=messages,
        response_format={"type": "json_object"}, **kwargs)
    elapsed = time.time() - start

    content = resp.choices[0].message.content or ""
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        result = {"error": "Failed to parse JSON response", "raw": content}

    usage = resp.usage
    pt = usage.prompt_tokens if usage else 0
    ct = usage.completion_tokens if usage else 0
    info = ResponseInfo(
        total_cost=_calc_chat_cost(resp, model),
        generation_time=elapsed,
        model=model, provider="openai",
        prompt_tokens=pt, completion_tokens=ct,
        total_tokens=(usage.total_tokens if usage else 0),
    )
    return result, info
