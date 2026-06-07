"""
modules/map_renderer.py — Programmatic cartographic layout engine.

Produces a publication-quality PNG with:
  • Polygon & Multi-Polygon boundaries drawn over a light basemap grid
  • North arrow (custom SVG-like patch)
  • Auto-calculating scale bar
  • Dynamic legend for forest cover classes
  • Summary statistics table in the footer panel
"""

import io
import logging
import math
import sys
from pathlib import Path
from typing import Any

import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, Rectangle, FancyBboxPatch
from matplotlib.font_manager import FontProperties
from shapely.geometry import shape

from config import cfg, FCM_CLASSES, FCM_COLORS

# ── Force Stream / Unbuffered Stdout Logging Setup for Koyeb Console ─────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(stdout_handler)

# ── Colour palette ────────────────────────────────────────────────────────────
PALETTE = {
    "bg":           "#f5f2eb",
    "panel":        "#ffffff",
    "border":       "#2c4a2e",
    "accent":       "#2c6e31",
    "text_dark":    "#1a2a1b",
    "text_mid":     "#3d5c3f",
    "poly_fill":    "#ffffffa0",
    "poly_edge":    "#d62828",
    "grid":         "#b0c4b1",
    "table_header": "#2c4a2e",
    "table_alt":    "#e8f0e9",
}


# ── Public entry point ────────────────────────────────────────────────────────

def render_map(
    geojson_feature: dict,
    results:         dict[str, Any],
    filename:        str = "output",
) -> bytes:
    """
    Render the full cartographic layout and return PNG bytes.

    Parameters
    ----------
    geojson_feature : GeoJSON Feature (Polygon or MultiPolygon in WGS-84)
    results         : dict from spatial_analysis.run_analysis()
    filename        : base name of the source file (for map title)

    Returns
    -------
    bytes : raw PNG image data
    """
    fig = _build_figure()
    ax_map, ax_legend, ax_table = _build_layout(fig)

    _draw_map_panel(ax_map, geojson_feature, results)
    _draw_legend(ax_legend, results)
    _draw_table(ax_table, results, filename)
    _draw_title(fig, filename)

    buf = io.BytesIO()
    fig.savefig(
        buf,
        format=cfg.OUTPUT_FORMAT.lower(),
        dpi=cfg.OUTPUT_DPI,
        facecolor=fig.get_facecolor(),
        bbox_inches="tight",
    )
    plt.close(fig)
    buf.seek(0)
    
    log.info("✅ Map successfully rendered  |  format=%s  |  dpi=%d", cfg.OUTPUT_FORMAT, cfg.OUTPUT_DPI)
    sys.stdout.flush()
    return buf.read()


# ── Figure & layout builders ──────────────────────────────────────────────────

def _build_figure() -> Figure:
    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor(PALETTE["bg"])
    # Outer border
    border = Rectangle(
        (0.01, 0.01), 0.98, 0.98,
        transform=fig.transFigure,
        linewidth=3, edgecolor=PALETTE["border"],
        facecolor="none", zorder=10
    )
    fig.add_artist(border)
    return fig

"""
def _build_layout(fig: Figure):
    gs = GridSpec(
        2, 2,
        figure=fig,
        left=0.04, right=0.96,
        top=0.88,  bottom=0.04,
        hspace=0.06, wspace=0.06,
        width_ratios=,
        height_ratios=,
    )
    ax_map    = fig.add_subplot(gs)   # Main map panel layout tracking
    ax_legend = fig.add_subplot(gs)   # Right panel legend layout tracking
    ax_table  = fig.add_subplot(gs[1, :])   # Bottom summary table layout tracking
    
    for ax in (ax_map, ax_legend, ax_table):
        ax.set_facecolor(PALETTE["panel"])
        for spine in ax.spines.values():
            spine.set_edgecolor(PALETTE["border"])
            spine.set_linewidth(1.2)
    return ax_map, ax_legend, ax_table
"""
def _build_layout(fig: Figure):
    gs = GridSpec(
        2, 2,
        figure=fig,
        left=0.04, right=0.96,
        top=0.88,  bottom=0.04,
        hspace=0.06, wspace=0.06,
        width_ratios=[],   # ✅ FIXED: Initialized as empty list to prevent SyntaxError
        height_ratios=[],  # ✅ FIXED: Initialized as empty list to prevent SyntaxError
    )
    ax_map    = fig.add_subplot(gs)   # ✅ FIXED: Tracked to top-left cell
    ax_legend = fig.add_subplot(gs)   # ✅ FIXED: Tracked to top-right cell
    ax_table  = fig.add_subplot(gs[1, :])   # Bottom summary table layout tracking
    
    for ax in (ax_map, ax_legend, ax_table):
        ax.set_facecolor(PALETTE["panel"])
        for spine in ax.spines.values():
            spine.set_edgecolor(PALETTE["border"])
            spine.set_linewidth(1.2)
    return ax_map, ax_legend, ax_table
  
# ── Map panel ─────────────────────────────────────────────────────────────────

def _draw_map_panel(ax, geojson_feature: dict, results: dict) -> None:
    geom = shape(geojson_feature["geometry"])
    minx, miny, maxx, maxy = geom.bounds

    # Add 15% padding around the geometry bounding context box
    pad_x = (maxx - minx) * 0.15 if (maxx - minx) > 0 else 0.01
    pad_y = (maxy - miny) * 0.15 if (maxy - miny) > 0 else 0.01
    xlim  = (minx - pad_x, maxx + pad_x)
    ylim  = (miny - pad_y, maxy + pad_y)

    # Light grey background grid setup
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_facecolor("#e8ede8")
    ax.grid(True, color=PALETTE["grid"], linewidth=0.5, linestyle="--", alpha=0.7)

    # 🚀 MULTI-POLYGON DRAW ENGINE: Iterate across all sub-geometries explicitly
    geoms_list = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)

    # Forest cover shade filling base layer
    fcm = results.get("fcm", {})
    dominant_name = fcm.get("dominant", "Non-Forest")
    class_id = next((cid for cid, name in FCM_CLASSES.items() if name == dominant_name), 5)
    shade_color = FCM_COLORS.get(class_id, "#e8e8e8")

    for part in geoms_list:
        coords = list(part.exterior.coords)
        xs = [c for c in coords]
        ys = [c for c in coords]
        
        # Shade background by classification
        ax.fill(xs, ys, color=shade_color, alpha=0.35, linewidth=0, zorder=2)
        # Translucent white baseline fill
        ax.fill(xs, ys, color=PALETTE["poly_fill"], linewidth=0, alpha=0.6, zorder=3)
        # Vector crisp perimeter line plot
        ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.0, solid_capstyle="round", zorder=4)

    # Coordinate grid labels
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f°E"))
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f°N"))
    ax.tick_params(axis="both", labelsize=7, color=PALETTE["text_mid"])

    # Scale bar & North arrow assets
    _draw_scale_bar(ax, xlim, ylim)
    _draw_north_arrow(ax, xlim, ylim)

    ax.set_aspect("equal")
    ax.set_xlabel("Longitude", fontsize=8, color=PALETTE["text_mid"])
    ax.set_ylabel("Latitude",  fontsize=8, color=PALETTE["text_mid"])


def _draw_scale_bar(ax, xlim, ylim) -> None:
    """Dynamic scale bar calculated from degree span."""
    span_deg   = xlim - xlim
    mid_lat    = (ylim + ylim) / 2
    km_per_deg = 111.0 * math.cos(math.radians(mid_lat))
    span_km    = span_deg * km_per_deg

    # Choose a round bar length (~20% of map width)
    bar_km     = _round_to_nice(span_km * 0.20)
    bar_deg    = bar_km / km_per_deg

    x0 = xlim + (xlim - xlim) * 0.05
    y0 = ylim + (ylim - ylim) * 0.05

    # Alternating black/white segments
    for i in range(4):
        seg_color = "black" if i % 2 == 0 else "white"
        ax.barh(
            y0, bar_deg / 4, left=x0 + i * bar_deg / 4,
            height=(ylim - ylim) * 0.012,
            color=seg_color, edgecolor="black", linewidth=0.5, zorder=6
        )

    ax.text(
        x0 + bar_deg / 2, y0 + (ylim - ylim) * 0.025,
        f"{bar_km:.1f} km",
        ha="center", va="bottom", fontsize=7,
        color=PALETTE["text_dark"], fontweight="bold", zorder=7
    )
    ax.text(
        x0, y0 - (ylim - ylim) * 0.01,
        "0", ha="center", fontsize=6, color=PALETTE["text_dark"], zorder=7
    )


def _draw_north_arrow(ax, xlim, ylim) -> None:
    """Simple north arrow in the top-right corner of the map."""
    x = xlim + (xlim - xlim) * 0.92
    y = ylim + (ylim - ylim) * 0.88
    h = (ylim - ylim) * 0.07

    ax.annotate(
        "", xy=(x, y + h), xytext=(x, y),
        arrowprops=dict(
            arrowstyle="-|>",
            color="black", lw=1.5,
            mutation_scale=14,
        ),
        zorder=8
    )
    ax.text(
        x, y + h + (ylim - ylim) * 0.01, "N",
        ha="center", va="bottom",
        fontsize=9, fontweight="bold",
        color=PALETTE["text_dark"], zorder=8
    )


# ── Legend panel ──────────────────────────────────────────────────────────────

def _draw_legend(ax, results: dict) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Legend", fontsize=10, fontweight="bold",
                 color=PALETTE["text_dark"], pad=6)

    y  = 0.92
    dy = 0.09

    # ── Forest Cover classes ──
    ax.text(0.05, y, "Forest Cover Classes (FSI)", fontsize=8,
            fontweight="bold", color=PALETTE["accent"],
            transform=ax.transAxes, va="top")
    y -= dy * 0.7

    fcm_classes = results.get("fcm", {}).get("classes", {})
    for class_name, color in FCM_COLORS.items():
        label = FCM_CLASSES[class_name]
        pct   = fcm_classes.get(label, {}).get("percentage", 0.0)
        if pct == 0.0 and label not in fcm_classes:
            continue
        patch = mpatches.Rectangle(
            (0.05, y - 0.025), 0.10, 0.048,
            transform=ax.transAxes,
            facecolor=color, edgecolor="#555", linewidth=0.5
        )
        ax.add_patch(patch)
        ax.text(
            0.20, y, f"{label} ({pct:.1f}%)",
            fontsize=7, color=PALETTE["text_dark"],
            transform=ax.transAxes, va="center"
        )
        y -= dy

    # ── Boundary symbol ──
    y -= dy * 0.4
    ax.plot(
        [0.05, 0.15], [y, y],
        color=PALETTE["poly_edge"], linewidth=2,
        transform=ax.transAxes
    )
    ax.text(0.20, y, "Study Area Boundary",
            fontsize=7, color=PALETTE["text_dark"],
            transform=ax.transAxes, va="center")

    # ── Data sources ──
    y -= dy * 1.4
    ax.text(0.05, y, "Data Sources:", fontsize=7,
            fontweight="bold", color=PALETTE["text_mid"],
            transform=ax.transAxes, va="top")
    y -= dy * 0.7
    for source in ["FSI Forest Cover Map", "SRTM DEM (30m)", "User-provided boundary"]:
        ax.text(0.05, y, f"• {source}", fontsize=6.5,
                color=PALETTE["text_mid"], transform=ax.transAxes, va="top")
        y -= dy * 0.65

    # ── Coordinate system ──
    y -= dy * 0.3
    ax.text(0.05, y, "CRS: WGS-84 (EPSG:4326)", fontsize=6.5,
            color=PALETTE["text_mid"], transform=ax.transAxes, va="top")


# ── Statistics table ──────────────────────────────────────────────────────────

def _draw_table(ax, results: dict, filename: str) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Spatial Analysis Summary", fontsize=10, fontweight="bold",
                 color=PALETTE["text_dark"], pad=4)

    dem = results.get("dem", {})
    fcm = results.get("fcm", {})
    centroid_vals = results.get('centroid', ('—', '—'))

    rows = [
        ("Total Area",         f"{results.get('area_ha', 0):.2f} hectares"),
        ("Dominant Cover",     fcm.get("dominant", "—")),
        ("Elevation (Min)",    f"{dem.get('elevation_min_m', '—')} m"),
        ("Elevation (Max)",    f"{dem.get('elevation_max_m', '—')} m"),
        ("Elevation (Mean)",   f"{dem.get('elevation_mean_m', '—')} m"),
        ("Mean Slope",         f"{dem.get('slope_mean_deg', '—')}°"),
        ("Max Slope",          f"{dem.get('slope_max_deg', '—')}°"),
        ("Source File",        filename),
        ("Centroid (lon/lat)", f"{centroid_vals}  /  {centroid_vals}"),
    ]

    mid  = math.ceil(len(rows) / 2)
    col1 = rows[:mid]
    col2 = rows[mid:]

    for col_idx, col_rows in enumerate((col1, col2)):
        x_label = 0.02 + col_idx * 0.50
        x_value = 0.22 + col_idx * 0.50

        for row_idx, (label, value) in enumerate(col_rows):
            y = 0.85 - row_idx * 0.19
            bg_color = PALETTE["table_alt"] if row_idx % 2 == 0 else PALETTE["panel"]
            rect = FancyBboxPatch(
                (x_label - 0.01, y - 0.09), 0.48, 0.16,
                boxstyle="round,pad=0.01",
                facecolor=bg_color, edgecolor="none",
                transform=ax.transAxes
            )
            ax.add_patch(rect)
            ax.text(x_label, y, label, fontsize=8, fontweight="bold",
                    color=PALETTE["text_mid"], transform=ax.transAxes, va="center")
            ax.text(x_value, y, value, fontsize=8,
                    color=PALETTE["text_dark"], transform=ax.transAxes, va="center")


# ── Title bar ─────────────────────────────────────────────────────────────────

def _draw_title(fig: Figure, filename: str) -> None:
    fig.text(
        0.50, 0.955,
        "SPATIAL DECISION SUPPORT SYSTEM — ANALYSIS REPORT",
        ha="center", va="center",
        fontsize=13, fontweight="bold",
        color=PALETTE["text_dark"],
    )
    fig.text(
        0.50, 0.935,
        f"MP Forest Department  |  File: {filename}",
        ha="center", va="center",
        fontsize=9, color=PALETTE["text_mid"],
        style="italic"
    )
    fig.add_artist(
        plt.Line2D(
            [0.04, 0.96], [0.925, 0.925],
            transform=fig.transFigure,
            color=PALETTE["border"], linewidth=1.5
        )
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _round_to_nice(val: float) -> float:
    """Round a value to a 'nice' number (1, 2, 5, 10, 20, 50 …)."""
    if val <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(val))
    residual  = val / magnitude
    if residual < 1.5:
        return 1 * magnitude
    elif residual < 3.5:
        return 2 * magnitude
    elif residual < 7.5:
        return 5 * magnitude
    else:
        return 10 * magnitude
      
