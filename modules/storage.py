"""
modules/storage.py — Supabase Storage helpers for Cloud-Optimized GeoTIFF access.

Key design: we NEVER download the full raster.  Instead we use rasterio's
VSICURL driver + COG windowing to stream only the pixel blocks that overlap
the user's polygon bounding box — this keeps memory usage well under the
512 MB Koyeb free-tier limit even for nation-wide rasters.
"""

import logging
import os
from contextlib import contextmanager
from typing import Generator

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.mask import mask as rio_mask
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
from shapely.geometry import shape, mapping
from supabase import create_client, Client

from config import cfg

log = logging.getLogger(__name__)

# ── Supabase client (singleton) ───────────────────────────────────────────────
_sb: Client | None = None


def _get_supabase() -> Client:
    global _sb
    if _sb is None:
        _sb = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
    return _sb


# ── Signed URL factory (1-hour expiry) ───────────────────────────────────────

def get_signed_url(object_name: str, expires_in: int = 3600) -> str:
    """
    Generate a time-limited signed URL for a private Supabase object.
    rasterio will use this URL via its VSICURL driver.
    """
    sb = _get_supabase()
    response = sb.storage.from_(cfg.SUPABASE_BUCKET).create_signed_url(
        object_name, expires_in
    )
    url: str = response["signedURL"]
    log.debug("Signed URL generated for %s", object_name)
    return url


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
                "Opened COG %s  |  CRS: %s  |  Shape: %s x %s",
                object_name, src.crs, src.height, src.width,
            )
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
        # Re-project polygon to raster CRS for the mask operation
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
            raise ValueError(
                f"Polygon does not overlap raster layer '{object_name}'. "
                "Ensure the polygon is within the study area extent."
            ) from exc

        meta = src.meta.copy()
        meta.update(
            {
                "driver":    "GTiff",
                "height":    out_image.shape[0],
                "width":     out_image.shape[1],
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
            "Extracted %d valid pixels from %s",
            masked.count(), object_name
        )
        return masked, meta
