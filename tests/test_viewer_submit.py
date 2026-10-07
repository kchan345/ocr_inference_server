from __future__ import annotations

import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ocr_viewer.app import create_app as create_viewer
from ocr_viewer.render import box_to_pixels
from ocr_viewer.submit import SubmissionError, prepare_image, safe_extract
from tests.conftest import make_png

REGION = (94, 592, 715, 935)  # the region reference in the mock OCR output


def expected_region_size(width: int, height: int) -> tuple[int, int]:
    x1, y1, x2, y2 = box_to_pixels(REGION, width, height, 1000)
    return x2 - x1, y2 - y1


def post(client: TestClient, data: bytes, filename: str = "sample.png", **form):
    return client.post("/api/submissions", files={"image": (filename, data, "image/png")}, data=form)


def wait_done(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/api/submissions/{job_id}").json()
        if record["state"] in ("imported", "error"):
            return record
        time.sleep(0.05)
    raise AssertionError(f"submission {job_id} not imported in time: {record}")


@pytest.fixture
def make_viewer(server, tmp_path):
    clients: list[TestClient] = []

    def factory(**overrides) -> TestClient:
        options = {
            "inbox": tmp_path / "inbox",
            "server_url": "http://testserver",
            "client_factory": lambda url: server,
            "poll_interval": 0.05,
        }
        options.update(overrides)
        client = TestClient(create_viewer([], **options))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def viewer(make_viewer) -> TestClient:
    return make_viewer()


# ----------------------------------------------------------------- image preparation
def test_prepare_image_without_box_keeps_bytes(sample_png):
    prepared = prepare_image(sample_png, "sample.png", None)
    assert prepared.data == sample_png
    assert (prepared.crop, prepared.mime, prepared.filename) == (None, "image/png", "sample.png")


def test_prepare_image_crops_and_clamps(sample_png):
    prepared = prepare_image(sample_png, "page.png", (100, 100, 600, 700))
    assert prepared.crop == (100, 100, 600, 700)
    assert prepared.filename == "page_crop_100_100_600_700.png"
    assert Image.open(io.BytesIO(prepared.data)).size == (500, 600)
    assert prepare_image(sample_png, "p.png", (-50, 5000, 200, -9)).crop == (0, 0, 200, 1100)
    assert prepare_image(sample_png, "p.png", (0, 0, 1000, 1100)).crop is None  # full image = no crop


@pytest.mark.parametrize(("data", "box", "code"), [
    (b"nope", None, "invalid_image"),
    (b"", None, "empty_image"),
    (None, (10, 10, 10, 50), "invalid_box"),
    (None, (2000, 2000, 3000, 3000), "invalid_box"),
])
def test_prepare_image_errors(sample_png, data, box, code):
    with pytest.raises(SubmissionError) as info:
        prepare_image(sample_png if data is None else data, "x.png", box)
    assert info.value.code == code and info.value.status_code == 400


def test_safe_extract_rejects_foreign_paths(tmp_path):
    job_id = "a" * 32
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{job_id}/job.json", "{}")
        zf.writestr(f"{job_id}/../evil.txt", "x")
    with pytest.raises(ValueError):
        safe_extract(buf.getvalue(), tmp_path, job_id)
    assert not (tmp_path / "evil.txt").exists()


# ----------------------------------------------------------------- viewer -> server round trips
def test_config_and_server_info(viewer):
    config = viewer.get("/api/config").json()
    assert config["submissions_enabled"] is True
    assert config["server_url"] == "http://testserver"
    info = viewer.get("/api/server/info").json()
    assert info["server_url"] == "http://testserver"
    assert info["info"]["handler"]["name"] == "ovisocr2"
    bad = viewer.get("/api/server/info", params={"url": "ftp://example"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_server_url"


def test_box_with_original_persisted(viewer, sample_png, tmp_path):
    resp = post(viewer, sample_png, box="100,100,600,700", persist="original")
    assert resp.status_code == 202, resp.text
    record = resp.json()
    assert record["state"] == "submitted"
    assert record["crop"] == {"box": [100, 100, 600, 700], "source_width": 1000, "source_height": 1100}

    record = wait_done(viewer, record["job_id"])
    assert (record["state"], record["job_status"]) == ("imported", "succeeded")
    folder = tmp_path / "inbox" / record["job_id"]
    assert record["key"] and record["folder"] == str(folder.resolve())
    assert (folder / "original.png").read_bytes() == sample_png
    assert not (folder / "input.png").exists()
    view = json.loads((folder / "viewer.json").read_text("utf-8"))
    assert (view["persisted_image"], view["image"], view["region_frame"]) == ("original", "original.png",
                                                                             [100, 100, 600, 700])
    assert not list((tmp_path / "inbox" / ".submissions").glob("*.source.*"))

    jobs = viewer.get("/api/jobs").json()
    assert jobs["total"] == 1
    detail = viewer.get(f"/api/jobs/{record['key']}").json()
    assert (detail["width"], detail["height"], detail["persisted_image"]) == (1000, 1100, "original")
    assert detail["region_frame"] == [100, 100, 600, 700]
    assert (detail["meta"]["input"]["width"], detail["meta"]["input"]["height"]) == (500, 600)
    assert detail["original_filename"] == "sample.png"
    assert viewer.get(detail["image_url"]).content == sample_png

    crop = viewer.get(f"/api/jobs/{record['key']}/images/bbox_94_592_715_935.jpg")
    assert Image.open(io.BytesIO(crop.content)).size == expected_region_size(500, 600)


def test_box_with_cropped_persisted(viewer, sample_png, tmp_path):
    record = post(viewer, sample_png, box="100,100,600,700", persist="cropped").json()
    record = wait_done(viewer, record["job_id"])
    assert record["state"] == "imported"
    folder = tmp_path / "inbox" / record["job_id"]
    assert not (folder / "original.png").exists()
    detail = viewer.get(f"/api/jobs/{record['key']}").json()
    assert (detail["width"], detail["height"], detail["persisted_image"]) == (500, 600, "cropped")
    assert detail["region_frame"] is None
    assert detail["crop"]["box"] == [100, 100, 600, 700]
    image = Image.open(io.BytesIO(viewer.get(detail["image_url"]).content))
    assert image.size == (500, 600)
    crop = viewer.get(f"/api/jobs/{record['key']}/images/bbox_94_592_715_935.jpg")
    assert Image.open(io.BytesIO(crop.content)).size == expected_region_size(500, 600)


def test_whole_image_and_failed_job(viewer, sample_png):
    whole = post(viewer, sample_png, persist="cropped").json()
    failed = post(viewer, sample_png, prompt="MOCK_FAIL").json()
    whole, failed = wait_done(viewer, whole["job_id"]), wait_done(viewer, failed["job_id"])
    assert (whole["crop"], whole["persist"], whole["job_status"]) == (None, "original", "succeeded")
    assert (failed["state"], failed["job_status"]) == ("imported", "failed")
    detail = viewer.get(f"/api/jobs/{whole['key']}").json()
    assert viewer.get(detail["image_url"]).content == sample_png
    assert detail["region_frame"] is None and detail["crop"] is None
    listed = viewer.get("/api/submissions").json()["items"]
    assert {r["job_id"] for r in listed} == {whole["job_id"], failed["job_id"]}


def test_server_errors_are_passed_through(viewer):
    big = make_png(3000, 3000)
    resp = post(viewer, big, filename="big.png")
    assert resp.status_code == 413
    error = resp.json()["error"]
    assert (error["code"], error["source"]) == ("image_too_large", "inference_server")
    accepted = post(viewer, big, filename="big.png", box="0,0,2000,2000")
    assert accepted.status_code == 202, accepted.text
    assert wait_done(viewer, accepted.json()["job_id"])["job_status"] == "succeeded"

    assert post(viewer, make_png(10, 10), box="1,2,3").json()["error"]["code"] == "invalid_box"
    assert post(viewer, make_png(10, 10), persist="both").json()["error"]["code"] == "invalid_persist"
    assert post(viewer, b"garbage").json()["error"]["code"] == "invalid_image"


def test_custom_server_url_unreachable(make_viewer, sample_png, tmp_path):
    viewer = make_viewer(inbox=tmp_path / "other", client_factory=None, server_url=None)
    missing = post(viewer, sample_png)
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "server_url_required"
    resp = post(viewer, sample_png, server_url="http://127.0.0.1:9")
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "server_unreachable"
    assert viewer.get("/api/server/info", params={"url": "http://127.0.0.1:9"}).status_code == 502


def test_pending_submissions_resume_after_restart(make_viewer, sample_png, tmp_path):
    first = make_viewer()
    job_id = post(first, sample_png, box="0,0,500,500", persist="original").json()["job_id"]
    first.__exit__(None, None, None)
    second = make_viewer()
    record = wait_done(second, job_id)
    assert record["state"] == "imported"
    assert (tmp_path / "inbox" / job_id / "original.png").read_bytes() == sample_png


def test_submissions_disabled_without_inbox(tmp_path, sample_png):
    with TestClient(create_viewer([tmp_path])) as client:
        assert client.get("/api/config").json()["submissions_enabled"] is False
        resp = post(client, sample_png)
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "submissions_disabled"
