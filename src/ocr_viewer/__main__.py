"""``python -m ocr_viewer`` / ``ocr-viewer`` entry point."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ocr-viewer",
        description="Browse OCR job artifact folders produced by the OCR inference server.",
    )
    parser.add_argument(
        "artifacts", nargs="+", type=Path,
        help="Artifact folders: a server artifact root, a folder of extracted job zips, or a single job folder.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-depth", type=int, default=3, help="How deep to search for job folders (default 3)")
    parser.add_argument("--open", action="store_true", help="Open the viewer in a web browser")
    args = parser.parse_args(argv)

    missing = [str(p) for p in args.artifacts if not p.is_dir()]
    if missing:
        parser.error(f"not a directory: {', '.join(missing)}")

    app = create_app(args.artifacts, max_depth=args.max_depth)
    if args.open:
        url = f"http://{args.host}:{args.port}/"
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
