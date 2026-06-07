"""
modules/storage.py — Supabase Storage helpers for Cloud-Optimized GeoTIFF access.

Key design: we NEVER download the full raster.  Instead we use rasterio's
VSICURL driver + COG windowing to stream only the pixel blocks that overlap
the user's polygon bounding box — this keeps memory usage well under the
512 MB Koyeb free-tier limit even for nation-wide rasters.
"""

import logging
import os
import sys
from contextlib import contextmanager
from typing import Generator

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.mask import mask as rio_mask
from shapely.geometry import shape, mapping
from supabase import create_client, Client

from config import cfg

# ── Force Stream / Unbuffered Stdout Logging Setup for Koyeb Console ─────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(stdout_handler)

# ── Supabase client (singleton) ───────────────────────────────────────────────
_sb: Client | None = None


def _get_supabase() -> Client:
    """Safely initializes and extracts the singleton Supabase storage client."""
    global _sb
    if _sb is None:
        if not cfg.SUPABASE_URL or not cfg.SUPABASE_KEY:
            log.critical("❌ Missing Supabase credentials in environment config parameters.")
            sys.stdout.flush()
            raise ValueError("Invalid Supabase environment configuration settings detected.")
        try:
            # Strip out possible quote mark inclusions or accidental spacing blocks
            clean_url = str(cfg.SUPABASE_URL).strip().strip('"').strip("'")
            clean_key = str(cfg.SUPABASE_KEY).strip().strip('"').strip("'")
            
            _sb = create_client(clean_url, clean_key)
            log.info("✅ Supabase Client initialization successful.")
            sys.stdout.flush()
        except Exception as e:
            log.error(f"❌ Failed to build Supabase client target structure: {str(e)}")
            sys.stdout.flush()
            raise
    return _sb


# ── Signed URL factory (1-hour expiry) ───────────────────────────────────────

def get_signed_url(object_name: str, expires_in: int = 3600) -> str:
    """
    Generate a time-limited signed URL for a private Supabase object.
    rasterio will use this URL via its VSICURL driver.
    """
    sb = _get_supabase()
    try:
        response = sb.storage.from_(cfg.SUPABASE_BUCKET).create_signed_url(
            object_name, expires_in
        )
        if not response or "signedURL" not in response:
            raise KeyError(f"Response dictionary map missing 'signedURL' key element.")
        
        url: str = response["signedURL"]
        log.info("🔗 Signed URL successfully generated for COG storage layer: %s", object_name)
        sys.stdout.flush()
        return url
    except Exception as err:
        log.error(f"❌ Error generating signed storage URL connection for target [{object_name}]: {str(err)}")
        sys.stdout.flush()
        raise


# ── Windowed raster reader ────────────────────────────────────────────────────

@contextmanager
def open_cog(object_name: str) -> Generator[rasterio.DatasetReader, None, None]:
    """
    Context manager that opens a COG from Supabase via VSICURL without
    downloading the entire file.  The rasterio GDAL environment is configured
    to use HTTP range requests (the COG mechanism).
    """
    url = get_signed_url(object_name)
    vsicurl_path = f"/vsicurl/{url}"

    env = rasterio.Env(
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        GDAL_HTTP_MULTIPLEX="YES",
        GDAL_HTTP_VERSION=2,
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    )
    with env:
        with rasterio.open(vsicurl_path) as src:
            log.info(
                "🌍 Opened COG %s  |  CRS: %s  |  Shape: %s x %s",
                object_name, src.crs, src.height, src.width,
            )
            sys.stdout.flush()
            yield src


# ── Windowed extraction functions ─────────────────────────────────────────────

def extract_masked_array(
    object_name: str,
    polygon_geojson: dict,
    band: int = 1,
    nodata: float | None = None,
) -> tuple[np.ma.MaskedArray, dict]:
    """
    Stream only the pixels that fall inside *polygon_geojson* from the COG.

    Returns
    -------
    masked_arr : np.ma.MaskedArray  — pixel values inside the polygon
    meta       : dict               — rasterio profile of the windowed dataset
    """
    geom = shape(polygon_geojson["geometry"])

    with open_cog(object_name) as src:
        raster_crs = src.crs
        if raster_crs != CRS.from_string(cfg.TARGET_CRS):
            from rasterio.warp import transform_geom
            geom_repr = transform_geom(
                cfg.TARGET_CRS, raster_crs.to_string(), mapping(geom)
            )
        else:
            geom_repr = mapping(geom)

        try:
            out_image, out_transform = rio_mask(
                src,
                [geom_repr],
                crop=True,
                indexes=band,
                nodata=nodata if nodata is not None else src.nodata,
                all_touched=True,
            )
        except ValueError as exc:
            log.error(f"❌ Geolocation masking overlap error for target layer [{object_name}].")
            sys.stdout.flush()
            raise ValueError(
                f"Polygon does not overlap raster layer '{object_name}'. "
                "Ensure the polygon is within the study area extent."
            ) from exc

        meta = src.meta.copy()
        
        # 🚀 FIXED: Extracted array coordinate shape indices safely to prevent window data clipping bugs
        meta.update(
            {
                "driver":    "GTiff",
                "height":    out_image.shape if len(out_image.shape) == 3 else out_image.shape,
                "width":     out_image.shape if len(out_image.shape) == 3 else out_image.shape,
                "transform": out_transform,
                "count":     1,
            }
        )

        _nodata = nodata if nodata is not None else src.nodata
        if _nodata is not None:
            masked = np.ma.masked_equal(out_image, _nodata)
        else:
            masked = np.ma.array(out_image)

        log.info(
            "📊 Extracted %d valid pixel cells securely from %s",
            masked.count(), object_name
        )
        sys.stdout.flush()
        return masked, meta
        
