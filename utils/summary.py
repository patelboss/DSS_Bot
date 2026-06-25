"""
utils/summary.py — Summary-only report builder.

Provides:
- English summary page
- Hindi summary page
- Key facts page
- Thank-you page

This module is imported by modules/map_renderer.py so the full PDF remains a
single report.
"""

from __future__ import annotations

import gc
import io
import logging
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import matplotlib

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(handler)

log.info("--- INITIALIZING SUMMARY.PY ---")

try:
    import mplcairo  # noqa: F401
    matplotlib.use("module://mplcairo.base", force=True)
    log.info("SUCCESS: 'mplcairo' backend activated.")
except Exception as e:
    log.warning(f"FAILED to load 'mplcairo' ({e}). Falling back to 'Agg'.")
    matplotlib.use("Agg", force=True)

try:
    import pypdf
    log.info("SUCCESS: pypdf imported successfully for merging pages.")
except ImportError:
    raise ImportError("Please install pypdf via 'pip install pypdf' to merge high-quality shaped PDF pages.")

try:
    if "text.parse_math" in matplotlib.rcParams:
        matplotlib.rcParams["text.parse_math"] = False
except Exception:
    pass

matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
matplotlib.rcParams["axes.unicode_minus"] = False

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Rectangle

# Grab centralized configuration setup
from config import cfg

PALETTE = {
    "bg": "#f5f2eb",
    "panel": "#ffffff",
    "border": "#2c4a2e",
    "accent": "#2c6e31",
    "text_dark": "#1a2a1b",
    "text_mid": "#3d5c3f",
    "table_alt": "#e8f0e9",
    "thank_bg": "#e7f3e6",
    "thank_text": "#1f5f2a",
}

FCM_LABELS = {
    "VDF": ("Very Dense Forest", "अत्यधिक घना वन"),
    "MDF": ("Moderately Dense Forest", "मध्यम घना वन"),
    "OPEN FOREST": ("Open Forest", "खुला वन"),
    "NON FOREST": ("Non Forest", "गैर-वन"),
    "SCRUB": ("Scrub", "झाड़ीदार क्षेत्र"),
    "WATER": ("Water", "जल"),
    "NO-DATA": ("No Data", "डेटा अनुपलब्ध"),
}

FCM_ALIASES = {
    "VERY DENSE FOREST": "VDF", "VERY DENSE": "VDF", "VDF": "VDF",
    "MODERATELY DENSE FOREST": "MDF", "MODERATELY DENSE": "MDF", "MDF": "MDF",
    "OPEN FOREST": "OPEN FOREST", "OPEN": "OPEN FOREST",
    "NON FOREST": "NON FOREST", "NON-FOREST": "NON FOREST", "NON FOREST AREA": "NON FOREST",
    "SCRUB": "SCRUB", "WATER": "WATER",
    "NO DATA": "NO-DATA", "NO-DATA": "NO-DATA", "NODATA": "NO-DATA",
}

# Pull font parameters directly from unified environment config instance
_DEVA_FP = cfg.fonts.props
_OUTPUT_DPI = int(getattr(cfg, "OUTPUT_DPI", 200) or 200)

def PathSafe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(name))

def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value) if value is not None else default
    except Exception:
        return default

def _normalize_fcm_label(label: Any) -> str:
    key = str(label or "").strip().upper()
    if not key:
        return "NO-DATA"
    return key if key in FCM_LABELS else FCM_ALIASES.get(key, key)

def _friendly_fcm_label(raw_label: str | None, lang: str = "en") -> str:
    key = _normalize_fcm_label(raw_label)
    en, hi = FCM_LABELS.get(key, (key or "No Data", "डेटा अनुपलब्ध"))
    return en if lang == "en" else hi

def _auto_sort_fcm_classes(fcm_classes: dict[str, dict[str, Any]]) -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    for label, metrics in (fcm_classes or {}).items():
        pct = _safe_float((metrics or {}).get("percentage", 0.0), 0.0) or 0.0
        items.append((_normalize_fcm_label(label), pct))
    # Crucial Fix: Sort by the percentage item index properly
    items.sort(key=lambda x: x, reverse=True)
    return items

def prepare_report_content(results: dict[str, Any]) -> dict[str, Any]:
    area = _safe_float(results.get("area_ha", 0.0), 0.0) or 0.0
    fcm = results.get("fcm", {}) or {}
    classes = fcm.get("classes", {}) or {}

    normalized_classes: dict[str, dict[str, Any]] = {}
    for raw_label, metrics in classes.items():
        canonical = _normalize_fcm_label(raw_label)
        normalized_classes[canonical] = {"percentage": _safe_float((metrics or {}).get("percentage", 0.0), 0.0) or 0.0}

    dominant_raw = _normalize_fcm_label(fcm.get("dominant", ""))
    if dominant_raw not in normalized_classes and normalized_classes:
        dominant_raw = max(normalized_classes.items(), key=lambda kv: kv["percentage"])

    dominant_en = _friendly_fcm_label(dominant_raw, "en")
    dominant_hi = _friendly_fcm_label(dominant_raw, "hi")
    dominant_pct = float(normalized_classes.get(dominant_raw, {}).get("percentage", 0.0) or 0.0)

    dem = results.get("dem", {}) or {}
    elev_min = dem.get("elevation_min_m")
    elev_max = dem.get("elevation_max_m")
    elev_mean = dem.get("elevation_mean_m")

    if elev_min is not None and elev_max is not None:
        elevation_range_en = f"{float(elev_min):.0f}–{float(elev_max):.0f} metres above mean sea level"
        elevation_range_hi = f"{float(elev_min):.0f}–{float(elev_max):.0f} मीटर समुद्र तल से ऊपर"
    else:
        elevation_range_en = "not available"
        elevation_range_hi = "उपलब्ध नहीं"

    mean_en = f"{float(elev_mean):.0f} metres" if elev_mean is not None else "not available"
    mean_hi = f"{float(elev_mean):.0f} मीटर" if elev_mean is not None else "उपलब्ध नहीं"

    sorted_classes = _auto_sort_fcm_classes(normalized_classes)
    non_dom_classes = [l for l, _ in sorted_classes if l != dominant_raw and l not in {"WATER", "NO-DATA"}]

    secondary_text_en, secondary_text_hi = "", ""
    if non_dom_classes:
        top2_en = [_friendly_fcm_label(lbl, "en") for lbl in non_dom_classes[:2]]
        top2_hi = [_friendly_fcm_label(lbl, "hi") for lbl in non_dom_classes[:2]]
        secondary_text_en = f" The next dominant classes, where present, are {', '.join(top2_en)}."
        secondary_text_hi = f" उपलब्ध होने पर द्वितीय एवं तृतीय प्रमुख आवरण वर्ग {', '.join(top2_hi)} हैं।"

    summary_en = str(results.get("summary_en") or "").strip()
    summary_hi = str(results.get("summary_hi") or "").strip()

    if not summary_en:
        summary_en = (
            f"The analysed area covers {area:.2f} hectares.\n\n"
            f"Forest Cover Mapping indicates that {dominant_en} is the dominant forest class, "
            f"accounting for {dominant_pct:.1f}% of the total area.\n\n"
            f"Terrain analysis derived from DEM data shows an elevation range of {elevation_range_en}, "
            f"with an average elevation of {mean_en}.\n\n"
            f"The area is predominantly forested with limited non-forest and scrub patches.{secondary_text_en}"
        )

    if not summary_hi:
        summary_hi = (
            f"विश्लेषण किए गए क्षेत्र का कुल क्षेत्रफल {area:.2f} हेक्टेयर है।\n\n"
            f"वन आवरण मैपिंग से पता चलता है कि {dominant_hi} मुख्य वन वर्ग है, "
            f"जो कुल क्षेत्रफल का {dominant_pct:.1f}% हिस्सा है।\n\n"
            f"DEM डेटा से किए गए भू-भाग विश्लेषण से पता चलता है कि समुद्र तल से ऊंचाई {elevation_range_hi} है, "
            f"और औसत ऊंचाई {mean_hi} है।\n\n"
            f"यह क्षेत्र मुख्य रूप से वनाच्छादित है, तथा गैर-वन और झाड़ीदार क्षेत्र सीमित हैं।{secondary_text_hi}"
        )

    keyfacts_lines = results.get("key_facts_lines")
    if not keyfacts_lines:
        keyfacts_lines = [
            f"- Total Area / कुल क्षेत्रफल: {area:.2f} ha",
            f"- Dominant Forest Class / प्रमुख वन वर्ग: {dominant_en} ({dominant_pct:.1f}%)",
            "- Forest Cover Distribution / वन आवरण वितरण:",
        ]
        preferred_order = ["VDF", "MDF", "OPEN FOREST", "NON FOREST", "SCRUB", "WATER", "NO-DATA"]
        seen = set()
        for label in preferred_order:
            canonical = _normalize_fcm_label(label)
            if canonical in normalized_classes:
                pct = float(normalized_classes.get(canonical, {}).get("percentage", 0.0) or 0.0)
                keyfacts_lines.append(f"    - {canonical} / {_friendly_fcm_label(canonical, 'hi')}: {pct:.1f}%")
                seen.add(canonical)
        for label, metrics in sorted_classes:
            if label not in seen:
                pct = float(metrics if isinstance(metrics, (int, float)) else 0.0)
                keyfacts_lines.append(f"    - {label}: {pct:.1f}%")
        if elev_min is not None and elev_max is not None:
            keyfacts_lines.append(f"- Elevation Range / ऊँचाई सीमा: {float(elev_min):.0f}–{float(elev_max):.0f} m")
        if elev_mean is not None:
            keyfacts_lines.append(f"- Mean Elevation / औसत ऊँचाई: {float(elev_mean):.0f} m")

    return {
        "summary_en": summary_en.strip(),
        "summary_hi": summary_hi.strip(),
        "keyfacts_lines": [str(x) for x in keyfacts_lines],
    }

def render_summary_pdf(results: dict[str, Any], filename: str = "output") -> io.BytesIO:
    log.info("Starting pure mplcairo single-page split render...")
    
    pages = [
        build_summary_figure(results, filename),
        build_keyfacts_figure(results, filename),
        build_thankyou_figure(results, filename),
    ]
    total_pages = len(pages)
    
    pdf_merger = pypdf.PdfMerger()
    temp_files: list[str] = []

    try:
        for idx, fig in enumerate(pages, start=1):
            _draw_page_footer(fig, idx, total_pages)
            
            fd, tmp_page_name = tempfile.mkstemp(prefix=f"page_{idx}_", suffix=".pdf")
            os.close(fd)
            temp_files.append(tmp_page_name)
            
            log.info(f"Rendering Page {idx} via isolated layout canvas contexts...")
            fig.savefig(tmp_page_name, format="pdf", dpi=_OUTPUT_DPI)
            plt.close(fig)
            
            pdf_merger.append(tmp_page_name)
        
        out_buf = io.BytesIO()
        pdf_merger.write(out_buf)
        pdf_merger.close()
        
        out_buf.seek(0)
        out_buf.name = f"{PathSafe(filename)}_summary.pdf"
        log.info("✅ High-fidelity text-shaped single stream compilation complete!")
        return out_buf

    finally:
        for tmp_f in temp_files:
            try:
                if os.path.exists(tmp_f):
                    os.unlink(tmp_f)
            except Exception:
                pass
        gc.collect()

def build_summary_figure(results: dict[str, Any], filename: str) -> Figure:
    content = prepare_report_content(results)
    fig = plt.figure(figsize=(16, 12))
    
    fig.patch.set_facecolor(PALETTE["bg"])
    fig.add_artist(Rectangle((0.01, 0.01), 0.98, 0.98, transform=fig.transFigure, linewidth=3, edgecolor=PALETTE["border"], facecolor="none", zorder=10))
    fig.text(0.50, 0.955, "SDSS SUMMARY", ha="center", va="center", fontsize=16, fontweight="bold", color=PALETTE["text_dark"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.935, f"MP Forest Department  |  File: {filename}", ha="center", va="center", fontsize=9, color=PALETTE["text_mid"], style="italic", fontproperties=_DEVA_FP)
    fig.add_artist(Line2D([0.04, 0.96], [0.925, 0.925], transform=fig.transFigure, color=PALETTE["border"], linewidth=1.5))

    ax = fig.add_axes([0.04, 0.06, 0.92, 0.84])
    ax.set_axis_off()

    _draw_text_panel(ax, 0.03, 0.52, 0.94, 0.38, "English Summary", content["summary_en"], box_face="#ffffff", title_color=PALETTE["accent"], text_color=PALETTE["text_dark"], font_size=11, line_gap=1.16, fontproperties=_DEVA_FP)
    _draw_text_panel(ax, 0.03, 0.08, 0.94, 0.38, "हिंदी सारांश", content["summary_hi"], box_face="#f8fbf6", title_color="#1e4620", text_color=PALETTE["text_dark"], font_size=11, line_gap=1.18, fontproperties=_DEVA_FP)
    return fig

def build_keyfacts_figure(results: dict[str, Any], filename: str) -> Figure:
    content = prepare_report_content(results)
    fig = plt.figure(figsize=(16, 12))
    
    fig.patch.set_facecolor(PALETTE["bg"])
    fig.add_artist(Rectangle((0.01, 0.01), 0.98, 0.98, transform=fig.transFigure, linewidth=3, edgecolor=PALETTE["border"], facecolor="none", zorder=10))
    fig.text(0.50, 0.955, "KEY FACTS", ha="center", va="center", fontsize=16, fontweight="bold", color=PALETTE["text_dark"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.935, f"MP Forest Department  |  File: {filename}", ha="center", va="center", fontsize=9, color=PALETTE["text_mid"], style="italic", fontproperties=_DEVA_FP)
    fig.add_artist(Line2D([0.04, 0.96], [0.925, 0.925], transform=fig.transFigure, color=PALETTE["border"], linewidth=1.5))

    ax = fig.add_axes([0.04, 0.08, 0.92, 0.80])
    ax.set_axis_off()

    _draw_text_panel(ax, 0.03, 0.03, 0.94, 0.90, "Key Facts / मुख्य तथ्य", content["keyfacts_lines"], box_face="#ffffff", title_color=PALETTE["accent"], text_color=PALETTE["text_dark"], font_size=11.5, line_gap=1.24, fontproperties=_DEVA_FP)
    return fig

def build_thankyou_figure(results: dict[str, Any], filename: str) -> Figure:
    fig = plt.figure(figsize=(16, 12))
    
    fig.patch.set_facecolor(PALETTE["thank_bg"])
    fig.add_artist(Rectangle((0.01, 0.01), 0.98, 0.98, transform=fig.transFigure, linewidth=4, edgecolor=PALETTE["thank_text"], facecolor="none", zorder=10))

    fig.text(0.50, 0.69, "THANK YOU", ha="center", va="center", fontsize=28, fontweight="bold", color=PALETTE["thank_text"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.59, "Spatial Decision Support System", ha="center", va="center", fontsize=18, fontweight="bold", color=PALETTE["thank_text"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.52, "MP Forest Department", ha="center", va="center", fontsize=15, color=PALETTE["thank_text"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.40, "धन्यवाद", ha="center", va="center", fontsize=30, fontweight="bold", color=PALETTE["thank_text"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.31, "स्थानिक निर्णय सहायता प्रणाली", ha="center", va="center", fontsize=18, color=PALETTE["thank_text"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.24, "मध्य प्रदेश वन विभाग", ha="center", va="center", fontsize=15, color=PALETTE["thank_text"], fontproperties=_DEVA_FP)
    fig.text(0.50, 0.10, f"Generated automatically by SDSS | File: {filename}", ha="center", va="center", fontsize=8.5, color=PALETTE["text_mid"], fontproperties=_DEVA_FP)
    return fig

def _draw_text_panel(
    ax, x: float, y: float, w: float, h: float, title: str,
    body_text: str | list[str], *, box_face: str, title_color: str,
    text_color: str, font_size: float, line_gap: float = 1.20,
    fontproperties: fm.FontProperties | None = None,
) -> None:
    text_kwargs: dict[str, Any] = {}
    if fontproperties is not None:
        text_kwargs["fontproperties"] = fontproperties

    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.02", transform=ax.transAxes, facecolor=box_face, edgecolor=PALETTE["border"], linewidth=1.0))
    ax.text(x + 0.02, y + h - 0.04, title, transform=ax.transAxes, fontsize=11, fontweight="bold", color=title_color, va="top", **text_kwargs)

    raw_lines = body_text.splitlines() if isinstance(body_text, str) else [str(item) for item in body_text]
    cursor_y = y + h - 0.09
    max_width = 92 if w >= 0.9 else 72

    for raw_line in raw_lines:
        if raw_line.strip() == "":
            cursor_y -= 0.02
            continue

        wrapped = textwrap.wrap(raw_line, width=max_width, break_long_words=False, break_on_hyphens=False) or [""]

        for wrapped_line in wrapped:
            ax.text(x + 0.02, cursor_y, wrapped_line, transform=ax.transAxes, fontsize=font_size, color=text_color, va="top", ha="left", **text_kwargs)
            cursor_y -= 0.032 * line_gap

        cursor_y -= 0.006
        if cursor_y < y + 0.02:
            break

def _draw_page_footer(fig: Figure, page_index: int, total_pages: int) -> None:
    fig.text(0.96, 0.02, f"Page {page_index} / {total_pages}", ha="right", va="bottom", fontsize=8, color=PALETTE["text_mid"], fontproperties=_DEVA_FP)
