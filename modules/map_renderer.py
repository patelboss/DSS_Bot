"""
modules/map_renderer.py — Programmatic cartographic layout engine.
Produces a multi-page PDF report with separate thematic pages for FCM, FTM,
DEM (vector or raster contours) and a bilingual summary page.
"""

from __future__ import annotations

import io
import logging
import math
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import rasterio
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
from matplotlib.patches import FancyBboxPatch, Rectangle
from rasterio.mask import mask as rio_mask
from shapely.geometry import GeometryCollection, LineString, MultiLineString, shape

from config import cfg

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(h)

PALETTE = {
    "bg": "#f5f2eb",
    "panel": "#ffffff",
    "border": "#2c4a2e",
    "accent": "#2c6e31",
    "text_dark": "#1a2a1b",
    "text_mid": "#3d5c3f",
    "poly_fill": "#ffffffa0",
    "poly_edge": "#d62828",
    "grid": "#b0c4b1",
    "table_alt": "#e8f0e9",
    "vdf": "#004d1a",
    "mdf": "#2d8f2d",
    "open": "#82cf4f",
    "nonforest": "#9a9a9a",
    "scrub": "#c08a5a",
    "water": "#4f9be8",
    "fallback": "#a3c2c2",
    "ftm": "#6d8c57",
    "contour": "#7a5a3a",
    "contour_faint": "#8d6a47",
}


def PathSafe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(name))


def render_map(
    geojson_feature: dict,
    results: dict[str, Any],
    filename: str = "output",
    map_mode: str = "bundle",
) -> io.BytesIO:
    """Return a multi-page PDF report in memory."""
    mode = str(map_mode or results.get("_map_mode", "bundle")).strip().lower()
    if mode not in {"bundle", "fcm", "ftm", "dem"}:
        mode = "bundle"

    page_modes = results.get("_map_modes") if mode == "bundle" else [mode]
    if mode == "bundle" and not page_modes:
        page_modes = _auto_modes(results)

    tmp_pdf = Path(tempfile.gettempdir()) / f"{PathSafe(filename)}_{mode}.pdf"
    try:
        with PdfPages(str(tmp_pdf)) as pdf:
            for page_mode in page_modes:
                fig = _build_figure()
                ax_map, ax_legend, ax_table = _build_layout(fig)
                _draw_map_panel(ax_map, geojson_feature, results, page_mode)
                _draw_legend(ax_legend, results, page_mode)
                _draw_table(ax_table, results, filename, page_mode)
                _draw_title(fig, filename, page_mode)
                pdf.savefig(fig, dpi=300, bbox_inches="tight")
                plt.close(fig)

            fig = _build_summary_figure(results, filename)
            pdf.savefig(fig, dpi=300, bbox_inches="tight")
            plt.close(fig)

        buf = io.BytesIO(tmp_pdf.read_bytes())
        buf.seek(0)
        buf.name = f"{PathSafe(filename)}.pdf"
        log.info("✅ Map successfully rendered | pages=%d | format=PDF | dpi=300", len(page_modes) + 1)
        return buf
    finally:
        try:
            if tmp_pdf.exists():
                tmp_pdf.unlink()
        except Exception:
            pass


def _auto_modes(results: dict[str, Any]) -> list[str]:
    modes: list[str] = []
    if results.get("_raw_fcm_gdfs"):
        modes.append("fcm")
    if results.get("_raw_ftm_gdfs"):
        modes.append("ftm")
    if results.get("_raw_demr_paths") or results.get("_raw_dem_gdfs"):
        modes.append("dem")
    return modes


def _build_figure() -> Figure:
    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor(PALETTE["bg"])
    fig.add_artist(Rectangle((0.01, 0.01), 0.98, 0.98, transform=fig.transFigure,
                             linewidth=3, edgecolor=PALETTE["border"], facecolor="none", zorder=10))
    return fig


def _build_layout(fig: Figure):
    gs = GridSpec(2, 2, figure=fig, left=0.04, right=0.96, top=0.88, bottom=0.04,
                  hspace=0.06, wspace=0.06, width_ratios=[4, 1], height_ratios=[3, 1])
    ax_map = fig.add_subplot(gs[0, 0])
    ax_legend = fig.add_subplot(gs[0, 1])
    ax_table = fig.add_subplot(gs[1, :])
    for ax in (ax_map, ax_legend, ax_table):
        ax.set_facecolor(PALETTE["panel"])
        for spine in ax.spines.values():
            spine.set_edgecolor(PALETTE["border"])
            spine.set_linewidth(1.2)
    return ax_map, ax_legend, ax_table


def _geom_to_xy(coords):
    return [c[0] for c in coords], [c[1] for c in coords]


def _iter_geoms(geom):
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


def _match_fcm_color(class_attr: str) -> str:
    s = str(class_attr or "").strip().upper()
    if "VDF" in s or "VERY DENSE" in s:
        return PALETTE["vdf"]
    if "MDF" in s or "MODERATELY DENSE" in s:
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


def _draw_fcm_layers(ax, results: dict) -> None:
    fcm_gdfs = results.get("_raw_fcm_gdfs", [])
    log.info("MAP_TRACE | mode=fcm | raw_fcm_gdfs=%d | rows=%s", len(fcm_gdfs), [len(gdf) for gdf in fcm_gdfs] if fcm_gdfs else [])
    for gdf in fcm_gdfs:
        if gdf is None or gdf.empty:
            continue
        if getattr(gdf, "crs", None) and str(gdf.crs).upper() != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            color = _match_fcm_color(row.get("class_name", ""))
            for part in _iter_geoms(geom):
                if part.geom_type == "Polygon":
                    xs, ys = _geom_to_xy(list(part.exterior.coords))
                    ax.fill(xs, ys, color=color, alpha=0.58, linewidth=0, zorder=2)
                elif part.geom_type == "MultiPolygon":
                    for sub in part.geoms:
                        xs, ys = _geom_to_xy(list(sub.exterior.coords))
                        ax.fill(xs, ys, color=color, alpha=0.58, linewidth=0, zorder=2)


def _draw_ftm_layers(ax, results: dict) -> None:
    ftm_gdfs = results.get("_raw_ftm_gdfs", [])
    log.info("MAP_TRACE | mode=ftm | raw_ftm_gdfs=%d | rows=%s", len(ftm_gdfs), [len(gdf) for gdf in ftm_gdfs] if ftm_gdfs else [])
    for gdf in ftm_gdfs:
        if gdf is None or gdf.empty:
            continue
        if getattr(gdf, "crs", None) and str(gdf.crs).upper() != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            for part in _iter_geoms(geom):
                if part.geom_type == "Polygon":
                    xs, ys = _geom_to_xy(list(part.exterior.coords))
                    ax.fill(xs, ys, color=PALETTE["ftm"], alpha=0.32, linewidth=0.6, zorder=2)
                elif part.geom_type == "MultiPolygon":
                    for sub in part.geoms:
                        xs, ys = _geom_to_xy(list(sub.exterior.coords))
                        ax.fill(xs, ys, color=PALETTE["ftm"], alpha=0.32, linewidth=0.6, zorder=2)


def _draw_dem_layers(ax, results: dict, study_geom) -> None:
    demr_paths = [Path(p) for p in results.get("_raw_demr_paths", []) if p]
    dem_gdfs = results.get("_raw_dem_gdfs", [])

    if demr_paths:
        log.info("MAP_TRACE | mode=dem-raster | raw_demr_paths=%d", len(demr_paths))
        contour_interval = int(results.get("_contour_interval_m", 20))
        for raster_path in demr_paths:
            if not raster_path.exists():
                continue
            try:
                with rasterio.open(str(raster_path)) as src:
                    out_image, out_transform = rio_mask(src, [study_geom.__geo_interface__], crop=True, filled=True, nodata=src.nodata)
                    data = np.array(out_image[0], dtype="float64")
                    if src.nodata is not None:
                        data[data == src.nodata] = np.nan
                    else:
                        data[~np.isfinite(data)] = np.nan
                    finite = data[np.isfinite(data)]
                    if finite.size < 2:
                        continue
                    min_val = float(np.nanmin(finite))
                    max_val = float(np.nanmax(finite))
                    if math.isclose(min_val, max_val):
                        continue
                    x_coords = out_transform.c + (np.arange(data.shape[1]) + 0.5) * out_transform.a
                    y_coords = out_transform.f + (np.arange(data.shape[0]) + 0.5) * out_transform.e
                    X, Y = np.meshgrid(x_coords, y_coords)
                    start = math.floor(min_val / contour_interval) * contour_interval
                    end = math.ceil(max_val / contour_interval) * contour_interval
                    levels = np.arange(start, end + contour_interval, contour_interval)
                    if levels.size < 2:
                        continue
                    ax.contour(X, Y, data, levels=levels, colors=PALETTE["contour"], linewidths=0.65, alpha=0.88, zorder=2)
            except Exception as exc:
                log.warning("Raster DEM contour render skipped for %s: %s", raster_path.name, exc)

    if dem_gdfs:
        log.info("MAP_TRACE | mode=dem-vector | raw_dem_gdfs=%d | rows=%s", len(dem_gdfs), [len(gdf) for gdf in dem_gdfs] if dem_gdfs else [])
        for gdf in dem_gdfs:
            if gdf is None or gdf.empty:
                continue
            if getattr(gdf, "crs", None) and str(gdf.crs).upper() != "EPSG:4326":
                gdf = gdf.to_crs("EPSG:4326")
            for _, row in gdf.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                for part in _iter_geoms(geom):
                    if part.geom_type == "LineString":
                        xs, ys = _geom_to_xy(list(part.coords))
                        ax.plot(xs, ys, color=PALETTE["contour"], linewidth=0.8, alpha=0.85, zorder=2)
                    elif part.geom_type == "MultiLineString":
                        for sub in part.geoms:
                            xs, ys = _geom_to_xy(list(sub.coords))
                            ax.plot(xs, ys, color=PALETTE["contour"], linewidth=0.8, alpha=0.85, zorder=2)


def _draw_study_boundary(ax, geom) -> None:
    for part in _iter_geoms(geom):
        if part.geom_type == "Polygon":
            xs, ys = _geom_to_xy(list(part.exterior.coords))
            ax.fill(xs, ys, color=PALETTE["poly_fill"], linewidth=0, alpha=0.15, zorder=3)
            ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.5, solid_capstyle="round", zorder=4)
        elif part.geom_type == "MultiPolygon":
            for sub in part.geoms:
                xs, ys = _geom_to_xy(list(sub.exterior.coords))
                ax.fill(xs, ys, color=PALETTE["poly_fill"], linewidth=0, alpha=0.15, zorder=3)
                ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.5, solid_capstyle="round", zorder=4)
        elif part.geom_type == "LineString":
            xs, ys = _geom_to_xy(list(part.coords))
            ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.5, zorder=4)
        elif part.geom_type == "MultiLineString":
            for sub in part.geoms:
                xs, ys = _geom_to_xy(list(sub.coords))
                ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.5, zorder=4)


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
        ax.barh(y0, bar_deg / 4, left=x0 + i * bar_deg / 4, height=y_span * 0.012,
                color=seg_color, edgecolor="black", linewidth=0.5, zorder=6)
    ax.text(x0 + bar_deg / 2, y0 + y_span * 0.025, f"{bar_km:.1f} km", ha="center", va="bottom",
            fontsize=7, color=PALETTE["text_dark"], fontweight="bold", zorder=7)
    ax.text(x0, y0 - y_span * 0.01, "0", ha="center", fontsize=6, color=PALETTE["text_dark"], zorder=7)


def _draw_north_arrow(ax, xlim, ylim) -> None:
    x_span = xlim[1] - xlim[0]
    y_span = ylim[1] - ylim[0]
    x = xlim[0] + x_span * 0.92
    y = ylim[0] + y_span * 0.88
    h = y_span * 0.07
    ax.annotate("", xy=(x, y + h), xytext=(x, y), arrowprops=dict(arrowstyle="-|>", color="black", lw=1.5, mutation_scale=14), zorder=8)
    ax.text(x, y + h + y_span * 0.01, "N", ha="center", va="bottom", fontsize=9, fontweight="bold", color=PALETTE["text_dark"], zorder=8)


def _draw_legend(ax, results: dict, mode: str) -> None:
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("Legend", fontsize=10, fontweight="bold", color=PALETTE["text_dark"], pad=6)
    y = 0.92
    dy = 0.075

    if mode == "dem":
        ax.text(0.05, y, "Contour Elevation View", fontsize=8, fontweight="bold", color=PALETTE["accent"], transform=ax.transAxes, va="top")
        y -= dy * 0.7
        ax.add_patch(mpatches.Rectangle((0.05, y - 0.022), 0.10, 0.044, transform=ax.transAxes,
                                        facecolor=PALETTE["contour"], edgecolor="#555", linewidth=0.5))
        ax.text(0.20, y, "Contours / Elevation Lines", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
        y -= dy
        dem = results.get("dem", {})
        for label, value in [("Elevation (Min)", dem.get("elevation_min_m", "—")),
                             ("Elevation (Max)", dem.get("elevation_max_m", "—")),
                             ("Elevation (Mean)", dem.get("elevation_mean_m", "—"))]:
            ax.text(0.05, y, f"{label}: {value}", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="top")
            y -= dy * 0.7
    elif mode == "ftm":
        ax.text(0.05, y, "Forest Type Mapping", fontsize=8, fontweight="bold", color=PALETTE["accent"], transform=ax.transAxes, va="top")
        y -= dy * 0.7
        ax.add_patch(mpatches.Rectangle((0.05, y - 0.022), 0.10, 0.044, transform=ax.transAxes,
                                        facecolor=PALETTE["ftm"], edgecolor="#555", linewidth=0.5))
        ax.text(0.20, y, "FTM Coverage", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
        y -= dy
        ftm_area = results.get("ftm", {}).get("area_ha")
        ax.text(0.05, y, f"Intersecting Area: {ftm_area:.2f} ha" if ftm_area is not None else "No FTM area calculated",
                fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="top")
    else:
        ax.text(0.05, y, "Forest Cover Classes (FCM)", fontsize=8, fontweight="bold", color=PALETTE["accent"], transform=ax.transAxes, va="top")
        y -= dy * 0.7
        fcm_classes = results.get("fcm", {}).get("classes", {})
        if not fcm_classes:
            ax.text(0.05, y, "No class summary available", fontsize=7, color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
        else:
            order = ["VDF", "MDF", "OPEN FOREST", "NON FOREST", "SCRUB", "WATER", "NO-DATA"]
            used = set()
            for label in order:
                if label not in fcm_classes:
                    continue
                pct = fcm_classes.get(label, {}).get("percentage", 0.0)
                ax.add_patch(mpatches.Rectangle((0.05, y - 0.022), 0.10, 0.044, transform=ax.transAxes,
                                                facecolor=_match_fcm_color(label), edgecolor="#555", linewidth=0.5))
                ax.text(0.20, y, f"{label} ({pct:.1f}%)", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
                y -= dy
                used.add(label)
            for label, metrics in fcm_classes.items():
                if label in used:
                    continue
                pct = metrics.get("percentage", 0.0)
                ax.add_patch(mpatches.Rectangle((0.05, y - 0.022), 0.10, 0.044, transform=ax.transAxes,
                                                facecolor=_match_fcm_color(label), edgecolor="#555", linewidth=0.5))
                ax.text(0.20, y, f"{label} ({pct:.1f}%)", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
                y -= dy
        y -= dy * 0.2
        ax.plot([0.05, 0.15], [y, y], color=PALETTE["poly_edge"], linewidth=2, transform=ax.transAxes)
        ax.text(0.20, y, "Study Area Boundary", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")

    y -= dy * 1.1
    ax.text(0.05, y, "Data Sources:", fontsize=7, fontweight="bold", color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
    y -= dy * 0.6
    if mode == "dem":
        sources = ["DEM raster / contours", "User boundary"]
    elif mode == "ftm":
        sources = ["Forest type layer", "User boundary"]
    else:
        sources = ["FSI Forest Cover Map", "User boundary"]
    for source in sources:
        ax.text(0.05, y, f"• {source}", fontsize=6.5, color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
        y -= dy * 0.55
    ax.text(0.05, y - dy * 0.1, "CRS: WGS-84 (EPSG:4326)", fontsize=6.5, color=PALETTE["text_mid"], transform=ax.transAxes, va="top")


def _draw_table(ax, results: dict, filename: str, mode: str) -> None:
    ax.set_xticks([]); ax.set_yticks([])
    title = {"fcm": "Spatial Analysis Summary", "ftm": "Forest Type Summary", "dem": "Terrain / Contour Summary"}.get(mode, "Spatial Analysis Summary")
    ax.set_title(title, fontsize=10, fontweight="bold", color=PALETTE["text_dark"], pad=4)
    dem = results.get("dem", {})
    fcm = results.get("fcm", {})
    ftm = results.get("ftm", {})
    centroid_x, centroid_y = results.get("centroid", ("—", "—"))
    centroid_str = f"{centroid_x:.6f}, {centroid_y:.6f}" if isinstance(centroid_x, (int, float)) and isinstance(centroid_y, (int, float)) else "—"
    rows = [
        ("Map Mode", mode.upper()),
        ("Total Area", f"{results.get('area_ha', 0):.2f} hectares"),
        ("Dominant Cover", fcm.get("dominant", "—")),
        ("FTM Area", f"{ftm.get('area_ha', '—')} ha" if ftm.get("area_ha") is not None else "—"),
        ("Elevation (Min)", f"{dem.get('elevation_min_m', '—')} m"),
        ("Elevation (Max)", f"{dem.get('elevation_max_m', '—')} m"),
        ("Elevation (Mean)", f"{dem.get('elevation_mean_m', '—')} m"),
        ("Source File", filename),
        ("Centroid (lon/lat)", centroid_str),
    ]
    mid = math.ceil(len(rows) / 2)
    for col_idx, col_rows in enumerate((rows[:mid], rows[mid:])):
        x_label = 0.02 + col_idx * 0.50
        x_value = 0.22 + col_idx * 0.50
        for row_idx, (label, value) in enumerate(col_rows):
            y = 0.85 - row_idx * 0.19
            bg_color = PALETTE["table_alt"] if row_idx % 2 == 0 else PALETTE["panel"]
            ax.add_patch(FancyBboxPatch((x_label - 0.01, y - 0.09), 0.48, 0.16, boxstyle="round,pad=0.01",
                                        facecolor=bg_color, edgecolor="none", transform=ax.transAxes))
            ax.text(x_label, y, label, fontsize=8, fontweight="bold", color=PALETTE["text_mid"], transform=ax.transAxes, va="center")
            ax.text(x_value, y, value, fontsize=8, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")


def _draw_title(fig: Figure, filename: str, mode: str) -> None:
    header = {"fcm": "FOREST COVER MAP", "ftm": "FOREST TYPE MAP", "dem": "CONTOUR ELEVATION MAP"}.get(mode, "ANALYSIS MAP")
    fig.text(0.50, 0.955, f"SPATIAL DECISION SUPPORT SYSTEM — {header}", ha="center", va="center", fontsize=13, fontweight="bold", color=PALETTE["text_dark"])
    fig.text(0.50, 0.935, f"MP Forest Department  |  File: {filename}", ha="center", va="center", fontsize=9, color=PALETTE["text_mid"], style="italic")
    fig.add_artist(plt.Line2D([0.04, 0.96], [0.925, 0.925], transform=fig.transFigure, color=PALETTE["border"], linewidth=1.5))


def _build_summary_figure(results: dict[str, Any], filename: str) -> Figure:
    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor(PALETTE["bg"])
    fig.add_artist(Rectangle((0.01, 0.01), 0.98, 0.98, transform=fig.transFigure,
                             linewidth=3, edgecolor=PALETTE["border"], facecolor="none", zorder=10))
    ax = fig.add_axes([0.04, 0.05, 0.92, 0.84])
    ax.set_axis_off()
    fig.text(0.50, 0.955, "SPATIAL DECISION SUPPORT SYSTEM — SUMMARY", ha="center", va="center",
             fontsize=15, fontweight="bold", color=PALETTE["text_dark"])
    fig.text(0.50, 0.935, f"MP Forest Department  |  File: {filename}", ha="center", va="center",
             fontsize=9, color=PALETTE["text_mid"], style="italic")
    fig.add_artist(plt.Line2D([0.04, 0.96], [0.925, 0.925], transform=fig.transFigure, color=PALETTE["border"], linewidth=1.5))

    summary_en = str(results.get("summary_en") or "No summary text was supplied for this analysis.").strip()
    summary_hi = str(results.get("summary_hi") or "इस विश्लेषण के लिए कोई सारांश उपलब्ध नहीं है।").strip()
    key_facts_en = list(results.get("key_facts_en") or ["No key facts available."])
    key_facts_hi = list(results.get("key_facts_hi") or ["कोई मुख्य तथ्य उपलब्ध नहीं है।"])

    def draw_box(x, y, w, h, title, body_lines, facecolor):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.02",
                                    transform=ax.transAxes, facecolor=facecolor, edgecolor=PALETTE["border"], linewidth=1.0))
        ax.text(x + 0.02, y + h - 0.05, title, transform=ax.transAxes, fontsize=11, fontweight="bold",
                color=PALETTE["accent"], va="top")
        yy = y + h - 0.10
        for line in body_lines:
            ax.text(x + 0.02, yy, line, transform=ax.transAxes, fontsize=9.5, color=PALETTE["text_dark"], va="top", wrap=True)
            yy -= 0.05

    draw_box(0.02, 0.52, 0.46, 0.40, "English Summary", textwrap.wrap(summary_en, width=58), "#ffffff")
    draw_box(0.52, 0.52, 0.46, 0.40, "हिंदी सारांश", textwrap.wrap(summary_hi, width=58), "#f8fbf6")
    draw_box(0.02, 0.08, 0.46, 0.35, "Key Facts (English)", [f"• {fact}" for fact in key_facts_en], "#fbfcf8")
    draw_box(0.52, 0.08, 0.46, 0.35, "मुख्य तथ्य (हिंदी)", [f"• {fact}" for fact in key_facts_hi], "#fbfcf8")
    fig.text(0.50, 0.02, "Generated automatically by SDSS | तैयार किया गया SDSS द्वारा", ha="center", va="bottom", fontsize=8, color=PALETTE["text_mid"])
    return fig


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
