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
from typing import Any, Iterable

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
from matplotlib.patches import FancyBboxPatch, Rectangle
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    shape,
)

from config import cfg

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    log.addHandler(stdout_handler)

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
    "vdf": "#004d1a",
    "mdf": "#2d8f2d",
    "open": "#82cf4f",
    "nonforest": "#9a9a9a",
    "scrub": "#c08a5a",
    "water": "#4f9be8",
    "fallback": "#a3c2c2",
    "contour": "#7a5a3a",
    "contour_faint": "#8d6a47",
}


def render_map(
    geojson_feature: dict,
    results: dict[str, Any],
    filename: str = "output",
    map_mode: str = "fcm",
) -> io.BytesIO:
    """
    Render the cartographic layout and return PNG in-memory BytesIO stream.

    map_mode:
      - "fcm": forest cover focus
      - "dem": contour / elevation focus
    """
    mode = str(map_mode or results.get("_map_mode", "fcm")).strip().lower()
    if mode not in {"fcm", "dem"}:
        mode = "fcm"

    fig = _build_figure()
    ax_map, ax_legend, ax_table = _build_layout(fig)

    _draw_map_panel(ax_map, geojson_feature, results, mode)
    _draw_legend(ax_legend, results, mode)
    _draw_table(ax_table, results, filename, mode)
    _draw_title(fig, filename, mode)

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
    buf.name = f"{PathSafe(filename)}_{mode}.png"

    log.info(
        "✅ Map successfully rendered | mode=%s | format=%s | dpi=%d",
        mode,
        cfg.OUTPUT_FORMAT,
        cfg.OUTPUT_DPI,
    )
    sys.stdout.flush()
    return buf


def PathSafe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(name))


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


def _geom_to_xy(coords):
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
    if geom.geom_type in {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}:
        if geom.geom_type in {"MultiPolygon", "MultiLineString"}:
            return list(geom.geoms)
        return [geom]
    return []


def _match_fcm_color(class_attr: str) -> str:
    s = str(class_attr or "").strip().upper()
    if "VERY DENSE" in s or "VDF" in s:
        return PALETTE["vdf"]
    if "MODERATELY DENSE" in s or "MDF" in s:
        return PALETTE["mdf"]
    if "OPEN" in s or "OF" in s:
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
    else:
        ax.set_facecolor("#e8ede8")
        ax.grid(True, color=PALETTE["grid"], linewidth=0.5, linestyle="--", alpha=0.7)

    if mode == "fcm":
        _draw_fcm_layers(ax, results)
    else:
        _draw_dem_layers(ax, results)

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
    log.info(
        "MAP_TRACE | mode=fcm | raw_fcm_gdfs=%d | rows=%s",
        len(fcm_gdfs),
        [len(gdf) for gdf in fcm_gdfs] if fcm_gdfs else [],
    )

    for gdf in fcm_gdfs:
        if gdf is None or gdf.empty:
            continue

        if getattr(gdf, "crs", None) and str(gdf.crs).upper() != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")

        for _, row in gdf.iterrows():
            fcm_geom = row.geometry
            if fcm_geom is None or fcm_geom.is_empty:
                continue

            class_attr = str(row.get("class_name", "")).strip().upper()
            poly_color = _match_fcm_color(class_attr)

            for part in _iter_geoms(fcm_geom):
                if part.geom_type == "Polygon":
                    xs, ys = _geom_to_xy(list(part.exterior.coords))
                    ax.fill(xs, ys, color=poly_color, alpha=0.55, linewidth=0, zorder=2)
                    for ring in part.interiors:
                        hole_xs, hole_ys = _geom_to_xy(list(ring.coords))
                        ax.fill(
                            hole_xs,
                            hole_ys,
                            color=ax.get_facecolor(),
                            linewidth=0,
                            zorder=2.5,
                        )
                elif part.geom_type == "MultiPolygon":
                    for sub in part.geoms:
                        xs, ys = _geom_to_xy(list(sub.exterior.coords))
                        ax.fill(xs, ys, color=poly_color, alpha=0.55, linewidth=0, zorder=2)
                elif part.geom_type in {"LineString", "MultiLineString"}:
                    _plot_lines(ax, part, color=poly_color, linewidth=1.0, alpha=0.55, zorder=2)


def _draw_dem_layers(ax, results: dict) -> None:
    dem_gdfs = results.get("_raw_dem_gdfs", [])
    log.info(
        "MAP_TRACE | mode=dem | raw_dem_gdfs=%d | rows=%s",
        len(dem_gdfs),
        [len(gdf) for gdf in dem_gdfs] if dem_gdfs else [],
    )

    for gdf in dem_gdfs:
        if gdf is None or gdf.empty:
            continue

        if getattr(gdf, "crs", None) and str(gdf.crs).upper() != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")

        elev_col = next(
            (c for c in gdf.columns if str(c).lower() in {"elevation", "elev", "contour", "z"}),
            None,
        )

        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue

            color = PALETTE["contour"]
            if elev_col and pd.notna(row.get(elev_col)):
                color = PALETTE["contour_faint"]

            for part in _iter_geoms(geom):
                if part.geom_type == "LineString":
                    _plot_lines(ax, part, color=color, linewidth=0.8, alpha=0.85, zorder=2)
                elif part.geom_type == "MultiLineString":
                    _plot_lines(ax, part, color=color, linewidth=0.8, alpha=0.85, zorder=2)
                elif part.geom_type == "Polygon":
                    xs, ys = _geom_to_xy(list(part.exterior.coords))
                    ax.plot(xs, ys, color=color, linewidth=0.75, alpha=0.7, zorder=2)
                elif part.geom_type == "MultiPolygon":
                    for sub in part.geoms:
                        xs, ys = _geom_to_xy(list(sub.exterior.coords))
                        ax.plot(xs, ys, color=color, linewidth=0.75, alpha=0.7, zorder=2)


def _plot_lines(ax, geom, color: str, linewidth: float, alpha: float, zorder: int) -> None:
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "LineString":
        xs, ys = _geom_to_xy(list(geom.coords))
        ax.plot(xs, ys, color=color, linewidth=linewidth, alpha=alpha, zorder=zorder)
    elif geom.geom_type == "MultiLineString":
        for part in geom.geoms:
            xs, ys = _geom_to_xy(list(part.coords))
            ax.plot(xs, ys, color=color, linewidth=linewidth, alpha=alpha, zorder=zorder)


def _draw_study_boundary(ax, geom) -> None:
    geoms_list = _iter_geoms(geom)
    for part in geoms_list:
        if part.geom_type == "Polygon":
            xs, ys = _geom_to_xy(list(part.exterior.coords))
            ax.fill(xs, ys, color=PALETTE["poly_fill"], linewidth=0, alpha=0.15, zorder=3)
            ax.plot(xs, ys, color=PALETTE["poly_edge"], linewidth=2.5, solid_capstyle="round", zorder=4)
            for ring in part.interiors:
                hole_xs, hole_ys = _geom_to_xy(list(ring.coords))
                ax.fill(hole_xs, hole_ys, color=ax.get_facecolor(), linewidth=0, zorder=3.5)
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


def _draw_legend(ax, results: dict, mode: str) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Legend", fontsize=10, fontweight="bold", color=PALETTE["text_dark"], pad=6)

    y = 0.92
    dy = 0.075

    if mode == "dem":
        ax.text(
            0.05,
            y,
            "Contour Elevation View",
            fontsize=8,
            fontweight="bold",
            color=PALETTE["accent"],
            transform=ax.transAxes,
            va="top",
        )
        y -= dy * 0.7

        patch = mpatches.Rectangle(
            (0.05, y - 0.022),
            0.10,
            0.044,
            transform=ax.transAxes,
            facecolor=PALETTE["contour"],
            edgecolor="#555",
            linewidth=0.5,
        )
        ax.add_patch(patch)
        ax.text(
            0.20,
            y,
            "Contours / Elevation Lines",
            fontsize=7,
            color=PALETTE["text_dark"],
            transform=ax.transAxes,
            va="center",
        )
        y -= dy

        dem = results.get("dem", {})
        for label, value in [
            ("Elevation (Min)", dem.get("elevation_min_m", "—")),
            ("Elevation (Max)", dem.get("elevation_max_m", "—")),
            ("Elevation (Mean)", dem.get("elevation_mean_m", "—")),
        ]:
            ax.text(
                0.05,
                y,
                f"{label}: {value}",
                fontsize=7,
                color=PALETTE["text_dark"],
                transform=ax.transAxes,
                va="top",
            )
            y -= dy * 0.7

    else:
        ax.text(
            0.05,
            y,
            "Forest Cover Classes (FCM)",
            fontsize=8,
            fontweight="bold",
            color=PALETTE["accent"],
            transform=ax.transAxes,
            va="top",
        )
        y -= dy * 0.7

        fcm_classes = results.get("fcm", {}).get("classes", {})
        log.info("LEGEND_TRACE | keys=%s", list(fcm_classes.keys()))

        if not fcm_classes:
            ax.text(
                0.05,
                y,
                "No class summary available",
                fontsize=7,
                color=PALETTE["text_mid"],
                transform=ax.transAxes,
                va="top",
            )
            y -= dy
        else:
            order = ["VDF", "MDF", "OPEN FOREST", "NON FOREST", "SCRUB", "WATER", "NO-DATA"]
            used = set()

            for label in order:
                if label not in fcm_classes:
                    continue
                pct = fcm_classes.get(label, {}).get("percentage", 0.0)
                color = _match_fcm_color(label)
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
                used.add(label)

            for label, metrics in fcm_classes.items():
                if label in used:
                    continue
                pct = metrics.get("percentage", 0.0)
                color = _match_fcm_color(label)
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

        y -= dy * 0.2
        ax.plot([0.05, 0.15], [y, y], color=PALETTE["poly_edge"], linewidth=2, transform=ax.transAxes)
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

    for source in ["FSI Forest Cover Map", "DEM contours / raster", "User-provided boundary"]:
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


def _draw_table(ax, results: dict, filename: str, mode: str) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    title = "Spatial Analysis Summary" if mode == "fcm" else "Contour Elevation Summary"
    ax.set_title(title, fontsize=10, fontweight="bold", color=PALETTE["text_dark"], pad=4)

    dem = results.get("dem", {})
    fcm = results.get("fcm", {})

    centroid_x, centroid_y = results.get("centroid", ("—", "—"))
    if isinstance(centroid_x, (int, float)) and isinstance(centroid_y, (int, float)):
        centroid_str = f"{centroid_x:.6f}, {centroid_y:.6f}"
    else:
        centroid_str = "—"

    rows = [
        ("Map Mode", mode.upper()),
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


def _draw_title(fig: Figure, filename: str, mode: str) -> None:
    header = "FOREST COVER MAP" if mode == "fcm" else "CONTOUR ELEVATION MAP"
    fig.text(
        0.50,
        0.955,
        f"SPATIAL DECISION SUPPORT SYSTEM — {header}",
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
