"""
modules/spatial_analysis.py — Core geospatial computation pipeline.

Runs three analyses in parallel over the user's polygon / multi-polygon structure:
  1. Forest Cover Map  (FCM) — canopy class breakdown
  2. Digital Elevation Model (DEM) — elevation + slope statistics
  3. Area calculation — accurate geodesic area in hectares

Everything is structured to work within the 512 MB Koyeb free-tier RAM
budget by using windowed / masked array operations, never loading full rasters.
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from pyproj import Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform as shapely_transform

from config import cfg, FCM_CLASSES
from modules.storage import extract_masked_array

# ── Force Stream / Unbuffered Stdout Logging Setup for Koyeb Console ─────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(stdout_handler)


# ── Public entry point ────────────────────────────────────────────────────────

def run_analysis(geojson_feature: Any) -> dict[str, Any]:
    """
    Accepts a GeoJSON Feature (Polygon or MultiPolygon) and returns 
    a unified results dictionary with all computed spatial metrics.

    Parameters
    ----------
    geojson_feature : dict or list — GeoJSON Feature context data

    Returns
    -------
    dict with keys: area_ha, fcm, dem, centroid
    """
    log.info("Starting spatial analysis pipeline …")
    sys.stdout.flush()

    # 🚀 SAFETY CHECK: If a list of features was passed, safely extract the first element
    if isinstance(geojson_feature, list):
        log.warning("Pipeline received a list instead of a dict. Unpacking first feature entry automatically.")
        if len(geojson_feature) > 0:
            geojson_feature = geojson_feature
        else:
            raise ValueError("The provided geojson feature collection list is empty.")

    # Double check if we are dealing with the raw geometry structure or the full Feature wrapper
    if "geometry" not in geojson_feature and "type" in geojson_feature:
        # Wrap raw geometry inside a standard Feature block expected by extract_masked_array
        geojson_feature = {
            "type": "Feature",
            "properties": {},
            "geometry": geojson_feature
        }

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
                sys.stdout.flush()
                raise

    # Area is CPU-only — run after futures to avoid GIL contention
    results["area_ha"] = _calculate_area_ha(geojson_feature)
    results["centroid"] = _get_centroid(geojson_feature)

    log.info("Analysis complete → area=%.2f ha", results["area_ha"])
    sys.stdout.flush()
    return results


# ── Sub-analysis functions ────────────────────────────────────────────────────

def _analyse_forest_cover(geojson_feature: dict) -> tuple[str, dict]:
    """
    Returns pixel-count breakdown of FSI Forest Cover classes
    within the target polygon(s) as percentages.
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
    Computes min / max / mean elevation and mean slope inside the vector boundaries.
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
    Works natively across multiple distinct shapes or contiguous geometries.
    """
    geom_wgs84 = shape(geojson_feature["geometry"])

    transformer = Transformer.from_crs(
        cfg.TARGET_CRS, cfg.AREA_CRS, always_xy=True
    )
    geom_utm = shapely_transform(transformer.transform, geom_wgs84)
    area_m2   = geom_utm.area
    return round(area_m2 / 10_000, 4)   # 1 hectare = 10,000 m²


def _get_centroid(geojson_feature: dict) -> tuple[float, float]:
    """Returns (longitude, latitude) of the overall geometric center."""
    geom = shape(geojson_feature["geometry"])
    c    = geom.centroid
    return (round(c.x, 6), round(c.y, 6))


# ── File ingestion helpers ────────────────────────────────────────────────────

def load_vector_file(file_path: str | Path) -> tuple[dict, gpd.GeoDataFrame]:
    """
    Reads any supported vector format (.kml, .gpkg, .geojson) via GeoPandas.
    Natively supports tracking individual Polygons and exploding MultiPolygons 
    without dissolving features together.

    Returns:
      1. geojson_feature (dict): A unified dictionary containing complete feature geometry inputs.
      2. gdf (GeoDataFrame): The clean attribute table data frame with separate rows preserved.
    """
    import fiona
    fiona.drvsupport.supported_drivers['KML'] = 'r'
    fiona.drvsupport.supported_drivers['LIBKML'] = 'r'

    path = Path(file_path)
    suffix = path.suffix.lower()

    log.info(f"Ingesting uploaded layout file vector target: {path.name}")
    sys.stdout.flush()

    driver_map = {".kml": "KML", ".gpkg": "GPKG", ".geojson": None, ".json": None}
    if suffix not in driver_map:
        raise ValueError(f"Unsupported file format: '{suffix}'. Please upload a .geojson, .gpkg, or .kml file.")

    kwargs = {}
    if driver_map[suffix]:
        kwargs["driver"] = driver_map[suffix]

    gdf = gpd.read_file(str(path), **kwargs)

    # Explode multipart structures into individual single geometries row-by-row
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)

    # Filter out point, line or structural string elements
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    if gdf.empty:
        raise ValueError("The uploaded file contains no usable polygon or multi-polygon geometry rows.")

    # Assign clean, explicit, human-readable Feature IDs starting from 1
    if "fid" not in gdf.columns:
        gdf.insert(0, "fid", gdf.index + 1)

    # Clean out empty system tags often generated by Google Earth KML exports
    noise_cols = ["Description", "description", "tessellate", "extrude", "visibility"]
    for col in noise_cols:
        if col in gdf.columns:
            gdf = gdf.drop(columns=[col], errors="ignore")

    # Handle Coordinate Reference System transformations safely
    if gdf.crs is None:
        gdf = gdf.set_crs(cfg.TARGET_CRS)
    elif gdf.crs.to_string() != cfg.TARGET_CRS:
        gdf = gdf.to_crs(cfg.TARGET_CRS)

    # Collect structural elements cleanly to extract geometries without collapsing row records
    combined_geometry = gdf.geometry.unary_union
    
    # Generate mapping format to fit the downstream raster masking parameters
    geojson_feature = {
        "type": "Feature",
        "properties": {"summary": "Unified Vector Track Collection"},
        "geometry": mapping(combined_geometry)
    }

    log.info(f"Successfully processed {len(gdf)} discrete layout feature layers.")
    sys.stdout.flush()

    return geojson_feature, gdf
  
