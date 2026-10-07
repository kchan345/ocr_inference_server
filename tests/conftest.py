from __future__ import annotations

import io
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mock_vllm.app import FIXTURE as MOCK_FIXTURE
from mock_vllm.app import create_mock_app
from ocr_server.app import create_app
from ocr_server.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_PNG = FIXTURES / "sample.png"
CANNED_RESPONSE = json.loads(MOCK_FIXTURE.read_text("utf-8"))
CANNED_CONTENT = CANNED_RESPONSE["choices"][0]["message"]["content"]


@pytest.fixture
def sample_png() -> bytes:
    return SAMPLE_PNG.read_bytes()


@pytest.fixture
def mock_backend():
    return create_mock_app()


@pytest.fixture
def make_server(tmp_path, mock_backend):
    clients: list[TestClient] = []

    def factory(*, transport: httpx.AsyncBaseTransport | None = None, **overrides) -> TestClient:
        values = {
            "backend_url": "http://mock-vllm/v1",
            "artifact_dir": tmp_path / "artifacts",
            "batch_window_seconds": 0.05,
        }
        values.update(overrides)
        app = create_app(Settings(**values), backend_transport=transport or httpx.ASGITransport(app=mock_backend))
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def server(make_server) -> TestClient:
    return make_server()


def submit(client: TestClient, data: bytes, filename: str = "sample.png", prompt: str | None = None):
    form = {"prompt": prompt} if prompt is not None else None
    return client.post("/v1/ocr", files={"image": (filename, data, "image/png")}, data=form)


def wait_for_job(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = client.get(f"/v1/jobs/{job_id}").json()
        if meta["status"] in ("succeeded", "failed"):
            return meta
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s (last status {meta['status']})")


def make_png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()
