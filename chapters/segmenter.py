"""
Transcript segmentation: classifies time-window groups by type via LLM,
merges adjacent same-type groups into chapters, and generates AI titles.
"""
import json
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ai.response import ResponseInfo, AggregatedResponseInfo


class TranscriptSegment:
    def __init__(self, text: str, start_time: float, end_time: float, index: int):
        self.text = text
        self.start_time = start_time
        self.end_time = end_time
        self.index = index
        self.duration = end_time - start_time

    def __repr__(self):
        return f"Segment({self.index}, {self.start_time:.2f}s-{self.end_time:.2f}s)"


class Chapter:
    def __init__(self, title: str, start_time: float, end_time: float,
                 segments: List[TranscriptSegment]):
        self.title = title
        self.start_time = start_time
        self.end_time = end_time
        self.segments = segments
        self.duration = end_time - start_time
        self.segment_types: List[Dict[str, Any]] = []

    def get_text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def format_timestamp(self, seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def __repr__(self):
        return (f"Chapter('{self.title}', "
                f"{self.format_timestamp(self.start_time)}-"
                f"{self.format_timestamp(self.end_time)})")


class TranscriptSegmenter:
    def __init__(self, config: dict, use_llm: bool = True):
        self.config = config
        self.use_llm = use_llm
        self.aggregated = AggregatedResponseInfo()

    def _cfg(self, *keys, default=None):
        v = self.config.get("project", {})
        for k in keys:
            if isinstance(v, dict):
                v = v.get(k, {})
            else:
                return default
        return default if (v == {} or v is None) else v

    def _audio_duration(self, file_path: str) -> float:
        try:
            cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "json", file_path]
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            return float(json.loads(out)["format"]["duration"])
        except Exception:
            return 0.0

    def _split_into_sentences(self, transcript: str, duration: float) -> List[TranscriptSegment]:
        sentences = [s.strip() for s in re.split(r"[.!?\n]+", transcript)
                     if s.strip() and len(s.strip()) > 5]

        # Whisper often produces one long unpunctuated string → force word-level chunking
        words = transcript.split()
        if len(sentences) <= 1 and len(words) > 60:
            n = 30
            sentences = [" ".join(words[i:i + n]) for i in range(0, len(words), n)]

        if not sentences:
            return []

        total_chars = sum(len(s) for s in sentences) or 1
        segs, t = [], 0.0
        for i, s in enumerate(sentences):
            dur = duration * len(s) / total_chars
            segs.append(TranscriptSegment(s, t, t + dur, i))
            t += dur
        return segs

    def _group_by_time(self, segments: List[TranscriptSegment]) -> List[List[TranscriptSegment]]:
        target = self._cfg("transcript_windows", "target_segment_length_s", default=110)
        if not isinstance(target, (int, float)) or target <= 0:
            target = 110
        groups: List[List[TranscriptSegment]] = []
        cur: List[TranscriptSegment] = []
        dur = 0.0
        for seg in segments:
            cur.append(seg)
            dur += seg.duration
            if dur >= target:
                groups.append(cur)
                cur, dur = [], 0.0
        if cur:
            groups.append(cur)
        return groups

    def _classify_groups(
        self, groups: List[List[TranscriptSegment]]
    ) -> Tuple[Dict[int, Dict], Optional[ResponseInfo]]:
        if not self.use_llm or not groups:
            return {}, None

        seg_cfg = self._cfg("models", "semantic_segmentation_model") or {}
        model = seg_cfg.get("name", "gpt-4.1-nano")
        api_key_env = seg_cfg.get("api_key_env", "OPENAI_API_KEY")
        system_prompt = seg_cfg.get("prompts", "You are an expert at analyzing transcript content.")
        custom = {k: v for k, v in seg_cfg.get("custom_settings", {}).items()}

        prompt_template = self._cfg("semantic_analysis", "prompts", "segment_classification") or ""
        if not prompt_template:
            return {}, None

        segments_data = [
            {"text": " ".join(s.text for s in g),
             "start_time": g[0].start_time,
             "end_time": g[-1].end_time,
             "index": i}
            for i, g in enumerate(groups)
        ]

        try:
            from ai.client import semantic_analysis
            analysis, info = semantic_analysis(
                segments_data, prompt_template, model=model,
                api_key_env=api_key_env, system_prompt=system_prompt, **custom)
        except Exception as e:
            print(f"  [warn] Classification failed: {e}")
            return {}, None

        if isinstance(analysis, dict) and "error" in analysis:
            return {}, info

        out: Dict[int, Dict] = {}
        for item in (analysis or {}).get("segment_classifications", []):
            idx = int(item.get("segment_index", 0)) - 1
            if 0 <= idx < len(groups):
                out[idx] = {
                    "type": item.get("type") or "other",
                    "confidence": float(item.get("confidence") or 0.5),
                    "summary": item.get("summary") or "",
                    "topic": (item.get("topic") or "").strip().lower(),
                }
        return out, info

    def _merge_adjacent_groups(
        self, groups: List[List[TranscriptSegment]], classification_map: Dict[int, Dict]
    ) -> List[Dict]:
        """Merge consecutive same-type groups into proto-chapters, capped by max duration."""
        if not groups:
            return []

        max_dur = self._cfg("chapters", "merging", "max_chapter_duration_s", default=300)
        if not isinstance(max_dur, (int, float)) or max_dur <= 0:
            max_dur = 300
        min_dur = self._cfg("chapters", "merging", "min_chapter_duration_s", default=0)
        if not isinstance(min_dur, (int, float)) or min_dur < 0:
            min_dur = 0
        split_on_topic = bool(self._cfg("chapters", "merging", "split_on_topic_change",
                                        default=True))

        proto: List[Dict] = []

        def _flush(cur_type, cur_groups, cur_indices):
            proto.append({
                "type": cur_type,
                "groups": cur_groups,
                "group_indices": cur_indices,
                "start": cur_groups[0][0].start_time,
                "end": max(cur_groups[-1][-1].end_time, cur_groups[0][0].start_time + 1.0),
            })

        def _topic(i):
            return (classification_map.get(i) or {}).get("topic", "")

        cur_type = (classification_map.get(0) or {}).get("type", "other")
        cur_topic = _topic(0)
        cur_groups = [groups[0]]
        cur_indices = [0]

        for i in range(1, len(groups)):
            g_type = (classification_map.get(i) or {}).get("type", "other")
            g_topic = _topic(i)
            g_end = max(groups[i][-1].end_time, groups[i][0].start_time + 1.0)
            cur_dur = g_end - cur_groups[0][0].start_time
            cur_len = groups[i][0].start_time - cur_groups[0][0].start_time

            topic_changed = (split_on_topic and g_topic and cur_topic
                             and g_topic != cur_topic and cur_len >= min_dur)

            if g_type == cur_type and cur_dur <= max_dur and not topic_changed:
                cur_groups.append(groups[i])
                cur_indices.append(i)
                if g_topic:
                    cur_topic = g_topic
            else:
                _flush(cur_type, cur_groups, cur_indices)
                cur_type = g_type
                cur_topic = g_topic
                cur_groups = [groups[i]]
                cur_indices = [i]

        _flush(cur_type, cur_groups, cur_indices)

        # Fold sub-minimum chapters into the previous one (or the next, for the first)
        if min_dur > 0:
            folded: List[Dict] = []
            for pc in proto:
                if folded and (pc["end"] - pc["start"]) < min_dur:
                    prev = folded[-1]
                    prev["groups"].extend(pc["groups"])
                    prev["group_indices"].extend(pc["group_indices"])
                    prev["end"] = max(prev["end"], pc["end"])
                elif not folded and len(proto) > 1 and (pc["end"] - pc["start"]) < min_dur:
                    # Too-short opener: mark for merging into the next chapter
                    folded.append(pc)
                    pc["_fold_forward"] = True
                else:
                    if folded and folded[-1].pop("_fold_forward", False):
                        short = folded.pop()
                        pc = {
                            "type": pc["type"],
                            "groups": short["groups"] + pc["groups"],
                            "group_indices": short["group_indices"] + pc["group_indices"],
                            "start": short["start"],
                            "end": pc["end"],
                        }
                    folded.append(pc)
            if folded:
                folded[-1].pop("_fold_forward", None)
            proto = folded

        return proto

    def _generate_titles(
        self, proto_chapters: List[Dict], classification_map: Dict[int, Dict]
    ) -> Tuple[List[str], Optional[ResponseInfo]]:
        """Batch-generate chapter titles via LLM in a single request."""
        if not proto_chapters:
            return [], None
        if not self.use_llm or not classification_map:
            return [self._fallback_title(pc) for pc in proto_chapters], None

        title_cfg = self._cfg("models", "chapter_title_model") or {}
        model = title_cfg.get("name", "gpt-4.1")
        api_key_env = title_cfg.get("api_key_env", "OPENAI_API_KEY")
        system = title_cfg.get("prompts",
                               "You are a helpful assistant that creates concise chapter titles.")
        custom = {k: v for k, v in title_cfg.get("custom_settings", {}).items()}

        sections = []
        for i, pc in enumerate(proto_chapters, 1):
            # Prefer per-group LLM summaries — they cover the whole chapter without truncation
            summaries = [
                classification_map.get(gi, {}).get("summary", "")
                for gi in pc["group_indices"]
                if classification_map.get(gi, {}).get("summary")
            ]
            if summaries:
                content = " | ".join(summaries)[:800]
            else:
                content = " ".join(s.text for g in pc["groups"] for s in g)[:600]
            t = pc["start"]
            h, rem = divmod(int(t), 3600)
            m, s = divmod(rem, 60)
            ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            sections.append(f"Chapter {i} [{pc['type']}] (from {ts}):\n{content}")

        user_prompt = (
            "Create concise chapter titles (3-8 words each) for these transcript sections.\n\n"
            + "\n\n---\n\n".join(sections)
            + '\n\nRespond ONLY with a JSON array of title strings, one per chapter, in order.\n'
              'Example: ["Introduction", "Core Topic", "Wrap Up"]'
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]

        try:
            from ai.client import chat
            response, info = chat(messages, model=model, api_key_env=api_key_env, **custom)
            raw = response.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw.strip())
            titles = json.loads(raw)
            if isinstance(titles, list):
                while len(titles) < len(proto_chapters):
                    titles.append(self._fallback_title(proto_chapters[len(titles)]))
                return [str(t) for t in titles[:len(proto_chapters)]], info
        except Exception as e:
            print(f"  [warn] Title generation failed: {e}")

        return [self._fallback_title(pc) for pc in proto_chapters], None

    def _fallback_title(self, proto_chapter: Dict) -> str:
        return proto_chapter["type"].replace("_", " ").title()

    def _segments_from_timing(self, timing: Optional[dict]) -> List[TranscriptSegment]:
        """Build segments from Whisper's timestamps."""
        out: List[TranscriptSegment] = []
        for s in (timing or {}).get("segments") or []:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            out.append(TranscriptSegment(text, float(s["start"]), float(s["end"]), len(out)))
        return out

    def segment_transcript(
        self, transcript: str, audio_file: str, return_cost: bool = False,
        timing: Optional[dict] = None,
    ) -> "List[Chapter] | Tuple[List[Chapter], ResponseInfo]":
        self.aggregated = AggregatedResponseInfo()

        if not transcript or not transcript.strip():
            return ([], ResponseInfo()) if return_cost else []
        if not audio_file or not os.path.exists(audio_file):
            return ([], ResponseInfo()) if return_cost else []

        duration = self._audio_duration(audio_file) or (len(transcript.split()) / 150.0 * 60.0)

        print(f"\n=== Segmenting Transcript ===")
        print(f"Duration: {duration:.1f}s")

        sentences = self._segments_from_timing(timing)
        if sentences:
            print(f"Timestamped segments (from Whisper): {len(sentences)}")
        else:
            sentences = self._split_into_sentences(transcript, duration)
            print(f"Sentence segments: {len(sentences)}")

        groups = self._group_by_time(sentences) if sentences else []
        print(f"Time-window groups: {len(groups)}")

        if not groups:
            return ([], ResponseInfo()) if return_cost else []

        print("Classifying groups by type...")
        classification_map, cls_info = self._classify_groups(groups)
        if cls_info:
            self.aggregated.add(cls_info, "segment_classification")

        proto_chapters = self._merge_adjacent_groups(groups, classification_map)
        print(f"Proto-chapters after merging adjacent same-type: {len(proto_chapters)}")

        print("Generating chapter titles...")
        titles, title_info = self._generate_titles(proto_chapters, classification_map)
        if title_info:
            self.aggregated.add(title_info, "chapter_titles")

        title_cfg = self._cfg("chapters", "titles") or {}
        include_index = title_cfg.get("include_index", True)
        template = title_cfg.get("template", "{chapter_index}. {chapter_title}")

        chapters: List[Chapter] = []
        for i, (pc, title) in enumerate(zip(proto_chapters, titles), 1):
            all_segs = [s for g in pc["groups"] for s in g]
            display_title = (
                template.format(chapter_index=i, chapter_title=title)
                if include_index else title
            )
            ch = Chapter(
                title=display_title,
                start_time=pc["start"],
                end_time=pc["end"],
                segments=all_segs,
            )
            for g_idx, g in zip(pc["group_indices"], pc["groups"]):
                cls = classification_map.get(g_idx) or {}
                seg_type = cls.get("type") or pc["type"]
                ch.segment_types.append({
                    "type": seg_type,
                    "confidence": cls.get("confidence", 0.5),
                    "summary": cls.get("summary", ""),
                    "start_time": g[0].start_time,
                    "end_time": max(g[-1].end_time, g[0].start_time + 1.0),
                })
            chapters.append(ch)

        print(f"\nGenerated {len(chapters)} chapter(s):")
        for ch in chapters:
            print(f"  {ch}  ({len(ch.segment_types)} segment(s))")

        if not return_cost:
            return chapters

        info = ResponseInfo(
            total_cost=self.aggregated.total_cost,
            generation_time=self.aggregated.total_time,
            extra={"by_category": {k: v.to_dict() for k, v in self.aggregated.categories.items()}},
        )
        return chapters, info

    def export_chapters(self, chapters: List[Chapter], format: str = "json") -> str:
        if format != "json":
            raise ValueError(f"Unsupported format: {format}")
        return json.dumps([
            {
                "title": ch.title,
                "start_time": ch.start_time,
                "end_time": ch.end_time,
                "duration": ch.duration,
                "text": ch.get_text(),
                "segment_types": ch.segment_types,
            }
            for ch in chapters
        ], indent=2)


def segment_transcript(
    transcript: str,
    audio_file: str,
    config: dict,
    use_llm: bool = True,
    return_cost: bool = False,
    timing: dict = None,
) -> "List[Chapter] | Tuple[List[Chapter], ResponseInfo]":
    return TranscriptSegmenter(config, use_llm=use_llm).segment_transcript(
        transcript, audio_file, return_cost=return_cost, timing=timing)
