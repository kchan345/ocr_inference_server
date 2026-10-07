"""Submit images from the viewer to an OCR inference server and import the finished artifacts.

The browser never talks to the inference server directly (no CORS needed). Imported images are stored as
*sources* (``{inbox}/.sources``); the browser previews them through the viewer, which applies the optional
adjustments (grayscale, black & white threshold, rotation) with Pillow so that the preview, the user-drawn box
and the image sent for OCR all use the same pixels. A submission adjusts the source, crops it to the box,
forwards it to ``POST {server}/v1/ocr``, tracks the job in the background and, once it is finished, downloads
``/v1/jobs/{id}/artifacts`` and extracts it into the *inbox* folder, which is one of the viewer's artifact roots.
Sources are kept after submission so that a job can be redrawn and resubmitted.

Each imported job folder gets a ``viewer.json`` describing the submission:

* ``persisted_image = "cropped"`` – the folder keeps the image that was sent to the server (the crop).
* ``persisted_image = "original"`` – the cropped ``input.*`` is replaced by ``original.<ext>`` (the full image
  after adjustments, before cropping) and ``region_frame`` records where the crop sits inside it, so region
  references in the markdown (normalized to the crop) are mapped back onto the original image.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import json
import logging
import math
import os
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from PIL import Image

log = logging.getLogger("ocr_viewer.submit")

VIEW_FILE = "viewer.json"
RECORDS_DIR = ".submissions"
SOURCES_DIR = ".sources"
SOURCE_TTL_SECONDS = 24 * 3600  # unreferenced sources (imported but never submitted) are removed after this
PERSIST_MODES = ("original", "cropped")
STATUS_FILTERS = ("pending", "succeeded", "failed")
TERMINAL_STATUSES = ("succeeded", "failed")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif", "GIF": "gif"}
_CROP_MODES = {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}
_BROWSER_FORMATS = {"JPEG", "PNG", "GIF", "WEBP", "BMP"}
# Server errors that describe the submitted image/request; anything else is reported as 502.
_PASSTHROUGH_STATUS = {400, 413, 415, 422, 503}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SubmissionError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.details}}


def normalize_server_url(url: str | None) -> str:
    value = (url or "").strip().rstrip("/")
    if not value:
        raise SubmissionError(400, "server_url_required", "No inference server URL configured.")
    parsed = httpx.URL(value) if re.match(r"^https?://[^/\s]+", value, re.IGNORECASE) else None
    if parsed is None or not parsed.host:
        raise SubmissionError(400, "invalid_server_url", f"Not an http(s) URL: {value!r}")
    return value


def parse_box(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse ``"x1,y1,x2,y2"`` (pixels of the adjusted image); empty or ``"full"`` means no box."""
    if value is None or not value.strip() or value.strip().lower() == "full":
        return None
    parts = [p.strip() for p in value.split(",")]
    try:
        if len(parts) != 4:
            raise ValueError
        x1, y1, x2, y2 = (int(round(float(p))) for p in parts)
    except ValueError:
        raise SubmissionError(400, "invalid_box", "box must be 'x1,y1,x2,y2' in image pixels.") from None
    return x1, y1, x2, y2


def _parse_bool(value: Any, name: str) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SubmissionError(400, "invalid_adjustment", f"{name} must be true or false.")


def normalize_adjustments(rotation: Any = None, grayscale: Any = None, threshold: Any = None) -> dict | None:
    """Validate image adjustments; returns ``None`` when they change nothing.

    * ``rotation`` – degrees **clockwise**, rounded to 0.1° and normalized to (-180, 180].
    * ``grayscale`` – convert to 8-bit gray.
    * ``threshold`` – 0..255; pixels ``>= threshold`` become white, the rest black (implies grayscale).
    """
    try:
        r = float(rotation) if rotation not in (None, "") else 0.0
    except (TypeError, ValueError):
        raise SubmissionError(400, "invalid_adjustment", "rotation must be a number of degrees.") from None
    if not math.isfinite(r) or abs(r) > 3600:
        raise SubmissionError(400, "invalid_adjustment", "rotation must be between -3600 and 3600 degrees.")
    r = round(r, 1) % 360.0
    if r > 180:
        r -= 360.0
    r = round(r, 1) + 0.0  # + 0.0 turns -0.0 into 0.0
    gray = _parse_bool(grayscale, "grayscale")
    thr = None
    if threshold not in (None, ""):
        try:
            thr = float(threshold)
        except (TypeError, ValueError):
            thr = math.nan
        if not math.isfinite(thr) or not 0 <= thr <= 255 or thr != int(thr):
            raise SubmissionError(400, "invalid_adjustment", "threshold must be an integer between 0 and 255.")
        thr = int(thr)
    if r == 0 and not gray and thr is None:
        return None
    return {"rotation": r, "grayscale": gray or thr is not None, "threshold": thr}


def _open_image(data: bytes) -> Image.Image:
    if not data:
        raise SubmissionError(400, "empty_image", "The uploaded file is empty.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # any decoder error (incl. decompression bombs) means the upload is unusable
        raise SubmissionError(400, "invalid_image", f"The uploaded file is not a readable image ({exc}).") from None
    return img


def _flatten(img: Image.Image) -> Image.Image:
    """Convert to ``L`` or ``RGB``; transparency is composited onto white."""
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    if img.mode in ("LA", "La", "PA", "RGBA", "RGBa"):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    if img.mode.startswith("I;16"):
        return img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if img.mode in ("1", "L", "I", "F"):
        return img.convert("L")
    return img if img.mode == "RGB" else img.convert("RGB")


def apply_adjustments(img: Image.Image, adjustments: dict | None) -> Image.Image:
    """Rotate (clockwise, canvas expanded, white fill), then grayscale, then threshold."""
    if not adjustments:
        return img
    img = _flatten(img)
    rotation = adjustments.get("rotation") or 0
    if rotation:
        fill = 255 if img.mode == "L" else (255, 255, 255)
        img = img.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fill)
    threshold = adjustments.get("threshold")
    if (adjustments.get("grayscale") or threshold is not None) and img.mode != "L":
        img = img.convert("L")
    if threshold is not None:
        img = img.point([0] * threshold + [255] * (256 - threshold))
    return img


def _source_ext(img: Image.Image, filename: str) -> str:
    return _EXTENSIONS.get(img.format or "") or (Path(filename).suffix.lstrip(".").lower() or "img")


def _png(img: Image.Image, compress_level: int = 6) -> bytes:
    if img.mode not in _CROP_MODES:
        img = img.convert("RGBA" if "A" in img.mode else "RGB")
    out = io.BytesIO()
    img.save(out, "PNG", compress_level=compress_level)
    return out.getvalue()


@dataclass
class PreparedImage:
    data: bytes
    filename: str
    mime: str
    source_width: int
    source_height: int
    source_ext: str
    crop: tuple[int, int, int, int] | None


def prepare_image(
    data: bytes, filename: str, box: tuple[int, int, int, int] | None, adjustments: dict | None = None
) -> PreparedImage:
    """Validate the image, apply ``adjustments`` and crop to ``box`` (clamped, in adjusted-image pixels).

    Without adjustments and box the original bytes are sent unchanged; anything else is encoded losslessly as PNG.
    ``source_width``/``source_height`` describe the adjusted image the box refers to.
    """
    img = _open_image(data)
    fmt = img.format or ""
    ext = _source_ext(img, filename)
    stem = Path(filename).stem or "image"
    img = apply_adjustments(img, adjustments)
    width, height = img.size

    crop = None
    if box is not None:
        x1, y1, x2, y2 = box
        x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
        y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
        if x2 - x1 < 1 or y2 - y1 < 1:
            raise SubmissionError(400, "invalid_box", f"The box is empty after clamping to the {width}x{height} image.")
        if (x1, y1, x2, y2) != (0, 0, width, height):
            crop = (x1, y1, x2, y2)

    if crop is None and not adjustments:
        return PreparedImage(data, filename or f"image.{ext}", Image.MIME.get(fmt, "application/octet-stream"),
                             width, height, ext, None)
    out = img.crop(crop) if crop else img
    name = stem + ("_adjusted" if adjustments else "")
    if crop:
        name += f"_crop_{crop[0]}_{crop[1]}_{crop[2]}_{crop[3]}"
    return PreparedImage(_png(out), name + ".png", "image/png", width, height, ext, crop)


def render_full(data: bytes, filename: str, adjustments: dict | None) -> tuple[bytes, str]:
    """The full (uncropped) image after adjustments: original bytes when there are none, PNG otherwise."""
    img = _open_image(data)
    if not adjustments:
        return data, _source_ext(img, filename)
    return _png(apply_adjustments(img, adjustments)), "png"


@functools.lru_cache(maxsize=6)
def _render_preview(path: str, mtime: float, rotation: float, grayscale: bool, threshold: int | None):
    data = Path(path).read_bytes()
    img = _open_image(data)
    adjustments = normalize_adjustments(rotation, grayscale, threshold)
    if adjustments is None and img.format in _BROWSER_FORMATS:
        return data, Image.MIME.get(img.format, "application/octet-stream")
    img = apply_adjustments(img, adjustments) if adjustments else _flatten(img)
    return _png(img, compress_level=1), "image/png"


def _error_from_response(resp: httpx.Response) -> SubmissionError:
    try:
        error = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    code = str(error.get("code") or "server_error")
    message = str(error.get("message") or f"Inference server returned HTTP {resp.status_code}.")
    details = {k: v for k, v in error.items() if k not in ("code", "message")}
    details.update(source="inference_server", server_status=resp.status_code)
    if "retry-after" in resp.headers:
        details["retry_after"] = resp.headers["retry-after"]
    status = resp.status_code if resp.status_code in _PASSTHROUGH_STATUS else 502
    return SubmissionError(status, code, message, **details)


def safe_extract(archive: bytes, dest: Path, job_id: str) -> Path:
    """Extract a job zip whose entries all live under ``{job_id}/``; refuses anything else."""
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for info in zf.infolist():
            parts = PurePosixPath(info.filename).parts
            if not parts or parts[0] != job_id or any(p in ("..", "") for p in parts) or info.filename.startswith("/"):
                raise ValueError(f"Unexpected entry in artifact archive: {info.filename!r}")
        zf.extractall(dest)
    job_dir = dest / job_id
    if not (job_dir / "job.json").is_file():
        raise ValueError("Artifact archive has no job.json")
    return job_dir


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def submission_status(record: dict[str, Any]) -> str:
    """``pending`` (not finished yet), ``succeeded`` or ``failed`` (job failed or the submission errored)."""
    if record.get("state") == "error":
        return "failed"
    if record.get("state") == "imported":
        return "succeeded" if record.get("job_status") == "succeeded" else "failed"
    return "pending"


def job_image(folder: Path) -> tuple[Path | None, dict[str, Any]]:
    """The image shown for a job folder (``viewer.json`` image, else the input image) and its view settings."""
    view = _read_json(folder / VIEW_FILE)
    meta = _read_json(folder / "job.json")
    input_meta = meta.get("input") if isinstance(meta.get("input"), dict) else {}
    for name in (view.get("image"), input_meta.get("filename")):
        if name and Path(str(name)).name == name and (folder / name).is_file():
            return folder / name, view
    return None, view


class SubmissionManager:
    """Stores imported images, forwards submissions to inference servers and imports the finished jobs."""

    def __init__(
        self,
        inbox: Path,
        default_server_url: str | None = None,
        *,
        client_factory: Callable[[str], httpx.Client] | None = None,
        poll_interval: float = 1.0,
        request_timeout: float = 120.0,
    ) -> None:
        self.inbox = Path(inbox).resolve()
        self.records_dir = self.inbox / RECORDS_DIR
        self.sources_dir = self.inbox / SOURCES_DIR
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.default_server_url = default_server_url.strip().rstrip("/") if default_server_url else None
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout
        self._client_factory = client_factory
        self._clients: dict[str, httpx.Client] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._records: dict[str, dict[str, Any]] = {}
        for path in sorted(self.records_dir.glob("*.json")):
            record = _read_json(path)
            if JOB_ID_RE.match(str(record.get("job_id", ""))):
                self._records[record["job_id"]] = record

    # ------------------------------------------------------------------ plumbing
    def client(self, url: str) -> httpx.Client:
        client = self._clients.get(url)
        if client is None:
            if self._client_factory is not None:
                client = self._client_factory(url)
            else:
                client = httpx.Client(base_url=url, timeout=httpx.Timeout(self.request_timeout, connect=10.0))
            self._clients[url] = client
        return client

    async def start(self) -> None:
        await asyncio.to_thread(self.cleanup_sources)
        for job_id, record in self._records.items():
            if record.get("state") == "submitted":
                self._track(job_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._client_factory is None:
            for client in self._clients.values():
                client.close()
        self._clients.clear()

    def _save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = utcnow()
        self._records[record["job_id"]] = record
        _write_json(self.records_dir / f"{record['job_id']}.json", record)

    def _update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        record = {**self._records[job_id], **changes}
        self._save(record)
        return record

    # ------------------------------------------------------------------ sources (imported images)
    def add_source(self, data: bytes, filename: str) -> dict[str, Any]:
        img = _open_image(data)
        source_id = uuid.uuid4().hex
        ext = _source_ext(img, filename)
        meta = {
            "source_id": source_id,
            "filename": Path(filename or "").name or f"image.{ext}",
            "format": img.format,
            "ext": ext,
            "width": img.width,
            "height": img.height,
            "size": len(data),
            "created_at": utcnow(),
        }
        (self.sources_dir / f"{source_id}.{ext}").write_bytes(data)
        _write_json(self.sources_dir / f"{source_id}.json", meta)
        return meta

    def source(self, source_id: str) -> tuple[dict[str, Any], Path]:
        meta = _read_json(self.sources_dir / f"{source_id}.json") if JOB_ID_RE.match(source_id or "") else {}
        path = self.sources_dir / f"{source_id}.{meta.get('ext')}"
        if not meta or not path.is_file():
            raise SubmissionError(404, "source_not_found", f"No imported image {source_id!r}.")
        return meta, path

    def preview(self, source_id: str, adjustments: dict | None) -> tuple[bytes, str]:
        _, path = self.source(source_id)
        adj = adjustments or {}
        return _render_preview(str(path), path.stat().st_mtime, adj.get("rotation") or 0.0,
                               bool(adj.get("grayscale")), adj.get("threshold"))

    def _referenced_sources(self) -> set[str]:
        return {str(r["source_id"]) for r in self._records.values() if r.get("source_id")}

    def _remove_source(self, source_id: str) -> None:
        for path in self.sources_dir.glob(f"{source_id}.*"):
            with contextlib.suppress(OSError):
                path.unlink()

    def delete_source(self, source_id: str) -> bool:
        """Delete an imported image that was never submitted (submitted ones are kept for resubmission)."""
        self.source(source_id)
        if source_id in self._referenced_sources():
            return False
        self._remove_source(source_id)
        return True

    def cleanup_sources(self, max_age: float = SOURCE_TTL_SECONDS) -> int:
        referenced = self._referenced_sources()
        removed = 0
        for meta_path in self.sources_dir.glob("*.json"):
            source_id = meta_path.stem
            if source_id in referenced:
                continue
            with contextlib.suppress(OSError):
                if time.time() - meta_path.stat().st_mtime > max_age:
                    self._remove_source(source_id)
                    removed += 1
        return removed

    # ------------------------------------------------------------------ queries
    def records(self) -> list[dict[str, Any]]:
        return sorted(self._records.values(), key=lambda r: r.get("submitted_at") or "", reverse=True)

    def query(self, page: int = 1, page_size: int = 20, status: str | None = None, q: str | None = None) -> dict:
        items = self.records()
        if status:
            items = [r for r in items if submission_status(r) == status]
        if q:
            needle = q.strip().lower()
            items = [r for r in items if needle in r["job_id"] or needle in str(r.get("original_filename", "")).lower()
                     or needle in str(r.get("resubmit_of") or "")]
        total = len(items)
        pages = max(1, math.ceil(total / page_size))
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        return {"items": items[start:start + page_size], "total": total, "page": page, "page_size": page_size,
                "pages": pages}

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._records.get(job_id)

    def require(self, job_id: str) -> dict[str, Any]:
        record = self._records.get(job_id)
        if record is None:
            raise SubmissionError(404, "submission_not_found", f"No submission {job_id!r}.")
        return record

    def server_info(self, server_url: str | None) -> dict[str, Any]:
        url = normalize_server_url(server_url or self.default_server_url)
        try:
            resp = self.client(url).get("/v1/info")
        except httpx.HTTPError as exc:
            raise SubmissionError(502, "server_unreachable", f"Could not reach {url}: {exc}") from None
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        try:
            info = resp.json()
        except ValueError:
            raise SubmissionError(502, "invalid_server_response", f"{url}/v1/info did not return JSON.") from None
        return {"server_url": url, "info": info}

    # ------------------------------------------------------------------ drafts (redraw & resubmit)
    def draft(self, job_id: str) -> dict[str, Any]:
        """Settings to reopen a submission in the editor: its source image, adjustments, box, prompt, server."""
        record = self.require(job_id)
        crop = record.get("crop")
        box = list(crop["box"]) if crop else None
        adjustments = record.get("adjustments")
        source_id = record.get("source_id")
        try:
            meta = self.source(source_id)[0] if source_id else None
        except SubmissionError:
            meta = None
        if meta is None:
            # Source gone (or a submission from an older viewer): fall back to the image kept in the job folder.
            image, view = job_image(Path(record["folder"])) if record.get("folder") else (None, {})
            if image is None:
                raise SubmissionError(409, "source_unavailable", "The image of this submission is no longer available.")
            meta = self.add_source(image.read_bytes(), record.get("original_filename") or image.name)
            box = box if view.get("persisted_image") == "original" else None
            adjustments = None
        return {
            "source": meta,
            "box": box,
            "adjustments": adjustments,
            "persist": record.get("persist") or "original",
            "prompt": record.get("prompt"),
            "server_url": record.get("server_url") or self.default_server_url,
            "resubmit_of": job_id,
        }

    def draft_from_folder(self, folder: Path, job_id: str, filename: str | None) -> dict[str, Any]:
        """Draft for a job that was not submitted through this viewer: its displayed image, whole."""
        if job_id in self._records:
            return self.draft(job_id)
        image, view = job_image(folder)
        if image is None:
            raise SubmissionError(409, "source_unavailable", "The job folder has no input image.")
        frame = view.get("region_frame") if view.get("persisted_image") == "original" else None
        return {
            "source": self.add_source(image.read_bytes(), filename or image.name),
            "box": list(frame) if isinstance(frame, list) and len(frame) == 4 else None,
            "adjustments": None,
            "persist": view.get("persisted_image") or "original",
            "prompt": None,
            "server_url": view.get("server_url") or self.default_server_url,
            "resubmit_of": job_id if JOB_ID_RE.match(job_id) else None,
        }

    # ------------------------------------------------------------------ submission
    async def submit(
        self,
        source_id: str,
        *,
        server_url: str | None = None,
        prompt: str | None = None,
        box: tuple[int, int, int, int] | None = None,
        persist: str = "cropped",
        adjustments: dict | None = None,
        resubmit_of: str | None = None,
    ) -> dict[str, Any]:
        url = normalize_server_url(server_url or self.default_server_url)
        if persist not in PERSIST_MODES:
            raise SubmissionError(400, "invalid_persist", f"persist must be one of {', '.join(PERSIST_MODES)}.")
        if resubmit_of and not JOB_ID_RE.match(resubmit_of):
            raise SubmissionError(400, "invalid_resubmit_of", "resubmit_of must be a job id.")
        meta, path = self.source(source_id)
        data = await asyncio.to_thread(path.read_bytes)
        prepared = await asyncio.to_thread(prepare_image, data, meta["filename"], box, adjustments)
        prompt = prompt if prompt and prompt.strip() else None

        def post() -> httpx.Response:
            return self.client(url).post(
                "/v1/ocr",
                files={"image": (prepared.filename, prepared.data, prepared.mime)},
                data={"prompt": prompt} if prompt else None,
            )

        try:
            resp = await asyncio.to_thread(post)
        except httpx.HTTPError as exc:
            raise SubmissionError(502, "server_unreachable", f"Could not reach {url}: {exc}") from None
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        try:
            accepted = resp.json()
            job_id = str(accepted["job_id"])
        except (ValueError, KeyError, TypeError):
            raise SubmissionError(502, "invalid_server_response", "Server did not return a job_id.") from None
        if not JOB_ID_RE.match(job_id):
            raise SubmissionError(502, "invalid_server_response", f"Unexpected job id {job_id!r}.")

        record = {
            "job_id": job_id,
            "server_url": url,
            "state": "submitted",
            "job_status": accepted.get("status", "queued"),
            "submitted_at": utcnow(),
            "original_filename": meta["filename"],
            "submitted_filename": prepared.filename,
            "prompt": prompt,
            "persist": "original" if prepared.crop is None else persist,
            "crop": None if prepared.crop is None else {
                "box": list(prepared.crop),
                "source_width": prepared.source_width,
                "source_height": prepared.source_height,
            },
            "adjustments": adjustments,
            "source_id": source_id,
            "resubmit_of": resubmit_of or None,
            "folder": None,
            "error": None,
            "last_error": None,
        }
        self._save(record)
        self._track(job_id)
        return record

    async def resubmit(self, job_id: str, **overrides: Any) -> dict[str, Any]:
        """Submit a finished submission again; ``overrides`` (box, adjustments, persist, prompt, server_url)
        replace the stored settings."""
        draft = await asyncio.to_thread(self.draft, job_id)
        box = overrides["box"] if "box" in overrides else (tuple(draft["box"]) if draft["box"] else None)
        return await self.submit(
            draft["source"]["source_id"],
            server_url=overrides.get("server_url") or draft["server_url"],
            prompt=overrides["prompt"] if overrides.get("prompt") is not None else draft["prompt"],
            box=box,
            persist=overrides.get("persist") or draft["persist"],
            adjustments=overrides["adjustments"] if "adjustments" in overrides else draft["adjustments"],
            resubmit_of=job_id,
        )

    # ------------------------------------------------------------------ tracking / import
    def _track(self, job_id: str) -> None:
        if job_id not in self._tasks or self._tasks[job_id].done():
            self._tasks[job_id] = asyncio.create_task(self._follow(job_id), name=f"follow-{job_id}")

    async def _follow(self, job_id: str) -> None:
        delay = self.poll_interval
        while True:
            record = self._records[job_id]
            try:
                status = await asyncio.to_thread(self._fetch_status, record)
                if status != record.get("job_status") or record.get("last_error"):
                    record = self._update(job_id, job_status=status, last_error=None)
                if status in TERMINAL_STATUSES:
                    folder = await asyncio.to_thread(self._import, record)
                    self._update(job_id, state="imported", folder=str(folder), source_file=None)
                    log.info("Imported job %s into %s", job_id, folder)
                    return
                delay = self.poll_interval
            except SubmissionError as exc:
                self._update(job_id, state="error", error=f"{exc.code}: {exc.message}")
                return
            except (httpx.HTTPError, OSError) as exc:
                # Server temporarily unreachable: keep trying with backoff, the job is not lost.
                self._update(job_id, last_error=f"{type(exc).__name__}: {exc}")
                delay = min(max(delay, self.poll_interval) * 2, 30.0)
            except Exception as exc:
                log.exception("Importing job %s failed", job_id)
                self._update(job_id, state="error", error=f"Import failed: {type(exc).__name__}: {exc}")
                return
            await asyncio.sleep(delay)

    def _fetch_status(self, record: dict[str, Any]) -> str:
        resp = self.client(record["server_url"]).get(f"/v1/jobs/{record['job_id']}")
        if resp.status_code == 404:
            raise SubmissionError(404, "job_not_found", "The inference server no longer knows this job.")
        resp.raise_for_status()
        return str(resp.json().get("status"))

    def _import(self, record: dict[str, Any]) -> Path:
        job_id = record["job_id"]
        dest = self.inbox / job_id
        if (dest / VIEW_FILE).is_file():  # already imported (e.g. finished just before a restart)
            return dest
        resp = self.client(record["server_url"]).get(f"/v1/jobs/{job_id}/artifacts")
        resp.raise_for_status()
        staging = self.inbox / f".{job_id}.{uuid.uuid4().hex[:8]}.importing"
        staging.mkdir()
        try:
            job_dir = safe_extract(resp.content, staging, job_id)
            self._apply_view_settings(job_dir, record)
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(job_dir, dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if record.get("source_file"):  # legacy records kept a separate copy of the original
            with contextlib.suppress(OSError):
                (self.records_dir / record["source_file"]).unlink()
        return dest

    def _full_image(self, record: dict[str, Any]) -> tuple[bytes, str] | None:
        if record.get("source_file"):
            legacy = self.records_dir / record["source_file"]
            if legacy.is_file():
                return legacy.read_bytes(), legacy.name.rsplit(".", 1)[-1]
        try:
            meta, path = self.source(record.get("source_id") or "")
        except SubmissionError:
            return None
        return render_full(path.read_bytes(), meta["filename"], record.get("adjustments"))

    def _apply_view_settings(self, job_dir: Path, record: dict[str, Any]) -> None:
        meta = json.loads((job_dir / "job.json").read_text("utf-8"))
        input_name = str((meta.get("input") or {}).get("filename") or "")
        crop = record.get("crop")
        view: dict[str, Any] = {
            "schema_version": 1,
            "source": "ocr-viewer",
            "server_url": record["server_url"],
            "submitted_at": record["submitted_at"],
            "original_filename": record["original_filename"],
            "persisted_image": "cropped" if crop else "original",
            "image": input_name or None,
            "crop": crop,
            "region_frame": None,
            "adjustments": record.get("adjustments"),
            "source_id": record.get("source_id"),
            "resubmit_of": record.get("resubmit_of"),
        }
        if crop and record.get("persist") == "original":
            full = self._full_image(record)
            if full is None:
                log.warning("Original image of job %s is gone; keeping the cropped image", record["job_id"])
            else:
                data, ext = full
                name = f"original.{ext}"
                (job_dir / name).write_bytes(data)
                if input_name and Path(input_name).name == input_name and input_name != name:
                    (job_dir / input_name).unlink(missing_ok=True)
                view.update(persisted_image="original", image=name, region_frame=crop["box"])
        _write_json(job_dir / VIEW_FILE, view)
