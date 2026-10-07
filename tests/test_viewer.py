from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ocr_viewer.app import create_app as create_viewer
from ocr_viewer.render import box_to_pixels, render_markdown
from tests.conftest import CANNED_CONTENT, SAMPLE_PNG, submit, wait_for_job


def make_job(root: Path, job_id: str, *, created_at: str, status: str = "succeeded",
             markdown: str = CANNED_CONTENT, filename: str = "sample.png") -> Path:
    job_dir = root / job_id
    job_dir.mkdir(parents=True)
    shutil.copy(SAMPLE_PNG, job_dir / "input.png")
    (job_dir / "result.md").write_text(markdown + "\n", encoding="utf-8")
    (job_dir / "result_text.md").write_text(markdown.split("<img")[0], encoding="utf-8")
    meta = {
        "job_id": job_id,
        "status": status,
        "created_at": created_at,
        "handler": {"name": "ovisocr2", "bbox_scale": 1000},
        "input": {"filename": "input.png", "original_filename": filename, "mime": "image/png",
                  "width": 1000, "height": 1100},
        "result": {"region_count": 1, "finish_reason": "stop", "truncated": False, "warnings": []},
        "error": None if status == "succeeded" else {"code": "backend_error", "message": "boom"},
    }
    (job_dir / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    return job_dir


@pytest.fixture
def library(tmp_path) -> Path:
    root = tmp_path / "artifacts"
    for i in range(25):
        status = "failed" if i % 5 == 0 else "succeeded"
        make_job(root, f"{i:032x}", created_at=f"2026-10-07T10:{i:02d}:00.000Z", status=status,
                 filename=f"page{i}.png")
    return root


@pytest.fixture
def viewer(library) -> TestClient:
    with TestClient(create_viewer([library])) as client:
        yield client


def test_index_and_static_assets(viewer):
    assert "OCR Artifact Viewer" in viewer.get("/").text
    assert viewer.get("/static/app.js").status_code == 200
    assert viewer.get("/static/style.css").status_code == 200


def test_paging_sorted_newest_first(viewer):
    page1 = viewer.get("/api/jobs", params={"page": 1, "page_size": 10}).json()
    assert (page1["total"], page1["pages"], page1["page"]) == (25, 3, 1)
    assert len(page1["items"]) == 10
    assert page1["items"][0]["job_id"] == f"{24:032x}"
    page3 = viewer.get("/api/jobs", params={"page": 3, "page_size": 10}).json()
    assert len(page3["items"]) == 5
    assert page3["items"][-1]["job_id"] == f"{0:032x}"
    beyond = viewer.get("/api/jobs", params={"page": 99, "page_size": 10}).json()
    assert beyond["page"] == 3
    keys = {it["key"] for p in (1, 2, 3)
            for it in viewer.get("/api/jobs", params={"page": p, "page_size": 10}).json()["items"]}
    assert len(keys) == 25


def test_filters(viewer):
    failed = viewer.get("/api/jobs", params={"status": "failed", "page_size": 100}).json()
    assert failed["total"] == 5
    assert all(it["status"] == "failed" and it["error"] == "boom" for it in failed["items"])
    found = viewer.get("/api/jobs", params={"q": "page13.png"}).json()
    assert [it["original_filename"] for it in found["items"]] == ["page13.png"]


def test_job_detail_and_original_image(viewer):
    key = f"{3:032x}"
    detail = viewer.get(f"/api/jobs/{key}").json()
    assert detail["markdown"] == CANNED_CONTENT + "\n"
    assert detail["bbox_scale"] == 1000
    assert detail["has_edits"] is False
    image = viewer.get(detail["image_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == SAMPLE_PNG.read_bytes()
    assert viewer.get("/api/jobs/nope").status_code == 404


def test_region_image_generated_on_the_fly(viewer):
    key = f"{3:032x}"
    resp = viewer.get(f"/api/jobs/{key}/images/bbox_94_592_715_935.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    crop = Image.open(io.BytesIO(resp.content))
    assert crop.size == (715 - 94, 1028 - 651)
    png = viewer.get(f"/api/jobs/{key}/images/bbox_0_0_500_500.png")
    assert Image.open(io.BytesIO(png.content)).size == (500, 550)
    assert viewer.get(f"/api/jobs/{key}/images/bbox_10_10_10_20.jpg").status_code == 404
    assert viewer.get(f"/api/jobs/{key}/images/input.png").status_code == 404
    assert viewer.get(f"/api/jobs/{key}/images/bbox_0_0_99999_99999.jpg").status_code == 200


def test_box_to_pixels_matches_model_card():
    assert box_to_pixels((94, 592, 715, 935), 1000, 1100, 1000) == (94, 651, 715, 1028)


def test_render_rewrites_region_links_and_sanitizes(viewer):
    key = f"{3:032x}"
    md = CANNED_CONTENT + "\n\n$$ E=mc^{2} $$\n\n<script>alert(1)</script>\n\n<a href=\"javascript:alert(1)\">x</a>"
    html = viewer.post("/api/render", json={"markdown": md, "key": key}).json()["html"]
    assert "<table" in html and "<td>North</td>" in html
    assert f'src="/api/jobs/{key}/images/bbox_94_592_715_935.jpg"' in html
    assert "<h3>1. Summary</h3>" in html
    assert 'class="math' in html and "E=mc^{2}" in html
    assert "<script" not in html
    assert "javascript:" not in html


def test_render_function_without_key():
    html = render_markdown('<img src="images/bbox_1_2_3_4.jpg" />', "/base")
    assert 'src="/base/bbox_1_2_3_4.jpg"' in html


def test_save_and_revert_edits(viewer, library):
    key = f"{3:032x}"
    resp = viewer.put(f"/api/jobs/{key}/markdown", json={"markdown": "# Edited"})
    assert resp.json()["has_edits"] is True
    assert (library / key / "result.edited.md").read_text("utf-8") == "# Edited"
    assert (library / key / "result.md").read_text("utf-8") == CANNED_CONTENT + "\n"
    detail = viewer.get(f"/api/jobs/{key}").json()
    assert (detail["markdown"], detail["has_edits"]) == ("# Edited", True)
    reverted = viewer.delete(f"/api/jobs/{key}/markdown").json()
    assert reverted["markdown"] == CANNED_CONTENT + "\n"
    assert not (library / key / "result.edited.md").exists()


def test_multiple_roots_nested_and_duplicate_ids(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b" / "nested"
    make_job(a, "f" * 32, created_at="2026-01-01T00:00:00Z")
    make_job(b, "f" * 32, created_at="2026-01-02T00:00:00Z")
    single = make_job(tmp_path / "c", "e" * 32, created_at="2026-01-03T00:00:00Z")
    with TestClient(create_viewer([a, tmp_path / "b", single])) as client:
        data = client.get("/api/jobs").json()
    assert data["total"] == 3
    assert sorted(it["key"] for it in data["items"]) == ["e" * 32, "f" * 32, "f" * 32 + "-2"]


def test_viewer_reads_extracted_server_artifacts(server, tmp_path, sample_png):
    """End-to-end without a browser: server job -> zip download -> extract -> viewer."""
    job_ids = [submit(server, sample_png).json()["job_id"] for _ in range(2)]
    extract_dir = tmp_path / "downloaded"
    for job_id in job_ids:
        assert wait_for_job(server, job_id)["status"] == "succeeded"
        zipfile.ZipFile(io.BytesIO(server.get(f"/v1/jobs/{job_id}/artifacts").content)).extractall(extract_dir)
    with TestClient(create_viewer([extract_dir])) as client:
        data = client.get("/api/jobs").json()
        assert {it["job_id"] for it in data["items"]} == set(job_ids)
        detail = client.get(f"/api/jobs/{job_ids[0]}").json()
        assert detail["region_count"] == 1
        crop = client.get(f"/api/jobs/{job_ids[0]}/images/bbox_94_592_715_935.jpg")
        assert Image.open(io.BytesIO(crop.content)).size == (621, 377)
