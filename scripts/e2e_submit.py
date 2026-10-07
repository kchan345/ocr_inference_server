"""End-to-end check against running mock vLLM + inference server processes (used by CI).

Submits several jobs, verifies validation errors, async acceptance, artifact zips, and extracts the
artifacts into a folder that the viewer UI tests then browse.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx
from ocr_client import OCRServerError, Upload, download, extract, get_info, load_upload, submit, wait
from PIL import Image, ImageDraw


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"E2E CHECK FAILED: {message}")
    print(f"ok - {message}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--extract-to", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=args.server, timeout=60) as client:
        info = get_info(client)
        max_pixels = info["limits"]["max_pixels"]
        print(json.dumps(info["queue"], indent=2))

        sample = load_upload(args.image)
        jobs: dict[str, str] = {}

        first = submit(client, sample)
        status = client.get(f"/v1/jobs/{first['job_id']}").json()["status"]
        check(status in ("queued", "running"), f"request accepted asynchronously (status right after submit: {status})")
        jobs[first["job_id"]] = "sample"
        for _ in range(2):
            jobs[submit(client, sample)["job_id"]] = "sample"
        jobs[submit(client, sample, prompt="MOCK_FAIL")["job_id"]] = "fail"

        big = Image.new("RGB", (3000, 3000), "white")
        ImageDraw.Draw(big).text((100, 100), "Large scan", fill="black")
        big_path = args.out / "large_scan.png"
        big.save(big_path)
        try:
            submit(client, load_upload(big_path))
            check(False, "oversized image must be rejected")
        except OCRServerError as exc:
            check(exc.status_code == 413 and exc.code == "image_too_large", f"oversized image rejected: {exc}")
        fitted = load_upload(big_path, max_pixels)
        with Image.open(io.BytesIO(fitted.data)) as img:
            check(img.width * img.height <= max_pixels, f"client downscaled to {img.width}x{img.height}")
        jobs[submit(client, fitted)["job_id"]] = "fitted"

        try:
            submit(client, Upload(b"not an image", "bad.png", "image/png"))
            check(False, "invalid image must be rejected")
        except OCRServerError as exc:
            check(exc.status_code == 400 and exc.code == "invalid_image", f"invalid image rejected: {exc}")

        sample_sha = hashlib.sha256(sample.data).hexdigest()
        for job_id, kind in jobs.items():
            meta = wait(client, job_id, timeout=120, interval=0.5)
            expected = "failed" if kind == "fail" else "succeeded"
            check(meta["status"] == expected, f"{job_id} ({kind}) finished as {meta['status']}")
            check(bool(meta.get("batch")), f"{job_id} dispatched in batch {meta['batch'] and meta['batch']['batch_id']}")
            zip_path = download(client, job_id, args.out)
            with zipfile.ZipFile(zip_path) as zf:
                infos = zf.infolist()
                check(all(i.compress_type == zipfile.ZIP_DEFLATED for i in infos), f"{zip_path.name} is deflated")
                names = {i.filename for i in infos}
                input_name = f"{job_id}/{meta['input']['filename']}"
                check(input_name in names, f"{zip_path.name} contains the original input image")
                if kind == "sample":
                    check(hashlib.sha256(zf.read(input_name)).hexdigest() == sample_sha, "input image is byte-identical")
                if expected == "succeeded":
                    check("Quarterly Sales Report" in zf.read(f"{job_id}/result.md").decode(), "result.md has OCR text")
            extract(zip_path, args.extract_to)

        queue = get_info(client)["queue"]
        check(queue["buffered"] == 0 and queue["active"] == 0, "server queue drained")
        check(queue["peak_active"] <= queue["max_concurrency"], "backend concurrency limit respected")

    (args.out / "jobs.json").write_text(json.dumps(jobs, indent=2))
    print(f"E2E submit OK: {len(jobs)} jobs extracted into {args.extract_to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
