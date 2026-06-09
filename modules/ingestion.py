"""
modules/ingestion.py — Admin data ingestion pipeline.
Slices massive master vector datasets (FCM, FTM, DEM Contours) using a Supabase grid framework,
uploads the chunk files to a private Telegram channel drive via Pyrogram MTProto,
and logs the permanent mapping indices cleanly into MongoDB Atlas.
"""

import logging
import sys
import tempfile
import os
import shutil
import zipfile
import asyncio
import gc
from datetime import datetime, timezone
from pathlib import Path
import geopandas as gpd
import pandas as pd
import fiona

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from config import cfg
from modules.database import _get_db      # Dynamic helper targeting your active Atlas DB
from modules.storage import _get_supabase # Supabase client handler

# ── Logging Setup linked to standard output for Koyeb Console visibility ──────
logger = logging.getLogger("main.ingestion")
logger.setLevel(logging.INFO)

if not logger.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)


@Client.on_message(filters.command("upload_master") & filters.private)
async def cmd_upload_master(client: Client, message: Message) -> None:
    """
    Admin Command: /upload_master [DATA_TYPE] (Sent as a reply to a document)
    Slices vector layers by grid framework with strict memory ceiling caps and FloodWait backoffs.
    """
    # 1. Verification Guardrails
    if not message.reply_to_message or not message.reply_to_message.document:
        logger.warning("❌ Ingestion Rejected: Command executed without replying to a valid file document.")
        await message.reply_text(
            "⚠️ *Usage Instruction:*\n\n"
            "1. Upload your master vector file.\n"
            "2. Reply to that file.\n"
            "3. Send `/upload_master FCM` (or FTM, DEM).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    msg_text = message.text or ""
    args = msg_text.split()
    if len(args) < 2:
        logger.warning("❌ Ingestion Rejected: Missing DATA_TYPE parameter argument.")
        await message.reply_text(
            "⚠️ Missing data type variable.\n"
            "Usage: Reply with `/upload_master FCM`, `FTM`, or `DEM`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    data_type = args[1].strip().upper()
    if data_type not in {"FCM", "FTM", "DEM"}:
        await message.reply_text(
            "⚠️ Invalid DATA_TYPE.\nUsage: `/upload_master FCM`, `FTM`, or `DEM`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    document = message.reply_to_message.document
    safe_filename = Path(document.file_name or "master_layer.gpkg").name
    suffix = Path(safe_filename).suffix.lower()

    logger.info("==========================================================================")
    logger.info(f"🚀 INGESTION TRIGGERED | Type: {data_type} | Target Asset Name: {safe_filename}")
    logger.info(f"📦 Pyrogram Document File ID Pointer: {document.file_id}")
    logger.info("==========================================================================")

    if suffix not in {".geojson", ".gpkg", ".zip"}:
        logger.warning(f"❌ Ingestion Rejected: File extension '{suffix}' is unsupported.")
        await message.reply_text(
            "⚠️ Unsupported master format. Use `.geojson`, `.gpkg`, or shapefile `.zip`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        raw_channel_id = str(cfg.TELEGRAM_CHANNEL_ID).strip()
        CHANNEL_CHAT_ID = int(raw_channel_id)
    except ValueError:
        CHANNEL_CHAT_ID = raw_channel_id

    status_msg = await message.reply_text(
        f"⏳ *Initializing MTProto channel-drive pipeline for master {data_type} ingestion…*",
        parse_mode=ParseMode.MARKDOWN
    )
    sys.stdout.flush()

    tmp_dir = None

    try:
        tmp_dir = Path(tempfile.mkdtemp())
        master_path = tmp_dir / safe_filename
        grid_path = tmp_dir / "state_fishnet_grid.gpkg"

        # Download Master Vector Dataset
        logger.info(f"📥 Downloading raw file asset '{safe_filename}' via Pyrogram client...")
        await status_msg.edit_text(
            f"📥 *Downloading master {data_type} vector layer from Telegram updates…*",
            parse_mode=ParseMode.MARKDOWN
        )
        await client.download_media(message.reply_to_message, file_name=str(master_path))
        logger.info(f"💾 Local download locked in. Path: {master_path} | Size: {os.path.getsize(master_path) / (1024*1024):.2f} MB")

        # Download Framework Grid
        logger.info(f"🛰 Fetching structural grid 'state_grid.gpkg' from Supabase Bucket...")
        await status_msg.edit_text(
            "🛰 *Streaming Master Fishnet Grid framework from Supabase Storage…*",
            parse_mode=ParseMode.MARKDOWN
        )
        supabase = _get_supabase()
        with open(grid_path, "wb") as f:
            res = supabase.storage.from_(cfg.SUPABASE_BUCKET).download("state_grid.gpkg")
            f.write(res)
        logger.info(f"📐 Framework grid downloaded. Path: {grid_path} | Size: {os.path.getsize(grid_path) / (1024*1024):.2f} MB")

        grid_gdf = gpd.read_file(str(grid_path))
        logger.info(f"📊 Grid Records initialized: {len(grid_gdf)} mapping cells available.")

        final_master_source = master_path
        if suffix == ".zip":
            logger.info("🗜 Expanding zipped shapefile archive contents...")
            with zipfile.ZipFile(master_path, "r") as zip_ref:
                zip_ref.extractall(tmp_dir)
            shp_files = [s for s in tmp_dir.glob("**/*.shp") if s.is_file() and not s.name.startswith("._")]
            if not shp_files:
                raise ValueError("No valid .shp file found inside uploaded archive package.")
            if len(shp_files) > 1:
                logger.warning(f"Multiple shapefiles found in archive. Using first: {shp_files}")
            final_master_source = shp_files[0]

        with fiona.open(str(final_master_source)) as src:
            master_crs = src.crs_wkt or src.crs

        if not master_crs:
            raise ValueError("Master dataset does not contain a valid CRS definition.")

        db = _get_db()
        collection_map = {"FCM": "fcm_layers", "FTM": "ftm_layers", "DEM": "dem_layers"}
        collection_name = collection_map[data_type]
        col = db[collection_name]

        success_count = 0
        logger.info(f"✂️ Spatial streaming processes active. Target DB Collection: {collection_name}")

        # 🚀 HYBRID STREAMER: Keep Fiona outside the loop, filter cell-by-cell inside
        with fiona.open(str(final_master_source)) as source_stream:
            for idx, cell in grid_gdf.iterrows():
                grid_id = cell.get("grid_id", f"cell_{idx}")
                cell_geom = cell.geometry
                
                # Project bounding coordinates to layer projection standards
                cell_bbox_gdf = gpd.GeoDataFrame(geometry=[cell_geom], crs=grid_gdf.crs).to_crs(master_crs)
                cell_bbox = tuple(cell_bbox_gdf.total_bounds)

                # Fetch features matching bounding limits safely
                features_in_box = list(source_stream.filter(bbox=cell_bbox))
                if not features_in_box:
                    continue

                # Process this lightweight chunk completely isolated in memory
                clipped_gdf = gpd.GeoDataFrame.from_features(features_in_box, crs=master_crs)
                features_in_box.clear()

                if clipped_gdf.crs != grid_gdf.crs:
                    clipped_gdf = clipped_gdf.to_crs(grid_gdf.crs)

                try:
                    from shapely.validation import make_valid
                    clipped_gdf["geometry"] = clipped_gdf.geometry.apply(make_valid)
                except Exception:
                    clipped_gdf["geometry"] = clipped_gdf.geometry.buffer(0)
                
                clipped_gdf["geometry"] = clipped_gdf.geometry.intersection(cell_geom)
                clipped_gdf = clipped_gdf[clipped_gdf.geometry.notnull()].copy()
                clipped_gdf = clipped_gdf[~clipped_gdf.geometry.is_empty].copy()

                if clipped_gdf.empty:
                    continue

                for col_name in clipped_gdf.columns:
                    if isinstance(clipped_gdf[col_name].dtype, pd.StringDtype):
                        clipped_gdf[col_name] = clipped_gdf[col_name].astype(object)

                if success_count and success_count % 5 == 0:
                    try:
                        await status_msg.edit_text(
                            f"✂️ *Slicing {data_type} vector assets safely…*\n\n"
                            f"📍 Active Segment: `Grid_{grid_id}`\n"
                            f"📦 Total Cached Parts: `{success_count}` chunks",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception:
                        pass

                chunk_filename = f"{data_type.lower()}_{grid_id}.geojson"
                chunk_filepath = tmp_dir / chunk_filename
                clipped_gdf.to_file(str(chunk_filepath), driver="GeoJSON")
                
                file_bytes_size = chunk_filepath.stat().st_size
                if (file_bytes_size / (1024 * 1024)) > 1900:
                    continue
                
                chan_msg = None
                while not chan_msg:
                    try:
                        logger.info(f"📤 Uploading partition chunk: {chunk_filename} over to Channel ID: {CHANNEL_CHAT_ID}")
                        chan_msg = await client.send_document(
                            chat_id=CHANNEL_CHAT_ID,
                            document=str(chunk_filepath),
                            caption=f"📦 SDSS Production Master Part Asset\n• DataType: {data_type}\n• Partition Index: {grid_id}"
                        )
                    except FloodWait as flood_exception:
                        logger.warning(f"⚠️ Telegram FloodWait triggered! Cooling down processing loops for {flood_exception.value} seconds.")
                        await asyncio.sleep(flood_exception.value)
                    except Exception as upload_err:
                        logger.error(f"❌ Aborting channel pipe send action sequence: {upload_err}")
                        raise upload_err

                payload = {
                    "grid_id": grid_id,
                    "data_type": data_type,
                    "channel_chat_id": str(CHANNEL_CHAT_ID),
                    "channel_message_id": chan_msg.id,
                    "file_id": chan_msg.document.file_id,
                    "file_name": chunk_filename,
                    "feature_count": len(clipped_gdf),
                    "updated_at": datetime.now(timezone.utc)
                }
                col.update_one({"grid_id": grid_id, "data_type": data_type}, {"$set": payload}, upsert=True)
                
                success_count += 1
                chunk_filepath.unlink(missing_ok=True)

                # 🚀 RAM CLEANUP FLUSH
                del clipped_gdf
                del cell_bbox_gdf
                gc.collect()

        await status_msg.delete()
        await message.reply_text(
            f"✅ *Master Ingestion Pipeline Completed Successfully!*\n\n"
            f"🏷 *Data Type Index:* `{data_type}`\n"
            f"🧩 *Total Structural Part Segments Formed:* `{success_count}` chunks\n"
            f"🗂 *Target Registry Store:* MongoDB Cluster `[{collection_name}]`",
            parse_mode=ParseMode.MARKDOWN
        )

except Exception as pipeline_err:
    logger.error("A critical execution error derailed data ingestion pipeline.", exc_info=True)
    if 'status_msg' in locals():
        await status_msg.edit_text(f"❌ Master Ingestion Pipeline Crashed: {pipeline_err}")
finally:
    try:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir)
    except Exception:
        logger.exception("Failed to remove temporary directory workspace structures.")
    sys.stdout.flush()


# ── Manual Diagnostics & Broadcasting Interface ──────────────────────────────
@Client.on_message(filters.command("post") & filters.private)
async def cmd_manual_broadcast(client: Client, message: Message) -> None:
    """
    Diagnostic Command: /post [Message text or sent as a reply to media/text]
    Validates text parameters extraction and forwards raw packets to channel targets.
    """
    raw_channel_id = str(cfg.TELEGRAM_CHANNEL_ID).strip()
    if raw_channel_id.startswith("-") or raw_channel_id.isdigit():
        try:
            CHANNEL_CHAT_ID = int(raw_channel_id)
        except ValueError:
            CHANNEL_CHAT_ID = raw_channel_id
    else:
        CHANNEL_CHAT_ID = raw_channel_id

    logger.info(f"📣 Manual broadcast triggered. Destination target peer index: {CHANNEL_CHAT_ID}")

    message_text_raw = message.text or ""
    parts = message_text_raw.split(maxsplit=1)
    text_content = parts if len(parts) > 1 else ""

    try:
        if message.reply_to_message:
            replied = message.reply_to_message
            caption_text = text_content or replied.caption or replied.text or ""

            if replied.photo:
                await client.send_photo(chat_id=CHANNEL_CHAT_ID, photo=replied.photo.file_id, caption=caption_text)
            elif replied.video:
                await client.send_video(chat_id=CHANNEL_CHAT_ID, video=replied.video.file_id, caption=caption_text)
            elif replied.document:
                await client.send_document(chat_id=CHANNEL_CHAT_ID, document=replied.document.file_id, caption=caption_text)
            elif replied.text:
                await client.send_message(chat_id=CHANNEL_CHAT_ID, text=replied.text)
            else:
                await message.reply_text("⚠️ Manual forward failed: Unsupported media type detected.")
                return
        
        elif text_content:
            await client.send_message(chat_id=CHANNEL_CHAT_ID, text=text_content)
        
        else:
            await message.reply_text(
                "⚠️ *Usage Instruction:*\n\n"
                "• Send `/post Your Message Here` to dispatch text directly.\n"
                "• Reply to a photo, document, or video with `/post` to broadcast media.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        await message.reply_text(f"🚀 *Broadcast dispatched safely to target chat peer:* `{CHANNEL_CHAT_ID}`", parse_mode=ParseMode.MARKDOWN)

    except Exception as broadcast_err:
        logger.error("Diagnostic broadcast execution derailed.", exc_info=True)
        await message.reply_text(f"❌ *Broadcast delivery failed:* `{broadcast_err}`", parse_mode=ParseMode.MARKDOWN)
                        
