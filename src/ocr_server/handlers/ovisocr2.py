"""Handler for ATH-MaaS/OvisOCR2 served by vLLM.

Follows the reference usage published on https://huggingface.co/ATH-MaaS/OvisOCR2:

* the exact prompt (including its leading newline), image placed before the text,
* greedy decoding (``temperature=0``) with ``max_tokens=16384``,
* ``enable_thinking=False`` for the chat template,
* ``mm_processor_kwargs.images_kwargs`` with ``min_pixels=448*448`` / ``max_pixels=2880*2880``,
* post-processing: ``strip()``, optional removal of ``<img src="images/bbox_...">`` blocks and
  ``_clean_truncated_repeats`` for degenerate repeated tails.
"""

from __future__ import annotations

import re
from typing import Any

from .base import OCRHandler, OCRResult, Region
from .registry import register_handler

OVISOCR2_PROMPT = (
    "\nExtract all readable content from the image in natural human reading order and output the result as a "
    "single Markdown document. For charts or images, represent them using an HTML image tag: "
    '<img src="images/bbox_{left}_{top}_{right}_{bottom}.jpg" />, where left, top, right, bottom are bounding box '
    "coordinates scaled to [0, 1000). Format formulas as LaTeX. Format tables as HTML: <table>...</table>. "
    "Transcribe all other text as standard Markdown. Preserve the original text without translation or paraphrasing."
)

IMG_TAG_BLOCK_PREFIX = '<img src="images/bbox_'
# Model card pattern is r'<img src="images/bbox_(\d+)_(\d+)_(\d+)_(\d+)\.jpg" />'; whitespace is relaxed slightly.
BBOX_IMAGE_PATTERN = re.compile(r'<img\s+src="(images/bbox_(\d+)_(\d+)_(\d+)_(\d+)\.jpg)"\s*/?>')


def clean_truncated_repeats(
    text: str,
    min_text_len: int = 8000,
    max_period: int = 200,
    min_period: int = 1,
    min_repeat_chars: int = 100,
    min_repeat_times: int = 5,
) -> str:
    """Trim a degenerate periodic tail (verbatim port of ``OvisOCR2Parser._clean_truncated_repeats``)."""
    n = len(text)
    if n < min_text_len:
        return text

    max_period = min(max_period, n - 1)
    for unit_len in range(min_period, max_period + 1):
        if text[n - 1] != text[n - 1 - unit_len]:
            continue

        match_len = 1
        idx = n - 2
        while idx >= unit_len and text[idx] == text[idx - unit_len]:
            match_len += 1
            idx -= 1

        total_len = match_len + unit_len
        repeat_times = total_len // unit_len
        tail_len = total_len % unit_len

        if repeat_times >= min_repeat_times and total_len >= min_repeat_chars:
            return text[: n - total_len + unit_len] + text[n - tail_len:]

    return text


def filter_image_tags(text: str) -> str:
    """Drop blank-line separated blocks that start with a visual-region image tag (model card behaviour)."""
    return "\n\n".join(block for block in text.split("\n\n") if not block.strip().startswith(IMG_TAG_BLOCK_PREFIX))


def extract_regions(text: str) -> list[Region]:
    regions: list[Region] = []
    seen: set[str] = set()
    for match in BBOX_IMAGE_PATTERN.finditer(text):
        ref = match.group(1)
        if ref in seen:
            continue
        seen.add(ref)
        left, top, right, bottom = (int(v) for v in match.group(2, 3, 4, 5))
        regions.append(Region(left, top, right, bottom, ref))
    return regions


@register_handler
class OvisOCR2Handler(OCRHandler):
    name = "ovisocr2"
    display_name = "OvisOCR2"
    default_model = "ATH-MaaS/OvisOCR2"
    default_prompt = OVISOCR2_PROMPT
    bbox_scale = 1000
    min_pixels = 448 * 448
    max_pixels = 2880 * 2880

    def __init__(self, *, max_tokens: int | None = None, temperature: float = 0.0, **options: Any) -> None:
        super().__init__(**options)
        self.max_tokens = max_tokens or 16384
        self.temperature = temperature

    def build_payload(self, *, model: str, image_url: str, prompt: str | None = None) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": self.resolve_prompt(prompt)},
                    ],
                }
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "chat_template_kwargs": {"enable_thinking": False},
            "mm_processor_kwargs": {
                "images_kwargs": {"min_pixels": self.min_pixels, "max_pixels": self.max_pixels},
            },
        }

    def parse_response(self, response: dict[str, Any]) -> OCRResult:
        content, finish_reason = self.extract_message_content(response)
        text = content.strip()
        markdown = clean_truncated_repeats(text)
        text_markdown = clean_truncated_repeats(filter_image_tags(text))

        warnings: list[str] = []
        if markdown != text:
            warnings.append("Removed a degenerate repeated tail from the model output.")
        if finish_reason == "length":
            warnings.append("Model output reached max_tokens and may be incomplete.")

        return OCRResult(
            markdown=markdown,
            text_markdown=text_markdown,
            regions=extract_regions(markdown),
            finish_reason=finish_reason,
            usage=response.get("usage"),
            warnings=warnings,
        )
