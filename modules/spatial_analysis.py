"""
modules/spatial_analysis.py — Foundational Vector Ingestion Engine.
Unpacks, decompresses, repairs, and sanitizes incoming user spatial files 
(.geojson, .kml, .kmz, .gpkg, or shapefile .zip archives) under tight RAM limits.
"""

import logging
import sys
import zipfile
import tempfile
import shutil
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import shape, mapping

from config import cfg

# ── Force Stream / Unbuffered Stdout Logging Setup for Koyeb Console ─────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not logger.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(stdout_handler)


def load_vector_file(file_path: str | Path) -> tuple[dict, gpd.GeoDataFrame]:
    """
    Reads and extracts any supported vector format archive.
    Returns a unified geometry dictionary footprint and a cleaned GeoDataFrame tracking layer.
    """
    import fiona
    fiona.drvsupport.supported_drivers['KML'] = 'r'
    fiona.drvsupport.supported_drivers['LIBKML'] = 'r'

    path = Path(file_path)
    suffix = path.suffix.lower()

    log.info(f"Ingesting uploaded dataset file payload: {path.name}")
    sys.stdout.flush()

    # 1. Handle KMZ Archives (Extract KML internally)
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

    # 2. Handle Shapefile ZIP Archives safely
    if suffix == ".zip":
        log.info("Extracting Shapefile ZIP archive container layout safely…")
        sys.stdout.flush()
        
        tmp_extract_dir = Path(tempfile.mkdtemp())
        try:
            with zipfile.ZipFile(path, 'r') as zip_ref:
                zip_ref.extractall(tmp_extract_dir)
            
            shp_files = [
                f for f in tmp_extract_dir.glob("**/*.shp") 
                if f.is_file() and not f.name.startswith("._")
            ]
            
            if not shp_files:
                raise ValueError(
                    "Invalid Shapefile ZIP: Could not find any valid, underlying .shp file data inside the archive."
                )
            
            target_shp = shp_files[0]
            log.info(f"Targeting extracted shapefile: {target_shp.name}")
            sys.stdout.flush()
            
            gdf = gpd.read_file(str(target_shp))
            return _process_and_sanitize_gdf(gdf)
        finally:
            shutil.rmtree(tmp_extract_dir, ignore_errors=True)
          
    # 3. Standard Vector Reader
    driver_map = {".kml": "KML", ".gpkg": "GPKG", ".geojson": None, ".json": None}
    if suffix not in driver_map:
        raise ValueError(f"Unsupported file format structure '{suffix}'. Please use .geojson, .gpkg, .kml, .kmz, or .zip.")

    kwargs = {}
    if driver_map[suffix]:
        kwargs["driver"] = driver_map[suffix]

    gdf = gpd.read_file(str(path), **kwargs)
    return _process_and_sanitize_gdf(gdf)


def _process_and_sanitize_gdf(gdf: gpd.GeoDataFrame) -> tuple[dict, gpd.GeoDataFrame]:
    """Cleans up attribute structures, repairs geometries, handles CRS, and extracts geometry summaries."""

    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()

    if gdf.empty:
        raise ValueError(
            "The uploaded vector file contains no valid Polygon or MultiPolygon layouts."
        )

    if "fid" not in gdf.columns:
        gdf.insert(0, "fid", gdf.index + 1)

    noise_cols = [
        "Description",
        "description",
        "tessellate",
        "extrude",
        "visibility"
    ]

    for col in noise_cols:
        if col in gdf.columns:
            gdf = gdf.drop(columns=[col], errors="ignore")

    # CRS Normalization Alignment
    if gdf.crs is None:
        gdf = gdf.set_crs(cfg.TARGET_CRS)
    elif gdf.crs.to_string() != cfg.TARGET_CRS:
        gdf = gdf.to_crs(cfg.TARGET_CRS)

    # Geometry validation and automatic repair sweeps
    invalid_count = (~gdf.is_valid).sum()
    if invalid_count:
        log.warning(f"Detected {invalid_count} invalid geometries. Attempting automatic repair.")
        sys.stdout.flush()
        try:
            gdf["geometry"] = gdf.geometry.make_valid()
        except Exception:
            gdf["geometry"] = gdf.geometry.buffer(0)

    # Clean out tracking noise features elements rows
    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()

    if gdf.empty:
        raise ValueError("All geometries became invalid after repair.")

    # Compress layout elements into unified unary footprints bounds profiles
    try:
        combined_geometry = gdf.geometry.unary_union
    except Exception as exc:
        log.warning(f"Unary union failed ({exc}). Applying secondary buffer validation repair pass.")
        sys.stdout.flush()
        gdf["geometry"] = gdf.geometry.buffer(0)
        combined_geometry = gdf.geometry.unary_union

    geojson_feature = {
        "type": "Feature",
        "properties": {
            "summary": "Unified Vector Track Collection"
        },
        "geometry": mapping(combined_geometry)
    }

    log.info(f"Successfully processed {len(gdf)} discrete spatial layout features.")
    sys.stdout.flush()

    return geojson_feature, gdf

