"""FastAPI app for the local artifact viewer."""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .render import crop_region, parse_bbox_name, render_markdown
from .store import ArtifactLibrary, JobEntry
from .submit import (
    MAX_UPLOAD_BYTES,
    STATUS_FILTERS,
    SubmissionError,
    SubmissionManager,
    normalize_adjustments,
    parse_box,
    submission_status,
)

STATIC_DIR = Path(__file__).with_name("static")


class RenderRequest(BaseModel):
    markdown: str
    key: str | None = None


class MarkdownUpdate(BaseModel):
    markdown: str


def image_base(key: str) -> str:
    return f"/api/jobs/{quote(key, safe='')}/images"


def create_app(
    roots: list[Path],
    *,
    max_depth: int = 3,
    inbox: Path | None = None,
    server_url: str | None = None,
    client_factory: Callable[[str], httpx.Client] | None = None,
    poll_interval: float = 1.0,
) -> FastAPI:
    """Create the viewer app.

    ``inbox`` enables submitting images to an inference server (``server_url`` is the default target); finished
    jobs are extracted into ``inbox``, which is added to the artifact roots.
    """
    manager: SubmissionManager | None = None
    roots = list(roots)
    if inbox is not None:
        manager = SubmissionManager(inbox, server_url, client_factory=client_factory, poll_interval=poll_interval)
        if manager.inbox not in {Path(r).resolve() for r in roots}:
            roots.append(manager.inbox)
    library = ArtifactLibrary(roots, max_depth=max_depth)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manager is not None:
            await manager.start()
        try:
            yield
        finally:
            if manager is not None:
                await manager.shutdown()

    app = FastAPI(title="OCR Artifact Viewer", version=__version__, lifespan=lifespan)
    app.state.library = library
    app.state.submissions = manager

    @app.exception_handler(SubmissionError)
    async def submission_error(request: Request, exc: SubmissionError) -> JSONResponse:
        headers = {"Retry-After": str(exc.details["retry_after"])} if "retry_after" in exc.details else None
        return JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=headers)

    def require_manager() -> SubmissionManager:
        if manager is None:
            raise SubmissionError(404, "submissions_disabled", "Start the viewer with --inbox to submit images.")
        return manager

    def with_key(record: dict[str, Any]) -> dict[str, Any]:
        key = None
        if record.get("folder"):
            folder = Path(record["folder"])
            key = next((e.key for e in library.scan() if e.path == folder), None)
        return {**record, "key": key, "status": submission_status(record)}

    async def read_upload(upload: UploadFile) -> bytes:
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise SubmissionError(413, "file_too_large", f"Upload exceeds {MAX_UPLOAD_BYTES} bytes.")
        return data

    def entry_or_404(key: str) -> JobEntry:
        entry = library.get(key)
        if entry is None:
            raise HTTPException(404, f"Job {key!r} not found")
        return entry

    @app.get("/api/roots")
    def roots_info() -> dict[str, Any]:
        return {"roots": [str(r) for r in library.roots]}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "submissions_enabled": manager is not None,
            "server_url": manager.default_server_url if manager else None,
            "inbox": str(manager.inbox) if manager else None,
        }

    @app.get("/api/server/info")
    async def server_info(url: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(require_manager().server_info, url)

    # ---------------------------------------------------------------- imported images (sources)
    @app.post("/api/sources", status_code=201)
    async def upload_source(image: UploadFile = File(...)) -> dict[str, Any]:
        mgr = require_manager()
        data = await read_upload(image)
        return await asyncio.to_thread(mgr.add_source, data, image.filename or "image")

    @app.get("/api/sources/{source_id}")
    def source_info(source_id: str) -> dict[str, Any]:
        return require_manager().source(source_id)[0]

    @app.get("/api/sources/{source_id}/preview")
    async def source_preview(
        source_id: str, rotation: str | None = None, grayscale: str | None = None, threshold: str | None = None,
    ) -> Response:
        mgr = require_manager()
        adjustments = normalize_adjustments(rotation, grayscale, threshold)
        data, media_type = await asyncio.to_thread(mgr.preview, source_id, adjustments)
        return Response(data, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.delete("/api/sources/{source_id}")
    def delete_source(source_id: str) -> dict[str, Any]:
        return {"deleted": require_manager().delete_source(source_id)}

    # ---------------------------------------------------------------- submissions
    @app.post("/api/submissions", status_code=202)
    async def create_submission(
        image: UploadFile | None = File(None, description="Image to OCR (alternative to source_id)."),
        source_id: str | None = Form(None, description="An image previously uploaded to /api/sources."),
        server_url: str | None = Form(None),
        prompt: str | None = Form(None),
        box: str | None = Form(None, description="Crop box 'x1,y1,x2,y2' in pixels of the adjusted image."),
        persist: str = Form("cropped", description="'original' or 'cropped': which image the job folder keeps."),
        rotation: str | None = Form(None, description="Degrees clockwise, 0.1° steps."),
        grayscale: str | None = Form(None),
        threshold: str | None = Form(None, description="0-255: black & white threshold (implies grayscale)."),
        resubmit_of: str | None = Form(None),
    ) -> dict[str, Any]:
        mgr = require_manager()
        adjustments = normalize_adjustments(rotation, grayscale, threshold)
        parsed_box = parse_box(box)
        if not source_id:
            if image is None:
                raise SubmissionError(400, "image_required", "Send an image file or a source_id.")
            data = await read_upload(image)
            source_id = (await asyncio.to_thread(mgr.add_source, data, image.filename or "image"))["source_id"]
        record = await mgr.submit(
            source_id, server_url=server_url, prompt=prompt, box=parsed_box, persist=persist,
            adjustments=adjustments, resubmit_of=resubmit_of or None,
        )
        return with_key(record)

    @app.get("/api/submissions")
    def list_submissions(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        status: str | None = Query(None, description="pending, succeeded or failed"),
        q: str | None = None,
    ) -> dict[str, Any]:
        if status and status not in STATUS_FILTERS:
            raise SubmissionError(400, "invalid_status", f"status must be one of {', '.join(STATUS_FILTERS)}.")
        data = require_manager().query(page=page, page_size=page_size, status=status or None, q=q or None)
        return {**data, "items": [with_key(r) for r in data["items"]]}

    @app.get("/api/submissions/{job_id}")
    def get_submission(job_id: str) -> dict[str, Any]:
        return with_key(require_manager().require(job_id))

    @app.post("/api/submissions/{job_id}/draft")
    async def submission_draft(job_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(require_manager().draft, job_id)

    @app.post("/api/submissions/{job_id}/resubmit", status_code=202)
    async def resubmit(
        job_id: str,
        server_url: str | None = Form(None),
        prompt: str | None = Form(None),
        box: str | None = Form(None, description="Omit to reuse the previous box; 'full' for the whole image."),
        persist: str | None = Form(None),
        rotation: str | None = Form(None),
        grayscale: str | None = Form(None),
        threshold: str | None = Form(None),
    ) -> dict[str, Any]:
        mgr = require_manager()
        if submission_status(mgr.require(job_id)) == "pending":
            raise SubmissionError(409, "submission_pending", "Wait until the job has finished before resubmitting.")
        overrides: dict[str, Any] = {"server_url": server_url, "prompt": prompt, "persist": persist}
        if box is not None:
            overrides["box"] = parse_box(box)
        if any(v is not None for v in (rotation, grayscale, threshold)):
            overrides["adjustments"] = normalize_adjustments(rotation, grayscale, threshold)
        return with_key(await mgr.resubmit(job_id, **overrides))

    @app.get("/api/jobs")
    def list_jobs(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        status: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        return library.query(page=page, page_size=page_size, status=status or None, q=q or None)

    @app.get("/api/jobs/{key}")
    def job_detail(key: str) -> dict[str, Any]:
        entry = entry_or_404(key)
        image = entry.input_path()
        return {
            **entry.summary(),
            "meta": entry.meta,
            "markdown": entry.markdown(),
            "original_markdown": entry.original_markdown(),
            "bbox_scale": entry.bbox_scale,
            "image_url": f"/api/jobs/{quote(entry.key, safe='')}/image" if image else None,
            "image_base": image_base(entry.key),
        }

    @app.post("/api/jobs/{key}/draft")
    async def job_draft(key: str) -> dict[str, Any]:
        mgr = require_manager()
        entry = entry_or_404(key)
        filename = entry.summary().get("original_filename")
        return await asyncio.to_thread(mgr.draft_from_folder, entry.path, entry.job_id, filename)

    @app.get("/api/jobs/{key}/image")
    def job_image(key: str) -> FileResponse:
        entry = entry_or_404(key)
        path = entry.input_path()
        if path is None:
            raise HTTPException(404, "Input image not found in artifact folder")
        media_type = mimetypes.guess_type(path.name)[0] if path.name != entry.input_meta.get("filename") else None
        media_type = media_type or entry.input_meta.get("mime") or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    @app.get("/api/jobs/{key}/images/{name}")
    def region_image(key: str, name: str) -> Response:
        entry = entry_or_404(key)
        parsed = parse_bbox_name(name)
        if parsed is None:
            raise HTTPException(404, "Not a region image name (expected bbox_L_T_R_B.jpg)")
        path = entry.input_path()
        if path is None:
            raise HTTPException(404, "Input image not found in artifact folder")
        box, fmt = parsed
        data = crop_region(path, box, entry.bbox_scale, fmt, entry.region_frame)
        if data is None:
            raise HTTPException(404, "Region is empty")
        media_type = "image/png" if fmt == "PNG" else "image/jpeg"
        return Response(data, media_type=media_type, headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/render")
    def render(req: RenderRequest) -> dict[str, str]:
        base = image_base(req.key) if req.key else "images"
        return {"html": render_markdown(req.markdown, base)}

    @app.put("/api/jobs/{key}/markdown")
    def save_markdown(key: str, update: MarkdownUpdate) -> dict[str, Any]:
        entry = entry_or_404(key)
        entry.edited_path.write_text(update.markdown, encoding="utf-8")
        return {"saved": True, "has_edits": True, "file": entry.edited_path.name}

    @app.delete("/api/jobs/{key}/markdown")
    def revert_markdown(key: str) -> dict[str, Any]:
        entry = entry_or_404(key)
        entry.edited_path.unlink(missing_ok=True)
        return {"saved": True, "has_edits": False, "markdown": entry.original_markdown()}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
