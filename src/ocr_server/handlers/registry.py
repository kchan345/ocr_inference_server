"""Registry of available OCR handlers."""

from __future__ import annotations

from typing import Any

from .base import OCRHandler

_REGISTRY: dict[str, type[OCRHandler]] = {}


def register_handler(cls: type[OCRHandler]) -> type[OCRHandler]:
    """Class decorator registering an :class:`OCRHandler` under ``cls.name``."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name'")
    _REGISTRY[cls.name.lower()] = cls
    return cls


def get_handler(name: str, **options: Any) -> OCRHandler:
    try:
        cls = _REGISTRY[name.lower()]
    except KeyError:
        raise ValueError(f"Unknown OCR handler {name!r}. Available: {', '.join(available_handlers())}") from None
    return cls(**options)


def available_handlers() -> list[str]:
    return sorted(_REGISTRY)
