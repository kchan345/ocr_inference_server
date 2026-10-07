"""FastAPI application for the OCR inference server."""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings
from .handlers import get_handler
from .imaging import SUPPORTED_FORMATS, ImageRejected, inspect_image
from .jobs import JOB_FILE, SCHEMA_VERSION, TERMINAL_STATUSES, BufferFull, JobRunner, JobStore, utcnow

log = logging.getLogger("ocr_server")


class APIError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, *, headers: dict[str, str] | None = None, **details: Any
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.details = details


def error_body(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **details}}


def job_links(job_id: str) -> dict[str, str]:
    return {"self": f"/v1/jobs/{job_id}", "artifacts": f"/v1/jobs/{job_id}/artifacts"}


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    buf = bytearray()
    while chunk := await upload.read(1024 * 1024):
        buf += chunk
        if len(buf) > limit:
            raise APIError(413, "file_too_large", f"Upload exceeds the limit of {limit} bytes.", max_bytes=limit)
    return bytes(buf)


def create_app(settings: Settings | None = None, *, backend_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    handler_options: dict[str, Any] = {}
    if settings.max_tokens:
        handler_options["max_tokens"] = settings.max_tokens
    handler = get_handler(settings.handler, **handler_options)
    model = settings.backend_model or handler.default_model
    if not model:
        raise ValueError(f"OCR_BACKEND_MODEL is required for handler {handler.name!r}")
    max_pixels = settings.max_pixels or handler.max_pixels
    if handler.max_pixels and max_pixels and max_pixels > handler.max_pixels:
        raise ValueError(
            f"max_pixels={max_pixels} exceeds what {handler.name} processes without downscaling ({handler.max_pixels})"
        )
    store = JobStore(settings.artifact_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recovered = await asyncio.to_thread(store.recover_interrupted)
        if recovered:
            log.warning("Marked %d interrupted job(s) as failed", len(recovered))
        headers = {"Authorization": f"Bearer {settings.backend_api_key}"} if settings.backend_api_key else {}
        limits = httpx.Limits(max_connections=settings.max_concurrency, max_keepalive_connections=settings.max_concurrency)
        async with httpx.AsyncClient(
            base_url=settings.backend_url.rstrip("/") + "/",
            timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
            headers=headers,
            limits=limits,
            transport=backend_transport,
        ) as client:
            runner = JobRunner(
                store,
                handler,
                client,
                model=model,
                max_concurrency=settings.max_concurrency,
                max_buffer=settings.max_buffer,
                batch_max_size=settings.batch_max_size,
                batch_window=settings.batch_window_seconds,
            )
            runner.start()
            app.state.runner = runner
            try:
                yield
            finally:
                await runner.shutdown()

    app = FastAPI(title="OCR Inference Server", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.handler = handler

    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message, **exc.details), exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            error_body("invalid_request", "Request validation failed.", details=jsonable_encoder(exc.errors())), 422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(error_body("http_error", str(exc.detail)), exc.status_code, headers=exc.headers)

    def runner_of(request: Request) -> JobRunner:
        return request.app.state.runner

    def load_job(job_id: str) -> dict[str, Any]:
        meta = store.load(job_id)
        if meta is None:
            raise APIError(404, "job_not_found", f"Job {job_id!r} does not exist.")
        return meta

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/info")
    async def info(request: Request) -> dict[str, Any]:
        return {
            "version": __version__,
            "handler": handler.describe(),
            "backend": {"url": settings.backend_url, "model": model},
            "limits": {
                "max_pixels": max_pixels,
                "max_upload_bytes": settings.max_upload_bytes,
                "max_prompt_chars": settings.max_prompt_chars,
                "supported_formats": sorted(SUPPORTED_FORMATS),
            },
            "queue": runner_of(request).stats(),
            "default_prompt": handler.default_prompt,
        }

    @app.post("/v1/ocr", status_code=202)
    async def submit_ocr(
        request: Request,
        image: UploadFile = File(..., description="Page image (PNG, JPEG, WEBP, BMP, TIFF, GIF)."),
        prompt: str | None = Form(None, description="Optional prompt overriding the handler default."),
    ) -> JSONResponse:
        runner = runner_of(request)
        if runner.is_full:
            raise_buffer_full(runner)
        if prompt is not None and len(prompt) > settings.max_prompt_chars:
            raise APIError(
                400, "prompt_too_long", f"Prompt exceeds {settings.max_prompt_chars} characters.",
                max_prompt_chars=settings.max_prompt_chars,
            )
        data = await _read_limited(image, settings.max_upload_bytes)
        try:
            img = await asyncio.to_thread(inspect_image, data, max_pixels=max_pixels)
        except ImageRejected as exc:
            raise APIError(exc.status_code, exc.code, exc.message, **exc.details) from exc

        try:  # the buffer may have filled while the upload was being read
            runner.reserve()
        except BufferFull:
            raise_buffer_full(runner)

        job_id = store.new_job_id()
        input_name = f"input.{img.extension}"
        created_at = utcnow()
        custom_prompt = prompt if prompt and prompt.strip() else None
        meta: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "status": "queued",
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "handler": handler.describe(),
            "backend": {"url": settings.backend_url, "model": model},
            "prompt": custom_prompt,
            "prompt_used": handler.resolve_prompt(custom_prompt),
            "input": {
                "filename": input_name,
                "original_filename": image.filename,
                "content_type": image.content_type,
                "format": img.format,
                "mime": img.mime,
                "width": img.width,
                "height": img.height,
                "bytes": img.size_bytes,
                "sha256": img.sha256,
            },
            "batch": None,
            "result": None,
            "error": None,
            "files": [JOB_FILE, input_name],
        }
        try:
            await asyncio.to_thread(store.create, job_id, meta, input_name, data)
        except BaseException:
            runner.release_reservation()
            raise
        runner.enqueue(job_id)
        return JSONResponse(
            {"job_id": job_id, "status": "queued", "created_at": created_at, "links": job_links(job_id)},
            status_code=202,
            headers={"Location": f"/v1/jobs/{job_id}"},
        )

    def raise_buffer_full(runner: JobRunner) -> None:
        retry_after = str(max(1, math.ceil(settings.batch_window_seconds)))
        raise APIError(
            503,
            "buffer_full",
            f"The server already holds {runner.buffered} job(s) waiting for processing (limit {runner.max_buffer}). "
            "Retry later.",
            headers={"Retry-After": retry_after},
            buffered=runner.buffered,
            max_buffer=runner.max_buffer,
        )

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        meta = await asyncio.to_thread(load_job, job_id)
        return {**meta, "links": job_links(job_id)}

    @app.get("/v1/jobs/{job_id}/artifacts")
    async def get_artifacts(job_id: str) -> FileResponse:
        meta = await asyncio.to_thread(load_job, job_id)
        if meta.get("status") not in TERMINAL_STATUSES:
            raise APIError(
                409, "job_not_finished", f"Job {job_id} is {meta.get('status')}; artifacts are available once it "
                "has succeeded or failed.", status=meta.get("status"),
            )
        path = await asyncio.to_thread(store.build_archive, job_id)
        return FileResponse(path, media_type="application/zip", filename=f"{job_id}.zip")

    return app
