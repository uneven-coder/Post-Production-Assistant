import os
import re

import numpy as np
from PIL import Image
import filetype

from .nine_patch import NinePatchService


def _create_background(width: int, height: int, color: tuple = (0, 0, 0)) -> Image.Image:
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :] = color
    return Image.fromarray(arr)


def _add_border_image(video_file: str, nine_patch_path: str, output_path: str,
                      width: int, height: int, scale: float = 1.0,
                      border_settings: dict = None) -> None:
    settings = border_settings or {}
    color = tuple(settings.get("background_color", (0, 0, 0)))
    bg = _create_background(width, height, color)

    tmp_path = output_path + ".tmp_bg.png"
    bg.save(tmp_path)

    try:
        service = NinePatchService(nine_patch_path)
        service.process_image(
            tmp_path, output_path,
            content_width=width, content_height=height,
            scale=scale,
            title=settings.get("title"),
            font_path=settings.get("font_path"),
            font_size=int(settings.get("font_size", 32)),
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


class BorderProcessor:
    def __init__(self, config: dict):
        self.config = config
        self.project = config.get("project", {})
        out = self.project.get("output_directory", "./output/")
        self.output_dir = out.get("file", "./output/") if isinstance(out, dict) else out
        os.makedirs(self.output_dir, exist_ok=True)

    def _media_size(self, file_path: str) -> tuple[int | None, int | None]:
        kind = filetype.guess(file_path)
        if kind and kind.mime.startswith("image/"):
            try:
                with Image.open(file_path) as img:
                    return img.size
            except Exception:
                return None, None
        if kind and kind.mime.startswith("video/"):
            try:
                import cv2
                cap = cv2.VideoCapture(file_path)
                if cap.isOpened():
                    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                    return w, h
                cap.release()
            except Exception:
                pass
        return None, None

    def _detect_content_offset(self, bordered_image_path: str, background_color: tuple,
                                tol: int = 8, max_search: int = 64) -> tuple[int, int]:
        """Find the pixel offset of the content area centre relative to the image centre."""
        try:
            with Image.open(bordered_image_path) as img:
                arr = np.asarray(img.convert("RGB"))
        except Exception:
            return 0, 0

        h, w = arr.shape[:2]
        if not (w and h):
            return 0, 0

        bg = np.array(background_color[:3], dtype=np.int16)

        def is_bg(x, y):
            return bool(np.all(np.abs(arr[y, x].astype(np.int16) - bg) <= tol))

        cx, cy = w // 2, h // 2
        seed = None
        for r in range(max_search + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < w and 0 <= y < h and is_bg(x, y):
                        seed = (x, y)
                        break
                if seed:
                    break
            if seed:
                break

        if not seed:
            return 0, 0

        sx, sy = seed
        left = sx
        while left > 0 and is_bg(left - 1, sy):
            left -= 1
        right = sx
        while right < w - 1 and is_bg(right + 1, sy):
            right += 1
        top = sy
        while top > 0 and is_bg(sx, top - 1):
            top -= 1
        bottom = sy
        while bottom < h - 1 and is_bg(sx, bottom + 1):
            bottom += 1

        return (
            int(round((left + right) / 2.0 - w / 2.0)),
            int(round((top + bottom) / 2.0 - h / 2.0)),
        )

    def _resolve_video_path(self, path_cfg: dict) -> str | None:
        if path_cfg.get("file") and os.path.exists(path_cfg["file"]):
            return path_cfg["file"]
        dir_path, regex = path_cfg.get("path"), path_cfg.get("regex")
        if dir_path and regex:
            try:
                for fname in os.listdir(dir_path):
                    if re.fullmatch(regex, fname):
                        return os.path.abspath(os.path.join(dir_path, fname))
            except Exception:
                pass
        return None

    def process_borders(self) -> list[dict]:
        results = []
        for i, v in enumerate(self.project.get("videos", []), start=1):
            v.setdefault("asset_id", f"clip_{i:03d}")
            asset_id = v["asset_id"]

            video_file = self._resolve_video_path(v.get("path", {}))
            content_w = content_h = None
            if video_file:
                content_w, content_h = self._media_size(video_file)
                if content_w and content_h:
                    v["width"] = content_w
                    v["height"] = content_h

            border = v.get("overlay", {}).get("border", {})
            if not border.get("enabled"):
                continue

            np_info = border.get("nine_patch_path", {})
            nine_patch_path = (np_info.get("file") or np_info.get("path")
                               if isinstance(np_info, dict) else np_info)

            if not all([video_file, nine_patch_path, os.path.isfile(nine_patch_path),
                        content_w, content_h]):
                continue

            base = os.path.splitext(os.path.basename(video_file))[0]
            output_path = os.path.join(self.output_dir, f"{asset_id}_{base}_bordered.png")
            scale = border.get("scale", border.get("inset_px", 8) / 8)

            _add_border_image(video_file, nine_patch_path, output_path,
                              content_w, content_h, scale, border)

            if not os.path.exists(output_path):
                continue

            with Image.open(output_path) as img:
                bw, bh = img.size

            bg_color = tuple(border.get("background_color", (0, 0, 0)))
            ox, oy = self._detect_content_offset(output_path, bg_color)

            results.append({
                "asset_id": asset_id,
                "media_path": video_file,
                "border_image_path": output_path,
                "border_width": bw,
                "border_height": bh,
                "content_width": content_w,
                "content_height": content_h,
                "content_offset_x": ox,
                "content_offset_y": oy,
            })

        return results
