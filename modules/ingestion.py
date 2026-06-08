"""
modules/ingestion.py — Admin data ingestion pipeline.
Slices massive master vector datasets using a Supabase grid framework,
uploads the chunk files to a private Telegram channel drive via Pyrogram MTProto,
and logs the permanent mapping indices cleanly into MongoDB Atlas.
"""

import logging
import sys
import tempfile
import os
import shutil
import zipfile
from pathlib import Path
import geopandas as gpd
import pandas as pd
import fiona

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode

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
    Slices by grid, uploads files to a channel via MTProto, and indexes in MongoDB.
    """
    # 1. Verification Guardrails with Explicit Logging Traces
    if not message.reply_to_message or not message.reply_to_message.document:
        logger.warning("❌ Ingestion Rejected: Command was executed without replying to a valid file document.")
        await message.reply_text(
            "⚠️ *Usage Instruction:*\n\n"
            "1. Upload your master vector file.\n"
            "2. Reply to that file.\n"
            "3. Send `/upload_master FCM` (or FTM).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Pyrogram parses arguments into text split parameters
    args = message.text.split() if message.text else []
    if len(args) < 2:
        logger.warning("❌ Ingestion Rejected: Missing DATA_TYPE parameter argument.")
        await message.reply_text(
            "⚠️ Missing data type variable.\n"
            "Usage: Reply with `/upload_master FCM` or `/upload_master FTM`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    data_type = args[1].upper()
    document = message.reply_to_message.document
    suffix = Path(document.file_name or "").suffix.lower()

    logger.info("==========================================================================")
    logger.info(f"🚀 INGESTION TRIGGERED | Type: {data_type} | Target Asset Name: {document.file_name}")
    logger.info(f"📦 Pyrogram Document File ID Pointer: {document.file_id}")
    logger.info("==========================================================================")

    if suffix not in {".geojson", ".gpkg", ".zip"}:
        logger.warning(f"❌ Ingestion Rejected: File extension '{suffix}' is unsupported.")
        await message.reply_text(
            "⚠️ Unsupported master format. "
            "Use `.geojson`, `.gpkg`, or shapefile `.zip`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    CHANNEL_CHAT_ID = cfg.TELEGRAM_CHANNEL_ID

    status_msg = await message.reply_text(
        f"⏳ *Initializing MTProto channel-drive pipeline for master {data_type} ingestion…*",
        parse_mode=ParseMode.MARKDOWN
    )
    sys.stdout.flush()

    tmp_dir = Path(tempfile.mkdtemp())
    master_path = tmp_dir / document.file_name
    grid_path = tmp_dir / "state_fishnet_grid.gpkg"

    try:
        # -------------------------------------------------------------
        # Download uploaded file
        # -------------------------------------------------------------
        logger.info(f"📥 Downloading raw file asset '{document.file_name}' via Pyrogram client...")
        await status_msg.edit_text(
            "📥 *Downloading master vector layer from Telegram updates…*",
            parse_mode=ParseMode.MARKDOWN
        )

        await client.download_media(message.reply_to_message, file_name=str(master_path))
        logger.info(f"💾 Local download locked in. Path: {master_path} | Size: {os.path.getsize(master_path) / (1024*1024):.2f} MB")

        # -------------------------------------------------------------
        # Download grid from Supabase
        # -------------------------------------------------------------
        logger.info(f"🛰 Fetching structural grid 'state_grid.gpkg' from Supabase Bucket: '{cfg.SUPABASE_BUCKET}'...")
        await status_msg.edit_text(
            "🛰 *Streaming Master Fishnet Grid framework from Supabase Storage…*",
            parse_mode=ParseMode.MARKDOWN
        )

        supabase = _get_supabase()
        with open(grid_path, "wb") as f:
            res = (
                supabase.storage
                .from_(cfg.SUPABASE_BUCKET)
                .download("state_grid.gpkg")
            )
            f.write(res)
        logger.info(f"📐 Framework grid downloaded. Path: {grid_path} | Size: {os.path.getsize(grid_path) / (1024*1024):.2f} MB")

        # -------------------------------------------------------------
        # Read Bounding Framework Grid Only
        # -------------------------------------------------------------
        grid_gdf = gpd.read_file(str(grid_path))
        logger.info(f"📊 Grid Records initialized: {len(grid_gdf)} mapping cells available.")

        # Determine target file tracking source path node
        final_master_source = master_path
        if suffix == ".zip":
            logger.info("🗜 Expanding zipped shapefile archive contents...")
            with zipfile.ZipFile(master_path, "r") as zip_ref:
                zip_ref.extractall(tmp_dir)
            shp_files = [s for s in tmp_dir.glob("**/*.shp") if s.is_file() and not s.name.startswith("._")]
            if not shp_files:
                raise ValueError("No valid .shp file found inside uploaded archive package.")
            final_master_source = shp_files

        # Determine CRS alignment properties without reading geometries yet
        with fiona.open(str(final_master_source)) as src:
            master_crs = src.crs

        # Setup database collections
        db = _get_db()
        collection_name = "fcm_layers" if data_type == "FCM" else "ftm_layers"
        col = db[collection_name]

        success_count = 0
        logger.info(f"✂️ Spatial streaming processes active. Target DB Collection: {collection_name}")

        # -------------------------------------------------------------
        # 🚀 MEMORY OPTIMIZATION LAYER: Spatial Bounding-Box Streaming Loop
        # -------------------------------------------------------------
        for idx, cell in grid_gdf.iterrows():
            grid_id = cell.get("grid_id", f"cell_{idx}")
            cell_geom = cell.geometry
            
            # Align the bounding box of our cell target to the master layer CRS system profile
            cell_bbox_gdf = gpd.GeoDataFrame(geometry=[cell_geom], crs=grid_gdf.crs).to_crs(master_crs)
            cell_bbox = cell_bbox_gdf.geometry.iloc.bounds  # Returns (minx, miny, maxx, maxy)

            # 🛠 Fiona reads ONLY the geometric items inside this cell box from disk into memory
            with fiona.open(str(final_master_source)) as source_stream:
                features_in_box = list(source_stream.filter(bbox=cell_bbox))

            if not features_in_box:
                continue

            # Load the filtered lightweight subset items into our active pandas matrix frame
            clipped_gdf = gpd.GeoDataFrame.from_features(features_in_box, crs=master_crs)
            
            # Re-align projections to match grid frameworks
            if clipped_gdf.crs != grid_gdf.crs:
                clipped_gdf = clipped_gdf.to_crs(grid_gdf.crs)

            # Topology correction pass
            clipped_gdf["geometry"] = clipped_gdf.geometry.make_valid()
            
            # Clip vectors tightly to our bounding lines
            clipped_gdf["geometry"] = clipped_gdf.geometry.intersection(cell_geom)
            clipped_gdf = clipped_gdf[~clipped_gdf.geometry.is_empty & clipped_gdf.geometry.notnull()].copy()

            if clipped_gdf.empty:
                continue

            # Throttle status update signals to clear out rate-limiting rules
            if success_count % 5 == 0:
                try:
                    await status_msg.edit_text(
                        f"✂️ *Streaming & Slicing vector assets safely…*\n\n"
                        f"📍 Active Segment: `Grid_{grid_id}`\n"
                        f"📦 Total Cached Parts: `{success_count}` chunks",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception:
                    pass

            # Export individual slice arrays out to workspace paths
            chunk_filename = f"{data_type.lower()}_{grid_id}.geojson"
            chunk_filepath = tmp_dir / chunk_filename
            clipped_gdf.to_file(str(chunk_filepath), driver="GeoJSON")
            
            # Dispatch directly over MTProto channel interface links
            logger.info(f"📤 Uploading partition chunk: {chunk_filename} over to Channel ID: {CHANNEL_CHAT_ID}")
            chan_msg = await client.send_document(
                chat_id=CHANNEL_CHAT_ID,
                document=str(chunk_filepath),
                caption=f"📦 SDSS Production Master Part Asset\n• DataType: {data_type}\n• Partition Index: {grid_id}"
            )
            
            # Record tracking metadata information blocks to MongoDB Cluster Index collections
            payload = {
                "grid_id": grid_id,
                "data_type": data_type,
                "file_id": chan_msg.document.file_id,
                "file_name": chunk_filename,
                "feature_count": len(clipped_gdf),
                "updated_at": pd.Timestamp.now().isoformat()
            }
            col.update_one({"grid_id": grid_id}, {"$set": payload}, upsert=True)
            
            success_count += 1
            chunk_filepath.unlink(missing_ok=True)

        # Operational closure metrics pass
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
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        sys.stdout.flush()
    
