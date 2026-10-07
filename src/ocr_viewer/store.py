"""Discovery and access of job artifact folders on disk."""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JOB_FILE = "job.json"
RESULT_FILE = "result.md"
TEXT_RESULT_FILE = "result_text.md"
EDITED_FILE = "result.edited.md"
DEFAULT_BBOX_SCALE = 1000
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_key(value: str) -> str:
    key = _KEY_UNSAFE.sub("_", value).strip("._") or "job"
    return key[:100]


def _snippet(job_dir: Path, limit: int = 200) -> str:
    for name in (TEXT_RESULT_FILE, RESULT_FILE):
        path = job_dir / name
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(limit * 4)
            except OSError:
                return ""
            text = " ".join(text.split())
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def find_job_dirs(root: Path, max_depth: int) -> list[Path]:
    """Return folders containing ``job.json`` at most ``max_depth`` levels below ``root``."""
    if (root / JOB_FILE).is_file():
        return [root]
    if max_depth <= 0:
        return []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    found: list[Path] = []
    for child in children:
        found.extend(find_job_dirs(child, max_depth - 1))
    return found


@dataclass
class JobEntry:
    key: str
    path: Path
    meta: dict[str, Any]
    snippet: str

    @property
    def job_id(self) -> str:
        return str(self.meta.get("job_id") or self.path.name)

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "unknown")

    @property
    def created_at(self) -> str:
        return str(self.meta.get("created_at") or "")

    @property
    def input_meta(self) -> dict[str, Any]:
        value = self.meta.get("input")
        return value if isinstance(value, dict) else {}

    @property
    def bbox_scale(self) -> int:
        handler = self.meta.get("handler")
        scale = handler.get("bbox_scale") if isinstance(handler, dict) else None
        return int(scale) if isinstance(scale, int) and scale > 0 else DEFAULT_BBOX_SCALE

    def input_path(self) -> Path | None:
        name = self.input_meta.get("filename")
        if isinstance(name, str) and name and Path(name).name == name:
            candidate = self.path / name
            if candidate.is_file():
                return candidate
        for candidate in sorted(self.path.glob("input.*")):
            if candidate.is_file():
                return candidate
        return None

    @property
    def edited_path(self) -> Path:
        return self.path / EDITED_FILE

    def has_edits(self) -> bool:
        return self.edited_path.is_file()

    def original_markdown(self) -> str:
        path = self.path / RESULT_FILE
        return path.read_text("utf-8") if path.is_file() else ""

    def markdown(self) -> str:
        if self.has_edits():
            return self.edited_path.read_text("utf-8")
        return self.original_markdown()

    def summary(self) -> dict[str, Any]:
        result = self.meta.get("result") if isinstance(self.meta.get("result"), dict) else {}
        error = self.meta.get("error") if isinstance(self.meta.get("error"), dict) else None
        inp = self.input_meta
        return {
            "key": self.key,
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.meta.get("created_at"),
            "finished_at": self.meta.get("finished_at"),
            "original_filename": inp.get("original_filename"),
            "width": inp.get("width"),
            "height": inp.get("height"),
            "region_count": result.get("region_count"),
            "finish_reason": result.get("finish_reason"),
            "truncated": result.get("truncated"),
            "has_edits": self.has_edits(),
            "error": error.get("message") if error else None,
            "snippet": self.snippet,
            "folder": str(self.path),
        }


class ArtifactLibrary:
    """Indexes job folders found below a list of artifact roots."""

    def __init__(self, roots: list[Path], max_depth: int = 3) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        self.max_depth = max_depth
        self._cache: dict[Path, tuple[int, dict[str, Any], str]] = {}
        self._by_key: dict[str, JobEntry] = {}
        self._lock = threading.Lock()

    def _load(self, job_dir: Path) -> tuple[dict[str, Any], str] | None:
        job_file = job_dir / JOB_FILE
        try:
            mtime = job_file.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._cache.get(job_dir)
        if cached and cached[0] == mtime:
            return cached[1], cached[2]
        try:
            meta = json.loads(job_file.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        snippet = _snippet(job_dir)
        self._cache[job_dir] = (mtime, meta, snippet)
        return meta, snippet

    def scan(self) -> list[JobEntry]:
        with self._lock:
            seen: set[Path] = set()
            dirs: list[Path] = []
            for root in self.roots:
                for job_dir in find_job_dirs(root, self.max_depth):
                    resolved = job_dir.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        dirs.append(resolved)
            entries: list[JobEntry] = []
            used: set[str] = set()
            for job_dir in dirs:
                loaded = self._load(job_dir)
                if loaded is None:
                    continue
                meta, snippet = loaded
                base = _safe_key(str(meta.get("job_id") or job_dir.name))
                key, n = base, 2
                while key in used:
                    key, n = f"{base}-{n}", n + 1
                used.add(key)
                entries.append(JobEntry(key, job_dir, meta, snippet))
            entries.sort(key=lambda e: (e.created_at, e.key), reverse=True)
            self._by_key = {e.key: e for e in entries}
            return entries

    def get(self, key: str) -> JobEntry | None:
        entry = self._by_key.get(key)
        if entry is None or not (entry.path / JOB_FILE).is_file():
            self.scan()
            entry = self._by_key.get(key)
        return entry

    def query(self, page: int = 1, page_size: int = 20, status: str | None = None, q: str | None = None) -> dict:
        entries = self.scan()
        if status:
            entries = [e for e in entries if e.status == status]
        if q:
            needle = q.lower()
            entries = [
                e for e in entries
                if needle in e.job_id.lower()
                or needle in str(e.input_meta.get("original_filename") or "").lower()
                or needle in e.snippet.lower()
            ]
        total = len(entries)
        pages = max(1, math.ceil(total / page_size))
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        return {
            "items": [e.summary() for e in entries[start:start + page_size]],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": pages,
        }
