"""
modules/ingestion.py — Production-Grade Admin Data Ingestion Pipeline.
Capped for strict 512MB RAM constraints using pre-projected spatial arrays,
libc malloc_trim memory reclamation, and compressed GeoPackage batching.
"""

import logging
import sys
import tempfile
import os
import shutil
import zipfile
import asyncio
import gc
import ctypes
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
from modules.database import _get_db
from modules.storage import _get_supabase


logger = logging.getLogger("main.ingestion")
logger.setLevel(logging.INFO)

if not logger.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)


def mem_mb() -> float:
    """
    Returns the current Resident Set Size (RSS) memory
    of the active Linux container process in MB.
    """
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb_val = int(line.split()[1])
                    logger.info(line.strip())
                    return kb_val / 1024.0
    except Exception as e:
        logger.exception("mem_mb code exception: %s", e)

    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def log_mem(stage: str) -> None:
    logger.info("🧠 MEMORY [%s] RSS=%.1f MB", stage, mem_mb())


# ── OPTIMIZATION 1: Native Memory Trim Handler ────────────────────────────────
def release_memory() -> None:
    """Forces Python garbage collection and clears glibc memory arenas."""
    try:
        before = mem_mb()
    except Exception:
        before = 0.0

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

    try:
        after = mem_mb()
    except Exception:
        after = 0.0

    logger.info("🧹 MEMORY RECLAIM | before=%.1f MB | after=%.1f MB", before, after)


def _parse_limit_offset(args: list[str]) -> tuple[int | None, int]:
    """
    Supports:
      /upload_master FCM
      /upload_master FCM 50 100
      /upload_master FCM --limit 50 --offset 100
      /upload_master FCM --offset 100 --limit 50
    """
    limit = None
    offset = 0

    tokens = args[2:]
    positional: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        if tok in {"--limit", "-l"}:
            if i + 1 >= len(tokens):
                raise ValueError("Missing value after --limit.")
            limit = int(tokens[i + 1])
            i += 2
            continue
        if tok in {"--offset", "-o"}:
            if i + 1 >= len(tokens):
                raise ValueError("Missing value after --offset.")
            offset = int(tokens[i + 1])
            i += 2
            continue
        positional.append(tok)
        i += 1

    if positional:
        if limit is None and len(positional) >= 1:
            limit = int(positional[0])
        if len(positional) >= 2:
            offset = int(positional[1])

    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than 0.")
    if offset < 0:
        raise ValueError("offset must be 0 or greater.")

    return limit, offset


@Client.on_message(filters.command("upload_master") & filters.private)
async def cmd_upload_master(client: Client, message: Message) -> None:
    """
    Admin Command: /upload_master [DATA_TYPE] [LIMIT] [OFFSET]
    or: /upload_master [DATA_TYPE] --limit N --offset M

    Slices vector layers by grid framework with strict batch-clearing resets.
    LIMIT and OFFSET let you resume/backup ingestion from a selected grid window.
    """
    if not message.reply_to_message or not message.reply_to_message.document:
        logger.warning("❌ Ingestion Rejected: Command executed without replying to a valid file document.")
        await message.reply_text(
            "⚠️ *Usage Instruction:*\n\n"
            "1. Upload your master vector file.\n"
            "2. Reply to that file.\n"
            "3. Send `/upload_master FCM` (or FTM, DEM).\n\n"
            "Optional backup/resume window:\n"
            "`/upload_master FCM --limit 50 --offset 100`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg_text = message.text or ""
    args = msg_text.split()
    if len(args) < 2:
        logger.warning("❌ Ingestion Rejected: Missing DATA_TYPE parameter argument.")
        await message.reply_text(
            "⚠️ Missing data type variable.\n"
            "Usage: Reply with `/upload_master FCM`, `FTM`, or `DEM`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    data_type = args[1].strip().upper()
    if data_type not in {"FCM", "FTM", "DEM"}:
        await message.reply_text(
            "⚠️ Invalid DATA_TYPE.\nUsage: `/upload_master FCM`, `FTM`, or `DEM`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        limit, offset = _parse_limit_offset(args)
    except Exception as parse_err:
        await message.reply_text(
            f"⚠️ Invalid limit/offset values.\n\n`{parse_err}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    document = message.reply_to_message.document
    safe_filename = Path(document.file_name or "master_layer.gpkg").name
    suffix = Path(safe_filename).suffix.lower()

    logger.info("==========================================================================")
    logger.info("🚀 INGESTION TRIGGERED | Type: %s | Target Asset Name: %s", data_type, safe_filename)
    logger.info("📦 Pyrogram Document File ID Pointer: %s", document.file_id)
    logger.info("🧭 Window Settings | limit=%s | offset=%s", str(limit), offset)
    logger.info("==========================================================================")

    if suffix not in {".geojson", ".gpkg", ".zip"}:
        logger.warning("❌ Ingestion Rejected: File extension '%s' is unsupported.", suffix)
        await message.reply_text(
            "⚠️ Unsupported master format. Use `.geojson`, `.gpkg`, or shapefile `.zip`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        raw_channel_id = str(cfg.TELEGRAM_CHANNEL_ID).strip()
        CHANNEL_CHAT_ID = int(raw_channel_id)
    except ValueError:
        CHANNEL_CHAT_ID = raw_channel_id

    status_msg = await message.reply_text(
        f"⏳ *Initializing MTProto channel-drive pipeline for master {data_type} ingestion…*",
        parse_mode=ParseMode.MARKDOWN,
    )
    sys.stdout.flush()

    tmp_dir = None

    try:
        tmp_dir = Path(tempfile.mkdtemp())
        master_path = tmp_dir / safe_filename
        grid_path = tmp_dir / "state_fishnet_grid.gpkg"

        # Download Master Vector Dataset
        logger.info("📥 Downloading raw file asset '%s' via Pyrogram client...", safe_filename)
        await status_msg.edit_text(
            f"📥 *Downloading master {data_type} vector layer from Telegram updates…*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await client.download_media(message.reply_to_message, file_name=str(master_path))
        log_mem("MASTER FILE DOWNLOADED")
        logger.info(
            "💾 Local download locked in. Path: %s | Size: %.2f MB",
            master_path,
            os.path.getsize(master_path) / (1024 * 1024),
        )

        # Download Framework Grid
        logger.info("🛰 Fetching structural grid 'state_grid.gpkg' from Supabase Bucket...")
        await status_msg.edit_text(
            "🛰 *Streaming Master Fishnet Grid framework from Supabase Storage…*",
            parse_mode=ParseMode.MARKDOWN,
        )
        supabase = _get_supabase()
        with open(grid_path, "wb") as f:
            res = supabase.storage.from_(cfg.SUPABASE_BUCKET).download("state_grid.gpkg")
            f.write(res)
        logger.info(
            "📐 Framework grid downloaded. Path: %s | Size: %.2f MB",
            grid_path,
            os.path.getsize(grid_path) / (1024 * 1024),
        )

        # Load only required grid columns
        grid_gdf = gpd.read_file(str(grid_path))
        required_cols = ["TopoSheet_No", "geometry"]
        grid_gdf = grid_gdf[required_cols].copy()
        logger.info("📊 Grid Records initialized: %d mapping cells available.", len(grid_gdf))

        final_master_source = master_path
        if suffix == ".zip":
            logger.info("🗜 Expanding zipped shapefile archive contents...")
            with zipfile.ZipFile(master_path, "r") as zip_ref:
                zip_ref.extractall(tmp_dir)

            shp_files = [
                s for s in tmp_dir.glob("**/*.shp")
                if s.is_file() and not s.name.startswith("._")
            ]
            if not shp_files:
                raise ValueError("No valid .shp file found inside uploaded archive package.")
            if len(shp_files) > 1:
                logger.warning("Multiple shapefiles found in archive. Using first: %s", shp_files)

            final_master_source = shp_files[0]

        with fiona.open(str(final_master_source)) as src:
            master_crs = src.crs_wkt or src.crs

        if not master_crs:
            raise ValueError("Master dataset does not contain a valid CRS definition.")

        # Pre-project the full grid frame ONCE
        logger.info("📐 Computing uniform matrix coordinate transformations...")
        grid_master_crs = grid_gdf.to_crs(master_crs)

        # Apply backup/resume window here
        total_grid_cells = len(grid_gdf)
        start_idx = min(offset, total_grid_cells)
        end_idx = total_grid_cells if limit is None else min(start_idx + limit, total_grid_cells)

        if start_idx >= total_grid_cells:
            raise ValueError(
                f"Offset {offset} is beyond available grid cells ({total_grid_cells})."
            )

        grid_gdf = grid_gdf.iloc[start_idx:end_idx].reset_index(drop=True)
        grid_master_crs = grid_master_crs.iloc[start_idx:end_idx].reset_index(drop=True)

        logger.info(
            "🧭 Processing window applied | total=%d | start=%d | end=%d | selected=%d",
            total_grid_cells,
            start_idx,
            end_idx,
            len(grid_gdf),
        )

        db = _get_db()
        collection_map = {"FCM": "fcm_layers", "FTM": "ftm_layers", "DEM": "dem_layers"}
        collection_name = collection_map[data_type]
        col = db[collection_name]

        success_count = 0
        logger.info("✂️ Spatial streaming processes active. Target DB Collection: %s", collection_name)

        with fiona.open(str(final_master_source)) as source_stream:
            for idx, cell in grid_gdf.iterrows():
                grid_id = str(cell["TopoSheet_No"])
                cell_geom = cell.geometry

                cell_bbox = tuple(grid_master_crs.iloc[idx].geometry.bounds)

                features_in_box = list(source_stream.filter(bbox=cell_bbox))
                if not features_in_box:
                    continue

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
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

                # Switch Chunk Storage to GeoPackage
                chunk_filename = f"{data_type.lower()}_{grid_id}.gpkg"
                chunk_filepath = tmp_dir / chunk_filename
                clipped_gdf.to_file(str(chunk_filepath), driver="GPKG")

                # Prevent dangerous upload configurations
                file_bytes_size = chunk_filepath.stat().st_size
                if (file_bytes_size / (1024 * 1024)) > 250:
                    logger.warning(
                        "⚠️ Chunk skipped: Segment `%s` (%.1f MB) exceeds safety threshold.",
                        grid_id,
                        file_bytes_size / (1024 * 1024),
                    )
                    chunk_filepath.unlink(missing_ok=True)
                    continue

                chan_msg = None
                while not chan_msg:
                    try:
                        logger.info("📤 Uploading partition chunk: %s over to Channel ID: %s", chunk_filename, CHANNEL_CHAT_ID)
                        chan_msg = await client.send_document(
                            chat_id=CHANNEL_CHAT_ID,
                            document=str(chunk_filepath),
                            caption=(
                                f"📦 SDSS Production Master Part Asset\n"
                                f"• DataType: {data_type}\n"
                                f"• Partition Index: {grid_id}"
                            ),
                        )
                    except FloodWait as flood_exception:
                        logger.warning(
                            "⚠️ Telegram FloodWait triggered! Cooling down processing loops for %s seconds.",
                            flood_exception.value,
                        )
                        await asyncio.sleep(flood_exception.value)
                    except Exception as upload_err:
                        logger.error("❌ Aborting channel pipe send action sequence: %s", upload_err)
                        raise upload_err

                payload = {
                    "grid_id": grid_id,
                    "data_type": data_type,
                    "channel_chat_id": str(CHANNEL_CHAT_ID),
                    "channel_message_id": chan_msg.id,
                    "file_id": chan_msg.document.file_id,
                    "file_name": chunk_filename,
                    "feature_count": len(clipped_gdf),
                    "updated_at": datetime.now(timezone.utc),
                }
                col.update_one(
                    {"grid_id": grid_id, "data_type": data_type},
                    {"$set": payload},
                    upsert=True,
                )

                success_count += 1
                chunk_filepath.unlink(missing_ok=True)

                # Aggressive RAM Cleanup
                del clipped_gdf
                del features_in_box
                del cell_geom
                release_memory()

                # Reduce batch rejuvenation size
                if success_count % 25 == 0:
                    logger.info(
                        "🧹 Batch target reached (%d uploads). Pausing for glibc memory stabilization...",
                        success_count,
                    )
                    try:
                        await status_msg.edit_text(
                            f"🧹 *Batch milestone reached (`{success_count}` parts)!*\n\n"
                            f"⏸️ Freezing ingestion pipeline to stabilize system allocation maps...",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

                    release_memory()
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(0.01)

        # Release Grid Reprojections on Completion
        del grid_gdf
        del grid_master_crs
        release_memory()

        await status_msg.delete()
        await message.reply_text(
            f"✅ *Master Ingestion Pipeline Completed Successfully!*\n\n"
            f"🏷 *Data Type Index:* `{data_type}`\n"
            f"🧩 *Total Structural Part Segments Formed:* `{success_count}` chunks\n"
            f"🧭 *Window:* `offset={offset}` `limit={limit if limit is not None else 'ALL'}`\n"
            f"🗂 *Target Registry Store:* MongoDB Cluster `[{collection_name}]`",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as pipeline_err:
        logger.error("A critical execution error derailed data ingestion pipeline.", exc_info=True)
        if "status_msg" in locals():
            await status_msg.edit_text(f"❌ Master Ingestion Pipeline Crashed: {pipeline_err}")
    finally:
        try:
            if tmp_dir and tmp_dir.exists():
                shutil.rmtree(tmp_dir)
        except Exception:
            logger.exception("Failed to remove temporary directory workspace structures.")
        release_memory()
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

    logger.info("📣 Manual broadcast triggered. Destination target peer index: %s", CHANNEL_CHAT_ID)

    message_text_raw = message.text or ""
    parts = message_text_raw.split(maxsplit=1)
    text_content = parts[1] if len(parts) > 1 else ""

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
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await message.reply_text(
            f"🚀 *Broadcast dispatched safely to target chat peer:* `{CHANNEL_CHAT_ID}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as broadcast_err:
        logger.error("Diagnostic broadcast execution derailed.", exc_info=True)
        await message.reply_text(f"❌ *Broadcast delivery failed:* `{broadcast_err}`", parse_mode=ParseMode.MARKDOWN)
