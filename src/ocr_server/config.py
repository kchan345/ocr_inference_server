"""Server configuration, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    backend_url: str = "http://localhost:8000/v1"
    """Base URL of the OpenAI-compatible API (``.../v1``)."""
    backend_model: str | None = None
    """Model id sent to the backend; defaults to the handler's ``default_model``."""
    backend_api_key: str | None = None
    handler: str = "ovisocr2"
    artifact_dir: Path = Path("artifacts")
    max_pixels: int | None = None
    """Reject images with more pixels than this. Defaults to the handler's ``max_pixels``."""
    max_upload_bytes: int = 50 * 1024 * 1024
    max_prompt_chars: int = 8000
    request_timeout: float = 600.0
    max_tokens: int | None = None

    max_concurrency: int = 30
    """Maximum number of OCR calls in flight against the backend at any time."""
    max_buffer: int = 60
    """Maximum number of accepted jobs waiting for a backend slot before new submissions are rejected."""
    batch_max_size: int = 30
    """Dispatch a batch as soon as it holds this many jobs ..."""
    batch_window_seconds: float = 10.0
    """... or when its oldest job has waited this long, whichever comes first."""

    def __post_init__(self) -> None:
        for name in ("max_concurrency", "max_buffer", "batch_max_size", "max_upload_bytes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.batch_window_seconds < 0:
            raise ValueError("batch_window_seconds must be >= 0")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(key: str) -> str | None:
            value = env.get(key)
            return value if value not in (None, "") else None

        def as_int(key: str) -> int | None:
            value = get(key)
            return int(value) if value is not None else None

        def as_float(key: str) -> float | None:
            value = get(key)
            return float(value) if value is not None else None

        values = {
            "backend_url": get("OCR_BACKEND_URL"),
            "backend_model": get("OCR_BACKEND_MODEL"),
            "backend_api_key": get("OCR_BACKEND_API_KEY"),
            "handler": get("OCR_HANDLER"),
            "artifact_dir": Path(get("OCR_ARTIFACT_DIR")) if get("OCR_ARTIFACT_DIR") else None,
            "max_pixels": as_int("OCR_MAX_PIXELS"),
            "max_upload_bytes": as_int("OCR_MAX_UPLOAD_BYTES"),
            "max_prompt_chars": as_int("OCR_MAX_PROMPT_CHARS"),
            "request_timeout": as_float("OCR_REQUEST_TIMEOUT"),
            "max_tokens": as_int("OCR_MAX_TOKENS"),
            "max_concurrency": as_int("OCR_MAX_CONCURRENCY"),
            "max_buffer": as_int("OCR_MAX_BUFFER"),
            "batch_max_size": as_int("OCR_BATCH_MAX_SIZE"),
            "batch_window_seconds": as_float("OCR_BATCH_WINDOW_SECONDS"),
        }
        return cls(**{k: v for k, v in values.items() if v is not None})
