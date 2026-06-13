"""
modules/cmd.py — Command handlers, callback routing, and resource-conscious analysis orchestrator.
Dynamically streams intersected grid data directly via cached Telegram channel file_ids,
runs localized vector analytics within tight 512MB RAM constraints, and flushes cache nodes instantly.
"""

import io
import logging
import sys
import asyncio
import gc
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode, ChatAction

from config import cfg
from modules.database import _get_db, upsert_user, log_analysis
from modules.spatial_analysis import load_vector_file
from modules.map_renderer import render_map

logger = logging.getLogger("main.commands")
logger.setLevel(logging.INFO)

if not logger.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(stdout_handler)

# Runtime cache replacing python-telegram-bot's user_data context
USER_SESSION_CACHE = {}


def _align_gdf_crs(gdf: gpd.GeoDataFrame, target_crs) -> gpd.GeoDataFrame:
    """Return GeoDataFrame aligned to target CRS without mutating caller state."""
    if gdf.crs is None:
        return gdf.set_crs(target_crs)
    if gdf.crs != target_crs:
        return gdf.to_crs(target_crs)
    return gdf


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
        "_Upload your file to begin_ 👆"
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
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await message.reply_text(
        "📥 *Processing vector layout properties…*",
        parse_mode=ParseMode.MARKDOWN,
    )
    sys.stdout.flush()

    tmp_path = Path(tempfile.gettempdir()) / f"{user.id}_{document.file_name}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        await client.download_media(message, file_name=str(tmp_path))

        geojson_feature, gdf_attributes = load_vector_file(tmp_path)
        attr_df = gdf_attributes.drop(columns=["geometry"], errors="ignore")

        # Store both for backward compatibility and the new per-polygon workflow.
        USER_SESSION_CACHE[user.id] = {
            "current_feature": geojson_feature,
            "current_gdf": gdf_attributes,
            "filename": document.file_name,
            "cached_df_dict": attr_df.to_dict(orient="records"),
        }

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📋 View Attributes Table", callback_data="action_attributes")],
                [InlineKeyboardButton("🔬 Run Spatial DSS Analysis", callback_data="action_analysis")],
            ]
        )

        await status_msg.delete()
        await message.reply_text(
            f"✅ *Layer Ingested Successfully!*\n\n"
            f"📁 *File:* `{document.file_name}`\n"
            f"📦 *Total Features:* `{len(gdf_attributes)}` Polygons/Parts\n\n"
            f"Select processing task:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
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

    user_session = USER_SESSION_CACHE.get(user.id, {})
    uploaded_gdf = user_session.get("current_gdf")
    if uploaded_gdf is None and user_session.get("current_feature") is not None:
        uploaded_gdf = gpd.GeoDataFrame.from_features([user_session["current_feature"]])

    cached_records = user_session.get("cached_df_dict")
    filename = user_session.get("filename", "layer")

    if uploaded_gdf is None or cached_records is None:
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
        csv_bytes = csv_buffer.getvalue().encode("utf-8")
        csv_buffer.close()

        await callback_query.message.reply_text(text_preview, parse_mode=ParseMode.MARKDOWN)

        bio = io.BytesIO(csv_bytes)
        bio.name = f"{base_name}_attributes.csv"
        await callback_query.message.reply_document(
            document=bio,
            caption=f"📄 Full attribute table export for `{filename}`.",
        )

    elif action == "action_analysis":
        status_msg = await callback_query.message.reply_text(
            "⏳ *Initializing Real-Time Channel-Drive Vector Analysis Engines…*",
            parse_mode=ParseMode.MARKDOWN,
        )

        tmp_workspace = Path(tempfile.mkdtemp())
        grid_local_path = tmp_workspace / "state_fishnet_grid.gpkg"

        try:
            await client.send_chat_action(chat_id=user.id, action=ChatAction.TYPING)
            db = _get_db()

            # Work with all uploaded features individually.
            uploaded_gdf = uploaded_gdf.copy()
            if uploaded_gdf.crs is None:
                uploaded_gdf.set_crs("EPSG:4326", inplace=True)

            uploaded_gdf = uploaded_gdf.explode(index_parts=False).reset_index(drop=True)

            if uploaded_gdf.empty:
                await status_msg.edit_text(
                    "⚠️ *Analysis Completed:* The uploaded file contains no valid polygon features."
                )
                return

            # Download the framework grid once and reuse it for every polygon.
            await status_msg.edit_text("🛰 *Aligning layout against Spatial Mesh Framework Grid…*")
            from modules.storage import _get_supabase
            supabase = _get_supabase()
            with open(grid_local_path, "wb") as f:
                res = supabase.storage.from_(cfg.SUPABASE_BUCKET).download("state_grid.gpkg")
                f.write(res)

            grid_gdf = gpd.read_file(str(grid_local_path))
            grid_gdf = _align_gdf_crs(grid_gdf, uploaded_gdf.crs)

            polygon_total = len(uploaded_gdf)
            processed_any = False

            for poly_index, row in uploaded_gdf.iterrows():
                polygon_number = poly_index + 1

                try:
                    user_geom = row.geometry
                    if user_geom is None or user_geom.is_empty:
                        continue

                    single_gdf = gpd.GeoDataFrame(
                        [row.to_dict()],
                        geometry="geometry",
                        crs=uploaded_gdf.crs,
                    )

                    user_utm = single_gdf.to_crs(epsg=32644)
                    calculated_area_ha = float(user_utm.geometry.area.sum() / 10000.0)

                    # Render in WGS84 for map display, regardless of source CRS.
                    render_gdf = _align_gdf_crs(single_gdf.copy(), "EPSG:4326")
                    render_geom = render_gdf.geometry.iloc[0]

                    single_feature = {
                        "type": "Feature",
                        "properties": {
                            "polygon_id": polygon_number,
                            "source_file": filename,
                        },
                        "geometry": mapping(render_geom),
                    }

                    await status_msg.edit_text(
                        f"📍 *Processing polygon `{polygon_number}/{polygon_total}`…*"
                    )

                    intersecting_cells = grid_gdf[grid_gdf.geometry.intersects(user_geom)]
                    target_grid_ids = (
                        intersecting_cells["TopoSheet_No"]
                        .dropna()
                        .astype(str)
                        .unique()
                        .tolist()
                    )

                    if not target_grid_ids:
                        await callback_query.message.reply_text(
                            f"⚠️ *Polygon `{polygon_number}/{polygon_total}`:* "
                            "The uploaded boundary does not intersect the study framework grid area.",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        continue

                    await status_msg.edit_text(
                        f"📍 *Polygon `{polygon_number}/{polygon_total}`* "
                        f"intersects `{len(target_grid_ids)}` framework grid indices…"
                    )

                    fcm_intersected_gdfs = []
                    ftm_intersected_gdfs = []
                    dem_intersected_gdfs = []

                    for grid_id in target_grid_ids:
                        # ── TASK LAYER 1: FCM ──
                        fcm_doc = db.fcm_layers.find_one({"grid_id": grid_id, "data_type": "FCM"})
                        if fcm_doc:
                            fcm_local = tmp_workspace / f"fcm_{grid_id}.gpkg"
                            await client.download_media(fcm_doc["file_id"], file_name=str(fcm_local))
                            part_gdf = gpd.read_file(str(fcm_local))
                            part_gdf = _align_gdf_crs(part_gdf, uploaded_gdf.crs)
                            clip_part = part_gdf[part_gdf.geometry.intersects(user_geom)].copy()
                            if not clip_part.empty:
                                fcm_intersected_gdfs.append(clip_part)
                            fcm_local.unlink(missing_ok=True)

                        # ── TASK LAYER 2: FTM ──
                        ftm_doc = db.ftm_layers.find_one({"grid_id": grid_id, "data_type": "FTM"})
                        if ftm_doc:
                            ftm_local = tmp_workspace / f"ftm_{grid_id}.gpkg"
                            await client.download_media(ftm_doc["file_id"], file_name=str(ftm_local))
                            part_gdf = gpd.read_file(str(ftm_local))
                            part_gdf = _align_gdf_crs(part_gdf, uploaded_gdf.crs)
                            clip_part = part_gdf[part_gdf.geometry.intersects(user_geom)].copy()
                            if not clip_part.empty:
                                ftm_intersected_gdfs.append(clip_part)
                            ftm_local.unlink(missing_ok=True)

                        # ── TASK LAYER 3: DEM ──
                        dem_doc = db.dem_layers.find_one({"grid_id": grid_id, "data_type": "DEM"})
                        if dem_doc:
                            dem_local = tmp_workspace / f"dem_{grid_id}.gpkg"
                            await client.download_media(dem_doc["file_id"], file_name=str(dem_local))
                            part_gdf = gpd.read_file(str(dem_local))
                            part_gdf = _align_gdf_crs(part_gdf, uploaded_gdf.crs)
                            clip_part = part_gdf[part_gdf.geometry.intersects(user_geom)].copy()
                            if not clip_part.empty:
                                dem_intersected_gdfs.append(clip_part)
                            dem_local.unlink(missing_ok=True)

                        gc.collect()

                    report_text = (
                        f"🔬 *Conditional Spatial Analysis Report for `{filename}`*\n"
                        f"🧩 *Polygon:* `{polygon_number}/{polygon_total}`\n"
                        f"📅 *Generated on:* `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                        f"📊 *Total Boundary Area:* `{calculated_area_ha:.2f} ha`\n\n"
                    )

                    # ── FCM ANALYSIS BLOCK ──
                    fcm_class_summary = {}
                    dominant_cover_type = "NON FOREST"
                    passed_fcm_list = []

                    if fcm_intersected_gdfs:
                        fcm_compiled = pd.concat(fcm_intersected_gdfs, ignore_index=True)

                        logger.info("FCM Columns = %s", fcm_compiled.columns.tolist())
                        logger.info("FCM Sample = %s", fcm_compiled.head(3).to_dict("records"))

                        fcm_compiled["geometry"] = fcm_compiled.geometry.intersection(user_geom)
                        fcm_compiled = fcm_compiled[~fcm_compiled.geometry.is_empty].copy()

                        fcm_utm = fcm_compiled.to_crs(epsg=32644)

                        class_col = next(
                            (c for c in fcm_utm.columns if c.lower() == "class_name"),
                            None,
                        )

                        if class_col and not fcm_utm.empty:
                            fcm_utm["part_area_ha"] = fcm_utm.geometry.area / 10000.0
                            grouped = fcm_utm.groupby(class_col)["part_area_ha"].sum()

                            max_area = 0.0

                            for raw_class, class_ha in grouped.items():
                                c_str = str(raw_class).strip().upper()

                                if "VDF" in c_str:
                                    standard_label = "VDF"
                                elif "MDF" in c_str:
                                    standard_label = "MDF"
                                elif "OPEN FOREST" in c_str:
                                    standard_label = "OPEN FOREST"
                                elif "NON FOREST" in c_str:
                                    standard_label = "NON FOREST"
                                elif "SCRUB" in c_str:
                                    standard_label = "SCRUB"
                                elif "WATER" in c_str:
                                    standard_label = "WATER"
                                else:
                                    standard_label = "NO-DATA"

                                c_pct = (class_ha / calculated_area_ha) * 100.0 if calculated_area_ha else 0.0
                                fcm_class_summary[standard_label] = {
                                    "hectares": float(class_ha),
                                    "percentage": float(c_pct),
                                }

                                if standard_label not in {"WATER", "NO-DATA"} and class_ha > max_area:
                                    max_area = class_ha
                                    dominant_cover_type = standard_label

                        report_text += "🌲 *Forest Canopy Cover (FCM):*\n"
                        if fcm_class_summary:
                            for label, metrics in fcm_class_summary.items():
                                report_text += (
                                    f"• {label}: `{metrics['hectares']:.2f} ha` "
                                    f"({metrics['percentage']:.1f}%)\n"
                                )
                            report_text += "• Processing Status: `[Natively Evaluated]` ✅\n\n"
                        else:
                            report_text += (
                                "• Processing Status: `[Evaluated - class_name column not usable]` ⚠️\n\n"
                            )

                        passed_fcm_list = [fcm_compiled]
                    else:
                        report_text += "🌲 *Forest Canopy Cover (FCM):*\n"
                        report_text += "• Processing Status: `[Skipped - Layer Data Inactive/Not Found]` ⏳\n\n"

                    # ── FTM ANALYSIS BLOCK ──
                    if ftm_intersected_gdfs:
                        ftm_compiled = pd.concat(ftm_intersected_gdfs, ignore_index=True)
                        ftm_compiled["geometry"] = ftm_compiled.geometry.intersection(user_geom)
                        ftm_compiled = ftm_compiled[~ftm_compiled.geometry.is_empty].copy()

                        ftm_utm = ftm_compiled.to_crs(epsg=32644)
                        total_ftm_area_ha = ftm_utm.geometry.area.sum() / 10000.0

                        report_text += "🌿 *Forest Type Mapping (FTM):*\n"
                        report_text += f"• Active Intersecting Canopy Area: `{total_ftm_area_ha:.2f} ha`\n"
                        report_text += "• Processing Status: `[Natively Evaluated]` ✅\n\n"
                    else:
                        report_text += "🌿 *Forest Type Mapping (FTM):*\n"
                        report_text += "• Processing Status: `[Skipped - Layer Data Inactive/Not Found]` ⏳\n\n"

                    # ── DEM ANALYSIS BLOCK ──
                    if dem_intersected_gdfs:
                        dem_compiled = pd.concat(dem_intersected_gdfs, ignore_index=True)
                        elev_col = next(
                            (c for c in dem_compiled.columns if c.lower() in {"elevation", "elev", "contour", "z"}),
                            None,
                        )
                        if elev_col:
                            mean_val = pd.to_numeric(dem_compiled[elev_col], errors="coerce").mean()
                            max_val = pd.to_numeric(dem_compiled[elev_col], errors="coerce").max()
                            min_val = pd.to_numeric(dem_compiled[elev_col], errors="coerce").min()

                            dem_metrics = {
                                "elevation_min_m": round(float(min_val), 1),
                                "elevation_max_m": round(float(max_val), 1),
                                "elevation_mean_m": round(float(mean_val), 1),
                                "slope_mean_deg": "—",
                                "slope_max_deg": "—",
                            }

                            report_text += "⛰️ *Topographic Elevation Profiles (DEM):*\n"
                            report_text += f"• Intersecting Mean Elevation: `{mean_val:.1f} m`\n"
                            report_text += f"• Maximum Vertex Elevation Peak: `{max_val:.1f} m`\n"
                            report_text += "• Processing Status: `[Natively Evaluated]` ✅\n\n"
                        else:
                            dem_metrics = {}
                            report_text += "⛰️ *Topographic Elevation Profiles (DEM):*\n"
                            report_text += "• Processing Status: `[Evaluated - No Elevation Attribute Columns Found]` ⚠️\n\n"
                    else:
                        dem_metrics = {}
                        report_text += "⛰️ *Topographic Elevation Profiles (DEM):*\n"
                        report_text += "• Processing Status: `[Skipped - Layer Data Inactive/Not Found]` ⏳\n\n"

                    # ── RESULTS METADATA DICTIONARY ──
                    results = {
                        "area_ha": calculated_area_ha,
                        "centroid": [
                            round(float(user_geom.centroid.x), 6),
                            round(float(user_geom.centroid.y), 6),
                        ],
                        "fcm": {
                            "dominant": dominant_cover_type,
                            "classes": fcm_class_summary,
                        },
                        "dem": dem_metrics,
                        "_raw_fcm_gdfs": passed_fcm_list,
                        "_has_water": "WATER" in fcm_class_summary,
                    }

                    # Save metrics trail per polygon
                    polygon_filename = f"{base_name}_polygon_{polygon_number}"
                    log_analysis(
                        user.id,
                        polygon_filename,
                        single_feature,
                        {"status": "completed"},
                        results["centroid"],
                    )

                    # Render and send one report per polygon
                    try:
                        logger.info(
                            "RENDER_TRACE | file=%s | area=%.2f | fcm_classes=%s | raw_gdfs=%d | rows=%s | class_names=%s",
                            polygon_filename,
                            results.get("area_ha", 0),
                            list(results.get("fcm", {}).get("classes", {}).keys()),
                            len(results.get("_raw_fcm_gdfs", [])),
                            [len(gdf) for gdf in results.get("_raw_fcm_gdfs", [])],
                            [
                                gdf["class_name"].dropna().unique().tolist()
                                if "class_name" in gdf.columns
                                else "NO_CLASS_FIELD"
                                for gdf in results.get("_raw_fcm_gdfs", [])
                            ],
                        )

                        map_path = render_map(single_feature, results, polygon_filename)

                        if map_path and (isinstance(map_path, io.BytesIO) or Path(map_path).exists()):
                            await client.send_chat_action(chat_id=user.id, action=ChatAction.UPLOAD_PHOTO)
                            await callback_query.message.reply_photo(
                                photo=map_path,
                                caption=report_text,
                                parse_mode=ParseMode.MARKDOWN,
                            )
                            if isinstance(map_path, (str, Path)):
                                Path(map_path).unlink(missing_ok=True)
                        else:
                            await callback_query.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN)
                    except Exception as map_err:
                        logger.error(
                            "Map rendering module encountered non-fatal dropout error: %s",
                            map_err,
                            exc_info=True,
                        )
                        await callback_query.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN)

                    processed_any = True

                except Exception as polygon_err:
                    logger.error(
                        "Polygon %s analysis failed.",
                        polygon_number,
                        exc_info=True,
                    )
                    await callback_query.message.reply_text(
                        f"❌ *Polygon `{polygon_number}/{polygon_total}` analysis failed:* `{polygon_err}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    continue

            if not processed_any:
                await callback_query.message.reply_text(
                    "⚠️ No valid polygon reports could be generated from the uploaded file.",
                    parse_mode=ParseMode.MARKDOWN,
                )

            await status_msg.delete()

        except Exception as err:
            logger.error("Error executing dynamic channel vector intersections pipeline", exc_info=True)
            if "status_msg" in locals():
                await status_msg.edit_text(f"❌ *Analysis Execution Failed:* {err}")
        finally:
            if tmp_workspace.exists():
                shutil.rmtree(tmp_workspace)
            gc.collect()


# ── Plain text listener framework ─────────────────────────────────────────────
@Client.on_message(filters.text & filters.private & ~filters.regex(r"^/"))
async def catch_all_text(client: Client, message: Message) -> None:
    await message.reply_text(
        "👋 I am ready to process your spatial layouts!\n\n"
        "Please attach a valid configuration or spatial boundary file (.geojson, .kml, .zip) directly here. "
        "Type /help if you need formatting examples.",
        parse_mode=ParseMode.MARKDOWN,
    )
    sys.stdout.flush()
