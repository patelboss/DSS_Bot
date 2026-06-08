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

    # FIXED: args is the command string itself, args contains your datatype string parameter
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

    # Pulled directly from your updated central config
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

        # Pyrogram uses download_media directly targeting the message object
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
        # Read datasets
        # -------------------------------------------------------------
        logger.info("🔬 Parsing layers into GeoPandas DataFrames spatial memory engines...")
        await status_msg.edit_text(
            "🔬 *Executing spatial intersection matrix split…*\n"
            "(This might take a minute)",
            parse_mode=ParseMode.MARKDOWN
        )
        sys.stdout.flush()

        if suffix == ".zip":
            logger.info("🗜 Expanding zipped shapefile archive contents...")
            with zipfile.ZipFile(master_path, "r") as zip_ref:
                zip_ref.extractall(tmp_dir)

            shp_files = [
                shp
                for shp in tmp_dir.glob("**/*.shp")
                if shp.is_file() and not shp.name.startswith("._")
            ]

            if not shp_files:
                logger.error("❌ Extraction Error: Could not locate a valid .shp tracking node inside file package.")
                raise ValueError("No valid .shp file found inside uploaded archive package.")

            # 🚀 FIXED: Extract index from your shapefile list match array
            target_shp = shp_files[0]
            logger.info(f"🎯 Target shapefile found: {target_shp.name}")
            master_gdf = gpd.read_file(str(target_shp))
        else:
            master_gdf = gpd.read_file(str(master_path))

        grid_gdf = gpd.read_file(str(grid_path))
        logger.info(f"📊 Vector arrays initialized. Master Records: {len(master_gdf)} geometries | Grid Records: {len(grid_gdf)} cells.")

        # -------------------------------------------------------------
        # CRS harmonization
        # -------------------------------------------------------------
        if master_gdf.crs != grid_gdf.crs:
            logger.info(f"🔄 Projections disparity. Projecting Master from '{master_gdf.crs}' to match Grid '{grid_gdf.crs}'...")
            master_gdf = master_gdf.to_crs(grid_gdf.crs)

        # -------------------------------------------------------------
        # Geometry repair helper
        # -------------------------------------------------------------
        def repair_geometries(gdf, name="Dataset"):
            try:
                invalid_count = (~gdf.is_valid).sum()
                if invalid_count:
                    logger.warning(f"⚠️ Found {invalid_count} topology-broken shapes inside {name}. Deploying correction tools...")
                    try:
                        gdf["geometry"] = gdf.geometry.make_valid()
                    except Exception as err:
                        logger.warning(f"make_valid crashed ({err}). Defaulting to buffer(0) scaling fallback.")
                        gdf["geometry"] = gdf.geometry.buffer(0)
            except Exception as outer_err:
                logger.error(f"Error checking topologies inside {name}: {outer_err}")
                gdf["geometry"] = gdf.geometry.buffer(0)
            
            gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
            return gdf

        master_gdf = repair_geometries(master_gdf, "Master Dataset")
        grid_gdf = repair_geometries(grid_gdf, "Grid Framework")

        # -------------------------------------------------------------
        # Core Spatial Overlay & Data Chunk Dispersal Engine
        # -------------------------------------------------------------
        db = _get_db()
        collection_name = "fcm_layers" if data_type == "FCM" else "ftm_layers"
        col = db[collection_name]

        success_count = 0
        logger.info(f"✂️ Spatial intersecting processes active. Target DB Collection: {collection_name}")
        
        # Grid loop iteration index parsing 
        for idx, cell in grid_gdf.iterrows():
            grid_id = cell.get("grid_id", f"cell_{idx}")
            cell_geom = cell.geometry

            # Spatial boundary filtering via spatial vector intersection indexing mechanisms
            clipped_gdf = master_gdf[master_gdf.geometry.intersects(cell_geom)].copy()
            if clipped_gdf.empty:
                continue

            # Clip the intersecting geometries to the precise dimensions of the grid frame bounds
            clipped_gdf["geometry"] = clipped_gdf.geometry.intersection(cell_geom)
            clipped_gdf = clipped_gdf[~clipped_gdf.geometry.is_empty]

            if clipped_gdf.empty:
                continue

            # 🚀 FIXED INDENTATION BLOCK: Everything inside the loop tracking context is safely aligned
            await status_msg.edit_text(
                f"✂️ Slicing vector assets into matrix fields…\n"
                f"Processing: Grid {grid_id} ({success_count + 1} variants found)",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Export individual slice package arrays out to temporary workspace tracks
            chunk_filename = f"{data_type.lower()}_{grid_id}.geojson"
            chunk_filepath = tmp_dir / chunk_filename
            clipped_gdf.to_file(str(chunk_filepath), driver="GeoJSON")
            
            # MTProto Drive Pipe Upload Action Sequence Interface Dispatch
            logger.info(f"📤 Uploading partition chunk: {chunk_filename} over to Channel ID: {CHANNEL_CHAT_ID}")
            chan_msg = await client.send_document(
                chat_id=CHANNEL_CHAT_ID,
                document=str(chunk_filepath),
                caption=f"📦 SDSS Production Master Part Asset\n• DataType: {data_type}\n• Partition Index: {grid_id}"
            )
            
            # Map generated parameters into clean tracking indexing maps inside MongoDB Atlas
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
            if chunk_filepath.exists():
                chunk_filepath.unlink()

        # Finalize success notifications paths cleanly
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

