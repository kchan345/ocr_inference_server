"""Browser tests for the New OCR editor and the Submissions page (CI: real inference server + mock vLLM).

Environment:
  SUBMIT_VIEWER_URL   viewer started with ``--inbox ... --server-url <inference server>`` (tests skip if unset)
  E2E_SAMPLE_IMAGE    image to import (default tests/fixtures/sample.png, 1000x1100)
  E2E_SCREENSHOTS     directory for screenshots (default e2e-out/screenshots)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from ocr_viewer.render import box_to_pixels

SUBMIT_URL = (os.environ.get("SUBMIT_VIEWER_URL") or "").rstrip("/")
SAMPLE = Path(os.environ.get("E2E_SAMPLE_IMAGE", "tests/fixtures/sample.png"))
SHOTS = Path(os.environ.get("E2E_SCREENSHOTS", "e2e-out/screenshots"))
REGION = (94, 592, 715, 935)
ACTIVE = re.compile(r"\bactive\b")

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
    expect(page.locator("#nav-new")).to_have_class(ACTIVE)
    expect(page.locator("#server-status")).to_contain_text("Connected", timeout=15000)


def wait_preview(page: Page, query: str = "") -> None:
    """Wait until the crop editor shows the (adjusted) preview whose URL contains ``query``."""
    page.wait_for_function(
        """q => { const i = document.getElementById('crop-image');
                  return !document.getElementById('crop-stage').hidden && i.complete && i.naturalWidth > 0
                         && i.getAttribute('src').includes(q) && !i.style.transform; }""",
        arg=query,
    )


def import_sample(page: Page) -> None:
    page.set_input_files("#import-files", str(SAMPLE))
    expect(page.locator("#import-list li")).to_have_count(1)
    wait_preview(page, "/api/sources/")
    expect(page.locator("#adjust-panel")).to_be_enabled()


def draw_box(page: Page, fx1: float, fy1: float, fx2: float, fy2: float) -> None:
    box = page.locator("#crop-image").bounding_box()
    page.mouse.move(box["x"] + fx1 * box["width"], box["y"] + fy1 * box["height"])
    page.mouse.down()
    page.mouse.move(box["x"] + fx2 * box["width"], box["y"] + fy2 * box["height"], steps=10)
    page.mouse.up()
    expect(page.locator("#crop-box")).to_be_visible()
    expect(page.locator("#crop-info")).to_contain_text("Box")


def submit(page: Page) -> str:
    with page.expect_response(
        lambda r: r.url.endswith("/api/submissions") and r.request.method == "POST"
    ) as response:
        page.click("#submit-ocr")
    assert response.value.status == 202, response.value.text()
    job_id = response.value.json()["job_id"]
    expect(page.locator("#submit-messages .message.ok")).to_contain_text(job_id)
    expect(page.locator("#import-list li")).to_have_count(0)
    return job_id


def submission_row(page: Page, job_id: str):
    if "#/submissions" not in page.url:
        page.click("#nav-submissions")
    expect(page.locator("#submissions-view")).to_be_visible()
    row = page.locator(f"#submission-rows tr[data-job-id='{job_id}']")
    expect(row).to_have_attribute("data-state", "imported", timeout=60000)
    return row


def record_of(page: Page, job_id: str) -> dict:
    return page.request.get(f"{SUBMIT_URL}/api/submissions/{job_id}").json()


def open_result(page: Page, job_id: str, text: str = "Quarterly Sales Report") -> dict:
    row = submission_row(page, job_id)
    row.locator("a.open-job").click()
    expect(page.locator("#job-view")).to_be_visible()
    page.wait_for_function("document.getElementById('page-image').naturalWidth > 0")
    expect(page.locator("#rendered")).to_contain_text(text)
    page.wait_for_function(
        "() => { const i = document.querySelector('#rendered img'); return i && i.complete && i.naturalWidth > 0; }"
    )
    return record_of(page, job_id)


def region_size(crop_box: list[int]) -> list[int]:
    x1, y1, x2, y2 = crop_box
    rx1, ry1, rx2, ry2 = box_to_pixels(REGION, x2 - x1, y2 - y1, 1000)
    return [rx2 - rx1, ry2 - ry1]


def scale_of(page: Page, viewport: str = "#crop-viewport") -> float:
    return float(page.locator(viewport).get_attribute("data-scale"))


# ----------------------------------------------------------------- navigation & server
def test_sidebar_navigation(page: Page):
    page.goto(SUBMIT_URL + "/")
    expect(page.locator("#list-view")).to_be_visible()
    expect(page.locator("#nav-jobs")).to_have_class(ACTIVE)
    page.click("#nav-new")
    expect(page.locator("#new-view")).to_be_visible()
    page.click("#nav-submissions")
    expect(page.locator("#submissions-view")).to_be_visible()
    expect(page.locator("#nav-submissions")).to_have_class(ACTIVE)
    expect(page.locator("#new-view")).to_be_hidden()
    page.click("#nav-jobs")
    expect(page.locator("#list-view")).to_be_visible()


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


# ----------------------------------------------------------------- editor: zoom, pan, adjustments
def test_crop_editor_zoom_and_pan(page: Page):
    open_new_view(page)
    import_sample(page)
    fitted = scale_of(page)
    page.click("#crop-zoom-in")
    assert scale_of(page) == pytest.approx(fitted * 1.25, rel=1e-3)
    page.click("#crop-zoom-out")
    assert scale_of(page) == pytest.approx(fitted, rel=1e-3)

    vp = page.locator("#crop-viewport").bounding_box()
    cx, cy = vp["x"] + vp["width"] / 2, vp["y"] + vp["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.wheel(0, -400)
    page.wait_for_function(f"parseFloat(document.getElementById('crop-viewport').dataset.scale) > {fitted * 1.5}")
    expect(page.locator("#crop-zoom-level")).not_to_have_text(f"{round(fitted * 100)}%")

    page.click("#tool-pan")
    tx = float(page.locator("#crop-viewport").get_attribute("data-tx"))
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 150, cy + 80, steps=5)
    page.mouse.up()
    assert float(page.locator("#crop-viewport").get_attribute("data-tx")) == pytest.approx(tx + 150, abs=2)
    expect(page.locator("#crop-box")).to_be_hidden()

    # middle-button drag pans in box mode too
    page.click("#tool-box")
    tx = float(page.locator("#crop-viewport").get_attribute("data-tx"))
    page.mouse.move(cx, cy)
    page.mouse.down(button="middle")
    page.mouse.move(cx - 100, cy, steps=5)
    page.mouse.up(button="middle")
    assert float(page.locator("#crop-viewport").get_attribute("data-tx")) == pytest.approx(tx - 100, abs=2)
    expect(page.locator("#crop-box")).to_be_hidden()

    # drawing at high zoom still maps to image pixels
    page.click("#crop-zoom-fit")
    assert scale_of(page) == pytest.approx(fitted, rel=1e-3)
    page.click("#crop-zoom-actual")
    assert scale_of(page) == 1
    draw_box(page, 0.3, 0.4, 0.5, 0.55)
    page.click("#crop-zoom-fit")
    expect(page.locator("#crop-info")).to_contain_text("Box")
    shot(page, "10-new-ocr-zoom")


def test_adjustments_rotation_and_threshold(page: Page):
    open_new_view(page)
    import_sample(page)
    page.check("#adj-bw")
    expect(page.locator("#adj-grayscale")).to_be_checked()
    expect(page.locator("#adj-grayscale")).to_be_disabled()
    wait_preview(page, "threshold=128")

    page.click("#rot-right90")
    expect(page.locator("#adj-rotation")).to_have_value("90.0")
    wait_preview(page, "rotation=90.0")
    assert page.evaluate("document.getElementById('crop-image').naturalWidth") == 1100

    page.click("#rot-plus")
    expect(page.locator("#adj-rotation")).to_have_value("90.1")
    wait_preview(page, "rotation=90.1")
    assert page.evaluate("document.getElementById('crop-image').naturalWidth") > 1100
    page.locator("#adj-rotation").fill("-0.3")
    page.locator("#adj-rotation").dispatch_event("change")
    wait_preview(page, "rotation=-0.3")
    page.locator("#adj-rotation").fill("450")
    page.locator("#adj-rotation").dispatch_event("change")
    expect(page.locator("#adj-rotation")).to_have_value("90.0")
    wait_preview(page, "rotation=90.0")
    expect(page.locator("#import-list li .detail")).to_contain_text("rotated 90.0°, B&W ≥128")
    shot(page, "11-new-ocr-adjusted")

    page.check("#persist-original")
    job_id = submit(page)
    record = open_result(page, job_id)
    assert record["adjustments"] == {"rotation": 90.0, "grayscale": True, "threshold": 128}
    assert record["crop"] is None
    assert page.evaluate("[document.getElementById('page-image').naturalWidth, "
                         "document.getElementById('page-image').naturalHeight]") == [1100, 1000]
    values = page.evaluate("""() => {
        const img = document.getElementById('page-image');
        const c = document.createElement('canvas');
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        const ctx = c.getContext('2d');
        ctx.drawImage(img, 0, 0);
        const d = ctx.getImageData(0, 0, c.width, c.height).data;
        const seen = new Set();
        for (let i = 0; i < d.length; i += 4) { seen.add(d[i]); seen.add(d[i + 1]); seen.add(d[i + 2]); }
        return [...seen].sort((a, b) => a - b);
    }""")
    assert values == [0, 255]
    expect(page.locator("#job-file")).to_contain_text("rotated 90.0°")

    page.click("#nav-new")
    import_sample(page)
    page.check("#adj-grayscale")
    wait_preview(page, "grayscale=true")
    page.click("#adj-reset")
    expect(page.locator("#crop-image")).to_have_attribute("src", re.compile(r"/preview$"))
    wait_preview(page, "/preview")
    assert "?" not in page.locator("#crop-image").get_attribute("src")
    expect(page.locator("#adj-grayscale")).not_to_be_checked()
    page.click("#import-list li .remove")
    expect(page.locator("#import-list li")).to_have_count(0)


# ----------------------------------------------------------------- box + persisted image
def test_box_region_keep_original_image(page: Page):
    open_new_view(page)
    import_sample(page)
    draw_box(page, 0.05, 0.05, 0.8, 0.95)
    page.check("#persist-original")
    shot(page, "12-new-ocr-box")
    record = open_result(page, submit(page))

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
    shot(page, "13-result-original-with-frame")


def test_box_region_keep_cropped_image(page: Page):
    open_new_view(page)
    import_sample(page)
    draw_box(page, 0.1, 0.5, 0.9, 0.9)
    page.check("#persist-cropped")
    record = open_result(page, submit(page))
    x1, y1, x2, y2 = record["crop"]["box"]
    natural = page.evaluate("[document.getElementById('page-image').naturalWidth, "
                            "document.getElementById('page-image').naturalHeight]")
    assert natural == [x2 - x1, y2 - y1]
    expect(page.locator("#overlay .crop-frame")).to_have_count(0)
    expect(page.locator("#job-file")).to_contain_text("kept cropped image")
    size = page.locator("#rendered img").evaluate("i => [i.naturalWidth, i.naturalHeight]")
    assert size == region_size(record["crop"]["box"])


def test_whole_image_submission_and_clear_box(page: Page):
    open_new_view(page)
    import_sample(page)
    draw_box(page, 0.2, 0.2, 0.4, 0.4)
    page.click("#crop-clear")
    expect(page.locator("#crop-box")).to_be_hidden()
    expect(page.locator("#crop-info")).to_contain_text("whole image")
    record = open_result(page, submit(page))
    assert record["crop"] is None
    assert page.evaluate("document.getElementById('page-image').naturalWidth") == 1000


# ----------------------------------------------------------------- submissions page: resubmit & redraw
def test_resubmit_and_redraw_failed_job(page: Page):
    open_new_view(page)
    import_sample(page)
    page.fill("#ocr-prompt", "MOCK_FAIL please")
    draw_box(page, 0.1, 0.1, 0.6, 0.6)
    failed_id = submit(page)
    row = submission_row(page, failed_id)
    expect(row).to_have_attribute("data-status", "failed")
    expect(row.locator(".badge")).to_have_class("badge failed")

    page.select_option("#sub-status", "failed")
    expect(page).to_have_url(f"{SUBMIT_URL}/#/submissions?page=1&size=20&status=failed")
    expect(page.locator(f"#submission-rows tr[data-job-id='{failed_id}']")).to_be_visible()
    expect(page.locator("#submission-rows tr[data-status='succeeded']")).to_have_count(0)
    page.select_option("#sub-status", "")

    # resubmit with identical settings
    row = submission_row(page, failed_id)
    with page.expect_response(lambda r: r.url.endswith(f"/api/submissions/{failed_id}/resubmit")) as response:
        row.locator("button.resubmit").click()
    assert response.value.status == 202, response.value.text()
    again = response.value.json()
    assert again["resubmit_of"] == failed_id and again["crop"] == record_of(page, failed_id)["crop"]
    expect(page.locator("#sub-messages .message.ok")).to_contain_text(again["job_id"])
    again_row = submission_row(page, again["job_id"])
    expect(again_row).to_contain_text(f"resubmission of {failed_id[:8]}")
    expect(again_row).to_have_attribute("data-status", "failed")
    shot(page, "14-submissions")

    # redraw: reopens the image with the old box and prompt, then submit with a new box and prompt
    submission_row(page, failed_id).locator("button.redraw").click()
    expect(page.locator("#new-view")).to_be_visible()
    expect(page).to_have_url(f"{SUBMIT_URL}/#/new")
    expect(page.locator("#crop-resubmit")).to_contain_text(failed_id[:8])
    expect(page.locator("#import-list li")).to_have_count(1)
    wait_preview(page, "/preview")
    expect(page.locator("#crop-box")).to_be_visible()
    expect(page.locator("#ocr-prompt")).to_have_value("MOCK_FAIL please")
    page.fill("#ocr-prompt", "")
    draw_box(page, 0.0, 0.4, 1.0, 1.0)
    page.check("#persist-cropped")
    redrawn_id = submit(page)
    record = open_result(page, redrawn_id)
    assert record["resubmit_of"] == failed_id and record["prompt"] is None
    assert record["crop"]["box"][1] >= 400
    expect(page.locator("#job-file")).to_contain_text(f"resubmission of {failed_id[:8]}")

    # the result view can start a redraw of a succeeded job too
    page.click("#job-resubmit")
    expect(page.locator("#new-view")).to_be_visible()
    expect(page.locator("#crop-resubmit")).to_contain_text(redrawn_id[:8])
    wait_preview(page, "/preview")
    page.click("#crop-clear")
    resubmitted = submit(page)
    record = record_of(page, resubmitted)
    assert record["resubmit_of"] == redrawn_id and record["crop"] is None
    submission_row(page, resubmitted)
