"""
Apply 9-patch border to a video via FFmpeg filter_complex (ProRes 4444 output).
Only called when processing video files directly — not used by the main pipeline
(which generates a static border PNG for the Premiere timeline instead).
"""
import gc
import os
import subprocess
import tempfile
import time
import uuid
from typing import Optional

import numpy as np

from .interfaces import IVideoProcessor, IBorderApplicator, IPatchLoader
from .image_utils import numpy_to_pil


class OptimizedVideoProcessor(IVideoProcessor):
    def __init__(self, border_applicator: IBorderApplicator, patch_loader: IPatchLoader):
        self._border_applicator = border_applicator
        self._patch_loader = patch_loader

    def process_video(self, input_path: str, output_path: str,
                      ninepatch_path: str,
                      content_width: Optional[int] = None,
                      content_height: Optional[int] = None,
                      scale: float = 1.0,
                      fps: Optional[float] = None) -> str:
        import cv2
        print(f"Processing video: {input_path}")
        cap = cv2.VideoCapture(input_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {input_path}")
        src_fps = fps or cap.get(cv2.CAP_PROP_FPS)
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        cw = content_width or src_w
        ch = content_height or src_h

        patches = self._patch_loader.load(ninepatch_path)
        scaled = self._patch_loader.scale_patches(patches, scale)
        template, x_off, y_off = self._border_applicator.create_border_template(scaled, cw, ch)
        out_h, out_w = template.shape[:2]

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            overlay_path = tmp.name
        try:
            numpy_to_pil(template).save(overlay_path, "PNG", compress_level=1)
            actual_out = self._ffmpeg_overlay(
                input_path, output_path, overlay_path,
                cw, ch, x_off, y_off, out_w, out_h, src_fps, src_w, src_h,
            )
        finally:
            if os.path.exists(overlay_path):
                os.remove(overlay_path)
            gc.collect()

        print(f"Video saved to {actual_out}")
        return actual_out

    def _ffmpeg_overlay(self, input_path, output_path, overlay_path,
                        cw, ch, x_off, y_off, out_w, out_h, fps, src_w, src_h) -> str:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        if not output_path.lower().endswith(".mov"):
            output_path = os.path.splitext(output_path)[0] + ".mov"

        # Handle locked file on Windows
        if os.path.exists(output_path):
            for attempt in range(3):
                try:
                    gc.collect()
                    time.sleep(0.5)
                    os.remove(output_path)
                    break
                except PermissionError:
                    if attempt == 2:
                        output_path = (os.path.splitext(output_path)[0]
                                       + f"_{uuid.uuid4().hex[:8]}.mov")

        filters = []
        if src_w != cw or src_h != ch:
            filters.append(f"[0:v]scale={cw}:{ch}:flags=lanczos[scaled]")
            base = "[scaled]"
        else:
            base = "[0:v]"
        filters.append(f"{base}format=yuva420p[va]")
        filters.append(f"[va]pad={out_w}:{out_h}:{x_off}:{y_off}:color=0x00000000[padded]")
        filters.append("[padded][1:v]overlay=0:0:format=auto[out]")

        cmd = [
            "ffmpeg", "-y", "-i", input_path, "-i", overlay_path,
            "-filter_complex", ";".join(filters),
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "prores_ks", "-profile:v", "4444",
            "-pix_fmt", "yuva444p10le", "-vendor", "ap10", "-bits_per_mb", "8000",
            "-r", str(fps), "-c:a", "pcm_s16le",
            output_path,
        ]

        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE,
                                universal_newlines=True)
        for line in proc.stderr:
            if "frame=" in line:
                print(f"\r{line.strip()}", end="", flush=True)
        proc.wait()
        print()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        return output_path
