"""FastAPI app for the local artifact viewer."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .render import crop_region, parse_bbox_name, render_markdown
from .store import ArtifactLibrary, JobEntry

STATIC_DIR = Path(__file__).with_name("static")


class RenderRequest(BaseModel):
    markdown: str
    key: str | None = None


class MarkdownUpdate(BaseModel):
    markdown: str


def image_base(key: str) -> str:
    return f"/api/jobs/{quote(key, safe='')}/images"


def create_app(roots: list[Path], *, max_depth: int = 3) -> FastAPI:
    library = ArtifactLibrary(roots, max_depth=max_depth)
    app = FastAPI(title="OCR Artifact Viewer", version=__version__)
    app.state.library = library

    def entry_or_404(key: str) -> JobEntry:
        entry = library.get(key)
        if entry is None:
            raise HTTPException(404, f"Job {key!r} not found")
        return entry

    @app.get("/api/roots")
    def roots_info() -> dict[str, Any]:
        return {"roots": [str(r) for r in library.roots]}

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

    @app.get("/api/jobs/{key}/image")
    def job_image(key: str) -> FileResponse:
        entry = entry_or_404(key)
        path = entry.input_path()
        if path is None:
            raise HTTPException(404, "Input image not found in artifact folder")
        media_type = entry.input_meta.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
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
        data = crop_region(path, box, entry.bbox_scale, fmt)
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
