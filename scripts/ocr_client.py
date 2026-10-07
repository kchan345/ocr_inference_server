"""Reference client for the OCR inference server (requires ``httpx`` and ``Pillow``).

Examples::

    python scripts/ocr_client.py page1.png page2.jpg --server http://localhost:8080 --out downloads --extract
    python scripts/ocr_client.py huge_scan.png --fit        # downscale on the client if the server limit is exceeded
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image


class OCRServerError(Exception):
    def __init__(self, status_code: int, error: dict[str, Any]) -> None:
        super().__init__(f"HTTP {status_code} {error.get('code')}: {error.get('message')}")
        self.status_code = status_code
        self.code = error.get("code")
        self.error = error


@dataclass
class Upload:
    data: bytes
    filename: str
    mime: str


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            error = resp.json().get("error") or {}
        except ValueError:
            error = {"code": "http_error", "message": resp.text}
        raise OCRServerError(resp.status_code, error)


def get_info(client: httpx.Client) -> dict[str, Any]:
    resp = client.get("/v1/info")
    _raise_for_error(resp)
    return resp.json()


def load_upload(path: Path, max_pixels: int | None = None) -> Upload:
    """Read an image; if ``max_pixels`` is given and exceeded, downscale it client-side (aspect ratio kept)."""
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as img:
        fmt = img.format or "PNG"
        if not max_pixels or img.width * img.height <= max_pixels:
            return Upload(data, path.name, Image.MIME.get(fmt, "application/octet-stream"))
        factor = (max_pixels / (img.width * img.height)) ** 0.5
        size = (max(1, int(img.width * factor)), max(1, int(img.height * factor)))
        resized = img.convert("RGB" if fmt == "JPEG" else img.mode).resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    out_fmt = "JPEG" if fmt == "JPEG" else "PNG"
    resized.save(out, out_fmt, **({"quality": 95} if out_fmt == "JPEG" else {}))
    suffix = ".jpg" if out_fmt == "JPEG" else ".png"
    return Upload(out.getvalue(), path.stem + suffix, Image.MIME[out_fmt])


def submit(client: httpx.Client, upload: Upload, prompt: str | None = None, retries: int = 5) -> dict[str, Any]:
    """Submit an image. Retries (honouring ``Retry-After``) while the server buffer is full."""
    for attempt in range(retries + 1):
        resp = client.post(
            "/v1/ocr",
            files={"image": (upload.filename, upload.data, upload.mime)},
            data={"prompt": prompt} if prompt else None,
        )
        if resp.status_code == 503 and attempt < retries:
            time.sleep(float(resp.headers.get("retry-after", "1")))
            continue
        _raise_for_error(resp)
        return resp.json()
    raise AssertionError("unreachable")


def wait(client: httpx.Client, job_id: str, timeout: float = 900, interval: float = 1.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get(f"/v1/jobs/{job_id}")
        _raise_for_error(resp)
        meta = resp.json()
        if meta["status"] in ("succeeded", "failed"):
            return meta
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} still {meta['status']} after {timeout}s")
        time.sleep(interval)


def download(client: httpx.Client, job_id: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    resp = client.get(f"/v1/jobs/{job_id}/artifacts")
    _raise_for_error(resp)
    path = out_dir / f"{job_id}.zip"
    path.write_bytes(resp.content)
    return path


def extract(zip_path: Path, dest: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    return dest / zip_path.stem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--server", default="http://localhost:8080")
    parser.add_argument("--prompt")
    parser.add_argument("--fit", action="store_true", help="Downscale images that exceed the server pixel limit")
    parser.add_argument("--out", type=Path, default=Path("downloads"), help="Where artifact zips are saved")
    parser.add_argument("--extract", action="store_true", help="Also extract each zip into --out")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)

    with httpx.Client(base_url=args.server, timeout=120) as client:
        max_pixels = get_info(client)["limits"]["max_pixels"] if args.fit else None
        jobs: list[str] = []
        for path in args.images:
            try:
                job = submit(client, load_upload(path, max_pixels), args.prompt)
            except OCRServerError as exc:
                print(f"{path}: rejected: {exc}", file=sys.stderr)
                continue
            print(f"{path}: job {job['job_id']} accepted")
            jobs.append(job["job_id"])
        failed = 0
        for job_id in jobs:
            meta = wait(client, job_id, timeout=args.timeout)
            zip_path = download(client, job_id, args.out)
            if args.extract:
                extract(zip_path, args.out)
            print(f"{job_id}: {meta['status']} -> {zip_path}")
            failed += meta["status"] != "succeeded"
    return 1 if failed or len(jobs) != len(args.images) else 0


if __name__ == "__main__":
    sys.exit(main())
