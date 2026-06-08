"""
modules/cmd.py — Command handlers and conversation flows for the SDSS bot.
Separated from main.py to maintain deployment health stability.
"""

import io
import logging
import sys
import asyncio
import pandas as pd
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode, ChatAction

from config import cfg
from modules.database import upsert_user, log_analysis, get_user_history
from modules.spatial_analysis import load_vector_file, run_analysis
from modules.map_renderer import render_map

# ── Link directly to the main root orchestrator logging pipeline ──────────────
logger = logging.getLogger("main.commands")
logger.setLevel(logging.INFO)

if not logger.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(stdout_handler)

# 🚀 SESSION RUNTIME CACHE: Replaces python-telegram-bot's context.user_data
USER_SESSION_CACHE = {}


# ── /start ────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message) -> None:
    user = message.from_user
    upsert_user(user.id, user.username, f"{user.first_name} {user.last_name or ''}".strip())

    text = (
        f"🌿 *Welcome to the SDSS Bot, {user.first_name}!*\n\n"
        "This system performs automated spatial analysis on forest polygons "
        "for the *Madhya Pradesh Forest Department*.\n\n"
        "*What to do:*\n"
        "1️⃣  Upload a spatial boundary file (`.geojson`, `.kml`, `.kmz`, or `.zip`)\n"
        "2️⃣  Choose whether to view attributes or run environmental analysis\n"
        "3️⃣  Receive your custom reporting metrics instantly\n\n"
        "*Commands:*\n"
        "/help — Detailed instructions\n"
        "/history — Your last 5 analysis runs\n"
        "/status — Check system health\n\n"
        "_Upload your file to begin_ 👆"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    sys.stdout.flush()


# ── /help ─────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("help") & filters.private)
async def cmd_help(client: Client, message: Message) -> None:
    text = (
        "📖 *SDSS Bot — Usage Guide*\n\n"
        "*Supported Formats:*\n"
        "• GeoJSON `.geojson` / `.json` — Preferred format\n"
        "• KML `.kml` / KMZ `.kmz` — Google Earth exports\n"
        "• GeoPackage `.gpkg` — QGIS multi-layer vectors\n"
        "• Shapefile Package `.zip` — Archive containing .shp, .shx, .dbf, .prj\n\n"
        "*Requirements:*\n"
        "• Geometry must be a Polygon or MultiPolygon\n"
        "• Any coordinate reference system is accepted (auto-converted)\n\n"
        "*Output Report options:*\n"
        "• Custom Attributes Table inspection metrics\n"
        "• FSI Forest Cover class breakdowns and Terrain Elevation maps\n\n"
        "*Tips:*\n"
        "• Export polygons from QGIS/ArcGIS as GeoJSON for best results\n"
        "• Ensure the polygon falls within the study area extent"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    sys.stdout.flush()


# ── Vector File Document Catch Mechanism ──────────────────────────────────────
@Client.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: Message) -> None:
    document = message.document
    user = message.from_user

    suffix = Path(document.file_name or "").suffix.lower()
    if suffix not in {".geojson", ".json", ".kml", ".gpkg", ".kmz", ".zip"}:
        await message.reply_text(
            "⚠️ Please upload a valid spatial `.geojson`, `.kml`, `.gpkg`, `.kmz`, or shapefile `.zip` archive.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    status_msg = await message.reply_text("📥 *Processing vector layout properties…*", parse_mode=ParseMode.MARKDOWN)
    sys.stdout.flush()
    
    tmp_path = Path(tempfile.gettempdir() if hasattr(cfg, 'TEMP_DIR') == False else cfg.TEMP_DIR) / f"{user.id}_{document.file_name}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        # Pyrogram native streaming download interface hook
        await client.download_media(message, file_name=str(tmp_path))

        geojson_feature, gdf_attributes = load_vector_file(tmp_path)

        attr_df = gdf_attributes.drop(columns=["geometry"], errors="ignore")
        
        # 🚀 Buffer parsing metrics straight down into global module cache runtime lookup
        USER_SESSION_CACHE[user.id] = {
            "current_feature": geojson_feature,
            "filename": document.file_name,
            "cached_df_dict": attr_df.to_dict(orient="records")
        }

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Attributes Table", callback_data="action_attributes")],
            [InlineKeyboardButton("🔬 Run Spatial DSS Analysis", callback_data="action_analysis")]
        ])
        
        await status_msg.delete()
        await message.reply_text(
            f"✅ *Layer Ingested Successfully!*\n\n"
            f"📁 *File:* `{document.file_name}`\n"
            f"📦 *Total Features:* `{len(gdf_attributes)}` Polygons/Parts\n\n"
            f"Select processing task:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    except Exception as exc:
        logger.error("Failed to parse or ingest uploaded vector document.", exc_info=True)
        if status_msg:
            await status_msg.edit_text(f"❌ *Vector ingestion failed:* {exc}", parse_mode=ParseMode.MARKDOWN)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
        sys.stdout.flush()


# ── Interactive Callback Menu Router ──────────────────────────────────────────
@Client.on_callback_query()
async def handle_button_click(client: Client, callback_query: CallbackQuery) -> None:
    await callback_query.answer()

    action = callback_query.data
    user = callback_query.from_user  
    
    # Extract structural pointers out of global tracking map lookup securely
    user_session = USER_SESSION_CACHE.get(user.id, {})
    geojson_feature = user_session.get("current_feature")
    cached_records = user_session.get("cached_df_dict")
    filename = user_session.get("filename", "layer")

    if not geojson_feature or cached_records is None:
        await callback_query.edit_message_text("❌ Session expired. Please upload your vector file again.")
        return

    base_name = Path(filename).stem

    if action == "action_attributes":
        await client.send_chat_action(chat_id=user.id, action=ChatAction.UPLOAD_DOCUMENT)
        df = pd.DataFrame(cached_records)
        
        text_preview = f"📊 *Attributes Table Preview for `{filename}`*\n"
        text_preview += f"Total rows detected: `{len(df)}` \n\n"
        text_preview += "```text\n"
        
        cols = [c for c in df.columns if c not in ["description", "Description"]]
        text_preview += " | ".join(cols[:4]) + "\n"
        text_preview += "-" * 35 + "\n"
        
        for _, row in df.head(5).iterrows():
            vals = [str(row[c])[:12] for c in cols[:4]]
            text_preview += " | ".join(vals) + "\n"
            
        if len(df) > 5:
            text_preview += "... data frame truncated for chat view.\n"
        text_preview += "```"

        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        csv_bytes = csv_buffer.getvalue().encode('utf-8')
        csv_buffer.close()

        # Target response straight back to current message framework structures
        await callback_query.message.reply_text(text_preview, parse_mode=ParseMode.MARKDOWN)
        
        # Stream raw byte packets directly back to Telegram without InputFile wrapper
        bio = io.BytesIO(csv_bytes)
        bio.name = f"{base_name}_attributes.csv"
        await callback_query.message.reply_document(
            document=bio,
            caption=f"📂 *Complete Attribute Table Spreadsheet* (`fid` index mapped).",
            parse_mode=ParseMode.MARKDOWN
        )
        await callback_query.message.delete()
        sys.stdout.flush()

    elif action == "action_analysis":
        status_msg = await callback_query.edit_message_text(
            "🔬 *Running full spatial intersection algorithms…*\n"
            "_(Streaming raster tiles from cloud matrices)_",
            parse_mode=ParseMode.MARKDOWN
        )
        sys.stdout.flush()
        
        try:
            results = await asyncio.get_event_loop().run_in_executor(None, run_analysis, geojson_feature)
            map_png = await asyncio.get_event_loop().run_in_executor(None, render_map, geojson_feature, results, filename)

            log_analysis(user.id, filename, geojson_feature, results, results["centroid"])
            summary = _build_summary(results, filename)
            
            await status_msg.delete()
            await callback_query.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
            
            # Pack photo binary payload data structures cleanly
            photo_bio = io.BytesIO(map_png)
            photo_bio.name = "sdss_report.png"
            await callback_query.message.reply_photo(photo=photo_bio, caption="📊 *SDSS Cartographic Report*")
        except Exception as err:
            logger.error("Analysis thread pipeline execution failed: %s", err, exc_info=True)
            if status_msg:
                await status_msg.edit_text(f"❌ *Analysis processing failed:* {err}", parse_mode=ParseMode.MARKDOWN)
        finally:
            sys.stdout.flush()


# ── /history ──────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("history") & filters.private)
async def cmd_history(client: Client, message: Message) -> None:
    user = message.from_user
    logs = get_user_history(user.id, limit=5)

    if not logs:
        await message.reply_text(
            "📭 No analysis history found. Upload a file to get started!",
            parse_mode=ParseMode.MARKDOWN
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

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    sys.stdout.flush()


# ── /status ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("status") & filters.private)
async def cmd_status(client: Client, message: Message) -> None:
    await client.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    checks = {}

    try:
        from modules.database import _get_db
        _get_db().command("ping")
        checks["MongoDB Atlas"] = "✅ Connected"
    except Exception as exc:
        checks["MongoDB Atlas"] = f"❌ Error: {exc}"

    try:
        from modules.storage import _get_supabase
        _get_supabase().storage.from_(cfg.SUPABASE_BUCKET).list()
        checks["Supabase Storage"] = "✅ Connected"
    except Exception as exc:
        checks["Supabase Storage"] = f"❌ Error: {exc}"

    lines = ["🔧 *System Status*\n"]
    for service, status in checks.items():
        lines.append(f"*{service}:* {status}")

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    sys.stdout.flush()


# ── Unknown Message Fallbacks ─────────────────────────────────────────────────
@Client.on_message(filters.text & filters.private & ~filters.regex(r"^/"))
async def handle_unknown(client: Client, message: Message) -> None:
    await message.reply_text(
        "📂 Please upload a spatial vector file (`.geojson`, `.kml`, `.gpkg`, `.kmz`, or shapefile `.zip`) "
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

