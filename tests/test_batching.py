"""Batching, backend concurrency limit and buffer limit behaviour of the inference server."""

from __future__ import annotations

import time
from collections import Counter

from tests.conftest import submit, wait_for_job


def _batches(server, job_ids):
    metas = [wait_for_job(server, j) for j in job_ids]
    assert all(m["status"] == "succeeded" for m in metas), metas
    return metas, Counter(m["batch"]["batch_id"] for m in metas)


def test_batch_dispatched_as_soon_as_it_is_full(make_server, sample_png):
    server = make_server(batch_max_size=3, batch_window_seconds=60)
    started = time.monotonic()
    job_ids = [submit(server, sample_png).json()["job_id"] for _ in range(3)]
    metas, batches = _batches(server, job_ids)
    assert time.monotonic() - started < 10, "a full batch must not wait for the window"
    assert list(batches.values()) == [3]
    assert all(m["batch"]["size"] == 3 for m in metas)


def test_partial_batch_dispatched_when_window_expires(make_server, sample_png):
    server = make_server(batch_max_size=30, batch_window_seconds=0.6)
    job_id = submit(server, sample_png).json()["job_id"]
    time.sleep(0.2)
    assert server.get(f"/v1/jobs/{job_id}").json()["status"] == "queued"
    meta = wait_for_job(server, job_id)
    assert meta["status"] == "succeeded"
    assert meta["batch"]["size"] == 1
    assert meta["batch"]["waited_seconds"] >= 0.55


def test_batches_never_exceed_max_size(make_server, sample_png):
    server = make_server(batch_max_size=2, batch_window_seconds=0.5)
    job_ids = [submit(server, sample_png).json()["job_id"] for _ in range(5)]
    _, batches = _batches(server, job_ids)
    assert max(batches.values()) <= 2
    assert sum(batches.values()) == 5
    assert len(batches) >= 3


def test_backend_concurrency_is_capped(make_server, mock_backend, sample_png):
    mock_backend.state.delay = 0.3
    server = make_server(max_concurrency=2, batch_max_size=10, batch_window_seconds=0.05)
    job_ids = [submit(server, sample_png).json()["job_id"] for _ in range(6)]
    _batches(server, job_ids)
    assert mock_backend.state.peak_inflight == 2
    assert server.get("/v1/info").json()["queue"]["peak_active"] == 2


def test_buffer_full_rejects_new_jobs(make_server, sample_png):
    server = make_server(max_buffer=3, batch_max_size=30, batch_window_seconds=60)
    for _ in range(3):
        assert submit(server, sample_png).status_code == 202
    resp = submit(server, sample_png)
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == "buffer_full"
    assert (err["buffered"], err["max_buffer"]) == (3, 3)
    assert resp.headers["retry-after"] == "60"
    assert server.get("/v1/info").json()["queue"]["buffered"] == 3


def test_buffer_frees_once_jobs_are_dispatched(make_server, sample_png):
    server = make_server(max_buffer=2, batch_max_size=2, batch_window_seconds=60)
    first = [submit(server, sample_png).json()["job_id"] for _ in range(2)]
    _batches(server, first)
    second = [submit(server, sample_png) for _ in range(2)]
    assert [r.status_code for r in second] == [202, 202]
    _batches(server, [r.json()["job_id"] for r in second])


def test_active_jobs_do_not_count_against_buffer(make_server, mock_backend, sample_png):
    mock_backend.state.delay = 1.5
    server = make_server(max_concurrency=1, max_buffer=2, batch_max_size=1, batch_window_seconds=0)
    active = submit(server, sample_png).json()["job_id"]
    deadline = time.monotonic() + 5
    while server.get(f"/v1/jobs/{active}").json()["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    waiting = [submit(server, sample_png) for _ in range(2)]
    assert [r.status_code for r in waiting] == [202, 202]
    rejected = submit(server, sample_png)
    assert rejected.status_code == 503
    queue = server.get("/v1/info").json()["queue"]
    assert (queue["active"], queue["buffered"]) == (1, 2)
