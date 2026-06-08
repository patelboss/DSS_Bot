"""
modules/ingestion.py — Admin data ingestion pipeline.
Slices massive master vector datasets using a Supabase grid framework,
uploads the chunk files to a private Telegram channel drive, and logs 
the permanent message index mapping metadata cleanly into MongoDB.
"""

import logging
import sys
import tempfile
import shutil
import zipfile
from pathlib import Path
import geopandas as gpd

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import cfg
from modules.database import _get_db      # Dynamic helper targeting your active Atlas DB
from modules.storage import _get_supabase # Supabase client handler

# ── Force Stream / Unbuffered Stdout Logging Setup for Koyeb Console ─────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(stdout_handler)


async def cmd_upload_master(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin Command: /upload_master [DATA_TYPE] (Sent as a REPLY to a spatial document)
    Slices by grid, uploads files to a channel, and indexes them in MongoDB.
    """
    message = update.message
    
    # 1. Check if this command is a reply to an existing document file
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply_text(
            "⚠️ *Usage Instruction:*\n\n"
            "1. Upload your master vector file to the chat first.\n"
            "2. Long-press or click that file and select **Reply**.\n"
            "3. Type `/upload_master FCM` (or FTM) and send it!",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if not context.args:
        await message.reply_text(
            "⚠️ Missing data type variable.\nUsage: Reply with `/upload_master FCM` or `/upload_master FTM`", 
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 🚀 FIXED: Extract index from context.args list before running string functions!
    data_type = context.args.upper()
    document = message.reply_to_message.document
    suffix = Path(document.file_name or "").suffix.lower()

    if suffix not in {".geojson", ".gpkg", ".zip"}:
        await message.reply_text("⚠️ Unsupported master format. Please ensure the target file is a `.geojson`, `.gpkg`, or shapefile `.zip` archive.")
        return

    # 🎯 CONFIG: Set your target Channel ID
    # Make sure your bot is added as an Administrator to this channel!
    CHANNEL_CHAT_ID = -100358841607  # 👈 Replace this with your private channel's actual Chat ID
    
    status_msg = await message.reply_text(f"⏳ *Initializing channel-drive pipeline for master {data_type} ingestion…*", parse_mode=ParseMode.MARKDOWN)
    sys.stdout.flush()

    tmp_dir = Path(tempfile.mkdtemp())
    master_path = tmp_dir / document.file_name
    grid_path = tmp_dir / "state_fishnet_grid.gpkg"

    try:
        # 2. Download the uploaded Master Vector file from Telegram
        await status_msg.edit_text("📥 *Downloading master vector layer from Telegram updates…*", parse_mode=ParseMode.MARKDOWN)
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(str(master_path))

        # 3. Pull down your structural Master Grid from Supabase Storage
        await status_msg.edit_text("🛰 *Streaming Master Fishnet Grid framework from Supabase Storage…*", parse_mode=ParseMode.MARKDOWN)
        supabase = _get_supabase()
        
        with open(grid_path, "wb") as f:
            res = supabase.storage.from_(cfg.SUPABASE_BUCKET).download("state_grid.gpkg")
            f.write(res)

        # 4. Ingest datasets into memory GeoDataFrames
        await status_msg.edit_text("🔬 *Executing spatial intersection matrix split…\n(This might take a minute)*", parse_mode=ParseMode.MARKDOWN)
        sys.stdout.flush()
        
        # 🚀 FIXED: Extracted zip asset string mapping correctly using index
        if suffix == ".zip":
            with zipfile.ZipFile(master_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
            shp_files = [f for f in tmp_dir.glob("**/*.shp") if f.is_file() and not f.name.startswith("._")]
            if not shp_files:
                raise ValueError("No valid .shp file found inside uploaded archive package.")
            master_gdf = gpd.read_file(str(shp_files))
        else:
            master_gdf = gpd.read_file(str(master_path))

        grid_gdf = gpd.read_file(str(grid_path))

        # Match projections across data streams natively
        if master_gdf.crs != grid_gdf.crs:
            master_gdf = master_gdf.to_crs(grid_gdf.crs)

        # 5. Spatial Join Optimization
        joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")
        
        # Identify column name for grid references (Assumes grid has 'grid_id' or 'grid_num')
        grid_id_col = "grid_id" if "grid_id" in joined_gdf.columns else "grid_num"
        unique_grids = joined_gdf[grid_id_col].unique()

        db = _get_db()
        collection = db["grid_catalog"] # Lightweight catalog tracking database collection

        # 6. Loop chunks, slice clean boundaries, upload to channel, map to MongoDB
        counter = 0
        for grid_id in unique_grids:
            raw_chunk = joined_gdf[joined_gdf[grid_id_col] == grid_id]
            grid_poly = grid_gdf[grid_gdf[grid_id_col] == grid_id]
            
            # Clip features exactly at the boundary lines to avoid border duplication
            chunk = gpd.clip(raw_chunk, grid_poly)
            if chunk.empty:
                continue

            # Generate a deterministic file key layout name
            filename = f"{data_type}@Grid_{grid_id}.gpkg"
            chunk_out_path = tmp_dir / filename
            
            # Save local sub-grid chunk file to scratch space
            chunk.to_file(str(chunk_out_path), driver="GPKG")
            
            # 🚀 TELEGRAM UPLOAD: Push the file to your infinite storage channel drive
            with open(chunk_out_path, "rb") as file_payload:
                channel_msg = await context.bot.send_document(
                    chat_id=CHANNEL_CHAT_ID,
                    document=file_payload,
                    caption=f"📦 Grid Reference Asset: {filename}\nType: #{data_type} #Grid_{grid_id}"
                )
            
            # 🚀 DATABASE INDEXING: Store only metadata pointers inside MongoDB Atlas
            deterministic_key = f"{data_type}@Grid_{grid_id}"
            collection.update_one(
                {"_id": deterministic_key},
                {
                    "$set": {
                        "grid_id": int(grid_id) if isinstance(grid_id, (int, float)) else str(grid_id),
                        "data_type": data_type,
                        "channel_chat_id": CHANNEL_CHAT_ID,
                        "channel_message_id": channel_msg.message_id, 
                        "file_name": filename
                    }
                },
                upsert=True
            )
            counter += 1

        await status_msg.edit_text(
            f"✅ *Ingestion Completed Successfully!*\n\n"
            f"📊 *Dataset:* `Master {data_type}`\n"
            f"📡 *Storage Backend:* `Telegram Channel Drive`\n"
            f"📦 *Slices Uploaded & Cataloged:* `{counter}` Grid Packages.",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as exc:
        log.error("Master processing channel pipeline failed.", exc_info=True)
        await status_msg.edit_text(f"❌ *Master channel-upload pipeline crashed:* {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.stdout.flush()
        
