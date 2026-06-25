import gc
import numpy as np
from functools import lru_cache
from numba import njit, prange
from PIL import Image


@njit(parallel=True, cache=True)
def find_vertical_bounds(alpha_channel: np.ndarray) -> tuple:
    height, width = alpha_channel.shape
    start, end = 0, height
    for i in range(height):
        if np.any(alpha_channel[i, :] > 0):
            start = i
            break
    for i in range(height - 1, -1, -1):
        if np.any(alpha_channel[i, :] > 0):
            end = i + 1
            break
    return start, end


@njit(parallel=True, cache=True)
def find_horizontal_bounds(alpha_channel: np.ndarray) -> tuple:
    height, width = alpha_channel.shape
    start, end = 0, width
    for i in range(width):
        if np.any(alpha_channel[:, i] > 0):
            start = i
            break
    for i in range(width - 1, -1, -1):
        if np.any(alpha_channel[:, i] > 0):
            end = i + 1
            break
    return start, end


@njit(cache=True)
def composite_alpha_fast(base: np.ndarray, overlay: np.ndarray, x: int, y: int) -> None:
    oh, ow = overlay.shape[:2]
    bh, bw = base.shape[:2]
    for i in range(oh):
        by = y + i
        if by >= bh:
            break
        for j in range(ow):
            bx = x + j
            if bx >= bw:
                break
            alpha_o = overlay[i, j, 3]
            if alpha_o == 0:
                continue
            if alpha_o == 255:
                base[by, bx] = overlay[i, j]
            else:
                af = alpha_o / 255.0
                ab = base[by, bx, 3] / 255.0
                ao = af + ab * (1.0 - af)
                if ao > 0:
                    for c in range(3):
                        base[by, bx, c] = np.uint8(
                            (overlay[i, j, c] * af + base[by, bx, c] * ab * (1.0 - af)) / ao
                        )
                    base[by, bx, 3] = np.uint8(ao * 255)


@njit(parallel=True, cache=True)
def blend_content_fast(base: np.ndarray, content: np.ndarray, x: int, y: int) -> None:
    ch, cw = content.shape[:2]
    has_alpha = content.shape[2] == 4
    for i in prange(ch):
        for j in prange(cw):
            if has_alpha:
                base[y + i, x + j] = content[i, j]
            else:
                base[y + i, x + j, :3] = content[i, j]
                base[y + i, x + j, 3] = 255


@lru_cache(maxsize=256)
def get_resize_dimensions(ow: int, oh: int, tw: int, th: int) -> tuple:
    return tw, th


def pil_to_numpy(image: Image.Image) -> np.ndarray:
    return np.asarray(image, dtype=np.uint8).copy()


def numpy_to_pil(array: np.ndarray) -> Image.Image:
    if array.shape[2] == 4:
        return Image.fromarray(array, mode="RGBA")
    return Image.fromarray(array, mode="RGB")


def cleanup_caches():
    get_resize_dimensions.cache_clear()
    gc.collect()
