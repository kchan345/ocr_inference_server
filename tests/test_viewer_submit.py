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
from ocr_viewer.submit import SubmissionError, normalize_adjustments, prepare_image, safe_extract
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


# ----------------------------------------------------------------- adjustments
@pytest.mark.parametrize(("args", "expected"), [
    ((None, None, None), None),
    (("0", "false", ""), None),
    (("360", None, None), None),
    (("90.04", None, None), {"rotation": 90.0, "grayscale": False, "threshold": None}),
    (("-0.15", None, None), {"rotation": -0.1, "grayscale": False, "threshold": None}),
    (("270", None, None), {"rotation": -90.0, "grayscale": False, "threshold": None}),
    (("-180", None, None), {"rotation": 180.0, "grayscale": False, "threshold": None}),
    ((None, "true", None), {"rotation": 0.0, "grayscale": True, "threshold": None}),
    ((None, None, "128"), {"rotation": 0.0, "grayscale": True, "threshold": 128}),
])
def test_normalize_adjustments(args, expected):
    assert normalize_adjustments(*args) == expected


@pytest.mark.parametrize("args", [("abc", None, None), ("4000", None, None), ("nan", None, None),
                                  (None, "maybe", None), (None, None, "256"), (None, None, "12.5")])
def test_normalize_adjustments_errors(args):
    with pytest.raises(SubmissionError) as info:
        normalize_adjustments(*args)
    assert (info.value.status_code, info.value.code) == (400, "invalid_adjustment")


def test_prepare_image_adjustments(sample_png):
    rotated = prepare_image(sample_png, "s.png", None, normalize_adjustments(90))
    assert Image.open(io.BytesIO(rotated.data)).size == (1100, 1000)
    assert (rotated.filename, rotated.crop, rotated.mime) == ("s_adjusted.png", None, "image/png")
    assert (rotated.source_width, rotated.source_height) == (1100, 1000)

    tilted = Image.open(io.BytesIO(prepare_image(sample_png, "s.png", None, normalize_adjustments(0.1)).data))
    assert tilted.width > 1000 and tilted.height > 1100

    gray = Image.open(io.BytesIO(prepare_image(sample_png, "s.png", None, normalize_adjustments(None, True)).data))
    assert gray.mode == "L" and gray.size == (1000, 1100)

    bw = prepare_image(sample_png, "s.png", (10, 10, 510, 610), normalize_adjustments(None, None, 128))
    assert bw.filename == "s_adjusted_crop_10_10_510_610.png"
    bw_img = Image.open(io.BytesIO(bw.data))
    assert bw_img.mode == "L" and bw_img.size == (500, 600)
    assert {v for _, v in bw_img.getcolors(256)} <= {0, 255}


# ----------------------------------------------------------------- sources, drafts, resubmission
def upload(client: TestClient, data: bytes, filename: str = "sample.png") -> dict:
    resp = client.post("/api/sources", files={"image": (filename, data, "image/png")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def submit_source(client: TestClient, source_id: str, **form):
    return client.post("/api/submissions", data={"source_id": source_id, **form})


def test_sources_upload_preview_delete(viewer, sample_png):
    meta = upload(viewer, sample_png)
    assert (meta["width"], meta["height"], meta["filename"], meta["ext"]) == (1000, 1100, "sample.png", "png")
    sid = meta["source_id"]
    assert viewer.get(f"/api/sources/{sid}").json() == meta
    raw = viewer.get(f"/api/sources/{sid}/preview")
    assert raw.content == sample_png and raw.headers["content-type"] == "image/png"
    rotated = viewer.get(f"/api/sources/{sid}/preview", params={"rotation": "90"})
    assert Image.open(io.BytesIO(rotated.content)).size == (1100, 1000)
    bw = Image.open(io.BytesIO(viewer.get(f"/api/sources/{sid}/preview", params={"threshold": "100"}).content))
    assert bw.mode == "L" and {v for _, v in bw.getcolors(256)} <= {0, 255}
    bad = viewer.get(f"/api/sources/{sid}/preview", params={"rotation": "x"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_adjustment"
    assert viewer.post("/api/sources", files={"image": ("x.png", b"junk", "image/png")}).status_code == 400

    assert viewer.delete(f"/api/sources/{sid}").json() == {"deleted": True}
    gone = viewer.get(f"/api/sources/{sid}")
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "source_not_found"
    assert viewer.post("/api/submissions", data={"server_url": "http://testserver"}).json()["error"]["code"] == (
        "image_required")


def test_submit_source_with_adjustments(viewer, sample_png, tmp_path):
    sid = upload(viewer, sample_png)["source_id"]
    resp = submit_source(viewer, sid, rotation="90", threshold="128", box="100,50,1100,650", persist="original")
    assert resp.status_code == 202, resp.text
    record = resp.json()
    assert record["adjustments"] == {"rotation": 90.0, "grayscale": True, "threshold": 128}
    assert record["crop"] == {"box": [100, 50, 1100, 650], "source_width": 1100, "source_height": 1000}
    assert record["source_id"] == sid and record["status"] == "pending"
    record = wait_done(viewer, record["job_id"])
    assert record["status"] == "succeeded"

    folder = tmp_path / "inbox" / record["job_id"]
    original = Image.open(folder / "original.png")
    assert original.size == (1100, 1000) and original.mode == "L"
    view = json.loads((folder / "viewer.json").read_text("utf-8"))
    assert view["adjustments"] == record["adjustments"] and view["source_id"] == sid
    assert view["region_frame"] == [100, 50, 1100, 650]
    detail = viewer.get(f"/api/jobs/{record['key']}").json()
    assert detail["adjustments"] == record["adjustments"]
    assert (detail["meta"]["input"]["width"], detail["meta"]["input"]["height"]) == (1000, 600)
    # sources referenced by a submission are kept for redraw/resubmit
    assert viewer.delete(f"/api/sources/{sid}").json() == {"deleted": False}


def test_draft_and_resubmit(viewer, sample_png):
    sid = upload(viewer, sample_png)["source_id"]
    first = submit_source(viewer, sid, rotation="0.5", box="0,0,400,400", persist="cropped", prompt="MOCK_FAIL")
    first = wait_done(viewer, first.json()["job_id"])
    assert first["status"] == "failed"

    draft = viewer.post(f"/api/submissions/{first['job_id']}/draft").json()
    assert draft["source"]["source_id"] == sid
    assert (draft["box"], draft["persist"], draft["prompt"]) == ([0, 0, 400, 400], "cropped", "MOCK_FAIL")
    assert draft["adjustments"]["rotation"] == 0.5 and draft["resubmit_of"] == first["job_id"]

    same = viewer.post(f"/api/submissions/{first['job_id']}/resubmit")
    assert same.status_code == 202, same.text
    same = same.json()
    assert same["resubmit_of"] == first["job_id"] and same["crop"]["box"] == [0, 0, 400, 400]
    assert same["adjustments"] == first["adjustments"] and same["prompt"] == "MOCK_FAIL"
    assert wait_done(viewer, same["job_id"])["status"] == "failed"

    changed = viewer.post(f"/api/submissions/{first['job_id']}/resubmit",
                          data={"box": "full", "prompt": "Read it", "rotation": "0"}).json()
    assert (changed["crop"], changed["adjustments"], changed["prompt"]) == (None, None, "Read it")
    assert wait_done(viewer, changed["job_id"])["status"] == "succeeded"

    viewer.app.state.submissions._records[changed["job_id"]]["state"] = "submitted"
    busy = viewer.post(f"/api/submissions/{changed['job_id']}/resubmit")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "submission_pending"
    missing = viewer.post(f"/api/submissions/{'f' * 32}/draft")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "submission_not_found"
    bad = submit_source(viewer, sid, resubmit_of="nope")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_resubmit_of"


def test_draft_falls_back_to_job_image(viewer, sample_png, tmp_path):
    sid = upload(viewer, sample_png)["source_id"]
    record = submit_source(viewer, sid, box="100,100,600,700", persist="original", grayscale="true").json()
    record = wait_done(viewer, record["job_id"])
    for path in (tmp_path / "inbox" / ".sources").glob(f"{sid}.*"):
        path.unlink()
    draft = viewer.post(f"/api/submissions/{record['job_id']}/draft").json()
    assert draft["source"]["source_id"] != sid
    assert (draft["source"]["width"], draft["source"]["height"]) == (1000, 1100)
    assert draft["box"] == [100, 100, 600, 700] and draft["adjustments"] is None

    job_draft = viewer.post(f"/api/jobs/{record['key']}/draft").json()
    assert job_draft["resubmit_of"] == record["job_id"]


def test_job_draft_for_foreign_folder(make_viewer, sample_png, tmp_path):
    library = tmp_path / "library"
    folder = library / ("c" * 32)
    folder.mkdir(parents=True)
    (folder / "input.png").write_bytes(sample_png)
    (folder / "job.json").write_text(json.dumps({
        "job_id": "c" * 32, "status": "succeeded", "input": {"filename": "input.png", "width": 1000, "height": 1100},
    }), "utf-8")
    (folder / "result.md").write_text("hello", "utf-8")
    client = TestClient(create_viewer([library], inbox=tmp_path / "inbox2", server_url="http://testserver"))
    with client:
        key = client.get("/api/jobs").json()["items"][0]["key"]
        draft = client.post(f"/api/jobs/{key}/draft")
        assert draft.status_code == 200, draft.text
        draft = draft.json()
        assert draft["box"] is None and draft["resubmit_of"] == "c" * 32
        assert draft["source"]["width"] == 1000


def test_submissions_paging_and_filters(viewer, sample_png):
    sid = upload(viewer, sample_png)["source_id"]
    ids = [submit_source(viewer, sid, **extra).json()["job_id"]
           for extra in ({}, {"prompt": "MOCK_FAIL"}, {"box": "0,0,300,300"})]
    for job_id in ids:
        wait_done(viewer, job_id)
    page = viewer.get("/api/submissions", params={"page_size": 2}).json()
    assert (page["total"], page["pages"], page["page"], len(page["items"])) == (3, 2, 1, 2)
    last = viewer.get("/api/submissions", params={"page_size": 2, "page": 9}).json()
    assert last["page"] == 2 and len(last["items"]) == 1
    failed = viewer.get("/api/submissions", params={"status": "failed"}).json()
    assert [r["job_id"] for r in failed["items"]] == [ids[1]]
    assert viewer.get("/api/submissions", params={"status": "succeeded"}).json()["total"] == 2
    assert viewer.get("/api/submissions", params={"q": ids[2][:10]}).json()["items"][0]["job_id"] == ids[2]
    bad = viewer.get("/api/submissions", params={"status": "weird"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_status"
