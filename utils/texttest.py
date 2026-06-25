
# utils/texttest.py
from __future__ import annotations

import gc
import io
import logging
import tempfile
import textwrap
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
from matplotlib.patches import FancyBboxPatch, Rectangle

log = logging.getLogger(__name__)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(handler)
log.setLevel(logging.INFO)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTILS_DIR = PROJECT_ROOT / "utils"

_OUTPUT_DPI = 300

TEXTTEST_SIMPLE_LINES = [
    "मुख्य",
    "प्रकार",
    "अत्यधिक",
    "क्षेत्रफल",
    "प्रमुख वन वर्ग",
    "वन आवरण वितरण",
    "मध्यम घना वन",
    "खुला वन",
    "गैर-वन",
    "झाड़ीदार क्षेत्र",
]

TEXTTEST_STRESS_LINES = [
    "क्ष",
    "त्र",
    "ज्ञ",
    "श्र",
    "प्र",
    "ख्य",
    "क्त",
    "द्व",
    "ध्य",
    "स्थ",
]

TEXTTEST_MIXED_LINES = [
    "mukhya / मुख्य",
    "prakar / प्रकार",
    "atyadhik / अत्यधिक",
    "kshetrafal / क्षेत्रफल",
    "Spatial Decision Support System / स्थानिक निर्णय सहायता प्रणाली",
    "MP Forest Department / मध्य प्रदेश वन विभाग",
]

FONT_CANDIDATES: tuple[str, ...] = (
    "/app/utils/fonts/Devanagari-Regular.ttf",
    "/app/utils/fonts/mangal.ttf",
    "/app/utils/fonts/NotoSansDevanagari-Regular.ttf",
    "/app/fonts/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Mangal.ttf",
    "/usr/local/share/fonts/NotoSansDevanagari-Regular.ttf",
    "C:/Windows/Fonts/Nirmala.ttf",
    "C:/Windows/Fonts/mangal.ttf",
)

PALETTE = {
    "bg": "#f5f2eb",
    "panel": "#ffffff",
    "border": "#2c4a2e",
    "accent": "#2c6e31",
    "text_dark": "#1a2a1b",
    "text_mid": "#3d5c3f",
    "table_alt": "#e8f0e9",
}


def _safe_font_path(font_path: str | None = None) -> Path | None:
    if font_path:
        p = Path(font_path)
        if p.exists():
            return p

    for candidate in FONT_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p

    search_dirs = [
        UTILS_DIR,
        UTILS_DIR / "fonts",
        PROJECT_ROOT / "fonts",
        Path("/app/fonts"),
        Path("/usr/local/share/fonts"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/truetype/msttcorefonts"),
    ]
    for base in search_dirs:
        if not base.exists():
            continue
        for suffix in ("*.ttf", "*.otf", "*.ttc", "*.TTF", "*.OTF", "*.TTC"):
            found = sorted(base.rglob(suffix))
            if found:
                return found[0]
    return None


def _make_font_properties(font_path: str | None = None) -> fm.FontProperties:
    p = _safe_font_path(font_path)
    if p is not None:
        try:
            fm.fontManager.addfont(str(p))
            fp = fm.FontProperties(fname=str(p))
            log.info("Text test font: %s | family=%s", p, fp.get_name())
            log.info("Resolved font file: %s", fm.findfont(fp))
            return fp
        except Exception as exc:
            log.warning("Could not load font %s: %s", p, exc)

    log.warning("Falling back to DejaVu Sans.")
    return fm.FontProperties(family="DejaVu Sans")


def _draw_text_panel(
    ax,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    body_text: str | list[str],
    *,
    box_face: str,
    title_color: str,
    text_color: str,
    font_size: float,
    line_gap: float = 1.20,
    fontproperties: fm.FontProperties | None = None,
) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.012,rounding_size=0.02",
            transform=ax.transAxes,
            facecolor=box_face,
            edgecolor=PALETTE["border"],
            linewidth=1.0,
        )
    )

    text_kwargs: dict[str, Any] = {}
    if fontproperties is not None:
        text_kwargs["fontproperties"] = fontproperties

    ax.text(
        x + 0.02,
        y + h - 0.04,
        title,
        transform=ax.transAxes,
        fontsize=11,
        fontweight="bold",
        color=title_color,
        va="top",
        **text_kwargs,
    )

    raw_lines = body_text.splitlines() if isinstance(body_text, str) else [str(item) for item in body_text]
    cursor_y = y + h - 0.09
    max_width = 92 if w >= 0.9 else 72

    for raw_line in raw_lines:
        if raw_line.strip() == "":
            cursor_y -= 0.02
            continue

        wrapped = textwrap.wrap(
            raw_line,
            width=max_width,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]

        for wrapped_line in wrapped:
            ax.text(
                x + 0.02,
                cursor_y,
                wrapped_line,
                transform=ax.transAxes,
                fontsize=font_size,
                color=text_color,
                va="top",
                ha="left",
                **text_kwargs,
            )
            cursor_y -= 0.032 * line_gap

        cursor_y -= 0.006
        if cursor_y < y + 0.02:
            break


def build_texttest_figure(
    font_path: str | None = None,
    lines: Sequence[str] | None = None,
    title: str = "TEXT SHAPING TEST",
) -> Figure:
    fp = _make_font_properties(font_path)
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

    fig.text(
        0.50,
        0.955,
        title,
        ha="center",
        va="center",
        fontsize=18,
        fontweight="bold",
        color=PALETTE["text_dark"],
        fontproperties=fp,
    )
    fig.text(
        0.50,
        0.935,
        f"Backend: {matplotlib.get_backend()}  |  Font: {fp.get_name()}",
        ha="center",
        va="center",
        fontsize=9,
        color=PALETTE["text_mid"],
        style="italic",
        fontproperties=fp,
    )
    fig.add_artist(
        Rectangle(
            (0.04, 0.925),
            0.92,
            0.0016,
            transform=fig.transFigure,
            linewidth=0,
            facecolor=PALETTE["border"],
        )
    )

    ax = fig.add_axes([0.04, 0.06, 0.92, 0.84])
    ax.set_axis_off()

    simple = list(lines) if lines else list(TEXTTEST_SIMPLE_LINES)

    _draw_text_panel(
        ax,
        0.03,
        0.66,
        0.94,
        0.24,
        "Hindi Samples",
        simple,
        box_face="#ffffff",
        title_color=PALETTE["accent"],
        text_color=PALETTE["text_dark"],
        font_size=13,
        line_gap=1.14,
        fontproperties=fp,
    )

    _draw_text_panel(
        ax,
        0.03,
        0.36,
        0.94,
        0.24,
        "Conjunct Stress Test",
        list(TEXTTEST_STRESS_LINES),
        box_face="#f8fbf6",
        title_color="#1e4620",
        text_color=PALETTE["text_dark"],
        font_size=13,
        line_gap=1.14,
        fontproperties=fp,
    )

    _draw_text_panel(
        ax,
        0.03,
        0.06,
        0.94,
        0.24,
        "Mixed / Latin Control",
        list(TEXTTEST_MIXED_LINES),
        box_face="#ffffff",
        title_color=PALETTE["accent"],
        text_color=PALETTE["text_dark"],
        font_size=12,
        line_gap=1.14,
        fontproperties=fp,
    )

    return fig


def render_texttest_pdf(
    font_path: str | None = None,
    lines: Sequence[str] | None = None,
    title: str = "TEXT SHAPING TEST",
) -> io.BytesIO:
    fig = build_texttest_figure(font_path=font_path, lines=lines, title=title)
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="pdf", dpi=_OUTPUT_DPI, bbox_inches="tight")
        buf.seek(0)
        buf.name = "texttest.pdf"
        return buf
    finally:
        plt.close(fig)
        gc.collect()


def render_texttest_png(
    font_path: str | None = None,
    lines: Sequence[str] | None = None,
    title: str = "TEXT SHAPING TEST",
) -> io.BytesIO:
    fig = build_texttest_figure(font_path=font_path, lines=lines, title=title)
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=_OUTPUT_DPI, bbox_inches="tight")
        buf.seek(0)
        buf.name = "texttest.png"
        return buf
    finally:
        plt.close(fig)
        gc.collect()


def render_texttest_file(
    out_dir: str | None = None,
    kind: str = "pdf",
    font_path: str | None = None,
    lines: Sequence[str] | None = None,
    title: str = "TEXT SHAPING TEST",
) -> str:
    out_kind = str(kind or "pdf").lower().strip()
    if out_kind not in {"pdf", "png"}:
        out_kind = "pdf"

    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="texttest_")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if out_kind == "pdf":
        buf = render_texttest_pdf(font_path=font_path, lines=lines, title=title)
        file_path = out_path / "texttest.pdf"
    else:
        buf = render_texttest_png(font_path=font_path, lines=lines, title=title)
        file_path = out_path / "texttest.png"

    file_path.write_bytes(buf.read())
    log.info("Text test file written: %s", file_path)
    return str(file_path)
