"""Model-agnostic OCR handler interface.

A handler encapsulates everything that is specific to one OCR model:
the prompt, the OpenAI chat-completions payload shape, and how the raw
model output is post-processed into an :class:`OCRResult`.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import Any


class OCRResponseError(ValueError):
    """Raised when a backend response cannot be interpreted by a handler."""


def scale_box(box: tuple[int, int, int, int], width: int, height: int, scale: int) -> tuple[int, int, int, int]:
    """Convert a normalized ``[0, scale)`` box to clamped pixel coordinates (left, top, right, bottom)."""
    left, top, right, bottom = box

    def sx(v: int) -> int:
        return max(0, min(width, round(v * width / scale)))

    def sy(v: int) -> int:
        return max(0, min(height, round(v * height / scale)))

    return sx(left), sy(top), sx(right), sy(bottom)


@dataclass(frozen=True)
class Region:
    """A visual region (chart, figure, photo) referenced from the markdown output.

    Coordinates are in the handler's normalized space ``[0, bbox_scale)``.
    """

    left: int
    top: int
    right: int
    bottom: int
    ref: str

    def to_pixels(self, width: int, height: int, scale: int) -> tuple[int, int, int, int]:
        return scale_box((self.left, self.top, self.right, self.bottom), width, height, scale)

    def to_dict(self, width: int | None = None, height: int | None = None, scale: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = asdict(self)
        if width and height and scale:
            data["pixels"] = list(self.to_pixels(width, height, scale))
        return data


@dataclass
class OCRResult:
    markdown: str
    """Full markdown, including visual-region references."""
    text_markdown: str
    """Markdown with visual-region references removed (text only)."""
    regions: list[Region] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


class OCRHandler(abc.ABC):
    """Base class for model specific OCR handlers.

    Subclasses must set :attr:`name` and implement :meth:`build_payload`
    and :meth:`parse_response`, then register themselves with
    :func:`ocr_server.handlers.register_handler`.
    """

    name: str = ""
    display_name: str = ""
    default_model: str | None = None
    default_prompt: str = ""
    bbox_scale: int | None = None
    """Normalization scale of region coordinates, or ``None`` if the model emits no regions."""
    min_pixels: int | None = None
    max_pixels: int | None = None
    """Largest image area (w*h) the model processes without downscaling."""

    def __init__(self, **options: Any) -> None:
        self.options = options

    def resolve_prompt(self, prompt: str | None) -> str:
        return prompt if prompt and prompt.strip() else self.default_prompt

    @abc.abstractmethod
    def build_payload(self, *, model: str, image_url: str, prompt: str | None = None) -> dict[str, Any]:
        """Return the JSON body for ``POST /v1/chat/completions``."""

    @abc.abstractmethod
    def parse_response(self, response: dict[str, Any]) -> OCRResult:
        """Convert a chat-completions response into an :class:`OCRResult`."""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "default_model": self.default_model,
            "bbox_scale": self.bbox_scale,
            "min_pixels": self.min_pixels,
            "max_pixels": self.max_pixels,
        }

    @staticmethod
    def extract_message_content(response: dict[str, Any]) -> tuple[str, str | None]:
        """Return ``(choices[0].message.content, choices[0].finish_reason)``."""
        try:
            choice = response["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OCRResponseError("Backend response has no choices[0].message.content") from exc
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise OCRResponseError("Backend response message content is not a string")
        return content, choice.get("finish_reason")
