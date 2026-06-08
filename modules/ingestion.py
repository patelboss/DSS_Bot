"""
modules/ingestion.py — Admin data ingestion pipeline.
Slices massive master vector datasets using a Supabase grid framework,
uploads the chunk files to a private Telegram channel drive (with ZIP fallback),
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
    Admin Command: /upload_master [DATA_TYPE]
    Reply to a master vector file and the dataset will be sliced by grid,
    uploaded to Telegram storage, and indexed in MongoDB.
    """
    message = update.message

    if not message.reply_to_message or not message.reply_to_message.document:
        log.warning("Ingestion rejected: Command sent without replying to a valid document.")
        await message.reply_text(
            "⚠️ *Usage Instruction:*\n\n"
            "1. Upload your master vector file.\n"
            "2. Reply to that file.\n"
            "3. Send `/upload_master FCM` (or FTM).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not context.args:
        log.warning("Ingestion rejected: Missing DATA_TYPE parameter arg.")
        await message.reply_text(
            "⚠️ Missing data type variable.\n"
            "Usage: Reply with `/upload_master FCM` or `/upload_master FTM`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    data_type = context.args.upper()
    document = message.reply_to_message.document
    suffix = Path(document.file_name or "").suffix.lower()

    log.info(f"🚀 Starting ingestion sequence for Data Type: {data_type} | File: {document.file_name}")

    if suffix not in {".geojson", ".gpkg", ".zip"}:
        log.warning(f"Ingestion rejected: Unsupported file format extension '{suffix}'")
        await message.reply_text(
            "⚠️ Unsupported master format. "
            "Use `.geojson`, `.gpkg`, or shapefile `.zip`.",
        )
        return

    CHANNEL_CHAT_ID = -1003588416077

    status_msg = await message.reply_text(
        f"⏳ *Initializing channel-drive pipeline for master {data_type} ingestion…*",
        parse_mode=ParseMode.MARKDOWN,
    )
    sys.stdout.flush()

    tmp_dir = Path(tempfile.mkdtemp())
    master_path = tmp_dir / document.file_name
    grid_path = tmp_dir / "state_fishnet_grid.gpkg"

    try:
        # -------------------------------------------------------------
        # Download uploaded file
        # -------------------------------------------------------------
        log.info("Downloading master layer from Telegram server to container scratch space...")
        await status_msg.edit_text(
            "📥 *Downloading master vector layer from Telegram updates…*",
            parse_mode=ParseMode.MARKDOWN,
        )

        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(str(master_path))
        log.info(f"Master layer successfully saved locally. Size: {os.path.getsize(master_path)} bytes.")

        # -------------------------------------------------------------
        # Download grid from Supabase
        # -------------------------------------------------------------
        log.info("Requesting 'state_grid.gpkg' frame layout matrix out of Supabase bucket storage...")
        await status_msg.edit_text(
            "🛰 *Streaming Master Fishnet Grid framework from Supabase Storage…*",
            parse_mode=ParseMode.MARKDOWN,
        )

        supabase = _get_supabase()
        with open(grid_path, "wb") as f:
            res = (
                supabase.storage
                .from_(cfg.SUPABASE_BUCKET)
                .download("state_grid.gpkg")
            )
            f.write(res)
        log.info("Master fishnet coordinate matrix safely downloaded and buffered.")

        # -------------------------------------------------------------
        # Read datasets
        # -------------------------------------------------------------
        log.info("Parsing vector layers into spatial dataframes inside memory matrix...")
        await status_msg.edit_text(
            "🔬 *Executing spatial intersection matrix split…*\n"
            "(This might take a minute)",
            parse_mode=ParseMode.MARKDOWN,
        )
        sys.stdout.flush()

        if suffix == ".zip":
            log.info("Decompressing uploaded zip shapefile cluster archive file structural layouts...")
            with zipfile.ZipFile(master_path, "r") as zip_ref:
                zip_ref.extractall(tmp_dir)

            shp_files = [
                shp
                for shp in tmp_dir.glob("**/*.shp")
                if shp.is_file() and not shp.name.startswith("._")
            ]

            if not shp_files:
                log.error("Structural mapping file array check failed: Missing native inside zip wrapper.")
                raise ValueError("No valid .shp file found inside uploaded archive package.")

            target_shp = shp_files
            log.info(f"Targeting active shapefile geometry path resource: {target_shp.name}")
            master_gdf = gpd.read_file(target_shp)
        else:
            master_gdf = gpd.read_file(str(master_path))

        grid_gdf = gpd.read_file(str(grid_path))
        log.info(f"Vectors parsed into memory. Master Layer records: {len(master_gdf)} rows | Grid Framework records: {len(grid_gdf)} cells.")

        # -------------------------------------------------------------
        # CRS harmonization
        # -------------------------------------------------------------
        if master_gdf.crs != grid_gdf.crs:
            log.info(f"CRS mismatch detected. Aligning CRS projections: From '{master_gdf.crs}' to target '{grid_gdf.crs}'...")
            master_gdf = master_gdf.to_crs(grid_gdf.crs)

        # -------------------------------------------------------------
        # Geometry repair helper
        # -------------------------------------------------------------
        def repair_geometries(gdf, name="Dataset"):
            try:
                invalid_count = (~gdf.is_valid).sum()
                if invalid_count:
                    log.warning(f"Repairing {invalid_count} structurally invalid geometries detected inside {name}.")
                    try:
                        gdf["geometry"] = gdf.geometry.make_valid()
                    except Exception as err:
                        log.warning(f"make_valid failed ({err}). Falling back onto buffer zero scaling.")
                        gdf["geometry"] = gdf.geometry.buffer(0)

                gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
                return gdf
            except Exception as severe_err:
                log.error(f"Critical issue in structural repair routine for {name}: {severe_err}")
                gdf["geometry"] = gdf.geometry.buffer(0)
                gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
                return gdf

        master_gdf = repair_geometries(master_gdf, "Master Layer Data")
        grid_gdf = repair_geometries(grid_gdf, "Fishnet Grid Framework")

        if master_gdf.empty:
            raise ValueError("Master dataset contains no valid geometries after cleanup procedures.")
        if grid_gdf.empty:
            raise ValueError("Grid dataset contains no valid geometries after cleanup procedures.")

        # -------------------------------------------------------------
        # Spatial join
        # -------------------------------------------------------------
        log.info("Calculating comprehensive spatial join mapping overlap indexing array rows...")
        try:
            joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")
        except Exception as exc:
            log.warning(f"Native spatial join loop failed. Executing fallback buffer resolution routine: {exc}")
            master_gdf["geometry"] = master_gdf.geometry.buffer(0)
            grid_gdf["geometry"] = grid_gdf.geometry.buffer(0)
            joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")

        if joined_gdf.empty:
            raise ValueError("No matching features intersect the loaded grid coordinate framework array.")

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
            raise ValueError(f"Could not locate a grid identifier column. Structure keys found: {list(joined_gdf.columns)}")

        unique_grids = joined_gdf[grid_id_col].dropna().unique()
        log.info(f"Targeting intersection matrix split processing across {len(unique_grids)} active unique grid spaces.")

        db = _get_db()
        collection = db["grid_catalog"]

        # -------------------------------------------------------------
        # Slice, compress check, and upload
        # -------------------------------------------------------------
        counter = 0

        for grid_id in unique_grids:
            log.info(f"⏱ Slicing chunk boundaries for Grid cell target coordinate ID: {grid_id}")
            raw_chunk = joined_gdf[joined_gdf[grid_id_col] == grid_id].copy()
            grid_poly = grid_gdf[grid_gdf[grid_id_col] == grid_id].copy()

            try:
                chunk = gpd.clip(raw_chunk, grid_poly)
            except Exception as exc:
                log.warning(f"Clip operation execution exception running against grid target {grid_id}: {exc}. Triggering self repair geometry pipeline.")
                raw_chunk["geometry"] = raw_chunk.geometry.buffer(0)
                grid_poly["geometry"] = grid_poly.geometry.buffer(0)
                chunk = gpd.clip(raw_chunk, grid_poly)

            if chunk.empty:
                log.info(f"Grid reference {grid_id} returned empty matrix parameters post clipping step. Skipping step.")
                continue

            gpkg_filename = f"{data_type}@Grid_{grid_id}.gpkg"
            chunk_out_path = tmp_dir / gpkg_filename

            # Save the raw GeoPackage file
            chunk.to_file(str(chunk_out_path), driver="GPKG")
            raw_filesize = os.path.getsize(chunk_out_path)
            log.info(f"Chunk saved. Raw file: {gpkg_filename} | Size: {raw_filesize / (1024*1024):.2f} MB")

            # Initialize variables for storage mode tracking
            storage_mode = "telegram_raw"
            final_upload_path = chunk_out_path
            final_filename = gpkg_filename

            # 🚀 TELEGRAM 50MB BOUNDARY COMPRESSION ENGINE RULE ROUTINE
            if raw_filesize > 48 * 1024 * 1024:
                log.warning(f"⚠️ Raw file limits exceed stable bot metrics threshold. Running fallback ZIP compression pipeline...")
                zip_filename = f"{data_type}@Grid_{grid_id}.zip"
                zip_out_path = tmp_dir / zip_filename

                with zipfile.ZipFile(zip_out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(chunk_out_path, arcname=gpkg_filename)
                
                zipped_filesize = os.path.getsize(zip_out_path)
                log.info(f"ZIP compression complete. Compressed Size: {zipped_filesize / (1024*1024):.2f} MB")

                if zipped_filesize < 49 * 1024 * 1024:
                    storage_mode = "telegram_zipped"
                    final_upload_path = zip_out_path
                    final_filename = zip_filename
                    chunk_out_path.unlink(missing_ok=True) # Clear out uncompressed file
                else:
                    # 🚀 DATABASE STRING SPLIT FALLBACK (IF FILE IS HUGE SAVED EVEN POST COMPRESSION EXTRACTIONS)
                    log.error("🚨 CRITICAL: Compressed file sizes still break the 50MB bot upload limit! Routing directly onto MongoDB String Text Collection pipeline...")
                    storage_mode = "mongodb_geojson_chunks"

            # ── Execute Upload Route Dependent on Sizing Matrix Results ──
            if storage_mode in ("telegram_raw", "telegram_zipped"):
                log.info(f"Shipping asset file handle via send_document endpoint streams onto channel: {CHANNEL_CHAT_ID}...")
                with open(final_upload_path, "rb") as fp:
                    channel_msg = await context.bot.send_document(
                        chat_id=CHANNEL_CHAT_ID,
                        document=fp,
                        caption=(
                            f"📦 Grid Reference Asset Layout: {final_filename}\n"
                            f"Type: #{data_type} #Grid_{grid_id} Mode: #{storage_mode}"
                        ),
                    )
                
                log.info(f"Telegram channel link accepted transmission. Document catalog registered at Message ID: {channel_msg.message_id}")
                
                # Index parameters update inside Cluster
                collection.update_one(
                    {"_id": f"{data_type}@Grid_{grid_id}"},
                    {
                        "$set": {
                            "grid_id": str(grid_id),
                            "data_type": data_type,
                            "storage_mode": storage_mode,
                            "channel_chat_id": CHANNEL_CHAT_ID,
                            "channel_message_id": channel_msg.message_id,
                            "file_name": final_filename,
                        }
                    },
                    upsert=True
                )
                final_upload_path.unlink(missing_ok=True)

            elif storage_mode == "mongodb_geojson_chunks":
                log.info("Serializing localized polygon matrices objects directly down into string elements mapping arrays...")
                geojson_str = chunk.to_json()
                
                # Split string payload text into stable 12MB segments to clear MongoDB's 16MB document cap limits safely
                chunk_packet_size = 12 * 1024 * 1024 
                string_text_arrays = [geojson_str[i:i+chunk_packet_size] for i in range(0, len(geojson_str), chunk_packet_size)]
                log.info(f"Vector text successfully diced across {len(string_text_arrays)} linked data packets array blocks.")

                collection.update_one(
                    {"_id": f"{data_type}@Grid_{grid_id}"},
                    {
                        "$set": {
                            "grid_id": str(grid_id),
                            "data_type": data_type,
                            "storage_mode": storage_mode,
                            "geojson_chunks": string_text_arrays,
                            "chunk_count": len(string_text_arrays)
                        }
                    },
                    upsert=True
                )
                log.info("Database matrix records writing locked in cleanly.")
                chunk_out_path.unlink(missing_ok=True)
                if 'zip_out_path' in locals():
                    zip_out_path.unlink(missing_ok=True)

            counter += 1
            log.info(f"✅ Target block successfully processed: Row item reference index sequential counter -> [{counter}]")

        log.info(f"Pipeline complete. All {counter} unique overlapping tiles successfully split and parsed.")
        await status_msg.edit_text(
            f"✅ *Ingestion Completed Successfully!*\n\n"
            f"📊 *Dataset:* `Master {data_type}`\n"
            f"📡 *Storage Backend:* `Hybrid Managed Environment`\n"
            f"📦 *Slices Processed & Cataloged:* `{counter}` Grid Chunks.",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:
        log.error("💥 CRITICAL PROCESSING CRASH ENCOUNTERED: Pipeline loop interrupted.", exc_info=True)
        await status_msg.edit_text(f"❌ *Master channel-upload pipeline crashed:* {exc}")
    finally:
        log.info("Cleaning ephemeral cache path records and clearing local node scratch spaces...")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.stdout.flush()
        log.info("🧹 Ephemeral container scratch workspace fully restored to pristine state.")
        
