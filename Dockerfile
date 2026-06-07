# ─────────────────────────────────────────────────────────────────────────────
# SDSS Telegram Bot — Dockerfile
# Target: Koyeb Free Web Service (512 MB RAM, Ubuntu 22.04 base)
#
# Strategy: Use python:3.11-slim (Debian-based) and install GDAL from the
# UbuntuGIS PPA so rasterio can compile/link against native C libraries.
# Multi-stage build keeps the final image lean.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Build dependencies ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

# GDAL headers needed to compile rasterio wheel
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        libspatialindex-dev \
        pkg-config \
        git \
    && rm -rf /var/lib/apt/lists/*

# Pin GDAL Python binding version to match system GDAL
ENV GDAL_VERSION=3.4.1
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /build
COPY requirements.txt .

RUN pip install --upgrade pip wheel && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Final runtime image ──────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime GDAL shared libraries only (no headers)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal30 \
        libgeos-c1v5 \
        libproj22 \
        libspatialindex6 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder stage
COPY --from=builder /install /usr/local

# Set GDAL environment for rasterio VSICURL (HTTP range requests)
ENV GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_VERSION=2 \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.tiff \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy application source
COPY . .

# Create temp directory for file downloads
RUN mkdir -p /tmp/sdss

# Koyeb free tier: single-process, long-polling (no webhook server required)
# Health-check endpoint is provided by PTB's built-in health mechanism.
# Koyeb will restart the container if the process exits (crash recovery).
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

CMD ["python", "main.py"]
