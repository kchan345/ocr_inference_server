FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OCR_ARTIFACT_DIR=/data/artifacts

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 10001 ocr && mkdir -p /data/artifacts && chown -R ocr /data

USER ocr
VOLUME ["/data/artifacts"]
EXPOSE 8080
CMD ["ocr-server", "--host", "0.0.0.0", "--port", "8080"]
