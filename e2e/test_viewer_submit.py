"""Browser tests for submitting images from the viewer to the inference server (CI: real server + mock vLLM).

Environment:
  SUBMIT_VIEWER_URL   viewer started with ``--inbox ... --server-url <inference server>`` (tests skip if unset)
  E2E_SAMPLE_IMAGE    image to import (default tests/fixtures/sample.png)
  E2E_SCREENSHOTS     directory for screenshots (default e2e-out/screenshots)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from ocr_viewer.render import box_to_pixels

SUBMIT_URL = (os.environ.get("SUBMIT_VIEWER_URL") or "").rstrip("/")
SAMPLE = Path(os.environ.get("E2E_SAMPLE_IMAGE", "tests/fixtures/sample.png"))
SHOTS = Path(os.environ.get("E2E_SCREENSHOTS", "e2e-out/screenshots"))
REGION = (94, 592, 715, 935)

pytestmark = pytest.mark.skipif(not SUBMIT_URL, reason="SUBMIT_VIEWER_URL not set")


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1600, "height": 1100}}


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def open_new_view(page: Page) -> None:
    page.goto(f"{SUBMIT_URL}/#/new")
    expect(page.locator("#new-view")).to_be_visible()
    expect(page.locator("#server-status")).to_contain_text("Connected", timeout=15000)


def import_sample(page: Page) -> None:
    page.set_input_files("#import-files", str(SAMPLE))
    expect(page.locator("#import-list li")).to_have_count(1)
    page.wait_for_function("document.getElementById('crop-image').naturalWidth > 0")


def draw_box(page: Page, fx1: float, fy1: float, fx2: float, fy2: float) -> None:
    box = page.locator("#crop-image").bounding_box()
    page.mouse.move(box["x"] + fx1 * box["width"], box["y"] + fy1 * box["height"])
    page.mouse.down()
    page.mouse.move(box["x"] + fx2 * box["width"], box["y"] + fy2 * box["height"], steps=10)
    page.mouse.up()
    expect(page.locator("#crop-box")).to_be_visible()
    expect(page.locator("#crop-info")).to_contain_text("Box")


def submit_and_open(page: Page) -> dict:
    with page.expect_response(
        lambda r: r.url.endswith("/api/submissions") and r.request.method == "POST"
    ) as response:
        page.click("#submit-ocr")
    assert response.value.status == 202, response.value.text()
    job_id = response.value.json()["job_id"]
    expect(page.locator("#submit-messages .message.ok")).to_contain_text(job_id)
    expect(page.locator("#import-list li")).to_have_count(0)
    row = page.locator(f"#submission-rows tr[data-job-id='{job_id}']")
    expect(row.locator("a.open-job")).to_be_visible(timeout=60000)
    record = page.request.get(f"{SUBMIT_URL}/api/submissions/{job_id}").json()
    row.locator("a.open-job").click()
    expect(page.locator("#job-view")).to_be_visible()
    page.wait_for_function("document.getElementById('page-image').naturalWidth > 0")
    expect(page.locator("#rendered")).to_contain_text("Quarterly Sales Report")
    page.wait_for_function(
        "() => { const i = document.querySelector('#rendered img'); return i && i.complete && i.naturalWidth > 0; }"
    )
    return record


def region_size(crop_box: list[int]) -> list[int]:
    x1, y1, x2, y2 = crop_box
    rx1, ry1, rx2, ry2 = box_to_pixels(REGION, x2 - x1, y2 - y1, 1000)
    return [rx2 - rx1, ry2 - ry1]


def test_custom_server_url(page: Page):
    open_new_view(page)
    url = page.locator("#server-url")
    default = url.input_value()
    url.fill("http://127.0.0.1:9")
    url.dispatch_event("change")
    expect(page.locator("#server-status")).to_contain_text("Not reachable")
    page.reload()
    assert page.locator("#server-url").input_value() == "http://127.0.0.1:9", "custom URL is remembered"
    page.click("#server-reset")
    expect(page.locator("#server-url")).to_have_value(default)
    expect(page.locator("#server-status")).to_contain_text("Connected")


def test_box_region_keep_original_image(page: Page):
    open_new_view(page)
    import_sample(page)
    draw_box(page, 0.05, 0.05, 0.8, 0.95)
    page.check("#persist-original")
    shot(page, "10-new-ocr-box")
    record = submit_and_open(page)

    crop = record["crop"]
    assert (crop["source_width"], crop["source_height"]) == (1000, 1100)
    x1, y1, x2, y2 = crop["box"]
    assert abs(x1 - 50) <= 5 and abs(y1 - 55) <= 6 and abs(x2 - 800) <= 5 and abs(y2 - 1045) <= 6
    assert page.evaluate("document.getElementById('page-image').naturalWidth") == 1000
    expect(page.locator("#overlay .crop-frame")).to_have_count(1)
    expect(page.locator("#overlay .region")).to_have_count(1)
    expect(page.locator("#job-file")).to_contain_text("kept original image")
    size = page.locator("#rendered img").evaluate("i => [i.naturalWidth, i.naturalHeight]")
    assert size == region_size(crop["box"])
    shot(page, "11-result-original-with-frame")


def test_box_region_keep_cropped_image(page: Page):
    open_new_view(page)
    import_sample(page)
    draw_box(page, 0.1, 0.5, 0.9, 0.9)
    page.check("#persist-cropped")
    record = submit_and_open(page)
    x1, y1, x2, y2 = record["crop"]["box"]
    natural = page.evaluate("[document.getElementById('page-image').naturalWidth, "
                            "document.getElementById('page-image').naturalHeight]")
    assert natural == [x2 - x1, y2 - y1]
    expect(page.locator("#overlay .crop-frame")).to_have_count(0)
    expect(page.locator("#job-file")).to_contain_text("kept cropped image")
    size = page.locator("#rendered img").evaluate("i => [i.naturalWidth, i.naturalHeight]")
    assert size == region_size(record["crop"]["box"])
    shot(page, "12-result-cropped")


def test_whole_image_submission_and_clear_box(page: Page):
    open_new_view(page)
    import_sample(page)
    draw_box(page, 0.2, 0.2, 0.4, 0.4)
    page.click("#crop-clear")
    expect(page.locator("#crop-box")).to_be_hidden()
    expect(page.locator("#crop-info")).to_contain_text("whole image")
    record = submit_and_open(page)
    assert record["crop"] is None
    assert page.evaluate("document.getElementById('page-image').naturalWidth") == 1000
