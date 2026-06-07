"""
modules/spatial_analysis.py — Core geospatial computation pipeline.

Runs three analyses in parallel over the user's polygon:
  1. Forest Cover Map  (FCM) — canopy class breakdown
  2. Digital Elevation Model (DEM) — elevation + slope statistics
  3. Area calculation — accurate geodesic area in hectares

Everything is structured to work within the 512 MB Koyeb free-tier RAM
budget by using windowed / masked array operations, never loading full rasters.
"""

import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as shapely_transform

from config import cfg, FCM_CLASSES
from modules.storage import extract_masked_array

log = logging.getLogger(__name__)


# ── Public entry point ────────────────────────────────────────────────────────

def run_analysis(geojson_feature: dict) -> dict[str, Any]:
    """
    Accepts a single GeoJSON Feature (polygon) and returns a unified
    results dictionary with all computed spatial metrics.

    Parameters
    ----------
    geojson_feature : dict   — GeoJSON Feature with a Polygon geometry

    Returns
    -------
    dict with keys: area_ha, fcm, dem, centroid
    """
    log.info("Starting spatial analysis pipeline …")

    # Run FCM and DEM extractions in parallel (both are I/O bound HTTP calls)
    results: dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_fcm = pool.submit(_analyse_forest_cover, geojson_feature)
        future_dem = pool.submit(_analyse_elevation,    geojson_feature)

        for future in as_completed([future_fcm, future_dem]):
            try:
                key, value = future.result()
                results[key] = value
            except Exception as exc:
                log.error("Analysis sub-task failed: %s", exc, exc_info=True)
                raise

    # Area is CPU-only — run after futures to avoid GIL contention
    results["area_ha"] = _calculate_area_ha(geojson_feature)
    results["centroid"] = _get_centroid(geojson_feature)

    log.info("Analysis complete → area=%.2f ha", results["area_ha"])
    return results


# ── Sub-analysis functions ────────────────────────────────────────────────────

def _analyse_forest_cover(geojson_feature: dict) -> tuple[str, dict]:
    """
    Returns pixel-count breakdown of FSI Forest Cover classes
    within the polygon as percentages.
    """
    masked, _ = extract_masked_array(
        cfg.COG_FCM, geojson_feature, band=1, nodata=255
    )

    total_valid = masked.count()
    if total_valid == 0:
        return "fcm", {"error": "No valid pixels in extent", "classes": {}}

    class_stats: dict[str, dict] = {}
    flat = masked.compressed()   # 1-D array of valid values only

    for class_val, class_name in FCM_CLASSES.items():
        count = int(np.sum(flat == class_val))
        pct   = round((count / total_valid) * 100, 2) if total_valid > 0 else 0.0
        if count > 0:
            class_stats[class_name] = {
                "pixel_count":   count,
                "percentage":    pct,
                "class_id":      class_val,
            }

    # Dominant class (excluding Water/NoData class 0)
    forest_classes = {k: v for k, v in class_stats.items() if "Water" not in k}
    dominant = max(forest_classes, key=lambda k: forest_classes[k]["pixel_count"]) \
               if forest_classes else "Non-Forest"

    return "fcm", {
        "classes":  class_stats,
        "dominant": dominant,
        "total_valid_pixels": total_valid,
    }


def _analyse_elevation(geojson_feature: dict) -> tuple[str, dict]:
    """
    Computes min / max / mean elevation and mean slope inside the polygon.
    Slope is derived via finite-difference gradient on the DEM window.
    """
    masked, meta = extract_masked_array(
        cfg.COG_DEM, geojson_feature, band=1, nodata=-9999
    )

    if masked.count() == 0:
        return "dem", {"error": "No valid pixels in extent"}

    valid = masked.compressed().astype(np.float32)

    # Pixel resolution in metres (approximate for UTM/metric DEM)
    res_x = abs(meta["transform"].a)
    res_y = abs(meta["transform"].e)

    # Slope computation from the full masked 2-D grid (not compressed)
    data_2d = masked.filled(np.nan).astype(np.float32)
    dy, dx = np.gradient(data_2d, res_y, res_x)
    slope_rad = np.arctan(np.sqrt(dx**2 + dy**2))
    slope_deg = np.degrees(slope_rad)
    # Mask slope where DEM was nodata
    slope_valid = slope_deg[~masked.mask] if masked.mask is not np.ma.nomask \
                  else slope_deg.ravel()

    return "dem", {
        "elevation_min_m":  round(float(valid.min()),  1),
        "elevation_max_m":  round(float(valid.max()),  1),
        "elevation_mean_m": round(float(valid.mean()), 1),
        "slope_mean_deg":   round(float(slope_valid.mean()), 1) if len(slope_valid) else 0.0,
        "slope_max_deg":    round(float(slope_valid.max()),  1) if len(slope_valid) else 0.0,
    }


# ── Area calculation ──────────────────────────────────────────────────────────

def _calculate_area_ha(geojson_feature: dict) -> float:
    """
    Accurate geodesic area in hectares using UTM re-projection.
    UTM Zone 44N (EPSG:32644) is appropriate for Madhya Pradesh / Chhattisgarh.
    """
    geom_wgs84 = shape(geojson_feature["geometry"])

    transformer = Transformer.from_crs(
        cfg.TARGET_CRS, cfg.AREA_CRS, always_xy=True
    )
    geom_utm = shapely_transform(transformer.transform, geom_wgs84)
    area_m2   = geom_utm.area
    return round(area_m2 / 10_000, 4)   # 1 hectare = 10,000 m²


def _get_centroid(geojson_feature: dict) -> tuple[float, float]:
    """Returns (longitude, latitude) of the polygon centroid."""
    geom = shape(geojson_feature["geometry"])
    c    = geom.centroid
    return (round(c.x, 6), round(c.y, 6))




# ── File ingestion helpers ────────────────────────────────────────────────────

def load_vector_file(file_path: str | Path) -> dict:
    """
    Reads any supported vector format (.kml, .gpkg, .geojson) via GeoPandas
    and returns a single normalised GeoJSON Feature (first polygon layer).

    Raises
    ------
    ValueError : if the file contains no polygon geometry
    """
    # 🚀 FORCE ENABLE: Tell fiona's global registry to allow KML files
    import fiona
    fiona.drvsupport.supported_drivers['KML'] = 'r'
    fiona.drvsupport.supported_drivers['LIBKML'] = 'r'

    path = Path(file_path)
    suffix = path.suffix.lower()

    driver_map = {".kml": "KML", ".gpkg": "GPKG", ".geojson": None, ".json": None}
    if suffix not in driver_map:
        raise ValueError(
            f"Unsupported file format: '{suffix}'. "
            "Please upload a .geojson, .gpkg, or .kml file."
        )

    kwargs = {}
    if driver_map[suffix]:
        kwargs["driver"] = driver_map[suffix]

    gdf: gpd.GeoDataFrame = gpd.read_file(str(path), **kwargs)
  
    # Keep only polygon-type geometries
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    if gdf.empty:
        raise ValueError(
            "The uploaded file contains no polygon geometry. "
            "Please provide a polygon layer (not points or lines)."
        )

    # Re-project to WGS-84 if needed
    if gdf.crs is None:
        log.warning("Input file has no CRS — assuming WGS-84.")
        gdf = gdf.set_crs(cfg.TARGET_CRS)
    elif gdf.crs.to_string() != cfg.TARGET_CRS:
        gdf = gdf.to_crs(cfg.TARGET_CRS)

    # Dissolve all features into a single polygon (union)
    dissolved = gdf.dissolve()
    feature   = dissolved.__geo_interface__["features"][0]

    log.info(
        "Loaded vector file '%s'  |  %d source feature(s) dissolved to 1",
        path.name, len(gdf)
    )
    return feature
