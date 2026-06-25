"""
NinePatchService — applies a 9-patch border to an image or video.
"""
import gc
import os

from PIL import Image, ImageDraw, ImageFont

from .applicator import OptimizedBorderApplicator
from .patch_loader import NumpyPatchLoader
from .image_utils import pil_to_numpy, numpy_to_pil, cleanup_caches
from .video_processor import OptimizedVideoProcessor


class NinePatchService:
    def __init__(self, ninepatch_path: str):
        self.ninepatch_path = ninepatch_path
        self._loader = NumpyPatchLoader()
        self._applicator = OptimizedBorderApplicator()
        self._video_processor = OptimizedVideoProcessor(self._applicator, self._loader)

    def process_image(self, input_path: str, output_path: str,
                      content_width: int = None, content_height: int = None,
                      scale: float = 1.0,
                      title: str = None,
                      font_path: str = None,
                      font_size: int = 32) -> None:
        with Image.open(input_path) as img:
            cw = content_width or img.size[0]
            ch = content_height or img.size[1]
            if img.size != (cw, ch):
                filt = Image.Resampling.LANCZOS if abs(img.size[0] - cw) > 100 else Image.Resampling.BILINEAR
                img = img.resize((cw, ch), filt)
            content = pil_to_numpy(img.convert("RGB"))

        patches = self._loader.load(self.ninepatch_path)
        scaled_patches = self._loader.scale_patches(patches, scale)
        bordered = self._applicator.apply_border(content, scaled_patches, scale)
        del content

        out_img = numpy_to_pil(bordered)

        if title:
            self._render_title(out_img, scaled_patches, title, font_path, font_size)

        out_img.save(output_path, optimize=True)
        print(f"Bordered image saved: {output_path} ({out_img.width}x{out_img.height})")
        out_img.close()
        gc.collect()

    def _render_title(self, img: Image.Image, patches: dict,
                      title: str, font_path: str, font_size: int) -> None:
        draw = ImageDraw.Draw(img)
        top = patches.get("t")
        top_h = top.shape[0] if top is not None and top.size > 0 else 32
        tl = patches.get("tl")
        tr = patches.get("tr")
        tl_w = tl.shape[1] if tl is not None and tl.size > 0 else 0
        tr_w = tr.shape[1] if tr is not None and tr.size > 0 else 0

        try:
            font = (ImageFont.truetype(font_path, font_size)
                    if font_path and os.path.exists(font_path)
                    else ImageFont.truetype("arial.ttf", font_size))
        except Exception:
            font = ImageFont.load_default()

        try:
            bbox = draw.textbbox((0, 0), title, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = font.getsize(title)

        x = max(tl_w, ((img.width - tl_w - tr_w) - tw) // 2 + tl_w)
        y = max(0, (top_h - th) // 2)
        draw.text((x + 1, y + 1), title, font=font, fill=(0, 0, 0))
        draw.text((x, y), title, font=font, fill=(255, 255, 255))

    def process_video(self, input_path: str, output_path: str,
                      content_width: int = None, content_height: int = None,
                      scale: float = 1.0, fps: float = None) -> None:
        self._video_processor.process_video(
            input_path, output_path, self.ninepatch_path,
            content_width, content_height, scale, fps)

    def __del__(self):
        cleanup_caches()
