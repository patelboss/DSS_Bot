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
    Admin Command: /upload_master [DATA_TYPE]
    Reply to a master vector file and the dataset will be sliced by grid,
    uploaded to Telegram storage, and indexed in MongoDB.
    """
    message = update.message

    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply_text(
            "⚠️ *Usage Instruction:*\n\n"
            "1. Upload your master vector file.\n"
            "2. Reply to that file.\n"
            "3. Send `/upload_master FCM` (or FTM).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not context.args:
        await message.reply_text(
            "⚠️ Missing data type variable.\n"
            "Usage: Reply with `/upload_master FCM` or `/upload_master FTM`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    data_type = context.args.upper()
    document = message.reply_to_message.document
    suffix = Path(document.file_name or "").suffix.lower()

    if suffix not in {".geojson", ".gpkg", ".zip"}:
        await message.reply_text(
            "⚠️ Unsupported master format. "
            "Use `.geojson`, `.gpkg`, or shapefile `.zip`.",
        )
        return

    CHANNEL_CHAT_ID = -100358841607

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
        await status_msg.edit_text(
            "📥 *Downloading master vector layer from Telegram updates…*",
            parse_mode=ParseMode.MARKDOWN,
        )

        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(str(master_path))

        # -------------------------------------------------------------
        # Download grid from Supabase
        # -------------------------------------------------------------
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

        # -------------------------------------------------------------
        # Read datasets
        # -------------------------------------------------------------
        await status_msg.edit_text(
            "🔬 *Executing spatial intersection matrix split…*\n"
            "(This might take a minute)",
            parse_mode=ParseMode.MARKDOWN,
        )
        sys.stdout.flush()

        if suffix == ".zip":
            with zipfile.ZipFile(master_path, "r") as zip_ref:
                zip_ref.extractall(tmp_dir)

            shp_files = [
                shp
                for shp in tmp_dir.glob("**/*.shp")
                if shp.is_file() and not shp.name.startswith("._")
            ]

            if not shp_files:
                raise ValueError(
                    "No valid .shp file found inside uploaded archive package."
                )

            target_shp = shp_files
            log.info(f"Using extracted shapefile: {target_shp.name}")
            sys.stdout.flush()

            master_gdf = gpd.read_file(target_shp)
        else:
            master_gdf = gpd.read_file(str(master_path))

        grid_gdf = gpd.read_file(str(grid_path))

        # -------------------------------------------------------------
        # CRS harmonization
        # -------------------------------------------------------------
        if master_gdf.crs != grid_gdf.crs:
            master_gdf = master_gdf.to_crs(grid_gdf.crs)

        # -------------------------------------------------------------
        # Geometry repair helper
        # -------------------------------------------------------------
        def repair_geometries(gdf):
            try:
                invalid_count = (~gdf.is_valid).sum()
                if invalid_count:
                    log.warning(f"Repairing {invalid_count} invalid geometries.")
                    try:
                        gdf["geometry"] = gdf.geometry.make_valid()
                    except Exception:
                        gdf["geometry"] = gdf.geometry.buffer(0)

                gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
                return gdf
            except Exception:
                gdf["geometry"] = gdf.geometry.buffer(0)
                gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
                return gdf

        master_gdf = repair_geometries(master_gdf)
        grid_gdf = repair_geometries(grid_gdf)

        if master_gdf.empty:
            raise ValueError("Master dataset contains no valid geometries.")
        if grid_gdf.empty:
            raise ValueError("Grid dataset contains no valid geometries.")

        # -------------------------------------------------------------
        # Spatial join
        # -------------------------------------------------------------
        try:
            joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")
        except Exception as exc:
            log.warning(f"Spatial join failed. Retrying with repaired geometries: {exc}")
            master_gdf["geometry"] = master_gdf.geometry.buffer(0)
            grid_gdf["geometry"] = grid_gdf.geometry.buffer(0)
            joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")

        if joined_gdf.empty:
            raise ValueError("No features intersect the supplied grid.")

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
            raise ValueError(
                f"Could not locate a grid identifier column. "
                f"Available columns: {list(joined_gdf.columns)}"
            )

        unique_grids = joined_gdf[grid_id_col].dropna().unique()
        db = _get_db()
        collection = db["grid_catalog"]

        # -------------------------------------------------------------
        # Slice and upload
        # -------------------------------------------------------------
        counter = 0

        for grid_id in unique_grids:
            raw_chunk = joined_gdf[joined_gdf[grid_id_col] == grid_id].copy()
            grid_poly = grid_gdf[grid_gdf[grid_id_col] == grid_id].copy()

            try:
                chunk = gpd.clip(raw_chunk, grid_poly)
            except Exception as exc:
                log.warning(f"Clip failed for grid {grid_id}: {exc}")
                raw_chunk["geometry"] = raw_chunk.geometry.buffer(0)
                grid_poly["geometry"] = grid_poly.geometry.buffer(0)
                chunk = gpd.clip(raw_chunk, grid_poly)

            if chunk.empty:
                continue

            filename = f"{data_type}@Grid_{grid_id}.gpkg"
            chunk_out_path = tmp_dir / filename

            chunk.to_file(str(chunk_out_path), driver="GPKG")

            with open(chunk_out_path, "rb") as fp:
                channel_msg = await context.bot.send_document(
                    chat_id=CHANNEL_CHAT_ID,
                    document=fp,
                    caption=(
                        f"📦 Grid Reference Asset: {filename}\n"
                        f"Type: #{data_type} #Grid_{grid_id}"
                    ),
                )

            collection.update_one(
                {"_id": f"{data_type}@Grid_{grid_id}"},
                {
                    "$set": {
                        "grid_id": str(grid_id),
                        "data_type": data_type,
                        "channel_chat_id": CHANNEL_CHAT_ID,
                        "channel_message_id": channel_msg.message_id,
                        "file_name": filename,
                    }
                },
                upsert=True,
            )
            counter += 1

        await status_msg.edit_text(
            f"✅ *Ingestion Completed Successfully!*\n\n"
            f"📊 *Dataset:* `Master {data_type}`\n"
            f"📡 *Storage Backend:* `Telegram Channel Drive`\n"
            f"📦 *Slices Uploaded & Cataloged:* `{counter}` Grid Packages.",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:
        log.error("Master processing channel pipeline failed.", exc_info=True)
        await status_msg.edit_text(f"❌ *Master channel-upload pipeline crashed:* {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.stdout.flush()
        
