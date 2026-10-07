"""Mock of a vLLM OpenAI-compatible server hosting ATH-MaaS/OvisOCR2.

It validates requests the way vLLM does (model name, decodable image) and returns the
real OvisOCR2 output captured from a live server for ``tests/fixtures/sample.png``.

Test hooks (substrings in the prompt text):

* ``MOCK_FAIL``   -> HTTP 500 error response
* ``MOCK_REPEAT`` -> appends a long degenerate repeated tail to the output

``max_tokens`` smaller than the canned completion length truncates the output with
``finish_reason="length"``. ``MOCK_DELAY_SECONDS`` adds latency to every completion.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image

FIXTURE = Path(__file__).with_name("fixtures") / "ovisocr2_sample_response.json"


def _error(status: int, type_: str, message: str, param: str | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": type_, "param": param, "code": status}}, status)


def create_mock_app(*, model: str | None = None, delay: float | None = None) -> FastAPI:
    canned: dict[str, Any] = json.loads(FIXTURE.read_text("utf-8"))
    app = FastAPI(title="Mock vLLM (OvisOCR2)")
    app.state.model = model or os.environ.get("MOCK_MODEL") or canned["model"]
    app.state.delay = float(os.environ.get("MOCK_DELAY_SECONDS", "0")) if delay is None else delay
    app.state.requests = []
    app.state.inflight = 0
    app.state.peak_inflight = 0

    @app.get("/health")
    async def health() -> Response:
        return Response(status_code=200)

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": "mock"}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [{"id": app.state.model, "object": "model", "owned_by": "vllm", "max_model_len": 262144}],
        }

    @app.get("/mock/stats")
    async def stats() -> dict[str, Any]:
        return {"requests": len(app.state.requests), "inflight": app.state.inflight,
                "peak_inflight": app.state.peak_inflight}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "BadRequestError", "Request body is not valid JSON.")
        app.state.requests.append(body)
        del app.state.requests[:-200]

        if body.get("model") != app.state.model:
            return _error(404, "NotFoundError", f"The model `{body.get('model')}` does not exist.", "model")

        texts: list[str] = []
        image_urls: list[str] = []
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
                continue
            for part in content or []:
                if part.get("type") == "image_url":
                    image_urls.append((part.get("image_url") or {}).get("url", ""))
                elif part.get("type") == "text":
                    texts.append(part.get("text", ""))
        for url in image_urls:
            if not url.startswith("data:") or "," not in url:
                return _error(400, "BadRequestError", "The mock server only supports base64 data: URLs.")
            try:
                raw = base64.b64decode(url.split(",", 1)[1], validate=True)
                with Image.open(io.BytesIO(raw)) as img:
                    img.load()
            except Exception:
                return _error(400, "BadRequestError",
                              "Failed to load image: cannot identify image file <_io.BytesIO object>")
        prompt = "\n".join(texts)

        app.state.inflight += 1
        app.state.peak_inflight = max(app.state.peak_inflight, app.state.inflight)
        try:
            if app.state.delay:
                await asyncio.sleep(app.state.delay)
        finally:
            app.state.inflight -= 1

        if "MOCK_FAIL" in prompt:
            return _error(500, "InternalServerError", "Mock failure requested by prompt.")

        response = copy.deepcopy(canned)
        choice = response["choices"][0]
        content: str = choice["message"]["content"]
        completion_tokens = response["usage"]["completion_tokens"]
        finish_reason = "stop"
        if "MOCK_REPEAT" in prompt:
            content = content + "\n\n" + "abcde" * 2000
        max_tokens = body.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens < completion_tokens:
            content = content[: max_tokens * 4]
            completion_tokens = max_tokens
            finish_reason = "length"

        response["id"] = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        response["created"] = int(time.time())
        response["model"] = app.state.model
        choice["message"]["content"] = content
        choice["finish_reason"] = finish_reason
        prompt_tokens = response["usage"]["prompt_tokens"]
        response["usage"].update(
            completion_tokens=completion_tokens, total_tokens=prompt_tokens + completion_tokens
        )
        return response

    return app


app = create_mock_app()
