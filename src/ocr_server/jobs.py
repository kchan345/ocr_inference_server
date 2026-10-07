"""Job persistence (artifact folders) and the batching job runner."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import logging
import os
import re
import threading
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .handlers import OCRHandler, OCRResponseError

log = logging.getLogger("ocr_server.jobs")

JOB_FILE = "job.json"
SCHEMA_VERSION = 1
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JobNotFound(KeyError):
    pass


class JobStore:
    """Stores each job in ``<root>/<job_id>/`` with a ``job.json`` metadata file."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.root / ".archives"
        self._lock = threading.Lock()

    @staticmethod
    def new_job_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def is_valid_id(job_id: str) -> bool:
        return bool(JOB_ID_RE.match(job_id))

    def job_dir(self, job_id: str) -> Path:
        if not self.is_valid_id(job_id):
            raise JobNotFound(job_id)
        return self.root / job_id

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def write_bytes(self, job_id: str, name: str, data: bytes) -> None:
        self._atomic_write(self.job_dir(job_id) / name, data)

    def write_text(self, job_id: str, name: str, text: str) -> None:
        self.write_bytes(job_id, name, text.encode("utf-8"))

    def write_json(self, job_id: str, name: str, obj: Any) -> None:
        self.write_text(job_id, name, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")

    def read_bytes(self, job_id: str, name: str) -> bytes:
        return (self.job_dir(job_id) / name).read_bytes()

    def create(self, job_id: str, meta: dict[str, Any], input_name: str, data: bytes) -> None:
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        self.write_bytes(job_id, input_name, data)
        self.write_json(job_id, JOB_FILE, meta)

    def load(self, job_id: str) -> dict[str, Any] | None:
        try:
            path = self.job_dir(job_id) / JOB_FILE
        except JobNotFound:
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return None

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            meta = self.load(job_id)
            if meta is None:
                raise JobNotFound(job_id)
            meta.update(changes)
            self.write_json(job_id, JOB_FILE, meta)
            return meta

    def fail_if_pending(self, job_id: str, code: str, message: str, **details: Any) -> None:
        """Mark a job failed unless it already reached a terminal state. Never raises."""
        try:
            with self._lock:
                meta = self.load(job_id)
                if meta is None or meta.get("status") in TERMINAL_STATUSES:
                    return
                meta.update(
                    status="failed",
                    finished_at=utcnow(),
                    error={"code": code, "message": message, **details},
                    files=self.list_files(job_id),
                )
                self.write_json(job_id, JOB_FILE, meta)
        except Exception:  # pragma: no cover - defensive
            log.exception("Could not mark job %s as failed", job_id)

    def list_files(self, job_id: str) -> list[str]:
        job_dir = self.job_dir(job_id)
        return sorted(
            p.relative_to(job_dir).as_posix() for p in job_dir.rglob("*") if p.is_file() and not p.name.startswith(".")
        )

    def job_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and self.is_valid_id(p.name))

    def recover_interrupted(self) -> list[str]:
        """Fail jobs left queued/running by a previous process."""
        recovered = []
        for job_id in self.job_ids():
            meta = self.load(job_id)
            if meta and meta.get("status") not in TERMINAL_STATUSES:
                self.fail_if_pending(job_id, "interrupted", "The server restarted before this job completed.")
                recovered.append(job_id)
        return recovered

    def build_archive(self, job_id: str) -> Path:
        """Return a deflate-compressed zip of the job folder (entries prefixed with ``<job_id>/``)."""
        src = self.job_dir(job_id)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        dest = self.archive_dir / f"{job_id}.zip"
        newest = max(p.stat().st_mtime_ns for p in src.rglob("*") if p.is_file())
        if dest.exists() and dest.stat().st_mtime_ns >= newest:
            return dest
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(src.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    zf.write(path, f"{job_id}/{path.relative_to(src).as_posix()}")
        os.replace(tmp, dest)
        return dest


class BufferFull(Exception):
    pass


@dataclass
class _Pending:
    job_id: str
    enqueued_at: float


def _redact_payload(payload: dict[str, Any], input_name: str) -> dict[str, Any]:
    redacted = copy.deepcopy(payload)
    for message in redacted.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    part["image_url"]["url"] = f"<base64 data of {input_name} omitted>"
    return redacted


def _encode_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class JobRunner:
    """Accepts jobs, groups them into batches and dispatches them to the OCR backend.

    * Jobs are buffered (at most ``max_buffer`` waiting jobs; :meth:`submit` raises :class:`BufferFull` beyond that).
    * A batch is dispatched once it holds ``batch_max_size`` jobs or its oldest job has waited
      ``batch_window`` seconds, whichever comes first.
    * At most ``max_concurrency`` backend calls are in flight; a job leaves the buffer when it obtains a slot.
    """

    def __init__(
        self,
        store: JobStore,
        handler: OCRHandler,
        client: httpx.AsyncClient,
        *,
        model: str,
        max_concurrency: int = 30,
        max_buffer: int = 60,
        batch_max_size: int = 30,
        batch_window: float = 10.0,
    ) -> None:
        self.store = store
        self.handler = handler
        self.client = client
        self.model = model
        self.max_concurrency = max_concurrency
        self.max_buffer = max_buffer
        self.batch_max_size = batch_max_size
        self.batch_window = batch_window

        self._waiting: deque[_Pending] = deque()
        self._wakeup = asyncio.Event()
        self._slots = asyncio.Semaphore(max_concurrency)
        self._buffered = 0
        self._active = 0
        self._peak_active = 0
        self._batches_dispatched = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending_ids: set[str] = set()
        self._dispatcher: asyncio.Task[None] | None = None

    # ----------------------------------------------------------------- state
    @property
    def buffered(self) -> int:
        return self._buffered

    @property
    def active(self) -> int:
        return self._active

    @property
    def is_full(self) -> bool:
        return self._buffered >= self.max_buffer

    def stats(self) -> dict[str, Any]:
        return {
            "buffered": self._buffered,
            "active": self._active,
            "peak_active": self._peak_active,
            "batches_dispatched": self._batches_dispatched,
            "max_buffer": self.max_buffer,
            "max_concurrency": self.max_concurrency,
            "batch_max_size": self.batch_max_size,
            "batch_window_seconds": self.batch_window,
        }

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="ocr-batch-dispatcher")

    async def shutdown(self) -> None:
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
            self._dispatcher = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job_id in list(self._pending_ids):
            self.store.fail_if_pending(job_id, "server_shutdown", "The server shut down before this job completed.")
        self._waiting.clear()
        self._pending_ids.clear()

    # ------------------------------------------------------------- intake
    def reserve(self) -> None:
        """Reserve a buffer slot; raises :class:`BufferFull` if the buffer is at capacity."""
        if self.is_full:
            raise BufferFull()
        self._buffered += 1

    def release_reservation(self) -> None:
        self._buffered -= 1

    def enqueue(self, job_id: str) -> None:
        """Queue a job whose buffer slot was obtained with :meth:`reserve`."""
        self._pending_ids.add(job_id)
        self._waiting.append(_Pending(job_id, asyncio.get_running_loop().time()))
        self._wakeup.set()

    def submit(self, job_id: str) -> None:
        """Reserve a buffer slot and queue the job. Must be called from the event loop."""
        self.reserve()
        self.enqueue(job_id)

    async def _dispatch_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            while not self._waiting:
                self._wakeup.clear()
                await self._wakeup.wait()
            deadline = self._waiting[0].enqueued_at + self.batch_window
            while len(self._waiting) < self.batch_max_size:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), remaining)
                except TimeoutError:
                    break
            size = min(len(self._waiting), self.batch_max_size)
            batch = [self._waiting.popleft() for _ in range(size)]
            await self._dispatch_batch(batch)

    async def _dispatch_batch(self, batch: list[_Pending]) -> None:
        loop = asyncio.get_running_loop()
        batch_id = uuid.uuid4().hex[:12]
        self._batches_dispatched += 1
        dispatched_at = utcnow()
        log.info("Dispatching batch %s with %d job(s)", batch_id, len(batch))
        for item in batch:
            info = {
                "batch_id": batch_id,
                "size": len(batch),
                "dispatched_at": dispatched_at,
                "waited_seconds": round(loop.time() - item.enqueued_at, 3),
            }
            await asyncio.to_thread(self._safe_update, item.job_id, batch=info)
        for item in batch:
            await self._slots.acquire()
            self._buffered -= 1
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            task = asyncio.create_task(self._run(item.job_id), name=f"ocr-{item.job_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _safe_update(self, job_id: str, **changes: Any) -> None:
        try:
            self.store.update(job_id, **changes)
        except Exception:  # pragma: no cover - defensive
            log.exception("Could not update job %s", job_id)

    # ------------------------------------------------------------- execution
    async def _run(self, job_id: str) -> None:
        try:
            await self._process(job_id)
        except asyncio.CancelledError:
            self.store.fail_if_pending(job_id, "server_shutdown", "The server shut down before this job completed.")
            raise
        except Exception as exc:
            log.exception("Job %s failed unexpectedly", job_id)
            self.store.fail_if_pending(job_id, "internal_error", f"{type(exc).__name__}: {exc}")
        finally:
            self._active -= 1
            self._slots.release()
            self._pending_ids.discard(job_id)

    def _fail(self, job_id: str, code: str, message: str, **details: Any) -> None:
        self.store.fail_if_pending(job_id, code, message, **details)

    async def _process(self, job_id: str) -> None:
        meta = await asyncio.to_thread(self.store.update, job_id, status="running", started_at=utcnow())
        inp = meta["input"]
        data = await asyncio.to_thread(self.store.read_bytes, job_id, inp["filename"])
        image_url = await asyncio.to_thread(_data_url, data, inp["mime"])
        payload = self.handler.build_payload(model=self.model, image_url=image_url, prompt=meta.get("prompt"))
        redacted = _redact_payload(payload, inp["filename"])
        await asyncio.to_thread(self.store.write_json, job_id, "request.json", redacted)
        body_bytes = await asyncio.to_thread(_encode_json, payload)
        del image_url, payload

        try:
            resp = await self.client.post(
                "chat/completions", content=body_bytes, headers={"Content-Type": "application/json"}
            )
        except httpx.HTTPError as exc:
            self._fail(job_id, "backend_unreachable", f"Could not reach the OCR backend: {type(exc).__name__}: {exc}")
            return

        try:
            body: Any = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        await asyncio.to_thread(self.store.write_json, job_id, "response.json", body)

        if resp.status_code != 200:
            message = None
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                message = body["error"].get("message")
            self._fail(
                job_id,
                "backend_error",
                f"OCR backend returned HTTP {resp.status_code}: {message or resp.reason_phrase}",
                backend_status=resp.status_code,
            )
            return

        try:
            result = self.handler.parse_response(body)
        except OCRResponseError as exc:
            self._fail(job_id, "invalid_backend_response", str(exc))
            return

        width, height = inp["width"], inp["height"]
        scale = self.handler.bbox_scale
        regions = [r.to_dict(width, height, scale) for r in result.regions]

        def write_results() -> None:
            self.store.write_text(job_id, "result.md", result.markdown + "\n")
            self.store.write_text(job_id, "result_text.md", result.text_markdown + "\n")
            self.store.write_json(job_id, "regions.json", regions)

        await asyncio.to_thread(write_results)
        files = await asyncio.to_thread(self.store.list_files, job_id)
        await asyncio.to_thread(
            self.store.update,
            job_id,
            status="succeeded",
            finished_at=utcnow(),
            result={
                "finish_reason": result.finish_reason,
                "truncated": result.truncated,
                "usage": result.usage,
                "region_count": len(regions),
                "warnings": result.warnings,
            },
            error=None,
            files=files,
        )
