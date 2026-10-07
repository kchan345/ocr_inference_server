from __future__ import annotations

import base64
import io
import json
import time
import zipfile

import httpx
import pytest

from ocr_server.app import create_app
from ocr_server.config import Settings
from ocr_server.handlers.ovisocr2 import OVISOCR2_PROMPT
from tests.conftest import CANNED_CONTENT, make_png, submit, wait_for_job


def test_healthz_and_info(server):
    assert server.get("/healthz").json() == {"status": "ok"}
    info = server.get("/v1/info").json()
    assert info["handler"]["name"] == "ovisocr2"
    assert info["backend"]["model"] == "ATH-MaaS/OvisOCR2"
    assert info["limits"]["max_pixels"] == 2880 * 2880
    assert info["queue"]["max_concurrency"] == 30
    assert info["queue"]["max_buffer"] == 60
    assert info["queue"]["batch_max_size"] == 30
    assert info["default_prompt"] == OVISOCR2_PROMPT


def test_settings_defaults_match_requirements():
    s = Settings()
    assert (s.max_concurrency, s.max_buffer, s.batch_max_size, s.batch_window_seconds) == (30, 60, 30, 10.0)


def test_settings_from_env():
    s = Settings.from_env({
        "OCR_BACKEND_URL": "http://x/v1", "OCR_MAX_CONCURRENCY": "5", "OCR_MAX_BUFFER": "7",
        "OCR_BATCH_MAX_SIZE": "3", "OCR_BATCH_WINDOW_SECONDS": "2.5", "OCR_ARTIFACT_DIR": "/tmp/a",
    })
    assert s.backend_url == "http://x/v1"
    assert (s.max_concurrency, s.max_buffer, s.batch_max_size, s.batch_window_seconds) == (5, 7, 3, 2.5)
    with pytest.raises(ValueError):
        Settings(max_concurrency=0)


def test_submit_returns_job_and_artifacts_zip(server, sample_png):
    resp = submit(server, sample_png)
    assert resp.status_code == 202
    body = resp.json()
    job_id = body["job_id"]
    assert len(job_id) == 32
    assert body["status"] == "queued"
    assert resp.headers["location"] == f"/v1/jobs/{job_id}"
    assert body["links"] == {"self": f"/v1/jobs/{job_id}", "artifacts": f"/v1/jobs/{job_id}/artifacts"}

    meta = wait_for_job(server, job_id)
    assert meta["status"] == "succeeded", meta
    assert meta["input"]["width"] == 1000 and meta["input"]["height"] == 1100
    assert meta["input"]["original_filename"] == "sample.png"
    assert meta["result"]["finish_reason"] == "stop"
    assert meta["result"]["region_count"] == 1
    assert meta["batch"]["batch_id"]
    assert meta["handler"]["bbox_scale"] == 1000

    resp = server.get(f"/v1/jobs/{job_id}/artifacts")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert f'{job_id}.zip' in resp.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    expected = {"job.json", "input.png", "request.json", "response.json", "result.md", "result_text.md", "regions.json"}
    assert names == {f"{job_id}/{n}" for n in expected}
    assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in zf.infolist())
    assert zf.read(f"{job_id}/input.png") == sample_png  # original persisted byte-for-byte
    assert zf.read(f"{job_id}/result.md").decode() == CANNED_CONTENT.strip() + "\n"
    assert "<img" not in zf.read(f"{job_id}/result_text.md").decode()
    regions = json.loads(zf.read(f"{job_id}/regions.json"))
    assert regions == [{"left": 94, "top": 592, "right": 715, "bottom": 935,
                        "ref": "images/bbox_94_592_715_935.jpg", "pixels": [94, 651, 715, 1028]}]
    request = json.loads(zf.read(f"{job_id}/request.json"))
    assert request["messages"][0]["content"][0]["image_url"]["url"] == "<base64 data of input.png omitted>"
    assert json.loads(zf.read(f"{job_id}/job.json"))["status"] == "succeeded"


def test_backend_receives_original_image_and_model_card_payload(server, mock_backend, sample_png):
    job_id = submit(server, sample_png).json()["job_id"]
    assert wait_for_job(server, job_id)["status"] == "succeeded"
    payload = mock_backend.state.requests[-1]
    assert payload["model"] == "ATH-MaaS/OvisOCR2"
    url = payload["messages"][0]["content"][0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == sample_png  # no server-side resampling
    assert payload["messages"][0]["content"][1]["text"] == OVISOCR2_PROMPT
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["mm_processor_kwargs"]["images_kwargs"]["max_pixels"] == 2880 * 2880
    assert payload["temperature"] == 0.0 and payload["max_tokens"] == 16384


def test_custom_prompt_is_forwarded(server, mock_backend, sample_png):
    job_id = submit(server, sample_png, prompt="Transcribe only the table.").json()["job_id"]
    meta = wait_for_job(server, job_id)
    assert meta["prompt"] == "Transcribe only the table."
    assert meta["prompt_used"] == "Transcribe only the table."
    assert mock_backend.state.requests[-1]["messages"][0]["content"][1]["text"] == "Transcribe only the table."


def test_request_is_accepted_before_ocr_completes(make_server, mock_backend, sample_png):
    mock_backend.state.delay = 0.5
    server = make_server()
    started = time.monotonic()
    resp = submit(server, sample_png)
    assert resp.status_code == 202
    assert time.monotonic() - started < 0.5
    job_id = resp.json()["job_id"]
    assert server.get(f"/v1/jobs/{job_id}").json()["status"] in ("queued", "running")
    conflict = server.get(f"/v1/jobs/{job_id}/artifacts")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "job_not_finished"
    assert wait_for_job(server, job_id)["status"] == "succeeded"


def test_too_large_image_is_rejected_not_downsampled(server, mock_backend, tmp_path):
    resp = submit(server, make_png(3000, 3000), filename="big.png")
    assert resp.status_code == 413
    err = resp.json()["error"]
    assert err["code"] == "image_too_large"
    assert (err["width"], err["height"], err["max_pixels"]) == (3000, 3000, 2880 * 2880)
    assert err["suggested_width"] * err["suggested_height"] <= 2880 * 2880
    assert 2870 <= err["suggested_width"] <= 2880
    assert "resize it on the client" in err["message"]
    assert mock_backend.state.requests == []
    assert [p for p in (tmp_path / "artifacts").iterdir() if not p.name.startswith(".")] == []


def test_configured_pixel_limit(make_server):
    server = make_server(max_pixels=500 * 500)
    assert submit(server, make_png(501, 500)).status_code == 413
    assert submit(server, make_png(500, 500)).status_code == 202


def test_pixel_limit_cannot_exceed_handler_limit(tmp_path):
    with pytest.raises(ValueError, match="downscaling"):
        create_app(Settings(artifact_dir=tmp_path, max_pixels=2881 * 2880))


@pytest.mark.parametrize(
    ("data", "status", "code"),
    [(b"", 400, "empty_image"), (b"definitely not an image", 400, "invalid_image")],
)
def test_invalid_uploads(server, data, status, code):
    resp = submit(server, data)
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


def test_upload_size_limit(make_server, sample_png):
    server = make_server(max_upload_bytes=1000)
    resp = submit(server, sample_png)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"


def test_missing_image_field(server):
    resp = server.post("/v1/ocr", data={"prompt": "x"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_prompt_too_long(make_server, sample_png):
    server = make_server(max_prompt_chars=10)
    resp = submit(server, sample_png, prompt="x" * 11)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "prompt_too_long"


@pytest.mark.parametrize("job_id", ["0" * 32, "not-a-job", "..%2F..%2Fetc"])
def test_unknown_job(server, job_id):
    for path in (f"/v1/jobs/{job_id}", f"/v1/jobs/{job_id}/artifacts"):
        resp = server.get(path)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] in ("job_not_found", "http_error")


def test_backend_error_marks_job_failed(server, sample_png):
    job_id = submit(server, sample_png, prompt="MOCK_FAIL").json()["job_id"]
    meta = wait_for_job(server, job_id)
    assert meta["status"] == "failed"
    assert meta["error"]["code"] == "backend_error"
    assert meta["error"]["backend_status"] == 500
    resp = server.get(f"/v1/jobs/{job_id}/artifacts")
    assert resp.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert f"{job_id}/input.png" in names and f"{job_id}/response.json" in names


def test_backend_model_mismatch_fails_job(make_server, sample_png):
    server = make_server(backend_model="other/model")
    meta = wait_for_job(server, submit(server, sample_png).json()["job_id"])
    assert meta["status"] == "failed"
    assert meta["error"]["backend_status"] == 404


class _Unreachable(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


def test_backend_unreachable(make_server, sample_png):
    server = make_server(transport=_Unreachable())
    meta = wait_for_job(server, submit(server, sample_png).json()["job_id"])
    assert meta["status"] == "failed"
    assert meta["error"]["code"] == "backend_unreachable"


def test_truncated_output_flagged(make_server, sample_png):
    server = make_server(max_tokens=20)
    meta = wait_for_job(server, submit(server, sample_png).json()["job_id"])
    assert meta["status"] == "succeeded"
    assert meta["result"]["truncated"] is True
    assert meta["result"]["finish_reason"] == "length"
    assert any("max_tokens" in w for w in meta["result"]["warnings"])


def test_repeated_tail_is_cleaned(server, sample_png):
    job_id = submit(server, sample_png, prompt="MOCK_REPEAT").json()["job_id"]
    assert wait_for_job(server, job_id)["status"] == "succeeded"
    zf = zipfile.ZipFile(io.BytesIO(server.get(f"/v1/jobs/{job_id}/artifacts").content))
    assert zf.read(f"{job_id}/result.md").decode().rstrip().endswith("abcde")
    assert len(zf.read(f"{job_id}/result.md")) < len(CANNED_CONTENT) + 100


def test_interrupted_jobs_are_recovered_on_startup(make_server, tmp_path):
    job_id = "a" * 32
    job_dir = tmp_path / "artifacts" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({"job_id": job_id, "status": "running"}))
    server = make_server()
    meta = server.get(f"/v1/jobs/{job_id}").json()
    assert meta["status"] == "failed"
    assert meta["error"]["code"] == "interrupted"
