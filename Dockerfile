FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        aria2 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install .

COPY config.yaml config.laptop.yaml config.docker.yaml config.docker.laptop.yaml ./
COPY scripts ./scripts

RUN mkdir -p /data/mirror /data/state /data/logs \
    && useradd --create-home --uid 1000 crawler \
    && chown -R crawler:crawler /app /data

USER crawler
WORKDIR /app

VOLUME ["/data/mirror", "/data/state", "/data/logs"]

ENTRYPOINT ["python", "-m", "ddosecrets_crawler"]
CMD ["status", "-c", "/app/config.docker.yaml"]