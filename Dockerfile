# ─────────────────────────────────────────────────────────────────────────────
# SDSS Telegram Bot — Dockerfile
# Target: Koyeb Free Web Service
# Python 3.11
# HarfBuzz + Raqm enabled mplcairo build
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Build dependencies ──────────────────────────────────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        libspatialindex-dev \
        pkg-config \
        libcairo2-dev \
        libfreetype6-dev \
        libharfbuzz-dev \
        libfribidi-dev \
        libraqm-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /build

COPY requirements.txt .

RUN pip install --upgrade pip setuptools wheel


# Install remaining requirements (excluding mplcairo to avoid wheel overwrite)
RUN PIP_NO_BINARY=mplcairo \
    pip install \
    --no-cache-dir \
    --prefix=/install \
    -r requirements.txt

# ── Stage 2: Runtime image ───────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        libspatialindex-dev \
        libcairo2 \
        libfreetype6 \
        libharfbuzz0b \
        libfribidi0 \
        libraqm0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled Python packages
COPY --from=builder /install /usr/local

# GDAL tuning for remote raster access
ENV GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_VERSION=2 \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.tiff \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .

RUN mkdir -p /tmp/sdss

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

CMD ["python", "main.py"]
