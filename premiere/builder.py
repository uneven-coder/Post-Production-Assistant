import os
import re
import subprocess
import json
import uuid
import xml.etree.ElementTree as ET
from xml.dom import minidom


def _fcp_time(seconds: float, fps: int) -> str:
    """Convert seconds to a FCPXML rational time string at the given frame rate."""
    frames = round(seconds * fps)
    return "0s" if frames == 0 else f"{frames}/{fps}s"


def _fcp_hms(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class TimelineBuilder:
    """Builds a Premiere Pro / FCPXML timeline from config + analysis results."""

    def __init__(self, config: dict, border_images: list = None, silent_intervals: list = None):
        self.config = config
        self.project = config["project"]
        self.name = self.project.get("name", "Timeline")
        self.videos = self.project.get("videos", [])
        self.fps = int(self.project.get("fps", 30))
        self.frame_width = int(self.project.get("frame_width", 1920))
        self.frame_height = int(self.project.get("frame_height", 1080))
        self.border_images = border_images or []
        self.silent_intervals = silent_intervals or []
        self.segment_colors = self.project.get("segment_colors", {})
        self.chapters: list = []

        out = self.project.get("output_directory", "./output/")
        self.output_dir = out.get("file", "./output/") if isinstance(out, dict) else out

        self._border_idx_map: dict = {}
        self._clip_transforms: dict = {}
        self._master_clip_names: dict = {}

    def set_chapters(self, chapters: list) -> None:
        self.chapters = chapters or []

    @staticmethod
    def _probe_duration(video_path: str, fps: int) -> int | None:
        try:
            cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "json", video_path]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return int(float(json.loads(r.stdout)["format"]["duration"]) * fps)
        except Exception:
            pass
        return None

    def _build_cuts(self, total_frames: int) -> list[tuple[int, int, str, str]]:
        raw: list[tuple[int, int, str, str]] = []

        print(f"\n=== Building Cuts (reference_duration={total_frames} frames @ {self.fps}fps = {total_frames/self.fps:.1f}s) ===")
        print(f"Chapters received: {len(self.chapters)}")

        for ci, ch in enumerate(self.chapters):
            segs = getattr(ch, "segment_types", None) or []
            print(f"\nChapter {ci}: {len(segs)} segment_types")
            for si, st in enumerate(segs):
                s = max(0, int(float(st.get("start_time", 0)) * self.fps))
                e = max(0, int(float(st.get("end_time", 0)) * self.fps))
                t = st.get("type") or "other"
                summary = st.get("summary", "") or ""
                print(f"  Seg {si}: {st.get('start_time', 0):.1f}s-{st.get('end_time', 0):.1f}s -> frames {s}-{e} type={t}")
                if e > s:
                    raw.append((s, e, t, summary))
                else:
                    print(f"    SKIPPED (end <= start)")

        print(f"\nRaw segments collected: {len(raw)}")

        if not raw:
            for ch in self.chapters:
                s = max(0, int(getattr(ch, "start_time", 0) * self.fps))
                e = max(0, int(getattr(ch, "end_time", 0) * self.fps))
                if e > s:
                    raw.append((s, e, "other", ""))
        if not raw:
            return [(0, total_frames, "other", "")]

        raw.sort()
        cuts: list[tuple[int, int, str, str]] = []
        for s, e, t, summ in raw:
            sc = min(s, total_frames)
            ec = min(e, total_frames)
            if cuts and sc < cuts[-1][1]:
                sc = cuts[-1][1]
            if ec > sc:
                cuts.append((sc, ec, t, summ))

        if not cuts:
            return [(0, total_frames, "other", "")]

        # Fill gaps
        filled = []
        if cuts[0][0] > 0:
            filled.append((0, cuts[0][0], "other", ""))
        filled.append(cuts[0])
        for i in range(1, len(cuts)):
            prev_end = filled[-1][1]
            if cuts[i][0] > prev_end:
                filled.append((prev_end, cuts[i][0], "other", ""))
            filled.append(cuts[i])
        if filled[-1][1] < total_frames:
            filled.append((filled[-1][1], total_frames, "other", ""))

        print(f"\nFinal cuts: {len(filled)}")
        return filled

    def _inject_silence_cuts(
        self, cuts: list[tuple[int, int, str, str]], total_frames: int,
    ) -> list[tuple[int, int, str, str]]:
        # type='silence' sub-segments render as disabled (hatched) clips
        if not self.silent_intervals:
            return cuts

        silence_frames = []
        for s, e in self.silent_intervals:
            sf = max(0, min(int(round(s * self.fps)), total_frames))
            ef = max(0, min(int(round(e * self.fps)), total_frames))
            if ef > sf:
                silence_frames.append((sf, ef))
        if not silence_frames:
            return cuts

        result: list[tuple[int, int, str, str]] = []
        for cs, ce, ctype, csumm in cuts:
            overlaps = sorted((sf, ef) for sf, ef in silence_frames if ef > cs and sf < ce)
            if not overlaps:
                result.append((cs, ce, ctype, csumm))
                continue
            cursor = cs
            for sf, ef in overlaps:
                sf, ef = max(sf, cs), min(ef, ce)
                if sf > cursor:
                    result.append((cursor, sf, ctype, csumm))
                if ef > cursor:
                    result.append((max(sf, cursor), ef, "silence", ""))
                cursor = max(cursor, ef)
            if cursor < ce:
                result.append((cursor, ce, ctype, csumm))
        return result

    def build_and_export(self) -> str:
        xmeml = ET.Element("xmeml", version="4")
        proj_el = ET.SubElement(xmeml, "project")
        ET.SubElement(proj_el, "name").text = self.name
        children = ET.SubElement(proj_el, "children")

        bin_el = ET.SubElement(children, "bin")
        ET.SubElement(bin_el, "name").text = "Clips"
        bin_ch = ET.SubElement(bin_el, "children")

        valid_videos, durations = [], []
        video_cfg_map, dur_by_path = {}, {}
        main_duration = None
        clip_idx = 1

        for n, vcfg in enumerate(self.videos, 1):
            vcfg.setdefault("asset_id", f"clip_{n:03d}")
            asset_id = vcfg["asset_id"]
            vpath = self._resolve_path(vcfg)
            if not vpath:
                continue
            abs_path = os.path.abspath(vpath)
            dur = self._probe_duration(abs_path, self.fps) or (int(vcfg.get("duration", 10)) * self.fps)
            display_name = f"{asset_id} - {os.path.basename(abs_path)}"

            tags = vcfg.get("tags", []) or []
            if "main" in tags or "audio_source" in tags:
                if main_duration is None or dur > main_duration:
                    main_duration = dur

            self._add_master_clip(bin_ch, clip_idx, abs_path, dur, display_name=display_name)
            self._master_clip_names[clip_idx] = display_name
            valid_videos.append((clip_idx, asset_id, vcfg, abs_path, dur))
            video_cfg_map[asset_id] = vcfg
            dur_by_path[abs_path] = dur
            durations.append(dur)
            clip_idx += 1

        border_map: dict = {}
        for bi in self.border_images:
            asset_id = bi.get("asset_id")
            if not asset_id or asset_id not in video_cfg_map:
                continue
            vcfg = video_cfg_map[asset_id]
            bw, bh = bi["border_width"], bi["border_height"]
            media_path = os.path.abspath(bi["media_path"])
            b_dur = dur_by_path.get(media_path, durations[0] if durations else self.fps * 10)
            b_name = f"{asset_id} - border - {os.path.basename(bi['border_image_path'])}"
            self._add_master_clip(bin_ch, clip_idx, bi["border_image_path"], b_dur, True, bw, bh, b_name)
            self._border_idx_map[clip_idx] = bi["border_image_path"]
            self._master_clip_names[clip_idx] = b_name
            border_map[asset_id] = {
                "idx": clip_idx, "video_cfg": vcfg,
                "border_w": bw, "border_h": bh,
                "content_w": bi["content_width"], "content_h": bi["content_height"],
                "content_offset_x": bi.get("content_offset_x", 0),
                "content_offset_y": bi.get("content_offset_y", 0),
            }
            clip_idx += 1

        ref_dur = main_duration or (min(durations) if durations else self.fps * 10)
        print(f"\nReference duration: {ref_dur} frames ({ref_dur/self.fps:.1f}s)")
        cuts = self._build_cuts(ref_dur)
        cuts = self._inject_silence_cuts(cuts, ref_dur)

        print(f"\nTimeline cuts ({len(cuts)} segments, {ref_dur} frames @ {self.fps}fps):")
        for s, e, t, _ in cuts:
            print(f"  [{s/self.fps:.1f}s - {e/self.fps:.1f}s] ({e-s} frames) type={t}")

        seq = ET.SubElement(children, "sequence", id="sequence-1")
        ET.SubElement(seq, "name").text = self.name
        ET.SubElement(seq, "duration").text = str(ref_dur)
        self._add_rate(seq)
        self._add_timecode(seq)

        media = ET.SubElement(seq, "media")
        vid_seq = ET.SubElement(media, "video")
        aud_seq = ET.SubElement(media, "audio")
        self._add_format(ET.SubElement(vid_seq, "format"))

        main_track = ET.SubElement(vid_seq, "track")

        for idx, asset_id, vcfg, abs_path, src_dur in valid_videos:
            is_main = "main" in (vcfg.get("tags") or [])

            if asset_id in border_map:
                self._cache_clip_transform(vcfg, asset_id)
                b = border_map[asset_id]
                b_track = ET.SubElement(vid_seq, "track")
                for ss, se, st, _ in cuts:
                    self._add_clip_segment(b_track, b["idx"], b["video_cfg"], src_dur,
                                           ss, se, st, is_border=True,
                                           border_w=b["border_w"], border_h=b["border_h"],
                                           content_w=b["content_w"], content_h=b["content_h"],
                                           content_ox=b["content_offset_x"],
                                           content_oy=b["content_offset_y"],
                                           asset_id=asset_id,
                                           display_name=self._master_clip_names.get(b["idx"]),
                                           apply_color=False)

            track = main_track if is_main else ET.SubElement(vid_seq, "track")
            for ss, se, st, summ in cuts:
                self._add_clip_segment(track, idx, vcfg, src_dur, ss, se, st,
                                       resolved_path=abs_path, asset_id=asset_id,
                                       display_name=self._master_clip_names.get(idx),
                                       apply_color=is_main, seg_summary=summ)

            self._add_audio_clip_segments(ET.SubElement(aud_seq, "track"), idx, src_dur, cuts,
                                          abs_path, asset_id, self._master_clip_names.get(idx))

        # Sequence markers — these appear as yellow diamonds on Premiere's timeline ruler
        for ch in self.chapters:
            ch_start  = float(getattr(ch, "start_time", 0))
            ch_title  = getattr(ch, "title", "Chapter")
            ch_frame  = int(ch_start * self.fps)
            seg_types = getattr(ch, "segment_types", []) or []
            comment_lines = []
            for seg in seg_types:
                stype   = seg.get("type", "other")
                summary = seg.get("summary", "")
                st      = seg.get("start_time", ch_start)
                en      = seg.get("end_time",   ch_start)
                line    = f"[{stype}] {_fcp_hms(st)}–{_fcp_hms(en)}"
                if summary:
                    line += f": {summary}"
                comment_lines.append(line)
            mk = ET.SubElement(seq, "marker")
            ET.SubElement(mk, "name").text    = ch_title
            ET.SubElement(mk, "comment").text = "\n".join(comment_lines)
            ET.SubElement(mk, "in").text      = str(ch_frame)
            ET.SubElement(mk, "out").text     = "-1"

        os.makedirs(self.output_dir, exist_ok=True)
        out_path = os.path.join(self.output_dir, f"{self.name}.xml")
        xml_str = minidom.parseString(ET.tostring(xmeml, encoding="unicode")).toprettyxml(indent="  ")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(xml_str)
        print(f"Timeline exported: {out_path}")

        self.build_and_export_fcpxml()
        return out_path

    def build_and_export_fcpxml(self) -> str:
        """Export a FCPXML file where each chapter is a titled spine clip with segment notes."""
        fps = self.fps

        root = ET.Element("fcpxml", version="1.9")
        resources = ET.SubElement(root, "resources")
        ET.SubElement(resources, "format",
                      id="r1",
                      frameDuration=f"1/{fps}s",
                      width=str(self.frame_width),
                      height=str(self.frame_height))

        asset_entries: list[tuple[str, dict, str, int, list]] = []
        main_frames: int | None = None

        for n, vcfg in enumerate(self.videos, 1):
            path = self._resolve_path(vcfg)
            if not path:
                continue
            abs_path = os.path.abspath(path)
            frames = self._probe_duration(abs_path, fps) or (fps * 10)
            tags = vcfg.get("tags") or []
            asset_id = f"r{n + 1}"
            has_audio = "1" if ("audio_source" in tags or "main" in tags) else "0"

            a = ET.SubElement(resources, "asset",
                              id=asset_id,
                              name=os.path.basename(abs_path),
                              uid=uuid.uuid4().hex.upper(),
                              start="0s",
                              duration=_fcp_time(frames / fps, fps),
                              hasVideo="1",
                              hasAudio=has_audio,
                              format="r1")
            ET.SubElement(a, "media-rep",
                          kind="original-media",
                          src="file://localhost/" + abs_path.replace("\\", "/"))

            asset_entries.append((asset_id, vcfg, abs_path, frames, tags))
            if "main" in tags or "audio_source" in tags:
                if main_frames is None or frames > main_frames:
                    main_frames = frames

        if not asset_entries:
            print("[warn] FCPXML: no valid video assets — skipping")
            return ""

        total_frames = main_frames or asset_entries[0][3]
        total_seconds = total_frames / fps

        main_entry = next(
            (e for e in asset_entries if "main" in e[4] or "audio_source" in e[4]),
            asset_entries[0],
        )
        main_id, _, _, _, _ = main_entry

        overlay_entries = [e for e in asset_entries if "overlay" in e[4]]

        library   = ET.SubElement(root, "library")
        event     = ET.SubElement(library, "event", name="PAE Export")
        project   = ET.SubElement(event, "project", name=self.name)
        sequence  = ET.SubElement(project, "sequence",
                                  duration=_fcp_time(total_seconds, fps),
                                  format="r1",
                                  tcStart="0s",
                                  tcFormat="NDF",
                                  audioLayout="stereo",
                                  audioRate="48k")
        spine = ET.SubElement(sequence, "spine")

        if self.chapters:
            for ch_idx, ch in enumerate(self.chapters):
                start    = float(getattr(ch, "start_time", 0))
                end      = float(getattr(ch, "end_time",   0))
                title    = getattr(ch, "title", f"Chapter {ch_idx + 1}")
                duration = max(0.0, end - start)
                if duration <= 0:
                    continue

                seg_types = getattr(ch, "segment_types", []) or []

                # Build note text before creating the clip so it can be reused in markers
                note_lines = []
                for seg in seg_types:
                    stype   = seg.get("type", "other")
                    summary = seg.get("summary", "")
                    st      = seg.get("start_time", start)
                    en      = seg.get("end_time",   end)
                    line    = f"[{stype}] {_fcp_hms(st)}–{_fcp_hms(en)}"
                    if summary:
                        line += f": {summary}"
                    note_lines.append(line)

                clip = ET.SubElement(spine, "asset-clip",
                                     ref=main_id,
                                     name=title,
                                     offset=_fcp_time(start, fps),
                                     duration=_fcp_time(duration, fps),
                                     start=_fcp_time(start, fps),
                                     format="r1",
                                     audioRole="dialogue")

                # FCP X chapter navigation marker — shows in the viewer chapter menu
                ET.SubElement(clip, "chapter-marker",
                              start=_fcp_time(start, fps),
                              duration="0s",
                              value=title,
                              posterOffset="0s")

                # Chapter-level marker with full note for Premiere and other editors
                chapter_note = "\n".join(note_lines)
                ET.SubElement(clip, "marker",
                              start=_fcp_time(start, fps),
                              duration="0s",
                              value=title,
                              note=chapter_note,
                              completed="0")

                # Per-segment markers so every classified segment is visible in the timeline
                for seg in seg_types:
                    stype   = seg.get("type", "other")
                    summary = seg.get("summary", "")
                    seg_st  = seg.get("start_time", start)
                    seg_en  = seg.get("end_time",   end)
                    # skip duplicate at chapter start — already covered by the chapter marker above
                    if abs(seg_st - start) < 0.1:
                        continue
                    seg_label = f"[{stype}] {_fcp_hms(seg_st)}–{_fcp_hms(seg_en)}"
                    ET.SubElement(clip, "marker",
                                  start=_fcp_time(seg_st, fps),
                                  duration="0s",
                                  value=seg_label,
                                  note=summary,
                                  completed="0")

                if note_lines:
                    ET.SubElement(clip, "note").text = chapter_note

                # Overlay videos as connected clips (lane 1 = above primary storyline).
                # Attached to the first chapter they overlap, spanning their full source duration.
                if ch_idx == 0:
                    for lane, (ov_id, _, _, ov_frames, _) in enumerate(overlay_entries, 1):
                        ov_dur = ov_frames / fps
                        ET.SubElement(clip, "asset-clip",
                                      ref=ov_id,
                                      lane=str(lane),
                                      offset="0s",
                                      duration=_fcp_time(ov_dur, fps),
                                      start="0s",
                                      format="r1")
        else:
            # No chapters — single clip for the whole project
            ET.SubElement(spine, "asset-clip",
                          ref=main_id,
                          name=self.name,
                          offset="0s",
                          duration=_fcp_time(total_seconds, fps),
                          start="0s",
                          format="r1",
                          audioRole="dialogue")

        raw = ET.tostring(root, encoding="unicode")
        pretty = minidom.parseString(raw).toprettyxml(indent="  ")
        # minidom writes its own declaration; replace with the standard FCPXML one
        pretty = '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(pretty.splitlines()[1:])

        os.makedirs(self.output_dir, exist_ok=True)
        out_path = os.path.join(self.output_dir, f"{self.name}.fcpxml")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(pretty)
        print(f"FCPXML exported: {out_path}")
        return out_path

    def _resolve_path(self, vcfg: dict) -> str | None:
        pi = vcfg.get("path", {})
        if pi.get("file"):
            return pi["file"]
        d, rx = pi.get("path"), pi.get("regex")
        if d and rx:
            try:
                for fn in os.listdir(d):
                    if re.fullmatch(rx, fn):
                        return os.path.abspath(os.path.join(d, fn))
            except Exception:
                pass
        return None

    def _add_rate(self, parent):
        r = ET.SubElement(parent, "rate")
        ET.SubElement(r, "timebase").text = str(self.fps)
        ET.SubElement(r, "ntsc").text = "FALSE"

    def _add_timecode(self, seq):
        tc = ET.SubElement(seq, "timecode")
        self._add_rate(tc)
        ET.SubElement(tc, "string").text = "00:00:00:00"
        ET.SubElement(tc, "frame").text = "0"
        ET.SubElement(tc, "displayformat").text = "NDF"

    def _add_format(self, parent, w=None, h=None):
        sc = ET.SubElement(parent, "samplecharacteristics")
        self._add_rate(sc)
        ET.SubElement(sc, "width").text = str(w or self.frame_width)
        ET.SubElement(sc, "height").text = str(h or self.frame_height)
        ET.SubElement(sc, "anamorphic").text = "FALSE"
        ET.SubElement(sc, "pixelaspectratio").text = "square"
        ET.SubElement(sc, "fielddominance").text = "none"

    def _add_master_clip(self, parent, idx, path, dur, is_border=False,
                         bw=None, bh=None, display_name=None):
        name = display_name or os.path.basename(path)
        clip = ET.SubElement(parent, "clip", id=f"masterclip-{idx}")
        ET.SubElement(clip, "name").text = name
        ET.SubElement(clip, "duration").text = str(dur)
        self._add_rate(clip)

        vt = ET.SubElement(ET.SubElement(ET.SubElement(clip, "media"), "video"), "track")
        ci = ET.SubElement(vt, "clipitem", id=f"clipitem-source-{idx}")
        ET.SubElement(ci, "name").text = name

        fr = ET.SubElement(ci, "file", id=f"file-{idx}")
        ET.SubElement(fr, "name").text = name
        ET.SubElement(fr, "pathurl").text = "file://localhost/" + os.path.abspath(path).replace("\\", "/")
        ET.SubElement(fr, "duration").text = str(dur)
        med = ET.SubElement(fr, "media")
        self._add_format(ET.SubElement(med, "video"), bw, bh)
        if not is_border:
            ac = ET.SubElement(ET.SubElement(med, "audio"), "samplecharacteristics")
            ET.SubElement(ac, "depth").text = "16"
            ET.SubElement(ac, "samplerate").text = "48000"

    def _add_clip_segment(self, track, idx, vcfg, src_dur,
                           start_f, end_f, seg_type,
                           is_border=False, border_w=None, border_h=None,
                           content_w=None, content_h=None,
                           content_ox=0, content_oy=0,
                           resolved_path=None, asset_id=None,
                           display_name=None, apply_color=False,
                           seg_summary=""):
        name = display_name or os.path.basename(resolved_path or "Unknown")
        ci = ET.SubElement(track, "clipitem", id=f"clipitem-{idx}-{start_f}")
        ET.SubElement(ci, "masterclipid").text = f"masterclip-{idx}"
        ET.SubElement(ci, "name").text = name
        ET.SubElement(ci, "enabled").text = "FALSE" if seg_type == "silence" else "TRUE"
        ET.SubElement(ci, "duration").text = str(int(src_dur))
        self._add_rate(ci)
        ET.SubElement(ci, "start").text = str(int(start_f))
        ET.SubElement(ci, "end").text = str(int(end_f))
        ET.SubElement(ci, "in").text = str(int(start_f))
        ET.SubElement(ci, "out").text = str(int(end_f))
        ET.SubElement(ci, "file", id=f"file-{idx}")
        ET.SubElement(ci, "alphatype").text = "none"
        ET.SubElement(ci, "pixelaspectratio").text = "square"
        ET.SubElement(ci, "anamorphic").text = "FALSE"

        if seg_type == "silence":
            color = self.segment_colors.get("silence", "gray")
            ET.SubElement(ET.SubElement(ci, "labels"), "label2").text = self._premiere_color(color)
        elif apply_color and seg_type:
            color = self.segment_colors.get(seg_type, self.segment_colors.get("default", "white"))
            ET.SubElement(ET.SubElement(ci, "labels"), "label2").text = self._premiere_color(color)

        if is_border:
            self._apply_border_motion(ci, asset_id, border_w, border_h,
                                       content_w, content_h, content_ox, content_oy)
        else:
            overlay = (vcfg or {}).get("overlay")
            if overlay:
                self._apply_clip_motion(ci, overlay, vcfg, asset_id)

        self._add_log_and_color(ci, seg_type=seg_type, summary=seg_summary)

    def _add_audio_clip_segments(self, track, idx, src_dur, cuts, path, asset_id, display_name):
        # split audio at the same cut points as video, else they desync on delete
        name = display_name or os.path.basename(path or "Unknown")
        for ss, se, seg_type, _ in cuts:
            ci = ET.SubElement(track, "clipitem", id=f"clipitem-audio-{idx}-{ss}",
                               premiereChannelType="mono")
            ET.SubElement(ci, "masterclipid").text = f"masterclip-{idx}"
            ET.SubElement(ci, "name").text = name
            ET.SubElement(ci, "enabled").text = "FALSE" if seg_type == "silence" else "TRUE"
            ET.SubElement(ci, "duration").text = str(int(src_dur))
            self._add_rate(ci)
            ET.SubElement(ci, "start").text = str(int(ss))
            ET.SubElement(ci, "end").text = str(int(se))
            ET.SubElement(ci, "in").text = str(int(ss))
            ET.SubElement(ci, "out").text = str(int(se))
            ET.SubElement(ci, "file", id=f"file-{idx}")
            st = ET.SubElement(ci, "sourcetrack")
            ET.SubElement(st, "mediatype").text = "audio"
            ET.SubElement(st, "trackindex").text = "1"
            if seg_type == "silence":
                color = self.segment_colors.get("silence", "gray")
                ET.SubElement(ET.SubElement(ci, "labels"), "label2").text = self._premiere_color(color)
            self._add_log_and_color(ci, seg_type=seg_type)

    def _add_log_and_color(self, ci, seg_type="", summary=""):
        li = ET.SubElement(ci, "logginginfo")
        ET.SubElement(li, "description").text = seg_type or ""
        ET.SubElement(li, "scene")
        ET.SubElement(li, "shottake")
        ET.SubElement(li, "lognote").text = summary or ""
        ET.SubElement(li, "good")
        col = ET.SubElement(ci, "colorinfo")
        for tag in ("lut", "lut1", "asc_sop", "asc_sat", "lut2"):
            ET.SubElement(col, tag)

    def _normalized_position(self, overlay: dict) -> tuple:
        w_pct = float(overlay.get("width", 0.3))
        h_pct = float(overlay.get("height", w_pct))
        tw = w_pct * self.frame_width
        th = h_pct * self.frame_height
        nx = float(overlay.get("x", 0.0)) * 0.5
        ny = float(overlay.get("y", 0.0)) * 0.5
        return nx, ny, tw, th

    def _apply_clip_motion(self, ci, overlay, vcfg, asset_id):
        src_w = vcfg.get("width")
        nx, ny, tw, th = self._normalized_position(overlay)
        cnx = nx * 0.35
        cny = ny * 0.35
        scale = (tw / float(src_w)) * 100.0 if src_w else float(overlay.get("width", 0.3)) * 100.0
        self._write_motion(ci, cnx, cny, scale)
        if asset_id:
            self._clip_transforms[asset_id] = {"norm_x": cnx, "norm_y": cny,
                                                 "scale_pct": scale, "target_w": tw, "target_h": th}

    def _cache_clip_transform(self, vcfg, asset_id):
        if not asset_id or asset_id in self._clip_transforms:
            return
        overlay = (vcfg or {}).get("overlay")
        if not overlay:
            return
        src_w = vcfg.get("width")
        nx, ny, tw, th = self._normalized_position(overlay)
        cnx, cny = nx * 0.35, ny * 0.35
        scale = (tw / float(src_w)) * 100.0 if src_w else float(overlay.get("width", 0.3)) * 100.0
        self._clip_transforms[asset_id] = {"norm_x": cnx, "norm_y": cny,
                                             "scale_pct": scale, "target_w": tw, "target_h": th}

    def _apply_border_motion(self, ci, asset_id, bw, bh, cw, ch, ox, oy):
        saved = self._clip_transforms.get(asset_id) if asset_id else None
        if not saved or not all([bw, bh, cw, ch]):
            return
        scale = (saved["target_w"] / float(cw)) * 100.0
        sf = scale / 100.0
        pw_x = float(self.frame_width) / float(bw) if bw else 1.0
        pw_y = float(self.frame_height) / float(bh) if bh else 1.0
        bnx = (saved["norm_x"] + (-(ox * sf) / self.frame_width)) * pw_x
        bny = (saved["norm_y"] + (-(oy * sf) / self.frame_height)) * pw_y
        self._write_motion(ci, bnx, bny, scale)

    def _write_motion(self, ci, nx, ny, scale):
        effect = ET.SubElement(ET.SubElement(ci, "filter"), "effect")
        for tag, val in [("name", "Basic Motion"), ("effectid", "basic"),
                          ("effectcategory", "motion"), ("effecttype", "motion"),
                          ("mediatype", "video")]:
            ET.SubElement(effect, tag).text = val
        pc = ET.SubElement(effect, "parameter")
        ET.SubElement(pc, "parameterid").text = "center"
        ET.SubElement(pc, "name").text = "Center"
        v = ET.SubElement(pc, "value")
        ET.SubElement(v, "horiz").text = f"{nx:.6f}"
        ET.SubElement(v, "vert").text = f"{ny:.6f}"
        ps = ET.SubElement(effect, "parameter")
        ET.SubElement(ps, "parameterid").text = "scale"
        ET.SubElement(ps, "name").text = "Scale"
        ET.SubElement(ps, "value").text = str(int(round(scale)))
        pr = ET.SubElement(effect, "parameter")
        ET.SubElement(pr, "parameterid").text = "rotation"
        ET.SubElement(pr, "name").text = "Rotation"
        ET.SubElement(pr, "value").text = "0"

    @staticmethod
    def _premiere_color(label: str) -> str:
        mapping = {
            "green": "Forest", "blue": "Blue", "yellow": "Yellow", "orange": "Mango",
            "red": "Rose", "pink": "Rose", "purple": "Purple", "violet": "Lavender",
            "lavender": "Lavender", "cyan": "Cyan", "gray": "Iris", "grey": "Iris",
            "white": "Caribbean", "tan": "Tan",
        }
        return mapping.get(str(label).strip().lower(), str(label).strip().title()) if label else "Caribbean"


def build_timeline(config: dict, border_images: list = None, chapters: list = None,
                    silent_intervals: list = None) -> str:
    builder = TimelineBuilder(config, border_images, silent_intervals)
    if chapters:
        builder.set_chapters(chapters)
    return builder.build_and_export()
