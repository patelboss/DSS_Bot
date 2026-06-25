"""
config.py — Centralised configuration loader for the SDSS bot.
All secrets are read from environment variables; never hard-coded.
"""

from __future__ import annotations
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv
import matplotlib
import matplotlib.font_manager as fm

load_dotenv()
log = logging.getLogger(__name__)


class FontConfig:
    # Single source of truth for font candidate paths
    CANDIDATES: tuple[str, ...] = (
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

    def __init__(self):
        self.font_path: Path | None = self._find_valid_path()
        self.props: fm.FontProperties = self._create_properties()

    def _find_valid_path(self) -> Path | None:
        # Check hardcoded candidates first
        for candidate in self.CANDIDATES:
            p = Path(candidate)
            if p.exists():
                return p

        # Deep search fallback directories
        project_root = Path(__file__).resolve().parent
        search_dirs = [
            project_root / "utils" / "fonts",
            project_root / "fonts",
            Path("/app/fonts"),
            Path("/usr/share/fonts/truetype/noto"),
        ]
        for base in search_dirs:
            if base.exists():
                for suffix in ("*.ttf", "*.otf", "*.ttc"):
                    found = sorted(base.rglob(suffix))
                    if found:
                        return found
        return None

    def _create_properties(self) -> fm.FontProperties:
        if self.font_path is not None:
            try:
                fm.fontManager.addfont(str(self.font_path))
                fp = fm.FontProperties(fname=str(self.font_path))
                log.info(f"✨ Global Font Initialized: {self.font_path} ({fp.get_name()})")
                return fp
            except Exception as e:
                log.warning(f"Failed to register global font {self.font_path}: {e}")
        
        log.warning("⚠️ Fallback to DejaVu Sans. Complex text shaping WILL break.")
        return fm.FontProperties(family="DejaVu Sans")


@dataclass(frozen=True)
class Config:
    # ── Telegram MTProto & Bot Credentials ────────────────────────────────────
    TELEGRAM_TOKEN: str = field(default_factory=lambda: _require("BOT_TOKEN"))
    API_ID: int = field(default_factory=lambda: int(_require("API_ID")))
    API_HASH: str = field(default_factory=lambda: _require("API_HASH"))
    TELEGRAM_CHANNEL_ID: int = field(default_factory=lambda: int(os.getenv("CHANNEL_ID", "-1003588416077")))

    # ── MongoDB Atlas ─────────────────────────────────────────────────────────
    MONGO_URI: str = field(default_factory=lambda: _require("MONGO_URI"))
    MONGO_DB: str = field(default_factory=lambda: os.getenv("MONGO_DB", "sdss"))
    MONGO_COLLECTION_USERS: str = "users"
    MONGO_COLLECTION_LOGS:  str = "analysis_logs"

    # ── Supabase Storage ──────────────────────────────────────────────────────
    SUPABASE_URL: str = field(default_factory=lambda: _require("SUPABASE_URL"))
    SUPABASE_KEY: str = field(default_factory=lambda: _require("SUPABASE_SERVICE_KEY"))
    SUPABASE_BUCKET: str = field(default_factory=lambda: os.getenv("SUPABASE_BUCKET", "raster-layers"))

    # ── COG layer file names (as stored in the Supabase bucket) ───────────────
    COG_FCM:  str = field(default_factory=lambda: os.getenv("COG_FCM",  "fcm.tif"))
    COG_FTM:  str = field(default_factory=lambda: os.getenv("COG_FTM",  "ftm.tif"))
    COG_DEM:  str = field(default_factory=lambda: os.getenv("COG_DEM",  "dem.tif"))

    # ── Analysis parameters ───────────────────────────────────────────────────
    TARGET_CRS: str = "EPSG:4326"           # WGS-84 (storage CRS)
    AREA_CRS:   str = "EPSG:32644"          # UTM Zone 44N — good for MP/CG

    # ── Rendering & Fonts ─────────────────────────────────────────────────────
    OUTPUT_DPI:    int = 200
    OUTPUT_FORMAT: str = "PNG"              # PNG for Telegram photo upload
    fonts: FontConfig = field(default_factory=lambda: FontConfig())

    # ── Server ────────────────────────────────────────────────────────────────
    MAX_FILE_MB: float = 20.0              # Reject files larger than this
    TEMP_DIR:    str   = field(default_factory=lambda: os.getenv("TEMP_DIR", "/tmp/sdss"))


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Add it to your .env file or Koyeb environment settings."
        )
    return val


# Single shared instance — import this everywhere
cfg = Config()


# ── Forest Cover class mapping (FSI legend) ───────────────────────────────────
FCM_CLASSES: dict[int, str] = {
    0: "NO DATA",
    1: "VDF",
    2: "MDF",
    3: "OPEN FOREST",
    4: "NON FOREST",
    5: "SCRUB",
    6: "Water",
}

FCM_COLORS: dict[int, str] = {
    0: "#25a8a8",
    1: "#07380e",
    2: "#17d133",
    3: "#c1c70e",
    4: "#8c8c88",
    5: "#ab180e",
    6: "#5064fa",
}
