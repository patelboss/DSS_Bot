"""
modules/map_renderer.py — Programmatic cartographic layout engine.
Produces a publication-quality PNG with:
  • Polygon & Multi-Polygon boundaries drawn over a light basemap grid
  • North arrow (custom SVG patch annotations)
  • Auto-calculating geographic scale bar
  • Dynamic legend for forest cover classes
  • Summary statistics table in the footer panel
"""

import io
import logging
import math
import sys
from typing import Any

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
from matplotlib.patches import Rectangle, FancyBboxPatch
from shapely.geometry import shape

from config import cfg, FCM_CLASSES, FCM_COLORS

# ── Force Stream / Unbuffered Stdout Logging Setup for Koyeb Console ─────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    log.addHandler(stdout_handler)

# ── Colour palette ────────────────────────────────────────────────────────────
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
    "table_header": "#2c4a2e",
    "table_alt": "#e8f0e9",
}


# ── Public entry point ────────────────────────────────────────────────────────
def render_map(
    geojson_feature: dict,
    results: dict[str, Any],
    filename: str = "output",
) -> io.BytesIO:
    """
    Render the full cartographic layout and return PNG in-memory BytesIO stream.
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

    log.info(
        "✅ Map successfully rendered  |  format=%s  |  dpi=%d",
        cfg.OUTPUT_FORMAT,
        cfg.OUTPUT_DPI,
    )
    sys.stdout.flush()
    return buf


# ── Figure & layout builders ──────────────────────────────────────────────────
def _build_figure() -> Figure:
    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor(PALETTE["bg"])

    border = Rectangle(
        (0.01, 0.01),
        0.98,
        0.98,
        transform=fig.transFigure,
        linewidth=3,
        edgecolor=PALETTE["border"],
        facecolor="none",
        zorder=10,
    )
    fig.add_artist(border)
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


# ── Helpers ───────────────────────────────────────────────────────────────────
def _geom_to_xy(coords):
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return xs, ys


def _iter_polygons(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    return []


# ── Map panel ─────────────────────────────────────────────────────────────────

def _draw_map_panel(ax, geojson_feature: dict, results: dict) -> None:
  
  
    geom = shape(geojson_feature["geometry"])
    minx, miny, maxx, maxy = geom.bounds

    pad_x = (maxx - minx) * 0.15 if (maxx - minx) > 0 else 0.01
    pad_y = (maxy - miny) * 0.15 if (maxy - miny) > 0 else 0.01
    xlim = (minx - pad_x, maxx + pad_x)
    ylim = (miny - pad_y, maxy + pad_y)

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_facecolor("#e8ede8")
    ax.set_axisbelow(True)
    ax.grid(True, color=PALETTE["grid"], linewidth=0.5, linestyle="--", alpha=0.7)

    # ── 1. DYNAMICALLY PLOT INTERSECTED CANOPY POLYGONS ──
    fcm_gdfs = results.get("_raw_fcm_gdfs", [])
    if fcm_gdfs:
        for gdf in fcm_gdfs:
            if gdf is None or gdf.empty:
                continue

            if getattr(gdf, "crs", None) and gdf.crs.to_string() != "EPSG:4326":
                gdf = gdf.to_crs("EPSG:4326")

            for _, row in gdf.iterrows():
                fcm_geom = row.geometry
                if fcm_geom is None or fcm_geom.is_empty:
                    continue

                fcm_parts = _iter_polygons(fcm_geom)
                if not fcm_parts:
                    continue

                class_attr = str(row.get("class_name", "")).strip().upper()

                if "VDF" in class_attr:
                    poly_color = FCM_COLORS.get(1, "#07380e")
                elif "MDF" in class_attr:
                    poly_color = FCM_COLORS.get(2, "#17d133")
                elif "OPEN FOREST" in class_attr:
                    poly_color = FCM_COLORS.get(3, "#c1c70e")
                elif "NON FOREST" in class_attr:
                    poly_color = FCM_COLORS.get(4, "#8c8c88")
                elif "SCRUB" in class_attr:
                    poly_color = FCM_COLORS.get(5, "#ab180e")
                elif "WATER" in class_attr:
                    poly_color = FCM_COLORS.get(6, "#5064fa")
                else:
                    poly_color = FCM_COLORS.get(0, "#25a8a8")

                for f_part in fcm_parts:
                    f_coords = list(f_part.exterior.coords)
                    f_xs, f_ys = _geom_to_xy(f_coords)
                    ax.fill(
                        f_xs,
                        f_ys,
                        color=poly_color,
                        alpha=0.65,
                        linewidth=0,
                        zorder=2,
                    )

                    for ring in f_part.interiors:
                        hole_coords = list(ring.coords)
                        hole_xs, hole_ys = _geom_to_xy(hole_coords)
                        ax.fill(
                            hole_xs,
                            hole_ys,
                            color=ax.get_facecolor(),
                            linewidth=0,
                            zorder=2.5,
                        )

  

    # ── 2. PLOT RED VECTOR STUDY BOUNDARY OVER THE TOP ──
    geoms_list = _iter_polygons(geom)

    for part in geoms_list:
        coords = list(part.exterior.coords)
        xs, ys = _geom_to_xy(coords)

        ax.fill(
            xs,
            ys,
            color=PALETTE["poly_fill"],
            linewidth=0,
            alpha=0.15,
            zorder=3,
        )
        ax.plot(
            xs,
            ys,
            color=PALETTE["poly_edge"],
            linewidth=2.5,
            solid_capstyle="round",
            zorder=4,
        )

        for ring in part.interiors:
            hole_coords = list(ring.coords)
            hole_xs, hole_ys = _geom_to_xy(hole_coords)
            ax.fill(
                hole_xs,
                hole_ys,
                color=ax.get_facecolor(),
                linewidth=0,
                zorder=3.5,
            )

    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f°E"))
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f°N"))
    ax.tick_params(axis="both", labelsize=7, color=PALETTE["text_mid"])

    _draw_scale_bar(ax, xlim, ylim)
    _draw_north_arrow(ax, xlim, ylim)

    ax.set_aspect("equal")
    ax.set_xlabel("Longitude", fontsize=8, color=PALETTE["text_mid"])
    ax.set_ylabel("Latitude", fontsize=8, color=PALETTE["text_mid"])
    log.info("MAP_TRACE | raw_gdfs=%d | rows=%s",len(fcm_gdfs),[len(gdf) for gdf in fcm_gdfs])



def _draw_scale_bar(ax, xlim, ylim) -> None:
    span_deg = xlim[1] - xlim[0]
    y_span = ylim[1] - ylim[0]
    mid_lat = (ylim[0] + ylim[1]) / 2

    km_per_deg = max(111.0 * math.cos(math.radians(mid_lat)), 0.0001)
    span_km = span_deg * km_per_deg

    bar_km = _round_to_nice(span_km * 0.20)
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
    ax.text(
        x0,
        y0 - y_span * 0.01,
        "0",
        ha="center",
        fontsize=6,
        color=PALETTE["text_dark"],
        zorder=7,
    )


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
        arrowprops=dict(
            arrowstyle="-|>",
            color="black",
            lw=1.5,
            mutation_scale=14,
        ),
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

# ── Legend panel ──────────────────────────────────────────────────────────────
def _draw_legend(ax, results: dict) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(
        "Legend",
        fontsize=10,
        fontweight="bold",
        color=PALETTE["text_dark"],
        pad=6,
    )

    y = 0.92
    dy = 0.075

    ax.text(
        0.05,
        y,
        "Forest Cover Classes (FSI)",
        fontsize=8,
        fontweight="bold",
        color=PALETTE["accent"],
        transform=ax.transAxes,
        va="top",
    )
    y -= dy * 0.7

    fcm_classes = results.get("fcm", {}).get("classes", {})
    
    # Precise dictionary translation to match long strings generated by cmd.py tracking loops
    label_bridge = {
        1: "VDF",
        2: "MDF",
        3: "OPEN Forest",
        4: "NON Forest",
        5: "SCRUB",
        6: "Water",
        0: "No Data"
    }

    # Iterate through standard configuration elements cleanly
    for class_id in sorted(FCM_COLORS.keys()):
        color = FCM_COLORS[class_id]
        report_label = label_bridge.get(class_id, FCM_CLASSES.get(class_id, f"Class {class_id}"))
        
        # Pull values out matching long report labels safely
        pct = fcm_classes.get(report_label, {}).get("percentage", 0.0)

        patch = mpatches.Rectangle(
            (0.05, y - 0.022),
            0.10,
            0.044,
            transform=ax.transAxes,
            facecolor=color,
            edgecolor="#555",
            linewidth=0.5,
        )
        ax.add_patch(patch)
        
        # Display short-form upper tokens alongside active calculated percentages
        display_name = FCM_CLASSES.get(class_id, report_label).upper()
        ax.text(
            0.20,
            y,
            f"{display_name} ({pct:.1f}%)",
            fontsize=7,
            color=PALETTE["text_dark"],
            transform=ax.transAxes,
            va="center",
        )
        y -= dy

    # Separates a standalone "No Data" fallback label if it drops outside standard loops
    if "No Data" in fcm_classes:
        nd_pct = fcm_classes.get("No Data", {}).get("percentage", 0.0)
        patch = mpatches.Rectangle((0.05, y - 0.022), 0.10, 0.044, transform=ax.transAxes, facecolor=FCM_COLORS.get(0, "#25a8a8"), edgecolor="#555", linewidth=0.5)
        ax.add_patch(patch)
        ax.text(0.20, y, f"NO DATA ({nd_pct:.1f}%)", fontsize=7, color=PALETTE["text_dark"], transform=ax.transAxes, va="center")
        y -= dy

    y -= dy * 0.2
    ax.plot(
        [0.05, 0.15],
        [y, y],
        color=PALETTE["poly_edge"],
        linewidth=2,
        transform=ax.transAxes,
    )
    ax.text(
        0.20,
        y,
        "Study Area Boundary",
        fontsize=7,
        color=PALETTE["text_dark"],
        transform=ax.transAxes,
        va="center",
    )

    y -= dy * 1.1
    ax.text(
        0.05,
        y,
        "Data Sources:",
        fontsize=7,
        fontweight="bold",
        color=PALETTE["text_mid"],
        transform=ax.transAxes,
        va="top",
    )
    y -= dy * 0.6

    for source in ["FSI Forest Cover Map", "SRTM DEM (30m)", "User-provided boundary"]:
        ax.text(
            0.05,
            y,
            f"• {source}",
            fontsize=6.5,
            color=PALETTE["text_mid"],
            transform=ax.transAxes,
            va="top",
        )
        y -= dy * 0.55

    y -= dy * 0.3
    ax.text(
        0.05,
        y,
        "CRS: WGS-84 (EPSG:4326)",
        fontsize=6.5,
        color=PALETTE["text_mid"],
        transform=ax.transAxes,
        va="top",
        )
  
"""
# ── Legend panel ──────────────────────────────────────────────────────────────
def _draw_legend(ax, results: dict) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(
        "Legend",
        fontsize=10,
        fontweight="bold",
        color=PALETTE["text_dark"],
        pad=6,
    )

    y = 0.92
    dy = 0.08

    ax.text(
        0.05,
        y,
        "Forest Cover Classes (FSI)",
        fontsize=8,
        fontweight="bold",
        color=PALETTE["accent"],
        transform=ax.transAxes,
        va="top",
    )
    y -= dy * 0.7

    fcm_classes = results.get("fcm", {}).get("classes", {})
    label_bridge = {
        1: "Very Dense Forest",
        2: "Moderately Dense Forest",
        3: "Open Forest",
        4: "Non-Forest",
        5: "Scrub"
    }

    for class_id in sorted(FCM_COLORS):
        color = FCM_COLORS[class_id]
        report_label = label_bridge.get(class_id, FCM_CLASSES[class_id])
        
       
        #label = FCM_CLASSES.get(class_id, str(class_id))
        pct = fcm_classes.get(label, {}).get("percentage", 0.0)

        patch = mpatches.Rectangle(
            (0.05, y - 0.022),
            0.10,
            0.044,
            transform=ax.transAxes,
            facecolor=color,
            edgecolor="#555",
            linewidth=0.5,
        )
        ax.add_patch(patch)
        ax.text(
            0.20,
            y,
            f"{label} ({pct:.1f}%)",
            fontsize=7,
            color=PALETTE["text_dark"],
            transform=ax.transAxes,
            va="center",
        )
        y -= dy

    if "Water Body" in fcm_classes or results.get("_has_water"):
        water_pct = fcm_classes.get("Water Body", {}).get("percentage", 0.0)
        patch = mpatches.Rectangle(
            (0.05, y - 0.022),
            0.10,
            0.044,
            transform=ax.transAxes,
            facecolor="#3399ff",
            edgecolor="#555",
            linewidth=0.5,
        )
        ax.add_patch(patch)
        ax.text(
            0.20,
            y,
            f"Water Body ({water_pct:.1f}%)",
            fontsize=7,
            color=PALETTE["text_dark"],
            transform=ax.transAxes,
            va="center",
        )
        y -= dy

    y -= dy * 0.2
    ax.plot(
        [0.05, 0.15],
        [y, y],
        color=PALETTE["poly_edge"],
        linewidth=2,
        transform=ax.transAxes,
    )
    ax.text(
        0.20,
        y,
        "Study Area Boundary",
        fontsize=7,
        color=PALETTE["text_dark"],
        transform=ax.transAxes,
        va="center",
    )

    y -= dy * 1.1
    ax.text(
        0.05,
        y,
        "Data Sources:",
        fontsize=7,
        fontweight="bold",
        color=PALETTE["text_mid"],
        transform=ax.transAxes,
        va="top",
    )
    y -= dy * 0.6

    for source in ["FSI Forest Cover Map", "SRTM DEM (30m)", "User-provided boundary"]:
        ax.text(
            0.05,
            y,
            f"• {source}",
            fontsize=6.5,
            color=PALETTE["text_mid"],
            transform=ax.transAxes,
            va="top",
        )
        y -= dy * 0.55

    y -= dy * 0.3
    ax.text(
        0.05,
        y,
        "CRS: WGS-84 (EPSG:4326)",
        fontsize=6.5,
        color=PALETTE["text_mid"],
        transform=ax.transAxes,
        va="top",
    )

"""
# ── Statistics table ──────────────────────────────────────────────────────────
def _draw_table(ax, results: dict, filename: str) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(
        "Spatial Analysis Summary",
        fontsize=10,
        fontweight="bold",
        color=PALETTE["text_dark"],
        pad=4,
    )

    dem = results.get("dem", {})
    fcm = results.get("fcm", {})

    centroid_x, centroid_y = results.get("centroid", ("—", "—"))
    if isinstance(centroid_x, (int, float)) and isinstance(centroid_y, (int, float)):
        centroid_str = f"{centroid_x:.6f}, {centroid_y:.6f}"
    else:
        centroid_str = "—"

    rows = [
        ("Total Area", f"{results.get('area_ha', 0):.2f} hectares"),
        ("Dominant Cover", fcm.get("dominant", "—")),
        ("Elevation (Min)", f"{dem.get('elevation_min_m', '—')} m"),
        ("Elevation (Max)", f"{dem.get('elevation_max_m', '—')} m"),
        ("Elevation (Mean)", f"{dem.get('elevation_mean_m', '—')} m"),
        ("Mean Slope", f"{dem.get('slope_mean_deg', '—')}°"),
        ("Max Slope", f"{dem.get('slope_max_deg', '—')}°"),
        ("Source File", filename),
        ("Centroid (lon/lat)", centroid_str),
    ]

    mid = math.ceil(len(rows) / 2)
    col1 = rows[:mid]
    col2 = rows[mid:]

    for col_idx, col_rows in enumerate((col1, col2)):
        x_label = 0.02 + col_idx * 0.50
        x_value = 0.22 + col_idx * 0.50

        for row_idx, (label, value) in enumerate(col_rows):
            y = 0.85 - row_idx * 0.19
            bg_color = PALETTE["table_alt"] if row_idx % 2 == 0 else PALETTE["panel"]

            rect = FancyBboxPatch(
                (x_label - 0.01, y - 0.09),
                0.48,
                0.16,
                boxstyle="round,pad=0.01",
                facecolor=bg_color,
                edgecolor="none",
                transform=ax.transAxes,
            )
            ax.add_patch(rect)

            ax.text(
                x_label,
                y,
                label,
                fontsize=8,
                fontweight="bold",
                color=PALETTE["text_mid"],
                transform=ax.transAxes,
                va="center",
            )
            ax.text(
                x_value,
                y,
                value,
                fontsize=8,
                color=PALETTE["text_dark"],
                transform=ax.transAxes,
                va="center",
            )


# ── Title bar ─────────────────────────────────────────────────────────────────
def _draw_title(fig: Figure, filename: str) -> None:
    fig.text(
        0.50,
        0.955,
        "SPATIAL DECISION SUPPORT SYSTEM — ANALYSIS REPORT",
        ha="center",
        va="center",
        fontsize=13,
        fontweight="bold",
        color=PALETTE["text_dark"],
    )
    fig.text(
        0.50,
        0.935,
        f"MP Forest Department  |  File: {filename}",
        ha="center",
        va="center",
        fontsize=9,
        color=PALETTE["text_mid"],
        style="italic",
    )
    fig.add_artist(
        plt.Line2D(
            [0.04, 0.96],
            [0.925, 0.925],
            transform=fig.transFigure,
            color=PALETTE["border"],
            linewidth=1.5,
        )
    )


# ── Helpers ───────────────────────────────────────────────────────────────────
def _round_to_nice(val: float) -> float:
    if val <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(val))
    residual = val / magnitude
    if residual < 1.5:
        return 1 * magnitude
    elif residual < 3.5:
        return 2 * magnitude
    elif residual < 7.5:
        return 5 * magnitude
    else:
        return 10 * magnitude
