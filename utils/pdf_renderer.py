"""
utils/pdf_renderer.py — Shared PDF rendering engine for SDSS.

This module centralizes:
- Matplotlib backend initialization
- Devanagari font resolution via config.py
- PDF/PNG page rendering
- Multi-page PDF compilation by merging individually rendered pages

Only this module calls Figure.savefig().
"""

from __future__ import annotations

import gc
import io
import logging
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

import matplotlib

try:
    import mplcairo  # noqa: F401
    matplotlib.use("module://mplcairo.base", force=True)
except Exception:
    matplotlib.use("Agg", force=True)

try:
    if "text.parse_math" in matplotlib.rcParams:
        matplotlib.rcParams["text.parse_math"] = False
except Exception:
    pass

matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
matplotlib.rcParams["axes.unicode_minus"] = False

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

try:
    from pypdf import PdfMerger
except Exception as exc:  # pragma: no cover - dependency should exist in production
    raise ImportError(
        "pypdf is required for SDSS PDF page merging. Install it with `pip install pypdf`."
    ) from exc

try:
    from config import cfg  # type: ignore
except Exception:  # pragma: no cover - config is optional in some tests
    cfg = None  # type: ignore

log = logging.getLogger(__name__)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(handler)
log.setLevel(logging.INFO)

_OUTPUT_DPI = int(getattr(cfg, "OUTPUT_DPI", 300) or 300) if cfg is not None else 300

_FONT_PROPS: fm.FontProperties | None = None


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
    # Prefer the centralized config font first.
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

    # Common fallbacks.
    candidates.extend([
        "/app/utils/fonts/Devanagari-Regular.ttf",
        "/app/utils/fonts/mangal.ttf",
        "/app/utils/fonts/NotoSansDevanagari-Regular.ttf",
        "/app/fonts/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Mangal.ttf",
        "/usr/local/share/fonts/NotoSansDevanagari-Regular.ttf",
        "C:/Windows/Fonts/Nirmala.ttf",
        "C:/Windows/Fonts/mangal.ttf",
    ])

    for item in candidates:
        p = Path(item)
        if p.exists():
            return p

    return None


def get_font_properties(font_path: str | None = None) -> fm.FontProperties:
    """
    Return the global Devanagari FontProperties used by SDSS text rendering.

    If font_path is supplied, that exact font file is preferred.
    Otherwise the centrally configured font from config.py is used.
    """
    global _FONT_PROPS

    if font_path:
        p = Path(font_path)
        if p.exists():
            try:
                fm.fontManager.addfont(str(p))
                fp = fm.FontProperties(fname=str(p))
                _FONT_PROPS = fp
                _apply_font_rcparams(fp)
                log.info("Using explicit font override: %s (%s)", fp.get_name(), p.name)
                return fp
            except Exception as exc:
                log.warning("Failed to load explicit font %s: %s", p, exc)

    if cfg is not None:
        font_cfg = getattr(cfg, "fonts", None)
        font_props = getattr(font_cfg, "props", None)
        if font_props is not None:
            try:
                _FONT_PROPS = font_props
                _apply_font_rcparams(font_props)
                return font_props
            except Exception as exc:
                log.warning("Failed to use configured font properties: %s", exc)

    if _FONT_PROPS is not None:
        return _FONT_PROPS

    fallback = _find_font_from_config_or_fs()
    if fallback is not None:
        try:
            fm.fontManager.addfont(str(fallback))
            fp = fm.FontProperties(fname=str(fallback))
            _FONT_PROPS = fp
            _apply_font_rcparams(fp)
            log.info("Resolved fallback font: %s (%s)", fp.get_name(), fallback.name)
            return fp
        except Exception as exc:
            log.warning("Failed to register fallback font %s: %s", fallback, exc)

    fp = fm.FontProperties(family="DejaVu Sans")
    _FONT_PROPS = fp
    _apply_font_rcparams(fp)
    log.warning("Falling back to DejaVu Sans.")
    return fp


def _apply_font_rcparams(font_props: fm.FontProperties) -> None:
    try:
        family_name = font_props.get_name()
    except Exception:
        family_name = "DejaVu Sans"

    try:
        matplotlib.rcParams["font.family"] = "sans-serif"
        matplotlib.rcParams["font.sans-serif"] = [family_name, "DejaVu Sans", "Arial", "sans-serif"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        matplotlib.rcParams["pdf.fonttype"] = 42
        matplotlib.rcParams["ps.fonttype"] = 42
    except Exception:
        pass


def get_output_dpi() -> int:
    return _OUTPUT_DPI


def render_figure_to_png_bytes(
    figure: Figure,
    *,
    dpi: int | None = None,
    close_figure: bool = False,
) -> io.BytesIO:
    """
    Render a single figure to an in-memory PNG.
    """
    output_dpi = int(dpi or _OUTPUT_DPI)
    buf = io.BytesIO()
    figure.savefig(buf, format="png", dpi=output_dpi, bbox_inches="tight")
    buf.seek(0)
    buf.name = "figure.png"
    if close_figure:
        plt.close(figure)
        gc.collect()
    return buf


def render_figure_to_pdf_bytes(
    figure: Figure,
    *,
    dpi: int | None = None,
    close_figure: bool = False,
) -> io.BytesIO:
    """
    Render a single figure to an in-memory PDF.
    """
    output_dpi = int(dpi or _OUTPUT_DPI)
    buf = io.BytesIO()
    figure.savefig(buf, format="pdf", dpi=output_dpi)
    buf.seek(0)
    buf.name = "figure.pdf"
    if close_figure:
        plt.close(figure)
        gc.collect()
    return buf


def render_page(
    figure: Figure,
    filename: str = "output",
    *,
    dpi: int | None = None,
    close_figure: bool = False,
) -> io.BytesIO:
    """
    Compatibility wrapper for a single PDF page.
    """
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
    Render and merge multiple figures into one multi-page PDF.

    Each figure is first saved individually using Figure.savefig(..., format="pdf"),
    then merged using pypdf so the final output keeps the same shaping path as the
    standalone text-test renderer.
    """
    if not figures:
        raise ValueError("render_pages() received no figures.")

    output_dpi = int(dpi or _OUTPUT_DPI)
    temp_paths: list[Path] = []

    try:
        with tempfile.TemporaryDirectory(prefix="sdss_pdf_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            merger = PdfMerger()

            try:
                for idx, fig in enumerate(figures, start=1):
                    page_path = tmpdir_path / f"{PathSafe(filename)}_{idx:03d}.pdf"
                    fig.savefig(str(page_path), format="pdf", dpi=output_dpi)
                    temp_paths.append(page_path)
                    merger.append(str(page_path))
                    if close_figures:
                        plt.close(fig)

                out_buf = io.BytesIO()
                merger.write(out_buf)
                merger.close()
                out_buf.seek(0)
                out_buf.name = f"{PathSafe(filename)}.pdf"
                log.info("✅ PDF rendered and merged | pages=%d | dpi=%d", len(figures), output_dpi)
                return out_buf
            finally:
                try:
                    merger.close()
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


def save_to_bytes(
    figures: Sequence[Figure],
    filename: str = "output",
    *,
    dpi: int | None = None,
) -> bytes:
    return render_pages(figures, filename=filename, dpi=dpi).getvalue()


def save_to_file(
    figures: Sequence[Figure],
    out_path: str | Path,
    filename: str = "output",
    *,
    dpi: int | None = None,
) -> str:
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
