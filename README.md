# OCR inference server

An asynchronous OCR job server that sits in front of an **OpenAI-compatible chat-completions endpoint**
(e.g. [vLLM](https://github.com/vllm-project/vllm) serving
[ATH-MaaS/OvisOCR2](https://huggingface.co/ATH-MaaS/OvisOCR2)), plus a **local web viewer** for the
artifacts it produces.

```
client ──POST /v1/ocr──▶ inference server ──batch / ≤30 parallel──▶ vLLM /v1/chat/completions
   ▲   ◀── 202 job_id ──        │  (OvisOCR2 handler builds the payload & parses the output)
   │                            ▼
   └──GET /v1/jobs/{id}/artifacts (deflate zip) ◀── artifacts/<job_id>/ (input image, markdown, regions…)
                                                         │ unzip anywhere
                                                         ▼
                                           ocr-viewer DIR [DIR…]  →  http://127.0.0.1:8765
```

* `src/ocr_server` – FastAPI inference server (job queue, batching, artifact store, zip download).
* `src/ocr_server/handlers` – model-specific request/response handling. `ovisocr2.py` is the OvisOCR2
  implementation; add new handlers for other OCR models.
* `src/ocr_viewer` – local web app (paged job list, side-by-side image / markdown view, image import with
  boxed-region OCR submission to a configurable server).
* `mock_vllm` – mocked vLLM endpoint returning a real OvisOCR2 response, used by tests and CI.
* `scripts/ocr_client.py` – reference client; `scripts/e2e_submit.py` – CI end-to-end driver.
* `e2e/` – Playwright browser tests for the viewer.

All tests and builds run in GitHub Actions (`.github/workflows/ci.yml`); nothing needs to be installed locally
to contribute.

---

## 1. Inference server

### Running

```bash
pip install .
OCR_BACKEND_URL=http://192.168.1.211:8080/v1 ocr-server --host 0.0.0.0 --port 8080
# or
docker build -t ocr-inference-server . && docker run -p 8080:8080 \
  -e OCR_BACKEND_URL=http://192.168.1.211:8080/v1 -v $PWD/artifacts:/data/artifacts ocr-inference-server
```

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `OCR_BACKEND_URL` | `http://localhost:8000/v1` | Base URL of the OpenAI-compatible API (`/chat/completions` is appended). |
| `OCR_BACKEND_MODEL` | handler default (`ATH-MaaS/OvisOCR2`) | `model` sent to the backend. |
| `OCR_BACKEND_API_KEY` | – | Sent as `Authorization: Bearer …` if set. |
| `OCR_HANDLER` | `ovisocr2` | Output handler (see [Handlers](#handlers)). |
| `OCR_ARTIFACT_DIR` | `./artifacts` | Where job folders are written. |
| `OCR_MAX_PIXELS` | handler limit (2880×2880 = 8 294 400) | Largest accepted `width × height`. Cannot exceed the handler limit. |
| `OCR_MAX_UPLOAD_BYTES` | 50 MiB | Largest accepted upload. |
| `OCR_MAX_PROMPT_CHARS` | 8000 | Longest accepted custom prompt. |
| `OCR_REQUEST_TIMEOUT` | 600 | Backend request timeout (seconds). |
| `OCR_MAX_TOKENS` | handler default (16384) | `max_tokens` sent to the backend. |
| `OCR_MAX_CONCURRENCY` | **30** | Maximum simultaneous backend OCR calls per server instance. |
| `OCR_MAX_BUFFER` | **60** | Maximum jobs held waiting for a backend slot before new requests get `503`. |
| `OCR_BATCH_MAX_SIZE` | **30** | A batch is dispatched as soon as it has this many jobs… |
| `OCR_BATCH_WINDOW_SECONDS` | **10** | …or when its oldest job has waited this long, whichever comes first. |

CLI flags `--backend-url --handler --artifact-dir --log-level` override the corresponding variables.

### Request lifecycle, batching, concurrency and buffering

1. `POST /v1/ocr` validates the upload synchronously (format, decodability, pixel count), writes the original
   bytes to the job folder and returns **`202 Accepted`** with a `job_id` immediately. OCR happens in the
   background; the request handler never waits for the backend.
2. Accepted jobs enter the **buffer**. A dispatcher groups them into batches: a batch closes when it holds
   `OCR_BATCH_MAX_SIZE` (30) jobs **or** `OCR_BATCH_WINDOW_SECONDS` (10 s) after its oldest job arrived,
   whichever is earlier. Each job records its `batch` (id, size, dispatch time, seconds waited).
3. Jobs in a dispatched batch are sent to the backend as individual chat-completion requests, gated by a
   semaphore so that **at most `OCR_MAX_CONCURRENCY` (30) backend calls are in flight** at any time (the HTTP
   connection pool is sized the same way).
4. **Buffer limit:** a job counts against `OCR_MAX_BUFFER` (60) from acceptance until it obtains a backend slot.
   When 60 jobs are waiting, `POST /v1/ocr` returns `503 buffer_full` with a `Retry-After` header. *Assumption:*
   jobs actively being processed (≤ 30) do not count toward the buffer, so one instance holds at most
   60 waiting + 30 running jobs.
5. On startup, jobs left `queued`/`running` by a previous process are marked `failed` with code `interrupted`;
   on shutdown, unfinished jobs are marked `failed` with `server_shutdown`.

### Image size policy (no server-side downsampling)

The server **never resizes** images. If `width × height` exceeds the limit it returns `413 image_too_large`
with a suggested size; the client must downscale and resubmit (see `scripts/ocr_client.py --fit`). The limit
defaults to OvisOCR2's `max_pixels` (2880²), so vLLM does not silently downscale either; the server refuses to
start if `OCR_MAX_PIXELS` is set above the handler limit.

### API

#### `POST /v1/ocr` — submit a job

`multipart/form-data`:

| Field | Required | Description |
|---|---|---|
| `image` | yes | PNG, JPEG, WEBP, BMP, TIFF or GIF (first frame). Stored byte-for-byte. |
| `prompt` | no | Overrides the handler's default prompt (OvisOCR2's document-parsing prompt). |

```bash
curl -F image=@page.png http://localhost:8080/v1/ocr
curl -F image=@formula.png -F prompt="Convert the formula to LaTeX." http://localhost:8080/v1/ocr
```

`202 Accepted` (also `Location: /v1/jobs/{job_id}`):

```json
{
  "job_id": "4f1c0f2a9b8e4d47a1d1f0e3c2b4a596",
  "status": "queued",
  "created_at": "2026-10-07T10:15:02.123Z",
  "links": {
    "self": "/v1/jobs/4f1c0f2a9b8e4d47a1d1f0e3c2b4a596",
    "artifacts": "/v1/jobs/4f1c0f2a9b8e4d47a1d1f0e3c2b4a596/artifacts"
  }
}
```

#### `GET /v1/jobs/{job_id}` — job status

Returns the job's `job.json` (below) plus `links`. `status` is `queued` → `running` → `succeeded` | `failed`.

#### `GET /v1/jobs/{job_id}/artifacts` — download artifacts

Once the job is `succeeded` or `failed`: `200 application/zip` named `{job_id}.zip`, every entry
**deflate-compressed** and stored under a top-level `{job_id}/` folder (so unzipping produces the job folder).
Before that: `409 job_not_finished`.

#### `GET /v1/info`, `GET /healthz`

`/v1/info` reports version, handler description, backend URL/model, limits (`max_pixels`, `max_upload_bytes`,
`max_prompt_chars`, `supported_formats`), queue statistics (`buffered`, `active`, `peak_active`,
`batches_dispatched`, `max_buffer`, `max_concurrency`, `batch_max_size`, `batch_window_seconds`) and the default
prompt. `/healthz` returns `{"status": "ok"}`.

#### Errors

All errors use one envelope: `{"error": {"code": "...", "message": "...", ...details}}`.

| HTTP | `code` | When / extra fields |
|---|---|---|
| 400 | `invalid_image` | Upload is not a decodable image. |
| 400 | `empty_image` | Empty upload. |
| 400 | `prompt_too_long` | `max_prompt_chars`. |
| 404 | `job_not_found` | Unknown or malformed job id. |
| 409 | `job_not_finished` | Artifacts requested before completion; `status`. |
| 413 | `image_too_large` | `width`, `height`, `pixels`, `max_pixels`, `suggested_width`, `suggested_height`. |
| 413 | `file_too_large` | Upload exceeds `max_upload_bytes`. |
| 415 | `unsupported_image_format` | Decodable but not an accepted format. |
| 422 | `invalid_request` | Missing `image` field, etc. |
| 503 | `buffer_full` | `buffered`, `max_buffer`; `Retry-After` header. |

Job-level failures (in `job.json` → `error.code`): `backend_error` (non-2xx from backend, with
`backend_status`), `backend_unreachable`, `invalid_backend_response`, `internal_error`, `server_shutdown`,
`interrupted`.

### Artifact folder layout

```
artifacts/<job_id>/
├── job.json          # job metadata & status (schema below)
├── input.png         # original upload, byte-identical (extension follows the detected format)
├── request.json      # exact backend payload (image base64 replaced by a placeholder)
├── response.json     # raw backend chat-completion response
├── result.md         # model markdown after OvisOCR2 post-processing (keeps <img src="images/bbox_…"> tags)
├── result_text.md    # same, with region <img> tags removed (text only)
└── regions.json      # visual regions: name, normalized box, pixel box
```

Failed jobs contain `job.json`, the input image and whatever backend exchange happened.

`job.json`:

```json
{
  "schema_version": 1,
  "job_id": "4f1c…",
  "status": "succeeded",
  "created_at": "…", "started_at": "…", "finished_at": "…",
  "handler": {"name": "ovisocr2", "display_name": "OvisOCR2", "default_model": "ATH-MaaS/OvisOCR2",
              "bbox_scale": 1000, "min_pixels": 200704, "max_pixels": 8294400, "…": "…"},
  "backend": {"url": "http://…/v1", "model": "ATH-MaaS/OvisOCR2"},
  "prompt": null,
  "prompt_used": "\nYou are an AI assistant specialized in converting PDF images to Markdown format. …",
  "input": {"filename": "input.png", "original_filename": "page.png", "content_type": "image/png",
            "format": "PNG", "mime": "image/png", "width": 1000, "height": 1100, "bytes": 31337,
            "sha256": "…"},
  "batch": {"batch_id": "a1b2c3d4e5f6", "size": 3, "dispatched_at": "…", "waited_seconds": 2.001},
  "result": {"finish_reason": "stop", "truncated": false,
             "usage": {"prompt_tokens": 1181, "completion_tokens": 247, "total_tokens": 1428},
             "region_count": 1, "warnings": []},
  "error": null,
  "files": ["job.json", "input.png", "request.json", "response.json", "result.md", "result_text.md",
            "regions.json"]
}
```

`regions.json`:

```json
[{"left": 94, "top": 592, "right": 715, "bottom": 935, "ref": "images/bbox_94_592_715_935.jpg",
  "pixels": [94, 651, 715, 1028]}]
```

### Handlers

`OCRHandler` (`src/ocr_server/handlers/base.py`) isolates everything model-specific:

| Member | Purpose |
|---|---|
| `name`, `default_model`, `default_prompt`, `min_pixels`, `max_pixels`, `bbox_scale` | Identity and limits. |
| `build_payload(*, model, image_url, prompt=None)` | Chat-completions request body (`image_url` is a `data:` URL). |
| `parse_response(response)` → `OCRResult` | Markdown, text-only markdown, regions, finish reason, usage, warnings. |
| `describe()` | Metadata stored in `job.json` / `/v1/info`. |

`OvisOCR2Handler` follows the model card's sample code: image part then the prompt text, `temperature=0`,
`max_tokens=16384`, `chat_template_kwargs={"enable_thinking": false}`,
`mm_processor_kwargs.images_kwargs = {min_pixels: 448², max_pixels: 2880²}`; output is stripped, passed through
`clean_truncated_repeats`, and region tags `<img src="images/bbox_L_T_R_B.jpg" />` (coordinates normalized to
0–1000) are extracted and mapped to pixels with the card's `round(v × size / 1000)` + clamp.

To support another model, subclass `OCRHandler`, set a unique `name`, decorate the class with
`@register_handler` and import its module in `handlers/__init__.py`; select it with `OCR_HANDLER=<name>`.

### Reference client

```bash
pip install httpx pillow
python scripts/ocr_client.py page1.png page2.jpg --server http://localhost:8080 --out downloads --extract
python scripts/ocr_client.py huge_scan.png --fit   # downscale client-side to the server's max_pixels
```

It submits all images first (honouring `Retry-After` on `503`), then polls, downloads and optionally extracts.

---

## 2. Artifact viewer (local web app)

```bash
pip install ".[viewer]"
ocr-viewer downloads/ /other/artifacts/ --port 8765 --open
```

* Accepts any number of folders; each is searched (up to `--max-depth`, default 3) for job folders
  containing `job.json` (a job folder itself may be passed). Server artifact dirs and unzipped downloads both work.
* **Job list:** paged (10/20/50/100 per page), newest first, status filter and search (job id / file name / text).
* **Job view:** side-by-side panels.
  * Left: original image with **zoom** (wheel at cursor, `+`/`-`, buttons, double-click), **pan** (drag),
    *Fit* / *1:1*, and optional region outlines.
  * Right: toggle between **Rendered** markdown (tables, KaTeX math when the CDN is reachable) and **Edit**
    (plain-text editor). *Save* (Ctrl+S) writes `result.edited.md` next to the original — `result.md` is never
    modified; *Revert* deletes the edit.
  * Region images referenced as `images/bbox_L_T_R_B.jpg` are **cropped from the original image on the fly**
    (`GET /api/jobs/{key}/images/bbox_….jpg`) using the same coordinate mapping as the model card. Clicking a
    rendered figure highlights and centers its region on the page image.
* Markdown is rendered server-side with `markdown-it-py` and sanitized with `nh3`.

### Submitting new images from the viewer

```bash
ocr-viewer downloads/ --server-url http://ocr-host:8080 --inbox ./ocr-submissions --port 8765 --open
```

A sidebar menu switches between **Jobs** (`#/jobs`, the artifact list), **New OCR** (`#/new`) and
**Submissions** (`#/submissions`); the last two are shown only when submissions are enabled.

**New OCR** – prepare and send images:

* **Server URL** – prefilled from `--server-url` (or `OCR_SERVER_URL`), editable in the page (remembered in the
  browser's localStorage, *Reset* restores the default). *Check* calls `GET {url}/v1/info` through the viewer and
  shows the handler, pixel limit and queue state.
* **Import images** – file picker or drag & drop; several images can be queued, each with its own adjustments and
  box. Each image is uploaded once to the viewer (`POST /api/sources`) and previewed from there, so formats the
  browser cannot display (e.g. TIFF) work too.
* **Zoom & pan** – mouse wheel (at the cursor), `+`/`-`/`0` (fit)/`1` (1:1) or the toolbar buttons; pan with the
  *Pan* tool, the middle mouse button, or by holding Space while dragging.
* **Adjustments** – *Grayscale*, *Black & white* (threshold 0–255, pixels ≥ threshold become white, implies
  grayscale) and *Rotation* in degrees clockwise with **0.1° steps** (number field, ±0.1° and ±90° buttons,
  slider; normalized to (-180°, 180°]). The canvas expands to fit the rotated image and the corners are filled
  white. The preview is rendered by the viewer backend with Pillow – the exact pixels that will be submitted – so
  the box always matches what is sent. Changing the rotation clears the box.
* **Boxed region** – drag on the image to draw a box (in pixels of the *adjusted* image); only that region is sent
  for OCR. *Clear box* sends the whole image. A warning is shown when the region exceeds the server's
  `max_pixels`; the viewer never downsamples – the server's 413 is shown as-is.
* **Keep for viewing** – `original` (default): the full (adjusted, uncropped) image is kept and the OCR region is
  drawn as a dashed frame on it; region images (`bbox_….jpg`) are cropped from it at the right offset.
  `cropped`: only the cropped image is kept (exactly what the model saw). Without a box both are the same.

**Submissions** – a paged table (10/20/50/100 per page, newest first) of everything sent from this viewer, with a
status filter (pending / succeeded / failed) and search (job id, file name, resubmitted job id). It refreshes
every second while jobs are pending. For finished jobs (succeeded or failed):

* **Open** – the result in the job view.
* **Resubmit** – submits again with identical settings (same image, adjustments, box, prompt, server).
* **Redraw** – reopens the image in *New OCR* with its adjustments, box, prompt and kept-image option prefilled, so
  the box can be redrawn (or adjustments changed) before submitting. The job view has the same *Redraw &
  resubmit* button, which also works for jobs that were not submitted through this viewer (their displayed image is
  used as the source).

New jobs reference the job they replace (`resubmit_of`); the old result is kept.

How it works: imported images are stored in `{inbox}/.sources/{source_id}.{ext}` (+ `.json` metadata); sources
never submitted are removed after 24 h, submitted ones are kept for redraw/resubmit. On submit the viewer
backend applies the adjustments and crops losslessly (PNG; the original bytes are sent unchanged when there are
no adjustments and no box), POSTs to `{server}/v1/ocr`, and follows the job in the background
(`GET /v1/jobs/{id}`; transient connection errors are retried with backoff). When the job finishes (succeeded or
failed) the zip is downloaded, validated (all entries must be under `{job_id}/`) and extracted to
`{inbox}/{job_id}/`; the inbox is also a viewer root, so the job appears in the job list. Submission records
are stored in `{inbox}/.submissions/{job_id}.json` and pending ones are resumed after a viewer restart. If the
source of an old submission is gone, *Redraw*/*Resubmit* fall back to the image kept in the job folder.

The viewer writes `viewer.json` into each imported job folder:

```json
{
  "schema_version": 1, "source": "ocr-viewer", "server_url": "http://ocr-host:8080",
  "submitted_at": "2025-01-01T00:00:00.000Z", "original_filename": "scan.png",
  "persisted_image": "original", "image": "original.png",
  "crop": {"box": [50, 55, 800, 1045], "source_width": 1000, "source_height": 1100},
  "region_frame": [50, 55, 800, 1045],
  "adjustments": {"rotation": 0.5, "grayscale": true, "threshold": null},
  "source_id": "3f1c…", "resubmit_of": null
}
```

With `persisted_image: "original"` and a crop, `input.*` (the cropped upload) is replaced by `original.<ext>`
(the adjusted full image); `job.json` from the server is left untouched and still describes the cropped input.
`region_frame` is the offset/size used to map the model's 0–1000 coordinates (relative to the crop) onto the
kept image. `adjustments` is `null` when none were applied. `--inbox` defaults to `./ocr-submissions`;
submission is disabled (404 `submissions_disabled`) when the app is created without an inbox.

Submission API (viewer):

| Endpoint | Description |
|---|---|
| `GET /api/config` | `submissions_enabled`, default `server_url`, `inbox` |
| `GET /api/server/info?url=` | proxied `GET {url}/v1/info` |
| `POST /api/sources` | multipart `image` → 201 `{source_id, filename, format, ext, width, height, size, created_at}` |
| `GET /api/sources/{id}` | source metadata |
| `GET /api/sources/{id}/preview?rotation&grayscale&threshold` | adjusted preview (PNG; original bytes for browser formats without adjustments) |
| `DELETE /api/sources/{id}` | `{deleted: bool}` – `false` when a submission still references it |
| `POST /api/submissions` | multipart/form: `image` **or** `source_id`; optional `server_url`, `prompt`, `box="x1,y1,x2,y2"` (adjusted-image pixels; empty/`full` = whole image), `persist=original\|cropped` (default `cropped`), `rotation`, `grayscale`, `threshold`, `resubmit_of` → 202 submission record |
| `GET /api/submissions?page&page_size&status&q` | `{items, total, page, page_size, pages}`; `status` = `pending\|succeeded\|failed` |
| `GET /api/submissions/{job_id}` | submission record |
| `POST /api/submissions/{job_id}/draft` | editor settings `{source, box, adjustments, persist, prompt, server_url, resubmit_of}` |
| `POST /api/submissions/{job_id}/resubmit` | form overrides (`box` omitted = reuse, `full` = whole image; `server_url`, `prompt`, `persist`, `rotation`, `grayscale`, `threshold`) → 202 new record |
| `POST /api/jobs/{key}/draft` | draft for any job folder |

A submission record contains `job_id`, `server_url`, `state` (`submitted` → `imported` or `error`), `status`
(`pending`/`succeeded`/`failed`), `job_status` (server status), `submitted_at`, `original_filename`,
`submitted_filename`, `prompt`, `persist`, `crop`, `adjustments`, `source_id`, `resubmit_of`, `folder`, `key`
(job-list key once imported), `error`, `last_error`.

Errors use the server's envelope (`{"error": {"code", "message", …}}`): viewer-side `invalid_image`,
`empty_image`, `invalid_box`, `invalid_persist`, `invalid_adjustment`, `invalid_status`, `invalid_resubmit_of`,
`image_required`, `server_url_required`, `invalid_server_url` (400), `source_not_found`, `submission_not_found`
(404), `submission_pending` (409, resubmitting an unfinished job), `source_unavailable` (409), `file_too_large`
(413), `server_unreachable` / `invalid_server_response` (502); errors from the inference server keep their status
(400/413/415/422/503, others → 502) and code, with `source: "inference_server"` and `Retry-After` when present.
Viewer API: `GET /api/jobs?page&page_size&status&q`, `GET /api/jobs/{key}`, `GET /api/jobs/{key}/image`,
`GET /api/jobs/{key}/images/{bbox_name}`, `POST /api/render`, `PUT|DELETE /api/jobs/{key}/markdown`,
`GET /api/roots`.

---

## 3. Mock vLLM endpoint

`uvicorn mock_vllm.app:app --port 8000` serves `/v1/chat/completions`, `/v1/models`, `/health`, `/version`
and `/mock/stats`. It validates the model name and the image data URL like vLLM and returns a real OvisOCR2
response captured from the live server (`mock_vllm/fixtures`). Test hooks: prompt containing `MOCK_FAIL` → 500;
`MOCK_REPEAT` → degenerate repetition; small `max_tokens` → truncated output with `finish_reason: "length"`;
`MOCK_DELAY_SECONDS` adds latency. `/mock/stats` reports request count and peak concurrency.

## 4. CI

`.github/workflows/ci.yml`:

* **test** – Python 3.11/3.12/3.13: `ruff check .` and `pytest` (handler, server API, batching/concurrency/
  buffer limits, viewer API, server→zip→viewer integration), all against the in-process mock.
* **e2e** – starts mock vLLM, the server and the viewer as real processes; `scripts/e2e_submit.py` submits
  jobs (including failure, oversized and invalid images), verifies the deflate zips and extracts them; Playwright
  tests drive the viewer (paging, side-by-side render, region crops, zoom/pan, edit/save/revert). A second viewer
  (`--server-url` pointing at the real server) is driven through *New OCR* and *Submissions*: sidebar
  navigation, custom server URL, zoom/pan, grayscale/B&W/0.1° rotation, boxed region with original vs. cropped
  image kept, whole-image submission, status filter, resubmit and redraw of failed/succeeded jobs. Screenshots,
  logs and artifacts are uploaded as the `e2e-output` workflow artifact.
* **build** – sdist/wheel (uploaded as `dist`), wheel install smoke test, Docker image build and smoke run.
