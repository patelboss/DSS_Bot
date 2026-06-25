"""
modules/map_renderer.py — Programmatic cartographic layout engine.

Produces thematic map pages for FCM, FTM, and DEM (vector or raster contours).
Summary / key facts / thank-you pages are provided by utils.summary and appended
into the same PDF.
"""

from __future__ import annotations

import gc
import io
import logging
import math
import os
import sys
import zlib
from pathlib import Path
from typing import Any, Iterable

import matplotlib
import mplcairo  # noqa: F401

matplotlib.use("module://mplcairo.base", force=True)

import logging

logging.getLogger(__name__).info(
    "Matplotlib backend: %s",
    matplotlib.get_backend()
)
# FORCE Matplotlib to pass literal strings to Cairo without internal parsing
if "text.parse_math" in matplotlib.rcParams:
    matplotlib.rcParams["text.parse_math"] = False

# matplotlib.rcParams["pgf.rcpresets"] = False
if "pgf.rcpresets" in matplotlib.rcParams:
    matplotlib.rcParams["pgf.rcpresets"] = False
# Tell the PDF backend to output actual native vectors rather than Type-3 fonts
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import rasterio
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Rectangle
from rasterio.mask import mask as rio_mask
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    mapping,
    shape,
)

from utils.pdf_renderer import render_pages
from utils.summary import (
    build_keyfacts_figure,
    build_summary_figure,
    build_thankyou_figure,
)

try:
    from config import cfg  # type: ignore
except Exception:  # pragma: no cover - config is optional
    cfg = None  # type: ignore

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(handler)


def _apply_global_font_config() -> None:
    """
    Apply the globally configured font from config.py only.
    No local font discovery, searching, or registration happens here.
    """
    if cfg is None:
        return

    try:
        font_cfg = getattr(cfg, "fonts", None)
        font_props = getattr(font_cfg, "props", None)
        if font_props is None:
            return

        family_name = font_props.get_name()

        plt.rcParams["font.family"] = family_name
        plt.rcParams["font.sans-serif"] = [family_name, "DejaVu Sans", "Arial", "sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False
        plt.rcParams["pdf.fonttype"] = 42
        plt.rcParams["ps.fonttype"] = 42

        log.info("Using configured font: %s", family_name)
    except Exception as exc:
        log.warning("Failed to apply configured font settings: %s", exc)


_apply_global_font_config()

PALETTE = {
    "bg": "#f5f2eb",
    "panel": "#ffffff",
    "border": "#2c4a2e",
    "accent": "#2c6e31",
    "text_dark": "#1a2a1b",
    "text_mid": "#3d5c3f",
    "poly_fill": "#ffffffff",
    "poly_edge": "#d62828",
    "grid": "#b0c4b1",
    "table_alt": "#e8f0e9",
    
    "vdf": "#07380e",
    "mdf": "#17d133",
    "open": "#c1c70e",
    "nonforest": "#8c8c88",
    "scrub": "#ab180e",
    "water": "#5064fa",
    "fallback": "#8c8c88",
    
    "ftm": "#6d8c57",
    "contour": "#7a5a3a",
    "contour_faint": "#a07b5d",
}

FCM_LABELS = {
    "VDF": ("Very Dense Forest", "अत्यधिक घना वन"),
    "MDF": ("Moderately Dense Forest", "मध्यम घना वन"),
    "OPEN FOREST": ("Open Forest", "खुला वन"),
    "SCRUB": ("Scrub", "झाड़ीदार क्षेत्र"),
    "WATER": ("Water", "जल"),
    "NO-DATA": ("Non Forest", "गैर-वन"), # The single unified label
}

FCM_ALIASES = {
    "VERY DENSE FOREST": "VDF",
    "VERY DENSE": "VDF",
    "VDF": "VDF",
    "MODERATELY DENSE FOREST": "MDF",
    "MODERATELY DENSE": "MDF",
    "MDF": "MDF",
    "OPEN FOREST": "OPEN FOREST",
    "OPEN": "OPEN FOREST",
    "NON FOREST": "NO-DATA",
    "NON-FOREST": "NO-DATA",
    "NON FOREST AREA": "NO-DATA",
    "SCRUB": "SCRUB",
    "WATER": "WATER",
    "NO DATA": "NO-DATA",
    "NO-DATA": "NO-DATA",
    "NODATA": "NO-DATA",
}

FTM_FALLBACK_PALETTE = [
    "#4c8c2b",
    "#6a994e",
    "#a7c957",
    "#386641",
    "#bc6c25",
    "#a98467",
    "#52796f",
    "#2f5233",
    "#8d6e63",
    "#708238",
    "#7f9c45",
    "#5f7a36",
]

_OUTPUT_DPI = int(getattr(cfg, "OUTPUT_DPI", 300) or 300) if cfg is not None else 300
_OUTPUT_FORMAT = str(getattr(cfg, "OUTPUT_FORMAT", "PDF") or "PDF").upper()


def PathSafe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(name))


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _round_to_nice(val: float) -> float:
    if val <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(val))
    residual = val / magnitude
    if residual < 1.5:
        return 1 * magnitude
    if residual < 3.5:
        return 2 * magnitude
    if residual < 7.5:
        return 5 * magnitude
    return 10 * magnitude


def _geom_to_xy(coords: Iterable) -> tuple[list[float], list[float]]:
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return xs, ys


def _iter_geoms(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, GeometryCollection):
        out = []
        for part in geom.geoms:
            out.extend(_iter_geoms(part))
        return out
    if geom.geom_type in {"MultiPolygon", "MultiLineString"}:
        return list(geom.geoms)
    if geom.geom_type in {"Polygon", "LineString"}:
        return [geom]
    return []


def _normalize_fcm_label(label: Any) -> str:
    key = str(label or "").strip().upper()
    if not key:
        return "NO-DATA"
    if key in FCM_LABELS:
        return key
    return FCM_ALIASES.get(key, key)


def _friendly_fcm_label(raw_label: str | None, lang: str = "en") -> str:
    key = _normalize_fcm_label(raw_label)
    en, hi = FCM_LABELS.get(key, (key or "No Data", "डेटा अनुपलब्ध"))
    return en if lang == "en" else hi
"""

def _match_fcm_color(class_attr: str) -> str:
    s = str(class_attr or "").strip().upper()
    if "VERY DENSE" in s or s == "VDF":
        return PALETTE["vdf"]
    if "MODERATELY DENSE" in s or s == "MDF":
        return PALETTE["mdf"]
    if "OPEN" in s:
        return PALETTE["open"]
    if "NON FOREST" in s or "NON-FOREST" in s:
        return PALETTE["nonforest"]
    if "SCRUB" in s:
        return PALETTE["scrub"]
    if "WATER" in s:
        return PALETTE["water"]

    return PALETTE["fallback"]

"""
def _match_fcm_color(class_attr: str) -> str:
    # Coerce to string to safely process missing or None attributes
    s = str(class_attr or "").strip().upper()
    
    if "VERY DENSE" in s or s == "VDF":
        return PALETTE["vdf"]
    if "MODERATELY DENSE" in s or s == "MDF":
        return PALETTE["mdf"]
    if "OPEN" in s:
        return PALETTE["open"]
#    if "NON FOREST" in s or "NON-FOREST" in s:
#        return PALETTE["nonforest"]
    if "SCRUB" in s:
        return PALETTE["scrub"]
    if "WATER" in s:
        return PALETTE["water"]

    # If the attribute string itself explicitly specifies no data
    if "NO DATA" in s or "NO-DATA" in s or "NODATA" in s:
        return PALETTE.get("nodata", PALETTE["fallback"])

    # Fallback default: when there is no matching class attribute string, 
    # it treats it as an unmapped space/missing data tile
    return PALETTE.get("nodata", PALETTE["fallback"])
    

def _stable_color_for_label(label: str) -> str:
    idx = zlib.crc32(str(label).encode("utf-8")) % len(FTM_FALLBACK_PALETTE)
    return FTM_FALLBACK_PALETTE[idx]


def _extract_ftm_label(row) -> str:
    for field in ("class_name", "forest_type", "type", "name", "species", "category", "label", "cover_type"):
        try:
            if hasattr(row, field):
                value = getattr(row, field)
            elif isinstance(row, dict):
                value = row.get(field)
            else:
                value = row[field] if field in row else None
            if value not in (None, ""):
                return str(value).strip()
        except Exception:
            pass
    return "FTM"


def _collect_ftm_stats(results: dict) -> list[tuple[str, int, str]]:
    ftm_gdfs = results.get("_raw_ftm_gdfs", []) or []
    counts: dict[str, int] = {}
    for gdf in ftm_gdfs:
        if gdf is None or gdf.empty:
            continue
        for _, row in gdf.iterrows():
            label = _extract_ftm_label(row)
            counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [(label, count, _stable_color_for_label(label)) for label, count in ranked]


def _resolve_thematic_modes(results: dict[str, Any], requested_mode: str) -> list[str]:
    has_fcm = bool(results.get("_raw_fcm_gdfs"))
    has_ftm = bool(results.get("_raw_ftm_gdfs"))
    has_dem = bool(results.get("_raw_demr_paths") or results.get("_raw_dem_gdfs"))

    available: list[str] = []
    if has_fcm:
        available.append("fcm")
    if has_dem:
        available.append("dem")
    if has_ftm:
        available.append("ftm")

    if requested_mode == "bundle":
        return available

    if requested_mode in {"fcm", "dem", "ftm"}:
        return [requested_mode] if requested_mode in available else available

    return available


def render_map(
    geojson_feature: dict,
    results: dict[str, Any],
    filename: str = "output",
    map_mode: str = "bundle",
) -> io.BytesIO:
    """
    Return a multi-page PDF report in memory.

    Page order:
      - FCM if available
      - DEM if available
      - FTM if available
      - Summary
      - Key Facts
      - Thank You
    """
    mode = str(map_mode or results.get("_map_mode", "bundle")).strip().lower()
    if mode not in {"bundle", "fcm", "ftm", "dem"}:
        mode = "bundle"

    thematic_modes = _resolve_thematic_modes(results, mode)
    page_specs: list[str] = thematic_modes + ["summary", "keyfacts", "thanks"]

    figures: list[Figure] = []
    try:
        for page_kind in page_specs:
            if page_kind in {"fcm", "ftm", "dem"}:
                fig = _build_thematic_figure(page_kind, geojson_feature, results, filename)
            elif page_kind == "summary":
                fig = build_summary_figure(results, filename)
            elif page_kind == "keyfacts":
                fig = build_keyfacts_figure(results, filename)
            else:
                fig = build_thankyou_figure(results, filename)
            figures.append(fig)

        buf = render_pages(figures, filename=filename, close_figures=False)
        log.info("✅ Map successfully rendered | pages=%d | format=PDF | dpi=%d", len(page_specs), _OUTPUT_DPI)
        return buf
    finally:
        for fig in figures:
            try:
                plt.close(fig)
            except Exception:
                pass
        gc.collect()


def _build_figure() -> Figure:
    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor(PALETTE["bg"])
    fig.add_artist(
        Rectangle(
            (0.01, 0.01),
            0.98,
            0.98,
            transform=fig.transFigure,
            linewidth=3,
            edgecolor=PALETTE["border"],
            facecolor="none",
            zorder=10,
        )
    )
    return fig


def _build_layout(fig: Figure):
    gs = GridSpec(
        2,
        2,
        figure=fig,
        left=0.04,
        right=0.96,
        top=0.88,
        bottom=0.04,
        hspace=0.06,
        wspace=0.06,
        width_ratios=[4, 1],
        height_ratios=[3, 1],
    )

    ax_map = fig.add_subplot(gs[0, 0])
    ax_legend = fig.add_subplot(gs[0, 1])
    ax_table = fig.add_subplot(gs[1, :])

    for ax in (ax_map, ax_legend, ax_table):
        ax.set_facecolor(PALETTE["panel"])
        for spine in ax.spines.values():
            spine.set_edgecolor(PALETTE["border"])
            spine.set_linewidth(1.2)

    return ax_map, ax_legend, ax_table


def _build_thematic_figure(mode: str, geojson_feature: dict, results: dict, filename: str) -> Figure:
    fig = _build_figure()

    title = {
        "fcm": "FOREST COVER MAP (FCM)",
        "dem": "DEM CONTOURS (20 m)",
        "ftm": "FOREST TYPE MAP (FTM)",
    }.get(mode, "ANALYSIS MAP")

    fig.text(
        0.50,
        0.955,
        f"SPATIAL DECISION SUPPORT SYSTEM — {title}",
        ha="center",
        va="center",
        fontsize=13,
        fontweight="bold",
        color=PALETTE["text_dark"],
    )
    fig.text(
        0.50,
        0.935,
        f"Developed by Pankaj Patidar for use in MP Forest Department  |  File: {filename}",
        ha="center",
        va="center",
        fontsize=9,
        color=PALETTE["text_mid"],
        style="italic",
    )
    fig.add_artist(
        Line2D(
            [0.04, 0.96],
            [0.925, 0.925],
            transform=fig.transFigure,
            color=PALETTE["border"],
            linewidth=1.5,
        )
    )

    ax_map, ax_legend, ax_table = _build_layout(fig)
    _draw_map_panel(ax_map, geojson_feature, results, mode)
    _draw_legend(ax_legend, results, mode)
    _draw_table(ax_table, results, filename, mode)
    return fig


def _draw_map_panel(ax, geojson_feature: dict, results: dict, mode: str) -> None:
    geom = shape(geojson_feature["geometry"])
    minx, miny, maxx, maxy = geom.bounds
    pad_x = (maxx - minx) * 0.15 if (maxx - minx) > 0 else 0.01
    pad_y = (maxy - miny) * 0.15 if (maxy - miny) > 0 else 0.01
    xlim = (minx - pad_x, maxx + pad_x)
    ylim = (miny - pad_y, maxy + pad_y)

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_axisbelow(True)

    if mode == "dem":
        ax.set_facecolor("#efe8de")
        ax.grid(True, color="#cdbfae", linewidth=0.45, linestyle="--", alpha=0.55)
    elif mode == "ftm":
        ax.set_facecolor("#eef4ea")
        ax.grid(True, color="#c5d3bf", linewidth=0.45, linestyle="--", alpha=0.55)
    else:
        ax.set_facecolor("#e8ede8")
        ax.grid(True, color=PALETTE["grid"], linewidth=0.5, linestyle="--", alpha=0.7)

    if mode == "fcm":
        _draw_fcm_layers(ax, results)
    elif mode == "ftm":
        _draw_ftm_layers(ax, results)
    else:
        _draw_dem_layers(ax, results, geom)

    _draw_study_boundary(ax, geom)
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f°E"))
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f°N"))
    ax.tick_params(axis="both", labelsize=7, color=PALETTE["text_mid"])
    _draw_scale_bar(ax, xlim, ylim)
    _draw_north_arrow(ax, xlim, ylim)
    ax.set_aspect("equal")
    ax.set_xlabel("Longitude", fontsize=8, color=PALETTE["text_mid"])
    ax.set_ylabel("Latitude", fontsize=8, color=PALETTE["text_mid"])


def _safe_iterrows_frame(gdf):
    try:
        return gdf.iterrows()
    except Exception:
        return []


def _maybe_to_wgs84(gdf):
    if getattr(gdf, "crs", None) and str(gdf.crs).upper() != "EPSG:4326":
        try:
            return gdf.to_crs("EPSG:4326")
        except Exception:
            return gdf
    return gdf


def _draw_fcm_layers(ax, results: dict) -> None:
    fcm_gdfs = results.get("_raw_fcm_gdfs", []) or []
    log.info("MAP_TRACE | mode=fcm | raw_fcm_gdfs=%d | rows=%s", len(fcm_gdfs), [len(gdf) for gdf in fcm_gdfs] if fcm_gdfs else [])

    for gdf in fcm_gdfs:
        if gdf is None or getattr(gdf, "empty", True):
            continue
        gdf = _maybe_to_wgs84(gdf)

        for _, row in gdf.iterrows():
            geom = getattr(row, "geometry", None)
            if geom is None or geom.is_empty:
                continue

            class_attr = getattr(row, "class_name", "")
            poly_color = _match_fcm_color(class_attr)

            for part in _iter_geoms(geom):
                if part.geom_type == "Polygon":
                    xs, ys = _geom_to_xy(list(part.exterior.coords))
                    ax.fill(xs, ys, color=poly_color, alpha=0.62, linewidth=0, zorder=2)
                    for ring in part.interiors:
                        hx, hy = _geom_to_xy(list(ring.coords))
                        ax.fill(hx, hy, color=ax.get_facecolor(), linewidth=0, zorder=2.5)
                elif part.geom_type == "MultiPolygon":
                    for sub in part.geoms:
                        xs, ys = _geom_to_xy(list(sub.exterior.coords))
                        ax.fill(xs, ys, color=poly_color, alpha=0.62, linewidth=0, zorder=2)
                elif part.geom_type in {"LineString", "MultiLineString"}:
                    _plot_lines(ax, part, color=poly_color, linewidth=1.0, alpha=0.55, zorder=2)


def _draw_ftm_layers(ax, results: dict) -> None:
    ftm_gdfs = results.get("_raw_ftm_gdfs", []) or []
    log.info("MAP_TRACE | mode=ftm | raw_ftm_gdfs=%d | rows=%s", len(ftm_gdfs), [len(gdf) for gdf in ftm_gdfs] if ftm_gdfs else [])

    for gdf in ftm_gdfs:
        if gdf is None or getattr(gdf, "empty", True):
            continue
        gdf = _maybe_to_wgs84(gdf)

        for _, row in gdf.iterrows():
            geom = getattr(row, "geometry", None)
            if geom is None or geom.is_empty:
                continue

            label = _extract_ftm_label(row)
            color = _stable_color_for_label(label)

            for part in _iter_geoms(geom):
                if part.geom_type == "Polygon":
                    xs, ys = _geom_to_xy(list(part.exterior.coords))
                    ax.fill(xs, ys, color=color, alpha=0.40, linewidth=0.4, edgecolor="#ffffff40", zorder=2)
                    for ring in part.interiors:
                        hx, hy = _geom_to_xy(list(ring.coords))
                        ax.fill(hx, hy, color=ax.get_facecolor(), linewidth=0, zorder=2.5)
                elif part.geom_type == "MultiPolygon":
                    for sub in part.geoms:
                        xs, ys = _geom_to_xy(list(sub.exterior.coords))
                        ax.fill(xs, ys, color=color, alpha=0.40, linewidth=0.4, edgecolor="#ffffff40", zorder=2)
                elif part.geom_type in {"LineString", "MultiLineString"}:
                    _plot_lines(ax, part, color=color, linewidth=0.8, alpha=0.5, zorder=2)


def _draw_dem_layers(ax, results: dict, study_geom) -> None:
    demr_paths = [Path(p) for p in results.get("_raw_demr_paths", []) if p]
    dem_gdfs = results.get("_raw_dem_gdfs", []) or []

    if demr_paths:
        log.info("MAP_TRACE | mode=dem-raster | raw_demr_paths=%d", len(demr_paths))
        contour_interval = int(results.get("_contour_interval_m", 20) or 20)

        for raster_path in demr_paths:
            if not raster_path.exists():
                continue
            try:
                with rasterio.open(str(raster_path)) as src:
                    out_image, out_transform = rio_mask(
                        src,
                        [mapping(study_geom)],
                        crop=True,
                        filled=True,
                        nodata=src.nodata,
                    )

                    band = out_image[0].astype("float64")
                    if src.nodata is not None:
                        band[band == src.nodata] = np.nan
                    else:
                        band[~np.isfinite(band)] = np.nan

                    finite = band[np.isfinite(band)]
                    if finite.size < 2:
                        continue

                    min_val = float(np.nanmin(finite))
                    max_val = float(np.nanmax(finite))
                    if math.isclose(min_val, max_val):
                        continue

                    height, width = band.shape
                    x_coords = out_transform.c + (np.arange(width) + 0.5) * out_transform.a
                    y_coords = out_transform.f + (np.arange(height) + 0.5) * out_transform.e
                    X, Y = np.meshgrid(x_coords, y_coords)

                    start = math.floor(min_val / contour_interval) * contour_interval
                    end = math.ceil(max_val / contour_interval) * contour_interval
                    levels = np.arange(start, end + contour_interval, contour_interval)
                    if levels.size < 2:
                        continue

                    minor_levels = [lvl for lvl in levels if int(lvl) % 100 != 0]
                    major_levels = [lvl for lvl in levels if int(lvl) % 100 == 0]

                    if minor_levels:
                        ax.contour(
                            X,
                            Y,
                            band,
                            levels=minor_levels,
                            colors=PALETTE["contour_faint"],
                            linewidths=0.45,
                            alpha=0.82,
                            zorder=2,
                            antialiased=True,
                        )

                    if major_levels:
                        cs_major = ax.contour(
                            X,
                            Y,
                            band,
                            levels=major_levels,
                            colors=PALETTE["contour"],
                            linewidths=0.9,
                            alpha=0.96,
                            zorder=3,
                            antialiased=True,
                        )
                        ax.clabel(cs_major, inline=True, fmt="%d m", fontsize=6, colors=PALETTE["contour"])
            except Exception as exc:
                log.warning("Raster DEM contour render skipped for %s: %s", raster_path.name, exc)

    if dem_gdfs:
        log.info("MAP_TRACE | mode=dem-vector | raw_dem_gdfs=%d | rows=%s", len(dem_gdfs), [len(gdf) for gdf in dem_gdfs] if dem_gdfs else [])
        for gdf in dem_gdfs:
            if gdf is None or getattr(gdf, "empty", True):
                continue
            gdf = _maybe_to_wgs84(gdf)

            elev_col = next((c for c in getattr(gdf, "columns", []) if str(c).lower() in {"elevation", "elev", "contour", "z", "elev_m"}), None)

            for _, row in gdf.iterrows():
                geom = getattr(row, "geometry", None)
                if geom is None or geom.is_empty:
                    continue

                elev_val = row.get(elev_col) if elev_col else None
                is_major = False
                if elev_val is not None:
                    try:
                        elev_num = float(elev_val)
                        is_major = int(round(elev_num)) % 100 == 0
                    except Exception:
                        is_major = False

                color = PALETTE["contour"] if is_major else PALETTE["contour_faint"]
                lw = 0.9 if is_major else 0.45

                for part in _iter_geoms(geom):
                    if part.geom_type == "LineString":
                        _plot_lines(ax, part, color=color, linewidth=lw, alpha=0.90, zorder=2)
                    elif part.geom_type == "MultiLineString":
                        _plot_lines(ax, part, color=color, linewidth=lw, alpha=0.90, zorder=2)
                    elif part.geom_type == "Polygon":
                        xs, ys = _geom_to_xy(list(part.exterior.coords))
                        ax.plot(xs, ys, color=color, linewidth=lw, alpha=0.90, zorder=2)
                    elif part.geom_type == "MultiPolygon":
                        for sub in part.geoms:
                            xs, ys = _geom_to_xy(list(sub.exterior.coords))
                            ax.plot(xs, ys, color=color, linewidth=lw, alpha=0.90, zorder=2)


def _plot_lines(ax, geom, *, color: str, linewidth: float, alpha: float, zorder: int) -> None:
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "LineString":
        xs, ys = _geom_to_xy(list(geom.coords))
        ax.plot(xs, ys, color=color, linewidth=linewidth, alpha=alpha, zorder=zorder)
    elif geom.geom_type == "MultiLineString":
        for sub in geom.geoms:
            xs, ys = _geom_to_xy(list(sub.coords))
            ax.plot(xs, ys, color=color, linewidth=linewidth, alpha=alpha, zorder=zorder)


def _draw_study_boundary(ax, geom) -> None:
    for part in _iter_geoms(geom):
        if part.geom_type == "Polygon":
            xs, ys = _geom_to_xy(list(part.exterior.coords))
            ax.fill(xs, ys, color=PALETTE["poly_fill"], linewidth=0, alpha=0.15, zorder=4)
            ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.4, solid_capstyle="round", zorder=5)
            for ring in part.interiors:
                hx, hy = _geom_to_xy(list(ring.coords))
                ax.fill(hx, hy, color=ax.get_facecolor(), linewidth=0, zorder=4.5)
        elif part.geom_type == "MultiPolygon":
            for sub in part.geoms:
                xs, ys = _geom_to_xy(list(sub.exterior.coords))
                ax.fill(xs, ys, color=PALETTE["poly_fill"], linewidth=0, alpha=0.15, zorder=4)
                ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.4, solid_capstyle="round", zorder=5)
        elif part.geom_type == "LineString":
            xs, ys = _geom_to_xy(list(part.coords))
            ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.4, zorder=5)
        elif part.geom_type == "MultiLineString":
            for sub in part.geoms:
                xs, ys = _geom_to_xy(list(sub.coords))
                ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.4, zorder=5)


def _draw_scale_bar(ax, xlim, ylim) -> None:
    span_deg = xlim[1] - xlim[0]
    y_span = ylim[1] - ylim[0]
    mid_lat = (ylim[0] + ylim[1]) / 2
    km_per_deg = max(111.0 * math.cos(math.radians(mid_lat)), 0.0001)
    bar_km = _round_to_nice(span_deg * km_per_deg * 0.20)
    bar_deg = bar_km / km_per_deg
    x0 = xlim[0] + span_deg * 0.05
    y0 = ylim[0] + y_span * 0.05

    for i in range(4):
        seg_color = "black" if i % 2 == 0 else "white"
        ax.barh(
            y0,
            bar_deg / 4,
            left=x0 + i * bar_deg / 4,
            height=y_span * 0.012,
            color=seg_color,
            edgecolor="black",
            linewidth=0.5,
            zorder=6,
        )

    ax.text(
        x0 + bar_deg / 2,
        y0 + y_span * 0.025,
        f"{bar_km:.1f} km",
        ha="center",
        va="bottom",
        fontsize=7,
        color=PALETTE["text_dark"],
        fontweight="bold",
        zorder=7,
    )
    ax.text(x0, y0 - y_span * 0.01, "0", ha="center", fontsize=6, color=PALETTE["text_dark"], zorder=7)


def _draw_north_arrow(ax, xlim, ylim) -> None:
    x_span = xlim[1] - xlim[0]
    y_span = ylim[1] - ylim[0]
    x = xlim[0] + x_span * 0.92
    y = ylim[0] + y_span * 0.88
    h = y_span * 0.07

    ax.annotate(
        "",
        xy=(x, y + h),
        xytext=(x, y),
        arrowprops=dict(arrowstyle="-|>", color="black", lw=1.5, mutation_scale=14),
        zorder=8,
    )
    ax.text(
        x,
        y + h + y_span * 0.01,
        "N",
        ha="center",
        va="bottom",
        fontsize=9,
        fontweight="bold",
        color=PALETTE["text_dark"],
        zorder=8,
    )


def _draw_legend(ax, results: dict, mode: str) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Legend", fontsize=10, fontweight="bold", color=PALETTE["text_dark"], pad=6)

    y = 0.92
    dy = 0.075

    if mode == "dem":
        ax.text(0.05, y, "Contour Elevation View", fontsize=8, fontweight="bold", color=PALETTE["accent"], transform=ax.transAxes, va="top")
        y -= dy * 0.7

        ax.add_patch(
            mpatches.Rectangle(
                (0.05, y - 0.022),
                0.10,
                0.044,
                transform=ax.transAxes,
                facecolor=PALETTE["contour"],
                edgecolor="#555",
                linewidth=0.5,
            )
        )
        ax.text(0.20, y, "Index Contours (100 m)", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
        y -= dy

        ax.add_patch(
            mpatches.Rectangle(
                (0.05, y - 0.022),
                0.10,
                0.044,
                transform=ax.transAxes,
                facecolor=PALETTE["contour_faint"],
                edgecolor="#555",
                linewidth=0.5,
            )
        )
        ax.text(0.20, y, "Intermediate Contours (20 m)", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
        y -= dy

        dem = results.get("dem", {}) or {}
        for label, value in [
            ("Elevation (Min)", dem.get("elevation_min_m", "—")),
            ("Elevation (Max)", dem.get("elevation_max_m", "—")),
            ("Elevation (Mean)", dem.get("elevation_mean_m", "—")),
        ]:
            ax.text(0.05, y, f"{label}: {value}", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="top")
            y -= dy * 0.7

    elif mode == "ftm":
        ax.text(0.05, y, "Forest Type Mapping", fontsize=8, fontweight="bold", color=PALETTE["accent"], transform=ax.transAxes, va="top")
        y -= dy * 0.8

        ftm_stats = _collect_ftm_stats(results)
        if not ftm_stats:
            ax.text(0.05, y, "No FTM classes available", fontsize=7, color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
        else:
            for label, count, color in ftm_stats[:8]:
                ax.add_patch(
                    mpatches.Rectangle(
                        (0.05, y - 0.022),
                        0.10,
                        0.044,
                        transform=ax.transAxes,
                        facecolor=color,
                        edgecolor="#555",
                        linewidth=0.5,
                    )
                )
                ax.text(0.20, y, f"{label} ({count})", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
                y -= dy

    else:
        ax.text(0.05, y, "Forest Cover Classes (FCM)", fontsize=8, fontweight="bold", color=PALETTE["accent"], transform=ax.transAxes, va="top")
        y -= dy * 0.7

        fcm_classes = results.get("fcm", {}).get("classes", {}) or {}
        normalized = {_normalize_fcm_label(k): v for k, v in fcm_classes.items()}
        log.info("LEGEND_TRACE | keys=%s", list(normalized.keys()))

        if not normalized:
            ax.text(0.05, y, "No class summary available", fontsize=7, color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
        else:
            order = ["VDF", "MDF", "OPEN FOREST", "SCRUB", "WATER", "NON FOREST", "NO-DATA"]
            used = set()

            for label in order:
                if label not in normalized:
                    continue
                pct = _safe_float(normalized.get(label, {}).get("percentage", 0.0), 0.0) or 0.0
                ax.add_patch(
                    mpatches.Rectangle(
                        (0.05, y - 0.022),
                        0.10,
                        0.044,
                        transform=ax.transAxes,
                        facecolor=_match_fcm_color(label),
                        edgecolor="#555",
                        linewidth=0.5,
                    )
                )
                ax.text(0.20, y, f"{label} ({pct:.1f}%)", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
                y -= dy
                used.add(label)

            for label, metrics in normalized.items():
                if label in used:
                    continue
                pct = _safe_float((metrics or {}).get("percentage", 0.0), 0.0) or 0.0
                ax.add_patch(
                    mpatches.Rectangle(
                        (0.05, y - 0.022),
                        0.10,
                        0.044,
                        transform=ax.transAxes,
                        facecolor=_match_fcm_color(label),
                        edgecolor="#555",
                        linewidth=0.5,
                    )
                )
                ax.text(0.20, y, f"{label} ({pct:.1f}%)", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
                y -= dy

        y -= dy * 0.2
        ax.plot([0.05, 0.15], [y, y], color=PALETTE["poly_edge"], linewidth=2, transform=ax.transAxes)
        ax.text(0.20, y, "Study Area Boundary", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")

    y -= dy * 1.1
    ax.text(0.05, y, "Data Sources:", fontsize=7, fontweight="bold", color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
    y -= dy * 0.6

    if mode == "dem":
        sources = ["DEM contours / raster", "User boundary"]
    elif mode == "ftm":
        sources = ["Forest type layer", "User boundary"]
    else:
        sources = ["FSI Forest Cover Map", "User boundary"]

    for source in sources:
        ax.text(0.05, y, f"- {source}", fontsize=6.5, color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
        y -= dy * 0.55

    ax.text(0.05, y - dy * 0.1, "CRS: WGS-84 (EPSG:4326)", fontsize=6.5, color=PALETTE["text_mid"], transform=ax.transAxes, va="top")


def _draw_table(ax, results: dict, filename: str, mode: str) -> None:
    ax.set_xticks([])
    ax.set_yticks([])

    title = {
        "fcm": "Spatial Analysis Summary",
        "ftm": "Forest Type Summary",
        "dem": "Terrain / Contour Summary",
    }.get(mode, "Spatial Analysis Summary")
    ax.set_title(title, fontsize=10, fontweight="bold", color=PALETTE["text_dark"], pad=4)

    dem = results.get("dem", {}) or {}
    fcm = results.get("fcm", {}) or {}
    ftm = results.get("ftm", {}) or {}

    centroid_x, centroid_y = results.get("centroid", ("—", "—"))
    centroid_str = f"{centroid_x:.6f}, {centroid_y:.6f}" if isinstance(centroid_x, (int, float)) and isinstance(centroid_y, (int, float)) else "—"

    rows = [
        ("Map Mode", mode.upper()),
        ("Total Area", f"{results.get('area_ha', 0):.2f} hectares"),
        ("Dominant Cover", _friendly_fcm_label(fcm.get("dominant", "—"), "en")),
        ("FTM Area", f"{float(ftm.get('area_ha')):.2f} ha" if ftm.get("area_ha") is not None else "—"),
        ("Elevation (Min)", f"{dem.get('elevation_min_m', '—')} m"),
        ("Elevation (Max)", f"{dem.get('elevation_max_m', '—')} m"),
        ("Elevation (Mean)", f"{dem.get('elevation_mean_m', '—')} m"),
        ("Source File", filename),
        ("Centroid (lon/lat)", centroid_str),
    ]

    mid = math.ceil(len(rows) / 2)
    cols = (rows[:mid], rows[mid:])

    for col_idx, col_rows in enumerate(cols):
        x_label = 0.02 + col_idx * 0.50
        x_value = 0.22 + col_idx * 0.50

        for row_idx, (label, value) in enumerate(col_rows):
            y = 0.85 - row_idx * 0.19
            bg_color = PALETTE["table_alt"] if row_idx % 2 == 0 else PALETTE["panel"]

            ax.add_patch(
                FancyBboxPatch(
                    (x_label - 0.01, y - 0.09),
                    0.48,
                    0.16,
                    boxstyle="round,pad=0.01",
                    facecolor=bg_color,
                    edgecolor="none",
                    transform=ax.transAxes,
                )
            )

            ax.text(x_label, y, label, fontsize=8, fontweight="bold", color=PALETTE["text_mid"], transform=ax.transAxes, va="center")
            ax.text(x_value, y, value, fontsize=8, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")


def _draw_page_footer(fig: Figure, page_index: int, total_pages: int) -> None:
    fig.text(
        0.96,
        0.02,
        f"Page {page_index} / {total_pages}",
        ha="right",
        va="bottom",
        fontsize=8,
        color=PALETTE["text_mid"],
    )
