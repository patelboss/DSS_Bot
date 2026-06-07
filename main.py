"""
main.py — SDSS Telegram Bot  (python-telegram-bot v21, async)

Command reference
-----------------
/start    — Welcome message + quick-start guide
/help     — Detailed usage instructions
/history  — Last 5 analysis runs for the current user
/status   — System health check (DB + Storage connectivity)

Upload workflow:
  User sends a .geojson / .kml / .gpkg file → bot runs the full
  analysis pipeline and replies with a summary + map PNG.

Koyeb Update: Integrated a background HTTP daemon server to pass health checks cleanly.
"""

import asyncio
import logging
import os
import threading
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer
from pathlib import Path

from telegram import (
    Document,
    Message,
    Update,
    InputFile,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import cfg
from modules.database      import upsert_user, log_analysis, get_user_history
from modules.spatial_analysis import load_vector_file, run_analysis
from modules.map_renderer  import render_map

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Ensure temp directory exists
Path(cfg.TEMP_DIR).mkdir(parents=True, exist_ok=True)


# ── Koyeb Health Check Web Server ─────────────────────────────────────────────

def run_koyeb_health_server():
    """
    Spins up a lightweight HTTP server on port 8080 to satisfy Koyeb's
    internal infrastructure monitoring and keep the container alive.
    """
    class HealthHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.end_headers()

    port = 8080
    try:
        # Allow reuse of address to prevent "Address already in use" errors on fast re-deploys
        TCPServer.allow_reuse_address = True
        with TCPServer(("", port), HealthHandler) as httpd:
            log.info("🌍 Koyeb Background Health Server active on port %d", port)
            httpd.serve_forever()
    except Exception as e:
        log.error("❌ Failed to start health server: %s", e)


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user.id, user.username, user.full_name)

    text = (
        f"🌿 *Welcome to the SDSS Bot, {user.first_name}!*\n\n"
        "This system performs automated spatial analysis on forest polygons "
        "for the *Madhya Pradesh Forest Department*.\n\n"
        "*What to do:*\n"
        "1️⃣  Upload a spatial boundary file (`.geojson`, `.kml`, or `.gpkg`)\n"
        "2️⃣  The bot analyses Forest Cover, Elevation, and Area\n"
        "3️⃣  Receive a cartographic map report within seconds\n\n"
        "*Commands:*\n"
        "/help — Detailed instructions\n"
        "/history — Your last 5 analysis runs\n"
        "/status — Check system health\n\n"
        "_Upload your file to begin_ 👆"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *SDSS Bot — Usage Guide*\n\n"
        "*Supported Formats:*\n"
        "• GeoJSON `.geojson` — Preferred format\n"
        "• KML `.kml` — Google Earth export\n"
        "• GeoPackage `.gpkg` — QGIS export\n\n"
        "*Requirements:*\n"
        "• File size must be under 20 MB\n"
        "• Geometry must be a Polygon or MultiPolygon\n"
        "• Any coordinate reference system is accepted (auto-converted)\n\n"
        "*Output Report includes:*\n"
        "• Polygon area in Hectares\n"
        "• FSI Forest Cover class breakdown (%)\n"
        "• Elevation stats (min/max/mean in metres)\n"
        "• Slope statistics (mean and max degrees)\n"
        "• High-resolution cartographic map PNG\n\n"
        "*Tips:*\n"
        "• Export polygons from QGIS/ArcGIS as GeoJSON for best results\n"
        "• Ensure the polygon falls within the study area extent\n"
        "• For KML with multiple layers, the first polygon layer is used"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /history ──────────────────────────────────────────────────────────────────

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logs = get_user_history(user.id, limit=5)

    if not logs:
        await update.message.reply_text(
            "📭 No analysis history found. Upload a file to get started!"
        )
        return

    lines = ["📋 *Your Last 5 Analyses:*\n"]
    for i, entry in enumerate(logs, 1):
        r   = entry.get("results", {})
        ts  = entry.get("created_at", "—")
        ts_str = ts.strftime("%d %b %Y %H:%M UTC") if hasattr(ts, "strftime") else str(ts)
        lines.append(
            f"*{i}.* `{entry.get('filename', '—')}`\n"
            f"   📅 {ts_str}\n"
            f"   🌲 {r.get('fcm', {}).get('dominant', '—')} | "
            f"📐 {r.get('area_ha', '—')} ha | "
            f"⛰ {r.get('dem', {}).get('elevation_mean_m', '—')} m\n"
        )

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_chat_action(ChatAction.TYPING)

    checks = {}

    # MongoDB
    try:
        from modules.database import _get_db
        _get_db().command("ping")
        checks["MongoDB Atlas"] = "✅ Connected"
    except Exception as exc:
        checks["MongoDB Atlas"] = f"❌ Error: {exc}"

    # Supabase
    try:
        from modules.storage import _get_supabase
        _get_supabase().storage.from_(cfg.SUPABASE_BUCKET).list()
        checks["Supabase Storage"] = "✅ Connected"
    except Exception as exc:
        checks["Supabase Storage"] = f"❌ Error: {exc}"

    lines = ["🔧 *System Status*\n"]
    for service, status in checks.items():
        lines.append(f"*{service}:* {status}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── File upload handler ───────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message:  Message  = update.message
    document: Document = message.document
    user                = update.effective_user

    # ── Validation ────────────────────────────────────────────────────────────
    suffix = Path(document.file_name or "").suffix.lower()
    if suffix not in {".geojson", ".json", ".kml", ".gpkg"}:
        await message.reply_text(
            "⚠️ *Unsupported file type.*\n"
            "Please upload a `.geojson`, `.kml`, or `.gpkg` file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    size_mb = (document.file_size or 0) / (1024 * 1024)
    if size_mb > cfg.MAX_FILE_MB:
        await message.reply_text(
            f"⚠️ File is too large ({size_mb:.1f} MB). "
            f"Maximum allowed is {cfg.MAX_FILE_MB} MB."
        )
        return

    # Acknowledge receipt immediately
    status_msg = await message.reply_text(
        "📥 *File received.* Starting spatial analysis…\n"
        "⏳ This usually takes 15–45 seconds.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)

    # ── Download to temp directory ────────────────────────────────────────────
    tmp_path = Path(cfg.TEMP_DIR) / f"{user.id}_{document.file_name}"
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(str(tmp_path))
        log.info("Downloaded '%s' (%s MB) for user %d", document.file_name, f"{size_mb:.2f}", user.id)

        # ── Load & validate vector geometry ──────────────────────────────────
        await _edit_status(status_msg, "🗺 *Validating geometry…*")
        geojson_feature = load_vector_file(tmp_path)

        # ── Upsert user record ────────────────────────────────────────────────
        upsert_user(user.id, user.username, user.full_name)

        # ── Run geospatial analysis ───────────────────────────────────────────
        await _edit_status(status_msg, "🔬 *Running spatial analysis…*\n_(Streaming raster tiles from cloud)_")
        results = await asyncio.get_event_loop().run_in_executor(
            None, run_analysis, geojson_feature
        )

        # ── Render map layout ─────────────────────────────────────────────────
        await _edit_status(status_msg, "🎨 *Rendering cartographic layout…*")
        map_png = await asyncio.get_event_loop().run_in_executor(
            None, render_map, geojson_feature, results, document.file_name
        )

        # ── Persist to MongoDB ────────────────────────────────────────────────
        log_analysis(
            user_id=user.id,
            filename=document.file_name,
            geojson=geojson_feature,
            results=results,
            centroid=results["centroid"],
        )

        # ── Build Markdown summary ────────────────────────────────────────────
        summary = _build_summary(results, document.file_name)

        # ── Reply ─────────────────────────────────────────────────────────────
        await status_msg.delete()
        await message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
        await message.reply_photo(
            photo=InputFile(map_png, filename="sdss_report.png"),
            caption="📊 *SDSS Cartographic Report*\nHigh-resolution map layout",
            parse_mode=ParseMode.MARKDOWN,
        )

    except ValueError as exc:
        log.warning("Validation error for user %d: %s", user.id, exc)
        await status_msg.edit_text(f"⚠️ *Validation Error:*\n{exc}", parse_mode=ParseMode.MARKDOWN)

    except Exception as exc:
        log.error("Unhandled error for user %d: %s", user.id, exc, exc_info=True)
        await status_msg.edit_text(
            "❌ *An unexpected error occurred.*\n"
            "The issue has been logged. Please try again or contact support.",
            parse_mode=ParseMode.MARKDOWN,
        )

    finally:
        # Wipe temp file from local disk
        if tmp_path.exists():
            tmp_path.unlink()
            log.debug("Deleted temp file: %s", tmp_path)


# ── Non-document message fallback ─────────────────────────────────────────────

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📂 Please upload a spatial vector file (`.geojson`, `.kml`, or `.gpkg`) "
        "to start analysis, or use /help for instructions.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_summary(results: dict, filename: str) -> str:
    fcm = results.get("fcm", {})
    dem = results.get("dem", {})
    lon, lat = results.get("centroid", ("—", "—"))

    classes = fcm.get("classes", {})
    cover_lines = ""
    for name, stats in classes.items():
        cover_lines += f"  • {name}: {stats['percentage']:.1f}%\n"

    return (
        f"✅ *Analysis Complete*\n\n"
        f"📁 *File:* `{filename}`\n"
        f"📍 *Centroid:* `{lon}, {lat}`\n\n"
        f"📐 *Area:* `{results.get('area_ha', '—')} hectares`\n\n"
        f"🌲 *Forest Cover (dominant):* `{fcm.get('dominant', '—')}`\n"
        f"{cover_lines}\n"
        f"⛰ *Elevation:*\n"
        f"  • Min: `{dem.get('elevation_min_m', '—')} m`\n"
        f"  • Max: `{dem.get('elevation_max_m', '—')} m`\n"
        f"  • Mean: `{dem.get('elevation_mean_m', '—')} m`\n\n"
        f"📉 *Slope:*\n"
        f"  • Mean: `{dem.get('slope_mean_deg', '—')}°`\n"
        f"  • Max: `{dem.get('slope_max_deg', '—')}°`\n"
    )


async def _edit_status(msg, text: str) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass  # Silently skip if edit fails (e.g. message unchanged)


# ── Application bootstrap ─────────────────────────────────────────────────────

def build_application() -> Application:
    app = Application.builder().token(cfg.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("status",  cmd_status))

    app.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown)
    )

    return app


def main() -> None:
    # 🚀 Kick off Koyeb Http listener daemon before building Telegram Application
    health_thread = threading.Thread(target=run_koyeb_health_server, daemon=True)
    health_thread.start()

    log.info("Starting SDSS Telegram Bot with Health-Hooks…")
    app = build_application()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
  
