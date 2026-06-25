import numpy as np
from PIL import Image
from typing import Dict, Tuple
import cv2

from .interfaces import IPatchLoader
from .image_utils import find_vertical_bounds, find_horizontal_bounds, pil_to_numpy


class NumpyPatchLoader(IPatchLoader):
    def __init__(self):
        self._cache: dict = {}
        self._scale_cache: dict = {}
        self._access_count: dict = {}
        self._max_cache_size = 25

    def load(self, path: str) -> Dict[str, np.ndarray]:
        if path in self._cache:
            self._access_count[path] = self._access_count.get(path, 0) + 1
            return self._cache[path]

        if len(self._cache) >= self._max_cache_size:
            lru = min(self._access_count, key=self._access_count.get)
            del self._cache[lru], self._access_count[lru]

        with Image.open(path) as img:
            img = img.convert("RGBA")
            w, h = img.size
            arr = pil_to_numpy(img.crop((1, 1, w - 1, h - 1)))

        w, h = w - 2, h - 2
        third_w, third_h = w // 3, h // 3
        l, r = third_w, w - third_w
        t, b = third_h, h - third_h

        def crop_edge(patch, orientation):
            if patch.size == 0:
                return patch
            alpha = patch[:, :, 3]
            if orientation == "horizontal":
                s, e = find_vertical_bounds(alpha)
                return patch[s:e] if e > s else patch
            s, e = find_horizontal_bounds(alpha)
            return patch[:, s:e] if e > s else patch

        patches = {
            "tl": arr[0:t, 0:l].copy(),
            "tr": arr[0:t, r:w].copy(),
            "bl": arr[b:h, 0:l].copy(),
            "br": arr[b:h, r:w].copy(),
            "t":  crop_edge(arr[0:t, l:r], "horizontal").copy(),
            "b":  crop_edge(arr[b:h, l:r], "horizontal").copy(),
            "l":  crop_edge(arr[t:b, 0:l], "vertical").copy(),
            "r":  crop_edge(arr[t:b, r:w], "vertical").copy(),
            "center": arr[t:b, l:r].copy(),
        }
        self._cache[path] = patches
        self._access_count[path] = 1
        return patches

    def scale_patches(self, patches: Dict[str, np.ndarray], scale: float) -> Dict[str, np.ndarray]:
        if scale == 1.0:
            return patches
        key = (id(patches), scale)
        if key in self._scale_cache:
            return self._scale_cache[key]
        if len(self._scale_cache) > 50:
            for k in list(self._scale_cache)[:10]:
                del self._scale_cache[k]

        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        scaled = {}
        for k, p in patches.items():
            if p.size == 0:
                scaled[k] = p
                continue
            ph, pw = p.shape[:2]
            if ph > 0 and pw > 0:
                scaled[k] = cv2.resize(p, (max(1, int(pw * scale)), max(1, int(ph * scale))),
                                        interpolation=interp)
            else:
                scaled[k] = p

        self._scale_cache[key] = scaled
        return scaled
