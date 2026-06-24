─────────────────────────────────────────────────────────────────────────────

SDSS Telegram Bot — Dockerfile

Koyeb / Python 3.11

HarfBuzz + Raqm enabled mplcairo build

─────────────────────────────────────────────────────────────────────────────

── Stage 1 : Builder ────────────────────────────────────────────────────────

FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends 
build-essential 
pkg-config 
git 
gdal-bin 
libgdal-dev 
libgeos-dev 
libproj-dev 
libspatialindex-dev 
libcairo2-dev 
libfreetype6-dev 
libharfbuzz-dev 
libfribidi-dev 
libraqm-dev 
&& rm -rf /var/lib/apt/lists/*

ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /build

COPY requirements.txt .

RUN pip install --upgrade pip setuptools wheel

Force mplcairo source compilation against HarfBuzz/Raqm

RUN pip install 
--no-binary=mplcairo 
--prefix=/install 
mplcairo==0.6.1

Install everything else

IMPORTANT: remove mplcairo line from requirements.txt

RUN grep -vi "^mplcairo" requirements.txt > requirements_no_mplcairo.txt && 
pip install 
--no-cache-dir 
--prefix=/install 
-r requirements_no_mplcairo.txt

── Stage 2 : Runtime ────────────────────────────────────────────────────────

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends 
gdal-bin 
libgeos-c1v5 
libproj25 
libspatialindex6 
libcairo2 
libfreetype6 
libharfbuzz0b 
libfribidi0 
libraqm0 
curl 
&& rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

ENV GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES 
GDAL_HTTP_MULTIPLEX=YES 
GDAL_HTTP_VERSION=2 
CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.tiff 
GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR 
PYTHONDONTWRITEBYTECODE=1 
PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .

RUN mkdir -p /tmp/sdss

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 
CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

CMD ["python", "main.py"]
