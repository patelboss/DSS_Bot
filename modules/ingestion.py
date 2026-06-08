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

from pyrogram import Client
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

    data_type = args.upper()
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

            target_shp = shp_files
            logger.info(f"🎯 Target shapefile found: {target_shp.name}")
            master_gdf = gpd.read_file(target_shp)
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

                gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
                return gdf
            except Exception as severe_err:
                logger.error(f"❌ Severe crash in topology correction matrix for {name}: {severe_err}")
                gdf["geometry"] = gdf.geometry.buffer(0)
                gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
                return gdf

        master_gdf = repair_geometries(master_gdf, "Master Layer Data")
        grid_gdf = repair_geometries(grid_gdf, "Fishnet Grid Framework")

        # -------------------------------------------------------------
        # Spatial join
        # -------------------------------------------------------------
        logger.info("🗺 Executing bulk spatial join overlays to discover intersecting nodes...")
        try:
            joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")
        except Exception as exc:
            logger.warning(f"Spatial join failed ({exc}). Retrying with absolute buffer zeros standardizing steps...")
            master_gdf["geometry"] = master_gdf.geometry.buffer(0)
            grid_gdf["geometry"] = grid_gdf.geometry.buffer(0)
            joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")

        if joined_gdf.empty:
            raise ValueError("Zero overlapping vector elements matched the supplied framework layout.")

        # -------------------------------------------------------------
        # Detect grid field
        # -------------------------------------------------------------
        grid_candidates = ["grid_id", "grid_num", "GRID_ID", "GRID_NUM", "GRID_N", "GRID_NO"]
        grid_id_col = None

        for col in grid_candidates:
            if col in joined_gdf.columns:
                grid_id_col = col
                break

        if not grid_id_col:
            raise ValueError(f"Could not locate an indexing column handle. Keys: {list(joined_gdf.columns)}")

        unique_grids = joined_gdf[grid_id_col].dropna().unique()
        logger.info(f"✅ Mapping intersections linked across {len(unique_grids)} unique grid bounding environments.")

        db = _get_db()
        collection = db["grid_catalog"]

        # -------------------------------------------------------------
        # Slice, compress check, and upload
        # -------------------------------------------------------------
        counter = 0

        for grid_id in unique_grids:
            logger.info(f"👉 Slicing spatial matrix subset for Grid Cell ID: {grid_id}")
            raw_chunk = joined_gdf[joined_gdf[grid_id_col] == grid_id].copy()
            grid_poly = grid_gdf[grid_gdf[grid_id_col] == grid_id].copy()

            try:
                chunk = gpd.clip(raw_chunk, grid_poly)
            except Exception as exc:
                logger.warning(f"Clip operation failed on Cell {grid_id}: {exc}. Triggering topological geometry correction.")
                raw_chunk["geometry"] = raw_chunk.geometry.buffer(0)
                grid_poly["geometry"] = grid_poly.geometry.buffer(0)
                chunk = gpd.clip(raw_chunk, grid_poly)

            if chunk.empty:
                logger.info(f"Cell ID {grid_id} returned zero shapes post clipping operations. Skipping loop entry.")
                continue

            gpkg_filename = f"{data_type}@Grid_{grid_id}.gpkg"
            chunk_out_path = tmp_dir / gpkg_filename

            # Write individual file chunk to local disk
            chunk.to_file(str(chunk_out_path), driver="GPKG")
            raw_filesize = os.path.getsize(chunk_out_path)
            logger.info(f"  └─ File written: {gpkg_filename} | Size: {raw_filesize / (1024*1024):.2f} MB")

            # Setup defaults for routing evaluation
            storage_mode = "telegram_raw"
            final_upload_path = chunk_out_path
            final_filename = gpkg_filename

            # 🚀 MTProto allows up to 2GB uploads natively, but we still compress heavy files 
            # above 48MB into high-efficiency ZIP archives to save bandwidth.
            if raw_filesize > 48 * 1024 * 1024:
                logger.warning(f"  └─ ⚠️ File exceeds stable threshold size. Compressing to high-efficiency ZIP archive...")
                zip_filename = f"{data_type}@Grid_{grid_id}.zip"
                zip_out_path = tmp_dir / zip_filename

                with zipfile.ZipFile(zip_out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(chunk_out_path, arcname=gpkg_filename)
                
                zipped_filesize = os.path.getsize(zip_out_path)
                logger.info(f"  └─ ZIP Done. Compressed size: {zipped_filesize / (1024*1024):.2f} MB")

                storage_mode = "telegram_zipped"
                final_upload_path = zip_out_path
                final_filename = zip_filename
                chunk_out_path.unlink(missing_ok=True)

            # ── 🚀 Pyrogram Send Matrix: Uploads heavy files seamlessly up to 2GB ──
            logger.info(f"  └─ Shipping {final_filename} to Telegram Channel Storage Drive via MTProto client link...")
            channel_msg = await client.send_document(
                chat_id=CHANNEL_CHAT_ID,
                document=str(final_upload_path),
                caption=(
                    f"📦 Grid Reference Asset Layout: {final_filename}\n"
                    f"Type: #{data_type} #Grid_{grid_id} Mode: #{storage_mode}"
                )
            )
            
            # Extract internal message id directly out of Pyrogram's message object model
            logger.info(f"  └─ Channel link accepted transmission. Registered Message ID: {channel_msg.id}")
            
            # Write tracking details to catalog index
            collection.update_one(
                {"_id": f"{data_type}@Grid_{grid_id}"},
                {
                    "$set": {
                        "grid_id": str(grid_id),
                        "data_type": data_type,
                        "storage_mode": storage_mode,
                        "channel_chat_id": CHANNEL_CHAT_ID,
                        "channel_message_id": channel_msg.id, # Pyrogram targets .id instead of .message_id
                        "file_name": final_filename,
                    }
                },
                upsert=True
            )
            final_upload_path.unlink(missing_ok=True)
            counter += 1
            logger.info(f"📈 [SUCCESS] Grid chunk processing index loop locked in -> Count: {counter}")

        logger.info(f"🏆 Ingestion Loop Finalized. Total processed output slices: {counter}")
        await status_msg.edit_text(
            f"✅ *Ingestion Completed Successfully!*\n\n"
            f"📊 *Dataset:* `Master {data_type}`\n"
            f"📡 *Storage Backend:* `Pyrogram MTProto Channel Drive`\n"
            f"📦 *Slices Processed & Cataloged:* `{counter}` Grid Chunks.",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as exc:
        logger.error("💥 CRITICAL PROCESSING CRASH ENCOUNTERED INSIDE PIPELINE LOOP EXECUTION:", exc_info=True)
        await status_msg.edit_text(f"❌ *Master channel-upload pipeline crashed:* {exc}")
    finally:
        logger.info("🧹 Disposing local scratch files and reclaiming container disk spaces...")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.stdout.flush()
        logger.info("✨ Scratch environment fully restored to pristine state.")
            
