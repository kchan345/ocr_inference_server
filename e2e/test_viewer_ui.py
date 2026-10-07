"""Browser tests for the artifact viewer (run in CI against artifacts produced by the real server + mock vLLM).

Environment:
  VIEWER_URL          base URL of a running ``ocr-viewer`` (default http://127.0.0.1:8765)
  E2E_EXPECTED_JOBS   number of job folders the viewer should list (default 5)
  E2E_SCREENSHOTS     directory for screenshots (default e2e-out/screenshots)
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

VIEWER_URL = os.environ.get("VIEWER_URL", "http://127.0.0.1:8765").rstrip("/")
EXPECTED_JOBS = int(os.environ.get("E2E_EXPECTED_JOBS", "5"))
SHOTS = Path(os.environ.get("E2E_SCREENSHOTS", "e2e-out/screenshots"))


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1600, "height": 1000}}


@pytest.fixture
def jobs(page: Page) -> list[dict]:
    data = page.request.get(f"{VIEWER_URL}/api/jobs?page_size=200").json()
    assert data["total"] == EXPECTED_JOBS
    return data["items"]


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def viewport_state(page: Page) -> tuple[float, float, float]:
    ds = page.locator("#viewport")
    return (float(ds.get_attribute("data-scale")), float(ds.get_attribute("data-tx")),
            float(ds.get_attribute("data-ty")))


def open_job(page: Page, key: str) -> None:
    page.goto(f"{VIEWER_URL}/#/job/{key}")
    expect(page.locator("#job-view")).to_be_visible()
    page.wait_for_function("document.getElementById('page-image').naturalWidth > 0")
    page.wait_for_function("document.getElementById('viewport').dataset.scale !== undefined")


def test_paged_job_list(page: Page, jobs):
    page.goto(f"{VIEWER_URL}/#/jobs?page=1&size=2")
    expect(page.locator("#page-info")).to_have_text(f"Page 1 of {math.ceil(EXPECTED_JOBS / 2)}")
    expect(page.locator("#total-info")).to_have_text(f"{EXPECTED_JOBS} jobs")
    expect(page.locator("#job-rows tr")).to_have_count(2)
    expect(page.locator("#prev-page")).to_be_disabled()
    first_page_keys = page.locator("#job-rows tr").evaluate_all("rows => rows.map(r => r.dataset.key)")
    shot(page, "01-list-page1")

    page.click("#next-page")
    expect(page.locator("#page-info")).to_have_text(re.compile(r"^Page 2 of"))
    second_page_keys = page.locator("#job-rows tr").evaluate_all("rows => rows.map(r => r.dataset.key)")
    assert not set(first_page_keys) & set(second_page_keys)

    page.click("#last-page")
    expect(page.locator("#job-rows tr")).to_have_count(EXPECTED_JOBS - 2 * (math.ceil(EXPECTED_JOBS / 2) - 1))
    expect(page.locator("#next-page")).to_be_disabled()

    page.select_option("#status-filter", "failed")
    expect(page.locator("#job-rows tr")).to_have_count(1)
    expect(page.locator("#job-rows tr .badge.failed")).to_have_count(1)
    shot(page, "02-list-failed-filter")


def test_job_view_side_by_side_render(page: Page, jobs):
    job = next(j for j in jobs if j["status"] == "succeeded" and j["original_filename"] == "sample.png")
    open_job(page, job["key"])
    rendered = page.locator("#rendered")
    expect(rendered).to_contain_text("Quarterly Sales Report")
    expect(rendered.locator("table td", has_text="North")).to_have_count(1)
    region = rendered.locator("img[src*='bbox_94_592_715_935']")
    expect(region).to_have_count(1)
    page.wait_for_function(
        "() => { const i = document.querySelector('#rendered img'); return i && i.complete && i.naturalWidth > 0; }"
    )
    assert region.evaluate("i => [i.naturalWidth, i.naturalHeight]") == [621, 377]
    expect(page.locator("#overlay .region")).to_have_count(1)

    image_box = page.locator("#viewport").bounding_box()
    text_box = page.locator("#rendered").bounding_box()
    assert image_box["x"] + image_box["width"] <= text_box["x"] + 1, "panels should be side by side"
    shot(page, "03-job-rendered")

    region.click()
    expect(page.locator("#overlay .region.active")).to_have_count(1)


def test_zoom_and_pan(page: Page, jobs):
    job = next(j for j in jobs if j["status"] == "succeeded")
    open_job(page, job["key"])
    scale0, _, _ = viewport_state(page)
    assert scale0 > 0

    page.click("#zoom-in")
    scale1, _, _ = viewport_state(page)
    assert scale1 == pytest.approx(scale0 * 1.25, rel=1e-3)
    expect(page.locator("#zoom-level")).to_have_text(f"{round(scale1 * 100)}%")

    page.click("#zoom-out")
    assert viewport_state(page)[0] == pytest.approx(scale0, rel=1e-3)

    box = page.locator("#viewport").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.wheel(0, -400)
    page.wait_for_function(f"parseFloat(document.getElementById('viewport').dataset.scale) > {scale0 * 1.5}")

    _, tx0, ty0 = viewport_state(page)
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 120, cy + 80, steps=8)
    page.mouse.up()
    _, tx1, ty1 = viewport_state(page)
    assert tx1 - tx0 == pytest.approx(120, abs=2)
    assert ty1 - ty0 == pytest.approx(80, abs=2)
    shot(page, "04-job-zoomed-panned")

    page.click("#zoom-actual")
    assert viewport_state(page)[0] == 1
    page.click("#zoom-fit")
    assert viewport_state(page)[0] == pytest.approx(scale0, rel=1e-3)


def test_edit_save_and_revert(page: Page, jobs):
    job = next(j for j in jobs if j["status"] == "succeeded")
    open_job(page, job["key"])
    page.click("#mode-edit")
    editor = page.locator("#editor")
    expect(editor).to_be_visible()
    expect(page.locator("#rendered")).to_be_hidden()
    expect(page.locator("#save-md")).to_be_disabled()

    original = editor.input_value()
    assert "Quarterly Sales Report" in original
    editor.fill(original.replace("Quarterly Sales Report", "Corrected Sales Report"))
    expect(page.locator("#save-state")).to_have_text("Unsaved changes")
    page.click("#mode-render")
    expect(page.locator("#rendered")).to_contain_text("Corrected Sales Report")
    shot(page, "05-job-edited-preview")

    page.click("#save-md")
    expect(page.locator("#save-state")).to_have_text("Saved")
    page.reload()
    expect(page.locator("#rendered")).to_contain_text("Corrected Sales Report")
    expect(page.locator("#revert-md")).to_be_enabled()

    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#revert-md")
    expect(page.locator("#save-state")).to_have_text("Reverted to original")
    expect(page.locator("#rendered")).to_contain_text("Quarterly Sales Report")
    page.reload()
    expect(page.locator("#rendered")).not_to_contain_text("Corrected Sales Report")


def test_failed_job_shows_error(page: Page, jobs):
    job = next(j for j in jobs if j["status"] == "failed")
    open_job(page, job["key"])
    expect(page.locator("#job-status")).to_have_text("failed")
    expect(page.locator("#job-messages .message.error")).to_contain_text("backend_error")
    shot(page, "06-job-failed")
