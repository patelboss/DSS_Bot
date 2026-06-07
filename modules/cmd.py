"""
modules/cmd.py — Command handlers and conversation flows for the SDSS bot.
Separated from main.py to maintain deployment health stability.
"""

import asyncio
import logging
import os
from pathlib import Path

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes

from config import cfg
from modules.database import upsert_user, log_analysis, get_user_history
from modules.spatial_analysis import load_vector_file, run_analysis
from modules.map_renderer import render_map

log = logging.getLogger(__name__)

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
        "2️⃣  Choose whether to view attributes or run environmental analysis\n"
        "3️⃣  Receive your custom reporting metrics instantly\n\n"
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
        "*Output Report options:*\n"
        "• Custom Attributes Table inspection metrics\n"
        "• FSI Forest Cover class breakdowns and Terrain Elevation maps\n\n"
        "*Tips:*\n"
        "• Export polygons from QGIS/ArcGIS as GeoJSON for best results\n"
        "• Ensure the polygon falls within the study area extent"
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

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /status ───────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_chat_action(ChatAction.TYPING)
    checks = {}

    # MongoDB Check
    try:
        from modules.database import _get_db
        _get_db().command("ping")
        checks["MongoDB Atlas"] = "✅ Connected"
    except Exception as exc:
        checks["MongoDB Atlas"] = f"❌ Error: {exc}"

    # Supabase Check
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


# ── File Document Ingestion Handler ───────────────────────────────────────────
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    document = message.document
    user = update.effective_user

    suffix = Path(document.file_name or "").suffix.lower()
    if suffix not in {".geojson", ".json", ".kml", ".gpkg"}:
        await message.reply_text("⚠️ Please upload a valid spatial `.geojson`, `.kml`, or `.gpkg` file.")
        return

    size_mb = (document.file_size or 0) / (1024 * 1024)
    if size_mb > cfg.MAX_FILE_MB:
        await message.reply_text(f"⚠️ File is too large. Maximum allowed size is {cfg.MAX_FILE_MB} MB.")
        return

    status_msg = await message.reply_text("📥 *Processing vector layout properties…*", parse_mode=ParseMode.MARKDOWN)

    tmp_path = Path(cfg.TEMP_DIR) / f"{user.id}_{document.file_name}"
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(str(tmp_path))

        # Use our updated load function to get both the polygon and text summary
        geojson_feature, attr_summary = load_vector_file(tmp_path)

        # Cache variables inside the specific user's context dictionary memory
        context.user_data["current_feature"] = geojson_feature
        context.user_data["attr_summary"] = attr_summary
        context.user_data["filename"] = document.file_name

        # Construct Interactive Keyboard Options
        keyboard = [
            [InlineKeyboardButton("📋 View Attributes Table", callback_data="action_attributes")],
            [InlineKeyboardButton("🔬 Run Spatial DSS Analysis", callback_data="action_analysis")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await status_msg.delete()
        await message.reply_text(
            f"✅ *Layer Ingested Successfully!*\n\n"
            f"📁 *File:* `{document.file_name}`\n"
            f"📦 Features found matching valid boundaries.\n\n"
            f"What execution task should the SDSS run?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )

    except Exception as exc:
        log.error("Ingestion failed for user %d: %s", user.id, exc, exc_info=True)
        await status_msg.edit_text(f"⚠️ *Validation Error:*\n{exc}", parse_mode=ParseMode.MARKDOWN)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ── Interactive Callback Menu Router ──────────────────────────────────────────
async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action = query.data
    user = update.effective_user

    # Pull variables out of session context
    geojson_feature = context.user_data.get("current_feature")
    attr_summary = context.user_data.get("attr_summary")
    filename = context.user_data.get("filename", "output_layer")

    if not geojson_feature:
        await query.edit_message_text("❌ Session expired. Please upload your vector file again.")
        return

    if action == "action_attributes":
        await query.edit_message_text(
            f"📁 *Attributes Table Summary for {filename}:*\n\n{attr_summary}",
            parse_mode=ParseMode.MARKDOWN
        )

    elif action == "action_analysis":
        status_msg = await query.edit_message_text(
            "🔬 *Running full spatial intersection algorithms…*\n"
            "_(Streaming raster tiles from cloud matrices)_",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, run_analysis, geojson_feature
            )

            map_png = await asyncio.get_event_loop().run_in_executor(
                None, render_map, geojson_feature, results, filename
            )

            log_analysis(user.id, filename, geojson_feature, results, results["centroid"])
            summary = _build_summary(results, filename)
            
            await status_msg.delete()
            await query.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
            await query.message.reply_photo(
                photo=InputFile(map_png, filename="sdss_report.png"),
                caption="📊 *SDSS Cartographic Report*\nHigh-resolution map layout",
                parse_mode=ParseMode.MARKDOWN
            )

        except Exception as err:
            log.error("Analysis thread failed for user %d: %s", user.id, err, exc_info=True)
            await status_msg.edit_text(f"❌ *Analysis processing failed:* {err}")


# ── Unknown Message Fallback ──────────────────────────────────────────────────
async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📂 Please upload a spatial vector file (`.geojson`, `.kml`, or `.gpkg`) "
        "to start analysis, or use /help for instructions.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Report Text Builder Helper ────────────────────────────────────────────────
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

