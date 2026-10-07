"""``python -m ocr_server`` / ``ocr-server`` entry point."""

from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ocr-server",
        description="Async OCR inference server. Other settings come from OCR_* environment variables (see README).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--backend-url", help="OpenAI-compatible base URL, e.g. http://192.168.1.211:8080/v1")
    parser.add_argument("--handler", help="OCR handler name (default: ovisocr2)")
    parser.add_argument("--artifact-dir", type=Path, help="Folder where job artifact folders are written")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    overrides = {
        "backend_url": args.backend_url,
        "handler": args.handler,
        "artifact_dir": args.artifact_dir,
    }
    settings = dataclasses.replace(settings, **{k: v for k, v in overrides.items() if v is not None})
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
