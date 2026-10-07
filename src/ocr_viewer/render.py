"""Markdown rendering and on-the-fly generation of visual-region images."""

from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path

import nh3
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from PIL import Image

# Region reference produced by OvisOCR2: images/bbox_{left}_{top}_{right}_{bottom}.jpg
BBOX_NAME_RE = re.compile(r"^bbox_(\d+)_(\d+)_(\d+)_(\d+)\.(jpe?g|png)$", re.IGNORECASE)
_IMG_SRC_RE = re.compile(
    r"""(\bsrc\s*=\s*["'])(?:\./)?images/(bbox_\d+_\d+_\d+_\d+\.(?:jpe?g|png))(["'])""", re.IGNORECASE
)

_md = MarkdownIt("commonmark", {"html": True}).enable("table").use(dollarmath_plugin)

_TAGS = set(nh3.ALLOWED_TAGS) | {
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
    "img", "span", "div", "sub", "sup", "br", "hr", "pre", "code",
}
_ATTRIBUTES: dict[str, set[str]] = {k: set(v) for k, v in nh3.ALLOWED_ATTRIBUTES.items()}
for _tag, _attrs in {
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"rowspan", "colspan", "align"},
    "th": {"rowspan", "colspan", "align"},
    "table": {"border"},
    "span": {"class"},
    "div": {"class"},
    "code": {"class"},
}.items():
    _ATTRIBUTES.setdefault(_tag, set()).update(_attrs)


def render_markdown(markdown: str, image_base: str) -> str:
    """Render OCR markdown to sanitized HTML; region references point at ``{image_base}/bbox_*.jpg``."""
    html = _md.render(markdown)
    html = _IMG_SRC_RE.sub(lambda m: f"{m.group(1)}{image_base}/{m.group(2)}{m.group(3)}", html)
    return nh3.clean(html, tags=_TAGS, attributes=_ATTRIBUTES, url_schemes={"http", "https"})


def parse_bbox_name(name: str) -> tuple[tuple[int, int, int, int], str] | None:
    match = BBOX_NAME_RE.match(name)
    if not match:
        return None
    box = tuple(int(v) for v in match.group(1, 2, 3, 4))
    fmt = "PNG" if match.group(5).lower() == "png" else "JPEG"
    return box, fmt  # type: ignore[return-value]


def box_to_pixels(box: tuple[int, int, int, int], width: int, height: int, scale: int) -> tuple[int, int, int, int]:
    """Same mapping as the OvisOCR2 model card (round, then clamp to the image)."""
    left, top, right, bottom = box
    x1 = max(0, min(width, round(left * width / scale)))
    y1 = max(0, min(height, round(top * height / scale)))
    x2 = max(0, min(width, round(right * width / scale)))
    y2 = max(0, min(height, round(bottom * height / scale)))
    return x1, y1, x2, y2


@lru_cache(maxsize=256)
def _crop_cached(path: str, mtime_ns: int, box: tuple[int, int, int, int], scale: int, fmt: str) -> bytes | None:
    with Image.open(path) as img:
        img.load()
        x1, y1, x2, y2 = box_to_pixels(box, img.width, img.height, scale)
        if x2 <= x1 or y2 <= y1:
            return None
        crop = img.crop((x1, y1, x2, y2))
        out = io.BytesIO()
        if fmt == "JPEG":
            crop.convert("RGB").save(out, "JPEG", quality=90)
        else:
            crop.save(out, "PNG")
        return out.getvalue()


def crop_region(image_path: Path, box: tuple[int, int, int, int], scale: int, fmt: str) -> bytes | None:
    """Crop a normalized region out of the original page image; ``None`` if the region is empty."""
    return _crop_cached(str(image_path), image_path.stat().st_mtime_ns, box, scale, fmt)
