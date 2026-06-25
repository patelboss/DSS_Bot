# utils/dev_render.py
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import LineString, Polygon, box, mapping

log = logging.getLogger(__name__)


def _fake_boundary_feature() -> dict[str, Any]:
    geom = box(77.00, 23.00, 77.12, 23.12)
    return {
        "type": "Feature",
        "geometry": mapping(geom),
        "properties": {
            "name": "fake_test_area",
            "id": "fake_001",
        },
    }


def _fake_fcm_gdf() -> gpd.GeoDataFrame:
    rows = [
        {"class_name": "VDF", "Area": 12.4, "geometry": Polygon([(77.005, 23.105), (77.035, 23.105), (77.035, 23.075), (77.005, 23.075)])},
        {"class_name": "MDF", "Area": 18.8, "geometry": Polygon([(77.035, 23.105), (77.080, 23.105), (77.080, 23.070), (77.035, 23.070)])},
        {"class_name": "OPEN FOREST", "Area": 9.7, "geometry": Polygon([(77.005, 23.070), (77.045, 23.070), (77.045, 23.035), (77.005, 23.035)])},
        {"class_name": "NON FOREST", "Area": 6.2, "geometry": Polygon([(77.045, 23.070), (77.090, 23.070), (77.090, 23.035), (77.045, 23.035)])},
        {"class_name": "SCRUB", "Area": 2.6, "geometry": Polygon([(77.090, 23.070), (77.115, 23.070), (77.115, 23.045), (77.090, 23.045)])},
        {"class_name": "WATER", "Area": 2.0, "geometry": Polygon([(77.090, 23.045), (77.115, 23.045), (77.115, 23.025), (77.090, 23.025)])},
    ]
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return gdf


def _fake_ftm_gdf() -> gpd.GeoDataFrame:
    rows = [
        {"class_name": "Teak", "geometry": Polygon([(77.015, 23.115), (77.030, 23.115), (77.030, 23.098), (77.015, 23.098)])},
        {"class_name": "Sal", "geometry": Polygon([(77.030, 23.115), (77.045, 23.115), (77.045, 23.098), (77.030, 23.098)])},
        {"class_name": "Bamboo", "geometry": Polygon([(77.045, 23.115), (77.060, 23.115), (77.060, 23.098), (77.045, 23.098)])},
        {"class_name": "Misc. Forest", "geometry": Polygon([(77.060, 23.115), (77.078, 23.115), (77.078, 23.098), (77.060, 23.098)])},
    ]
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return gdf


def _fake_dem_gdf() -> gpd.GeoDataFrame:
    rows = [
        {"ELEV": 940, "geometry": LineString([(77.000, 23.020), (77.120, 23.020)])},
        {"ELEV": 960, "geometry": LineString([(77.000, 23.040), (77.120, 23.040)])},
        {"ELEV": 980, "geometry": LineString([(77.000, 23.060), (77.120, 23.060)])},
        {"ELEV": 1000, "geometry": LineString([(77.000, 23.080), (77.120, 23.080)])},
        {"ELEV": 1020, "geometry": LineString([(77.000, 23.100), (77.120, 23.100)])},
    ]
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return gdf


def build_fake_payload() -> tuple[dict[str, Any], dict[str, Any]]:
    geojson_feature = _fake_boundary_feature()

    results: dict[str, Any] = {
        "area_ha": 52.70,
        "centroid": (77.060000, 23.060000),
        "summary_en": "",
        "summary_hi": "",
        "key_facts_lines": [],
        "_map_mode": "bundle",
        "_contour_interval_m": 20,
        "_raw_fcm_gdfs": [_fake_fcm_gdf()],
        "_raw_ftm_gdfs": [_fake_ftm_gdf()],
        "_raw_dem_gdfs": [_fake_dem_gdf()],
        "_raw_demr_paths": [],
        "fcm": {
            "dominant": "MDF",
            "classes": {
                "VDF": {"percentage": 22.0},
                "MDF": {"percentage": 36.0},
                "OPEN FOREST": {"percentage": 18.0},
                "NON FOREST": {"percentage": 12.0},
                "SCRUB": {"percentage": 7.0},
                "WATER": {"percentage": 5.0},
            },
        },
        "dem": {
            "elevation_min_m": 940,
            "elevation_max_m": 1020,
            "elevation_mean_m": 980,
        },
        "ftm": {
            "area_ha": 28.40,
        },
    }

    return geojson_feature, results


def render_fake_report(filename: str = "testrender", map_mode: str = "bundle") -> io.BytesIO:
    """
    Build a fake PDF using the real map renderer, with fake inputs only.
    No analysis pipeline, no bot data, no external files.
    """
    from modules.map_renderer import render_map

    geojson_feature, results = build_fake_payload()
    results["_map_mode"] = str(map_mode or "bundle").strip().lower()

    buf = render_map(
        geojson_feature=geojson_feature,
        results=results,
        filename=filename,
        map_mode=map_mode,
    )
    buf.name = f"{filename}.pdf"
    return buf


def save_fake_report(out_path: str | Path | None = None, filename: str = "testrender", map_mode: str = "bundle") -> str:
    """
    Save the fake PDF to disk and return the final path.
    """
    if out_path is None:
        out_path = Path("/tmp")
    else:
        out_path = Path(out_path)

    out_path.mkdir(parents=True, exist_ok=True)

    buf = render_fake_report(filename=filename, map_mode=map_mode)
    pdf_path = out_path / f"{filename}.pdf"
    pdf_path.write_bytes(buf.getvalue())
    log.info("Fake report saved: %s", pdf_path)
    return str(pdf_path)
