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
import zipfile
import tempfile
import shutil
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

    # 🚀 FIXED: Unpacks the first feature entry from the array safely
    if isinstance(geojson_feature, list):
        log.warning("Pipeline received a list instead of a dict. Unpacking first feature entry automatically.")
        if len(geojson_feature) > 0:
            geojson_feature = geojson_feature
        else:
            raise ValueError("The provided geojson feature collection list is empty.")

    # Double check if we are dealing with the raw geometry structure or the full Feature wrapper
    if "geometry" not in geojson_feature and "type" in geojson_feature:
        geojson_feature = {
            "type": "Feature",
            "properties": {},
            "geometry": geojson_feature
        }

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

    results["area_ha"] = _calculate_area_ha(geojson_feature)
    results["centroid"] = _get_centroid(geojson_feature)

    log.info("Analysis complete → area=%.2f ha", results["area_ha"])
    sys.stdout.flush()
    return results


# ── Sub-analysis functions ────────────────────────────────────────────────────

def _analyse_forest_cover(geojson_feature: dict) -> tuple[str, dict]:
    """Returns pixel-count breakdown of FSI Forest Cover classes as percentages."""
    masked, _ = extract_masked_array(cfg.COG_FCM, geojson_feature, band=1, nodata=255)

    total_valid = masked.count()
    if total_valid == 0:
        return "fcm", {"error": "No valid pixels in extent", "classes": {}}

    class_stats: dict[str, dict] = {}
    flat = masked.compressed()

    for class_val, class_name in FCM_CLASSES.items():
        count = int(np.sum(flat == class_val))
        pct   = round((count / total_valid) * 100, 2) if total_valid > 0 else 0.0
        if count > 0:
            class_stats[class_name] = {
                "pixel_count":   count,
                "percentage":    pct,
                "class_id":      class_val,
            }

    forest_classes = {k: v for k, v in class_stats.items() if "Water" not in k}
    dominant = max(forest_classes, key=lambda k: forest_classes[k]["pixel_count"]) \
               if forest_classes else "Non-Forest"

    return "fcm", {
        "classes":  class_stats,
        "dominant": dominant,
        "total_valid_pixels": total_valid,
    }


def _analyse_elevation(geojson_feature: dict) -> tuple[str, dict]:
    """Computes min / max / mean elevation and mean slope inside boundaries."""
    masked, meta = extract_masked_array(cfg.COG_DEM, geojson_feature, band=1, nodata=-9999)

    if masked.count() == 0:
        return "dem", {"error": "No valid pixels in extent"}

    valid = masked.compressed().astype(np.float32)
    res_x = abs(meta["transform"].a)
    res_y = abs(meta["transform"].e)

    data_2d = masked.filled(np.nan).astype(np.float32)
    dy, dx = np.gradient(data_2d, res_y, res_x)
    slope_rad = np.arctan(np.sqrt(dx**2 + dy**2))
    slope_deg = np.degrees(slope_rad)
    
    slope_valid = slope_deg[~masked.mask] if masked.mask is not np.ma.nomask else slope_deg.ravel()

    return "dem", {
        "elevation_min_m":  round(float(valid.min()),  1),
        "elevation_max_m":  round(float(valid.max()),  1),
        "elevation_mean_m": round(float(valid.mean()), 1),
        "slope_mean_deg":   round(float(slope_valid.mean()), 1) if len(slope_valid) else 0.0,
        "slope_max_deg":    round(float(slope_valid.max()),  1) if len(slope_valid) else 0.0,
    }


def _calculate_area_ha(geojson_feature: dict) -> float:
    """Accurate geodesic area calculation in hectares using UTM zone mapping."""
    geom_wgs84 = shape(geojson_feature["geometry"])
    transformer = Transformer.from_crs(cfg.TARGET_CRS, cfg.AREA_CRS, always_xy=True)
    geom_utm = shapely_transform(transformer.transform, geom_wgs84)
    return round(geom_utm.area / 10000, 4)


def _get_centroid(geojson_feature: dict) -> tuple[float, float]:
    """Returns (longitude, latitude) of the overall geometric center."""
    geom = shape(geojson_feature["geometry"])
    c = geom.centroid
    return (round(c.x, 6), round(c.y, 6))


# ── Advanced Multi-Format File Ingestion Helpers ──────────────────────────────

def load_vector_file(file_path: str | Path) -> tuple[dict, gpd.GeoDataFrame]:
    """
    Reads any supported spatial format (.geojson, .kml, .gpkg, .kmz, or shapefile .zip).
    Handles MultiPolygons natively without flattening feature records destructively.
    """
    import fiona
    fiona.drvsupport.supported_drivers['KML'] = 'r'
    fiona.drvsupport.supported_drivers['LIBKML'] = 'r'

    path = Path(file_path)
    suffix = path.suffix.lower()

    log.info(f"Ingesting uploaded dataset file payload: {path.name}")
    sys.stdout.flush()

    # 1. Handle KMZ Archives (Decompress internally to parse embedded doc.kml)
    if suffix == ".kmz":
        log.info("Extracting KMZ archive container stream…")
        with zipfile.ZipFile(path, 'r') as zip_ref:
            kml_files = [f for f in zip_ref.namelist() if f.lower().endswith('.kml')]
            if not kml_files:
                raise ValueError("Invalid KMZ layout: No underlying .kml files found inside.")
            
            tmp_extract_dir = Path(tempfile.mkdtemp())
            extracted_kml = zip_ref.extract(kml_files, path=tmp_extract_dir)
            path = Path(extracted_kml)
            suffix = ".kml"

    # 2. 🚀 FIXED: Pure Python Multi-part Unzip routing for Shapefiles
    elif suffix == ".zip":
        log.info("Extracting Shapefile ZIP archive container layout safely…")
        sys.stdout.flush()
        
        tmp_extract_dir = Path(tempfile.mkdtemp())
        try:
            with zipfile.ZipFile(path, 'r') as zip_ref:
                zip_ref.extractall(tmp_extract_dir)
            
            # Find the core structural .shp coordinate asset line
            shp_files = list(tmp_extract_dir.glob("**/*.shp"))
            if not shp_files:
                raise ValueError("Invalid Shapefile ZIP: Could not find any underlying .shp file inside.")
            
            gdf = gpd.read_file(str(shp_files))
            return _process_and_sanitize_gdf(gdf)
        finally:
            shutil.rmtree(tmp_extract_dir, ignore_errors=True)

    # 3. Standard Ingestion Router
    driver_map = {".kml": "KML", ".gpkg": "GPKG", ".geojson": None, ".json": None}
    if suffix not in driver_map:
        raise ValueError(f"Unsupported file format structure '{suffix}'. Please use .geojson, .gpkg, .kml, .kmz or .zip.")

    kwargs = {}
    if driver_map[suffix]:
        kwargs["driver"] = driver_map[suffix]

    gdf = gpd.read_file(str(path), **kwargs)
    return _process_and_sanitize_gdf(gdf)


def _process_and_sanitize_gdf(gdf: gpd.GeoDataFrame) -> tuple[dict, gpd.GeoDataFrame]:
    """Cleans up attribute structures, handles projection matching, and extracts geometry summaries."""
    # Explode multi-part features out to keep structural polygon instances independent
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)

    # Filter strictly for valid geometric bounding entities
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    if gdf.empty:
        raise ValueError("The uploaded vector file contains no valid Polygon or MultiPolygon layouts.")

    # Assign clean explicit human-readable tracking identifiers
    if "fid" not in gdf.columns:
        gdf.insert(0, "fid", gdf.index + 1)

    # Clean out empty system metadata blocks often added by Google Earth exports
    noise_cols = ["Description", "description", "tessellate", "extrude", "visibility"]
    for col in noise_cols:
        if col in gdf.columns:
            gdf = gdf.drop(columns=[col], errors="ignore")

    # Safeguard Coordinate Reference System alignments
    if gdf.crs is None:
        gdf = gdf.set_crs(cfg.TARGET_CRS)
    elif gdf.crs.to_string() != cfg.TARGET_CRS:
        gdf = gdf.to_crs(cfg.TARGET_CRS)

    # Collect complete geometric footprint without collapsing attribute rows
    combined_geometry = gdf.geometry.unary_union
    
    geojson_feature = {
        "type": "Feature",
        "properties": {"summary": "Unified Vector Track Collection"},
        "geometry": mapping(combined_geometry)
    }

    log.info(f"Successfully processed {len(gdf)} discrete spatial layout features.")
    sys.stdout.flush()

    return geojson_feature, gdf
          
