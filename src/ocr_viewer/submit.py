"""Submit images from the viewer to an OCR inference server and import the finished artifacts.

The browser never talks to the inference server directly (no CORS needed): it uploads to the viewer, which
optionally crops the image to a user-drawn box, forwards it to ``POST {server}/v1/ocr``, tracks the job in the
background and, once it is finished, downloads ``/v1/jobs/{id}/artifacts`` and extracts it into the *inbox*
folder, which is one of the viewer's artifact roots.

Each imported job folder gets a ``viewer.json`` describing the submission:

* ``persisted_image = "cropped"`` – the folder keeps the image that was sent to the server (the crop).
* ``persisted_image = "original"`` – the cropped ``input.*`` is replaced by ``original.<ext>`` (the image
  before cropping) and ``region_frame`` records where the crop sits inside it, so region references in the
  markdown (normalized to the crop) are mapped back onto the original image.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import shutil
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
PERSIST_MODES = ("original", "cropped")
TERMINAL_STATUSES = ("succeeded", "failed")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif", "GIF": "gif"}
_CROP_MODES = {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}
# Server errors that describe the submitted image/request; anything else is reported as 502.
_PASSTHROUGH_STATUS = {400, 413, 415, 422, 503}


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
    """Parse ``"x1,y1,x2,y2"`` (pixels of the original image); empty means no box."""
    if value is None or not value.strip():
        return None
    parts = [p.strip() for p in value.split(",")]
    try:
        if len(parts) != 4:
            raise ValueError
        x1, y1, x2, y2 = (int(round(float(p))) for p in parts)
    except ValueError:
        raise SubmissionError(400, "invalid_box", "box must be 'x1,y1,x2,y2' in image pixels.") from None
    return x1, y1, x2, y2


@dataclass
class PreparedImage:
    data: bytes
    filename: str
    mime: str
    source_width: int
    source_height: int
    source_ext: str
    crop: tuple[int, int, int, int] | None


def prepare_image(data: bytes, filename: str, box: tuple[int, int, int, int] | None) -> PreparedImage:
    """Validate the upload and crop it to ``box`` (clamped to the image). Crops are encoded losslessly as PNG."""
    if not data:
        raise SubmissionError(400, "empty_image", "The uploaded file is empty.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # any decoder error (incl. decompression bombs) means the upload is unusable
        raise SubmissionError(400, "invalid_image", f"The uploaded file is not a readable image ({exc}).") from None
    fmt = img.format or ""
    ext = _EXTENSIONS.get(fmt) or (Path(filename).suffix.lstrip(".").lower() or "img")
    width, height = img.size
    stem = Path(filename).stem or "image"

    crop = None
    if box is not None:
        x1, y1, x2, y2 = box
        x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
        y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
        if x2 - x1 < 1 or y2 - y1 < 1:
            raise SubmissionError(400, "invalid_box", f"The box is empty after clamping to the {width}x{height} image.")
        if (x1, y1, x2, y2) != (0, 0, width, height):
            crop = (x1, y1, x2, y2)

    if crop is None:
        return PreparedImage(data, filename or f"image.{ext}", Image.MIME.get(fmt, "application/octet-stream"),
                             width, height, ext, None)
    region = img.crop(crop)
    if region.mode not in _CROP_MODES:
        region = region.convert("RGBA" if "A" in region.mode else "RGB")
    out = io.BytesIO()
    region.save(out, "PNG")
    name = f"{stem}_crop_{crop[0]}_{crop[1]}_{crop[2]}_{crop[3]}.png"
    return PreparedImage(out.getvalue(), name, "image/png", width, height, ext, crop)


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


class SubmissionManager:
    """Forwards uploads to inference servers and imports the finished jobs into ``inbox``."""

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
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.default_server_url = default_server_url.strip().rstrip("/") if default_server_url else None
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout
        self._client_factory = client_factory
        self._clients: dict[str, httpx.Client] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._records: dict[str, dict[str, Any]] = {}
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and JOB_ID_RE.match(str(record.get("job_id", ""))):
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

    # ------------------------------------------------------------------ queries
    def records(self) -> list[dict[str, Any]]:
        return sorted(self._records.values(), key=lambda r: r.get("submitted_at") or "", reverse=True)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._records.get(job_id)

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

    # ------------------------------------------------------------------ submission
    async def submit(
        self,
        data: bytes,
        filename: str,
        *,
        server_url: str | None = None,
        prompt: str | None = None,
        box: tuple[int, int, int, int] | None = None,
        persist: str = "cropped",
    ) -> dict[str, Any]:
        url = normalize_server_url(server_url or self.default_server_url)
        if persist not in PERSIST_MODES:
            raise SubmissionError(400, "invalid_persist", f"persist must be one of {', '.join(PERSIST_MODES)}.")
        prepared = await asyncio.to_thread(prepare_image, data, filename, box)
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

        keep_original = prepared.crop is not None and persist == "original"
        source_file = None
        if keep_original:
            source_file = f"{job_id}.source.{prepared.source_ext}"
            await asyncio.to_thread((self.records_dir / source_file).write_bytes, data)
        record = {
            "job_id": job_id,
            "server_url": url,
            "state": "submitted",
            "job_status": accepted.get("status", "queued"),
            "submitted_at": utcnow(),
            "original_filename": filename,
            "submitted_filename": prepared.filename,
            "prompt": prompt,
            "persist": "original" if prepared.crop is None else persist,
            "crop": None if prepared.crop is None else {
                "box": list(prepared.crop),
                "source_width": prepared.source_width,
                "source_height": prepared.source_height,
            },
            "source_file": source_file,
            "folder": None,
            "error": None,
            "last_error": None,
        }
        self._save(record)
        self._track(job_id)
        return record

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
        if record.get("source_file"):
            with contextlib.suppress(OSError):
                (self.records_dir / record["source_file"]).unlink()
        return dest

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
        }
        if crop and record.get("persist") == "original" and record.get("source_file"):
            source = self.records_dir / record["source_file"]
            name = "original." + source.name.rsplit(".", 1)[-1]
            shutil.copyfile(source, job_dir / name)
            if input_name and Path(input_name).name == input_name:
                (job_dir / input_name).unlink(missing_ok=True)
            view.update(persisted_image="original", image=name, region_frame=crop["box"])
        _write_json(job_dir / VIEW_FILE, view)
