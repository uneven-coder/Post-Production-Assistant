import numpy as np
from functools import lru_cache
from typing import Dict, Tuple
import cv2

from .interfaces import IBorderApplicator
from .image_utils import composite_alpha_fast, blend_content_fast


class OptimizedBorderApplicator(IBorderApplicator):
    def __init__(self):
        self._border_cache: dict = {}
        self._template_pool: dict = {}
        self._max_pool_size = 5

    @lru_cache(maxsize=16)
    def _calculate_dimensions(self, lw: int, rw: int, th: int, bh: int,
                               cw: int, ch: int) -> Tuple[int, int]:
        return lw + cw + rw, th + ch + bh

    def apply_border(self, content: np.ndarray, patches: Dict[str, np.ndarray],
                     scale: float = 1.0) -> np.ndarray:
        ch, cw = content.shape[:2]
        border_template, x_off, y_off = self.create_border_template(patches, cw, ch)

        output = np.empty_like(border_template)
        np.copyto(output, border_template)

        if content.shape[2] == 4:
            content_rgba = content
        else:
            content_rgba = np.empty((ch, cw, 4), dtype=np.uint8)
            content_rgba[:, :, :3] = content
            content_rgba[:, :, 3] = 255

        blend_content_fast(output, content_rgba, x_off, y_off)
        return output

    def create_border_template(self, patches: Dict[str, np.ndarray],
                                content_w: int, content_h: int) -> Tuple[np.ndarray, int, int]:
        cache_key = (id(patches), content_w, content_h)
        if cache_key in self._border_cache:
            return self._border_cache[cache_key]

        tl_h, tl_w = patches["tl"].shape[:2] if patches["tl"].size > 0 else (0, 0)
        tr_h, tr_w = patches["tr"].shape[:2] if patches["tr"].size > 0 else (0, 0)
        bl_h, bl_w = patches["bl"].shape[:2] if patches["bl"].size > 0 else (0, 0)
        br_h, br_w = patches["br"].shape[:2] if patches["br"].size > 0 else (0, 0)

        left_w  = patches["l"].shape[1] if patches["l"].size > 0 else 0
        right_w = patches["r"].shape[1] if patches["r"].size > 0 else 0
        top_h   = patches["t"].shape[0] if patches["t"].size > 0 else 0
        bot_h   = patches["b"].shape[0] if patches["b"].size > 0 else 0

        out_w, out_h = self._calculate_dimensions(left_w, right_w, top_h, bot_h,
                                                   content_w, content_h)
        template = np.zeros((out_h, out_w, 4), dtype=np.uint8)

        self._paste_edges(template, patches, out_w, out_h,
                          left_w, right_w, top_h, bot_h,
                          content_w, content_h,
                          tl_w, tr_w, bl_w, br_w,
                          tl_h, tr_h, bl_h, br_h)
        self._paste_corners(template, patches, out_w, out_h)

        result = (template, left_w, top_h)
        if len(self._border_cache) < 10:
            self._border_cache[cache_key] = result
        return result

    def _paste_edges(self, out, patches, ow, oh, lw, rw, th, bh,
                     cw, ch, tl_w, tr_w, bl_w, br_w, tl_h, tr_h, bl_h, br_h):
        def _paste(patch, dst_w, dst_h, x, y):
            if patch.size == 0 or dst_w <= 0 or dst_h <= 0:
                return
            resized = cv2.resize(patch, (dst_w, dst_h), interpolation=cv2.INTER_LINEAR)
            composite_alpha_fast(out, resized, x, y)

        _paste(patches["t"], max(0, ow - tl_w - tr_w), th, tl_w, 0)
        _paste(patches["b"], max(0, ow - bl_w - br_w), bh, bl_w, oh - bh)
        _paste(patches["l"], lw, max(0, oh - tl_h - bl_h), 0, tl_h)
        _paste(patches["r"], rw, max(0, oh - tr_h - br_h), ow - rw, tr_h)

    def _paste_corners(self, out, patches, ow, oh):
        for patch, x, y in [
            (patches["tl"], 0, 0),
            (patches["tr"], ow - patches["tr"].shape[1] if patches["tr"].size > 0 else 0, 0),
            (patches["bl"], 0, oh - patches["bl"].shape[0] if patches["bl"].size > 0 else 0),
            (patches["br"],
             ow - patches["br"].shape[1] if patches["br"].size > 0 else 0,
             oh - patches["br"].shape[0] if patches["br"].size > 0 else 0),
        ]:
            if patch.size > 0:
                composite_alpha_fast(out, patch, x, y)
