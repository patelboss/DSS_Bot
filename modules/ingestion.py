"""
modules/ingestion.py — Admin data ingestion pipeline.
Slices massive master vector datasets using a Supabase grid framework 
and loads serialized data chunks straight into MongoDB automatically.
"""

import logging
import sys
import tempfile
import shutil
from pathlib import Path
import geopandas as gpd

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import cfg
from modules.database import _get_db # Dynamic helper targeting your active Atlas DB
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
    Expects a document attachment (.geojson, .gpkg, or shapefile .zip).
    """
    message = update.message
    
    # 1. Simple Guardrails
    if not message.document:
        await message.reply_text("⚠️ Please run this command by attaching your master vector file.")
        return

    if not context.args:
        await message.reply_text("⚠️ Missing data type variable. Usage: `/upload_master FCM` or `/upload_master FTM`", parse_mode=ParseMode.MARKDOWN)
        return

    data_type = context.args.upper()
    document = message.document
    suffix = Path(document.file_name or "").suffix.lower()

    if suffix not in {".geojson", ".gpkg", ".zip"}:
        await message.reply_text("⚠️ Unsupported master format. Please upload a `.geojson`, `.gpkg`, or shapefile `.zip` archive.")
        return

    status_msg = await message.reply_text(f"⏳ *Initializing pipeline for master {data_type} ingestion…*", parse_mode=ParseMode.MARKDOWN)
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
        
        # Pulls 'state_grid.gpkg' out of your 'raster-layers' bucket
        with open(grid_path, "wb") as f:
            res = supabase.storage.from_(cfg.SUPABASE_BUCKET).download("state_grid.gpkg")
            f.write(res)

        # 4. Ingest datasets into memory GeoDataFrames
        await status_msg.edit_text("🔬 *Executing spatial intersection matrix split…\n(This might take a minute)*", parse_mode=ParseMode.MARKDOWN)
        sys.stdout.flush()
        
        # If it's a zip file package, handle internal path expansion
        if suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(master_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
            shp_files = list(tmp_dir.glob("**/*.shp"))
            if not shp_files:
                raise ValueError("No .shp file found in uploaded archive package.")
            master_gdf = gpd.read_file(str(shp_files))
        else:
            master_gdf = gpd.read_file(str(master_path))

        grid_gdf = gpd.read_file(str(grid_path))

        # Match projections across data streams natively
        if master_gdf.crs != grid_gdf.crs:
            master_gdf = master_gdf.to_crs(grid_gdf.crs)

        # 5. Spatial Join Intersection routing
        joined_gdf = gpd.sjoin(master_gdf, grid_gdf, predicate="intersects")
        
        # Identify column name for grid references (Assumes your grid has a 'grid_id' column)
        grid_id_col = "grid_id" if "grid_id" in joined_gdf.columns else "grid_num"
        unique_grids = joined_gdf[grid_id_col].unique()

        db = _get_db()
        collection = db["grid_vectors"] # Dedicated optimized collection target

        # 6. Loop chunks, serialize to GeoJSON text format, and upsert straight to MongoDB
        counter = 0
        for grid_id in unique_grids:
            chunk = joined_gdf[joined_gdf[grid_id_col] == grid_id]
            
            # Convert clipped sub-grid dataframe cleanly into string-encoded GeoJSON format bytes
            geojson_str = chunk.to_json()
            
            deterministic_key = f"{data_type}@Grid_{grid_id}"
            
            # Upsert document into Atlas cluster: If key exists, update it; otherwise insert it.
            collection.update_one(
                {"_id": deterministic_key},
                {
                    "$set": {
                        "grid_id": int(grid_id) if isinstance(grid_id, (int, float)) else str(grid_id),
                        "data_type": data_type,
                        "geojson_data": geojson_str
                    }
                },
                upsert=True
            )
            counter += 1

        await status_msg.edit_text(
            f"✅ *Ingestion Completed Successfully!*\n\n"
            f"📊 *Dataset:* `Master {data_type}`\n"
            f"📦 *Slices Processed:* `{counter}` Grid Chunks optimized and indexed in MongoDB.",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as exc:
        log.error("Master processing framework failed.", exc_info=True)
        await status_msg.edit_text(f"❌ *Master upload pipeline crashed:* {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.stdout.flush()
      
