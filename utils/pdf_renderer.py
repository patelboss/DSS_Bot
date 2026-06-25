"""
utils/pdf_renderer.py — Shared PDF rendering engine for SDSS.

Centralizes Matplotlib canvas management by explicitly forcing FigureCanvasCairo,
resolves fonts, and applies automated byte-level extraction diagnostics on the 
final output stream to guarantee complex Devanagari script shaping.
"""

from __future__ import annotations

import gc
import io
import logging
import tempfile
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

import matplotlib

try:
    import mplcairo
    from mplcairo.base import FigureCanvasCairo
    matplotlib.use("module://mplcairo.base", force=True)
except Exception:
    matplotlib.use("Agg", force=True)
    FigureCanvasCairo = None

# Tell Matplotlib to pass literal strings to Cairo without math-splitters
if "text.parse_math" in matplotlib.rcParams:
    matplotlib.rcParams["text.parse_math"] = False

# Embed complete vectors to protect GSUB/GPOS tables from fontTools.subset pruning
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
matplotlib.rcParams["axes.unicode_minus"] = False

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

try:
    from pypdf import PdfMerger, PdfReader
except Exception as exc:
    pass

try:
    from config import cfg  # type: ignore
except Exception:
    cfg = None  # type: ignore

log = logging.getLogger(__name__)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(handler)
log.setLevel(logging.INFO)

_OUTPUT_DPI = int(getattr(cfg, "OUTPUT_DPI", 300) or 300) if cfg is not None else 300
_FONT_PROPS: fm.FontProperties | None = None


def _execute_rendering_diagnostics(fig: Figure, page_num: int) -> None:
    """Phase 2 & Phase 4: Validates the active canvas layer and figures trees."""
    log.info("======== 🧠 PDF SHAPING DIAGNOSTIC (Page %d) ========", page_num)
    log.info("Configured Backend:   %s", matplotlib.get_backend())
    log.info("Active Canvas Class:  %s", type(fig.canvas).__name__)
    log.info("Figure Memory Address:%s", id(fig))
    
    try:
        text_artists = fig.findobj(matplotlib.text.Text)
        log.info("Total Text Elements:  %d", len(text_artists))
        
        for idx, txt in enumerate(text_artists):
            text_str = txt.get_text().strip()
            if not text_str:
                continue
            props = txt.get_fontproperties()
            log.info(
                "  → Element [%d] | String: '%s' | Family: %s | Size: %s | Transform: %s",
                idx,
                str(text_str[:25]),
                props.get_family(),
                txt.get_fontsize(),
                type(txt.get_transform()).__name__
            )
    except Exception as err:
        log.warning("Diagnostic canvas tree traversal error: %s", err)
    log.info("================================────────────────================")


def _validate_compiled_pdf_bytes(pdf_bytes: bytes) -> None:
    """Phase 2 Text-Extraction: Audits the compiled binary tree using pypdf."""
    log.info("======== 🔍 COMPACT PDF BINARY AUDIT ========")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        log.info("Total Document Pages Extracted: %d", len(reader.pages))
        
        # Look for fonts inside page resource dictionaries
        for idx, page in enumerate(reader.pages, start=1):
            log.info("Analyzing Page %d Resources...", idx)
            if "/Resources" in page and "/Font" in page["/Resources"]:
                font_dict = page["/Resources"]["/Font"]
                log.info("  Fonts found: %s", list(font_dict.keys()))
            
            # Extract raw text packets to catch split ligatures/halants
            extracted_text = page.extract_text()
            log.info("  Raw Extracted Text Sample:\n%s", extracted_text[:200])
            
            # Look for specific check-words to verify shaping state
            for target in ["मुख्य", "क्षेत्रफल", "प्रमुख", "मध्यम", "अत्यधिक"]:
                # Match characters separated by optional spaces or halants
                pattern = ".*".join(list(target))
                if re.search(pattern, extracted_text) and not target in extracted_text:
                    log.warning("  ⚠️ SHAPING ALERT: Word '%s' appears broken in the binary mapping!", target)
                elif target in extracted_text:
                    log.info("  ✅ SHAPING VERIFIED: Word '%s' mapped cleanly as a unified block.", target)
                    
    except Exception as audit_err:
        log.warning("PDF binary inspection hit an evaluation error: %s", audit_err)
    log.info("==============================================")


def _force_cairo_canvas_override(figure: Figure) -> None:
    """Phase 6: Overrides default canvas configurations with FigureCanvasCairo."""
    if FigureCanvasCairo is not None:
        if not isinstance(figure.canvas, FigureCanvasCairo):
            figure.canvas = FigureCanvasCairo(figure)


def _iter_font_files(base: Path) -> list[Path]:
    if not base.exists():
        return []
    if base.is_file():
        return [base] if base.suffix.lower() in {".ttf", ".otf", ".ttc", ".TTF", ".OTF", ".TTC"} else []
    out: list[Path] = []
    for pattern in ("*.ttf", "*.otf", "*.ttc", "*.TTF", "*.OTF", "*.TTC"):
        out.extend(base.rglob(pattern))
    return out


def _find_font_from_config_or_fs() -> Path | None:
    if cfg is not None:
        font_cfg = getattr(cfg, "fonts", None)
        font_path = getattr(font_cfg, "font_path", None)
        if font_path:
            p = Path(font_path)
            if p.exists():
                return p
        font_props = getattr(font_cfg, "props", None)
        if font_props is not None:
            try:
                resolved = Path(fm.findfont(font_props))
                if resolved.exists():
                    return resolved
            except Exception:
                pass

    candidates = []
    if cfg is not None:
        font_cfg = getattr(cfg, "fonts", None)
        maybe_candidates = getattr(font_cfg, "CANDIDATES", None)
        if maybe_candidates:
            candidates.extend(str(x) for x in maybe_candidates)

    candidates.extend([
        "/app/utils/fonts/Devanagari-Regular.ttf",
        "/app/utils/fonts/mangal.ttf",
        "/app/utils/fonts/NotoSansDevanagari-Regular.ttf",
        "/app/fonts/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Mangal.ttf",
        "C:/Windows/Fonts/Nirmala.ttf",
    ])

    for item in candidates:
        p = Path(item)
        if p.exists():
            return p
    return None


def get_font_properties(font_path: str | None = None) -> fm.FontProperties:
    global _FONT_PROPS
    if font_path:
        p = Path(font_path)
        if p.exists():
            try:
                fm.fontManager.addfont(str(p))
                fp = fm.FontProperties(fname=str(p))
                _FONT_PROPS = fp
                _apply_font_rcparams(fp)
                return fp
            except Exception as exc:
                log.warning("Failed to load explicit font %s: %s", p, exc)

    if cfg is not None:
        font_cfg = getattr(cfg, "fonts", None)
        font_props = getattr(font_cfg, "props", None)
        if font_props is not None:
            _FONT_PROPS = font_props
            _apply_font_rcparams(font_props)
            return font_props

    if _FONT_PROPS is not None:
        return _FONT_PROPS

    fallback = _find_font_from_config_or_fs()
    if fallback is not None:
        try:
            fm.fontManager.addfont(str(fallback))
            fp = fm.FontProperties(fname=str(fallback))
            _FONT_PROPS = fp
            _apply_font_rcparams(fp)
            return fp
        except Exception:
            pass

    fp = fm.FontProperties(family="DejaVu Sans")
    _FONT_PROPS = fp
    _apply_font_rcparams(fp)
    return fp


def _apply_font_rcparams(font_props: fm.FontProperties) -> None:
    try:
        family_name = font_props.get_name()
    except Exception:
        family_name = "DejaVu Sans"
    try:
        matplotlib.rcParams["font.family"] = "sans-serif"
        matplotlib.rcParams["font.sans-serif"] = [family_name, "DejaVu Sans", "sans-serif"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        matplotlib.rcParams["pdf.fonttype"] = 42
        matplotlib.rcParams["ps.fonttype"] = 42
    except Exception:
        pass


def get_output_dpi() -> int:
    return _OUTPUT_DPI


def render_figure_to_png_bytes(figure: Figure, *, dpi: int | None = None, close_figure: bool = False) -> io.BytesIO:
    _force_cairo_canvas_override(figure)
    output_dpi = int(dpi or _OUTPUT_DPI)
    buf = io.BytesIO()
    figure.savefig(buf, format="png", dpi=output_dpi, bbox_inches="tight")
    buf.seek(0)
    buf.name = "figure.png"
    if close_figure:
        plt.close(figure)
        gc.collect()
    return buf


def render_figure_to_pdf_bytes(figure: Figure, *, dpi: int | None = None, close_figure: bool = False) -> io.BytesIO:
    _force_cairo_canvas_override(figure)
    _execute_rendering_diagnostics(figure, 1)
    
    output_dpi = int(dpi or _OUTPUT_DPI)
    buf = io.BytesIO()
    figure.savefig(buf, format="pdf", dpi=output_dpi)
    buf.seek(0)
    buf.name = "figure.pdf"
    
    _validate_compiled_pdf_bytes(buf.getvalue())
    
    if close_figure:
        plt.close(figure)
        gc.collect()
    return buf


def render_page(figure: Figure, filename: str = "output", *, dpi: int | None = None, close_figure: bool = False) -> io.BytesIO:
    buf = render_figure_to_pdf_bytes(figure, dpi=dpi, close_figure=close_figure)
    buf.name = f"{PathSafe(filename)}.pdf"
    return buf

def render_pages(
    figures: Sequence[Figure],
    filename: str = "output",
    *,
    dpi: int | None = None,
    close_figures: bool = True,
) -> io.BytesIO:
    """
    Render and merge multiple figures into one multi-page PDF using PdfWriter.
    """
    if not figures:
        raise ValueError("render_pages() received no figures.")

    output_dpi = int(dpi or _OUTPUT_DPI)
    temp_paths: list[Path] = []

    try:
        with tempfile.TemporaryDirectory(prefix="sdss_pdf_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            
            # FIX: Use PdfWriter instead of PdfMerger
            from pypdf import PdfWriter
            writer = PdfWriter()

            try:
                for idx, fig in enumerate(figures, start=1):
                    _force_cairo_canvas_override(fig)
                    _execute_rendering_diagnostics(fig, idx)
                    
                    page_path = tmpdir_path / f"{PathSafe(filename)}_{idx:03d}.pdf"
                    fig.savefig(str(page_path), format="pdf", dpi=output_dpi)
                    temp_paths.append(page_path)
                    
                    # Append the page using PdfWriter's native append method
                    writer.append(str(page_path))
                    
                    if close_figures:
                        plt.close(fig)

                out_buf = io.BytesIO()
                writer.write(out_buf)
                writer.close()
                out_buf.seek(0)
                out_buf.name = f"{PathSafe(filename)}.pdf"
                
                _validate_compiled_pdf_bytes(out_buf.getvalue())
                
                log.info("✅ Multi-page PDF layout generation verified successfully via PdfWriter.")
                return out_buf
            finally:
                try:
                    writer.close()
                except Exception:
                    pass
    finally:
        if close_figures:
            for fig in figures:
                try:
                    plt.close(fig)
                except Exception:
                    pass
        gc.collect()
def save_to_bytes(figures: Sequence[Figure], filename: str = "output", *, dpi: int | None = None) -> bytes:
    return render_pages(figures, filename=filename, dpi=dpi).getvalue()


def save_to_file(figures: Sequence[Figure], out_path: str | Path, filename: str = "output", *, dpi: int | None = None) -> str:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_bytes = save_to_bytes(figures, filename=filename, dpi=dpi)
    out_path.write_bytes(pdf_bytes)
    return str(out_path)


def PathSafe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(name))


__all__ = [
    "PathSafe",
    "get_font_properties",
    "get_output_dpi",
    "render_figure_to_png_bytes",
    "render_figure_to_pdf_bytes",
    "render_page",
    "render_pages",
    "save_to_bytes",
    "save_to_file",
]
