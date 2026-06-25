"""
modules/cmd.py — Command handlers, callback routing, and resource-conscious analysis orchestrator.
Dynamically streams intersected grid data directly via cached Telegram channel file_ids,
runs localized vector analytics within tight 512MB RAM constraints, and flushes cache nodes instantly.
"""
from __future__ import annotations

import gc
import io
import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from rasterio.mask import mask as rio_mask
from shapely.geometry import mapping

from config import cfg
from modules.database import _get_db, log_analysis, upsert_user
from modules.map_renderer import render_map
from modules.spatial_analysis import load_vector_file

logger = logging.getLogger("main.commands")
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(h)

USER_SESSION_CACHE = {}


def _align_gdf_crs(gdf: gpd.GeoDataFrame, target_crs) -> gpd.GeoDataFrame:
    if gdf is None or gdf.empty:
        return gdf
    if gdf.crs is None:
        return gdf.set_crs(target_crs)
    try:
        if gdf.crs.to_string().upper() != str(target_crs).upper():
            return gdf.to_crs(target_crs)
    except Exception:
        if str(gdf.crs).upper() != str(target_crs).upper():
            return gdf.to_crs(target_crs)
    return gdf


def _normalize_channel_id() -> int | str:
    raw = str(cfg.TELEGRAM_CHANNEL_ID).strip()
    try:
        return int(raw)
    except ValueError:
        return raw


def _class_hi(label: str) -> str:
    s = str(label or "").strip().upper()
    mapping_hi = {
        "VDF": "अत्यंत घना वन",
        "MDF": "मध्यम घना वन",
        "OPEN FOREST": "खुला वन",
        "NON FOREST": "गैर वन",
        "SCRUB": "झाड़ी",
        "WATER": "जल निकाय",
        "NO-DATA": "डेटा नहीं",
    }
    return mapping_hi.get(s, s)


def _build_bilingual_summary(
    area_ha: float,
    fcm_class_summary: dict,
    dominant_cover_type: str,
    dem_metrics: dict,
    ftm_area_ha: Optional[float] = None,
    has_raster_dem: bool = False,
) -> tuple[str, str, list[str], list[str]]:
    dominant_pct = float(fcm_class_summary.get(dominant_cover_type, {}).get("percentage", 0.0) or 0.0)
    classes_text = ", ".join(fcm_class_summary.keys()) if fcm_class_summary else "No FCM data available"
    classes_hi = ", ".join(_class_hi(k) for k in fcm_class_summary.keys()) if fcm_class_summary else "एफसीएम डेटा उपलब्ध नहीं है"

    dem_min = dem_metrics.get("elevation_min_m")
    dem_max = dem_metrics.get("elevation_max_m")
    dem_mean = dem_metrics.get("elevation_mean_m")

    if dem_min is not None and dem_max is not None and dem_mean is not None:
        dem_sentence_en = (
            f"Terrain analysis derived from {'raster DEM' if has_raster_dem else 'DEM data'} shows an elevation range of {dem_min}–{dem_max} metres above mean sea level, with a mean elevation of {dem_mean} metres."
        )
        dem_sentence_hi = (
            f"{ 'रास्टर DEM' if has_raster_dem else 'DEM डेटा' } पर आधारित भू-आकृतिक विश्लेषण में समुद्र तल से ऊँचाई {dem_min}–{dem_max} मीटर के बीच पाई गई है, तथा औसत ऊँचाई {dem_mean} मीटर है।"
        )
    else:
        dem_sentence_en = "Terrain analysis could not be completed because elevation data was not available."
        dem_sentence_hi = "ऊँचाई डेटा उपलब्ध न होने के कारण भू-आकृतिक विश्लेषण पूरा नहीं हो सका।"

    ftm_text_en = f"Forest Type Mapping area is {ftm_area_ha:.2f} hectares." if ftm_area_ha is not None else "Forest Type Mapping data was not available."
    ftm_text_hi = f"वन प्रकार मानचित्रण का क्षेत्रफल {ftm_area_ha:.2f} हेक्टेयर है।" if ftm_area_ha is not None else "वन प्रकार मानचित्रण डेटा उपलब्ध नहीं है।"

    summary_en = (
        f"The analysed area covers {area_ha:.2f} hectares. Forest Cover Mapping indicates that {dominant_cover_type} is the dominant class, accounting for {dominant_pct:.1f}% of the area. {ftm_text_en} {dem_sentence_en} The area contains the forest cover classes: {classes_text}."
    )
    summary_hi = (
        f"विश्लेषित क्षेत्र {area_ha:.2f} हेक्टेयर है। वन आच्छादन मानचित्रण के अनुसार { _class_hi(dominant_cover_type) } प्रमुख वर्ग है, जो कुल क्षेत्र का {dominant_pct:.1f}% भाग घेरता है। {ftm_text_hi} {dem_sentence_hi} उपलब्ध वन आच्छादन वर्ग: {classes_hi}।"
    )

    key_facts_en = [
        f"Total Area: {area_ha:.2f} ha",
        f"Dominant Forest Class: {dominant_cover_type}",
        f"Forest Cover Type: {classes_text}",
    ]
    key_facts_hi = [
        f"कुल क्षेत्रफल: {area_ha:.2f} हेक्टेयर",
        f"प्रमुख वन वर्ग: { _class_hi(dominant_cover_type) }",
        f"वन आच्छादन वर्ग: {classes_hi}",
    ]
    if dem_min is not None and dem_max is not None and dem_mean is not None:
        key_facts_en.extend([
            f"Elevation Range: {dem_min}–{dem_max} m",
            f"Mean Elevation: {dem_mean} m",
        ])
        key_facts_hi.extend([
            f"ऊँचाई सीमा: {dem_min}–{dem_max} मीटर",
            f"औसत ऊँचाई: {dem_mean} मीटर",
        ])
    return summary_en, summary_hi, key_facts_en, key_facts_hi


def _summarize_raster_dem(paths: list[str], study_geom) -> tuple[dict, list[str]]:
    accum = {"min": None, "max": None, "sum": 0.0, "count": 0}
    kept_paths: list[str] = []

    for path in paths:
        try:
            with rasterio.open(path) as src:
                arr, _ = rio_mask(src, [study_geom.__geo_interface__], crop=True, filled=True, nodata=src.nodata)
                data = np.array(arr[0], dtype="float64")
                if src.nodata is not None:
                    data[data == src.nodata] = np.nan
                else:
                    data[~np.isfinite(data)] = np.nan
                finite = data[np.isfinite(data)]
                if finite.size == 0:
                    continue
                kept_paths.append(path)
                fmin = float(np.nanmin(finite))
                fmax = float(np.nanmax(finite))
                accum["min"] = fmin if accum["min"] is None else min(accum["min"], fmin)
                accum["max"] = fmax if accum["max"] is None else max(accum["max"], fmax)
                accum["sum"] += float(np.nansum(finite))
                accum["count"] += int(finite.size)
        except Exception as exc:
            logger.warning("DEMR stats skipped for %s: %s", path, exc)

    if accum["count"]:
        return {
            "elevation_min_m": round(float(accum["min"]), 1),
            "elevation_max_m": round(float(accum["max"]), 1),
            "elevation_mean_m": round(float(accum["sum"] / accum["count"]), 1),
            "slope_mean_deg": "—",
            "slope_max_deg": "—",
        }, kept_paths
    return {}, kept_paths


def _summarize_vector_dem(gdfs: list[gpd.GeoDataFrame], study_geom) -> tuple[dict, list[gpd.GeoDataFrame]]:
    kept: list[gpd.GeoDataFrame] = []
    values = []
    mins = []
    maxs = []
    for gdf in gdfs:
        if gdf is None or gdf.empty:
            continue
        if getattr(gdf, "crs", None) and str(gdf.crs).upper() != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")
        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.intersection(study_geom)
        gdf = gdf[~gdf.geometry.is_empty].copy()
        if gdf.empty:
            continue
        elev_col = next((c for c in gdf.columns if str(c).lower() in {"elevation", "elev", "contour", "z"}), None)
        if elev_col is None:
            continue
        series = pd.to_numeric(gdf[elev_col], errors="coerce").dropna()
        if series.empty:
            continue
        kept.append(gdf)
        values.extend(series.tolist())
        mins.append(float(series.min()))
        maxs.append(float(series.max()))
    if not values:
        return {}, kept
    return {
        "elevation_min_m": round(float(min(mins)), 1),
        "elevation_max_m": round(float(max(maxs)), 1),
        "elevation_mean_m": round(float(sum(values) / len(values)), 1),
        "slope_mean_deg": "—",
        "slope_max_deg": "—",
    }, kept


@Client.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message) -> None:
    user = message.from_user
    upsert_user(user.id, user.username, f"{user.first_name} {user.last_name or ''}".strip())
    text = (
        f"🌿 *Welcome to the SDSS Bot, {user.first_name}!*\n\n"
        "This system performs automated spatial analysis for the *Madhya Pradesh Forest Department*.\n\n"
        "*What to do:*\n"
        "1️⃣ Upload a spatial boundary file (`.geojson`, `.kml`, `.kmz`, `.zip`)\n"
        "2️⃣ Choose whether to view attributes or run analysis\n"
        "3️⃣ Receive a multi-page PDF report with all available maps\n\n"
        "*Commands:*\n"
        "/help — Detailed instructions\n"
        "/status — Check system health\n\n"
        "_Upload your file to begin_ 👆"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
        "• Separate FCM / FTM / DEM maps in one PDF when available\n"
        "• Final bilingual summary page in English + Hindi\n\n"
        "_Upload your file to begin_ 👆"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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

    status_msg = await message.reply_text("📥 *Processing vector layout properties…*", parse_mode=ParseMode.MARKDOWN)
    tmp_path = Path(tempfile.gettempdir()) / f"{user.id}_{document.file_name}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        await client.download_media(message, file_name=str(tmp_path))
        geojson_feature, gdf_attributes = load_vector_file(tmp_path)
        attr_df = gdf_attributes.drop(columns=["geometry"], errors="ignore")
        USER_SESSION_CACHE[user.id] = {
            "current_feature": geojson_feature,
            "current_gdf": gdf_attributes,
            "filename": document.file_name,
            "cached_df_dict": attr_df.to_dict(orient="records"),
        }

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Attributes Table", callback_data="action_attributes")],
            [InlineKeyboardButton("🔬 Run Spatial DSS Analysis", callback_data="action_analysis")],
        ])
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
            tmp_path.unlink(missing_ok=True)


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
        await callback_query.message.reply_document(document=bio, caption=f"📄 Full attribute table export for `{filename}`.")
        return

    if action != "action_analysis":
        return

    status_msg = await callback_query.message.reply_text(
        "⏳ *Initializing Real-Time Multi-Map PDF Analysis…*",
        parse_mode=ParseMode.MARKDOWN,
    )
    tmp_workspace = Path(tempfile.mkdtemp())
    grid_local_path = tmp_workspace / "state_fishnet_grid.gpkg"

    try:
        await client.send_chat_action(chat_id=user.id, action=ChatAction.TYPING)
        db = _get_db()

        uploaded_gdf = uploaded_gdf.copy()
        if uploaded_gdf.crs is None:
            uploaded_gdf.set_crs("EPSG:4326", inplace=True)
        uploaded_gdf = uploaded_gdf.explode(index_parts=False).reset_index(drop=True)
        uploaded_gdf = uploaded_gdf[uploaded_gdf.geometry.notnull() & ~uploaded_gdf.geometry.is_empty].copy()
        if uploaded_gdf.empty:
            await status_msg.edit_text("⚠️ *Analysis Completed:* The uploaded file contains no valid polygon features.")
            return

        from modules.storage import _get_supabase
        await status_msg.edit_text("🛰 *Aligning layout against Spatial Mesh Framework Grid…*")
        supabase = _get_supabase()
        with open(grid_local_path, "wb") as f:
            f.write(supabase.storage.from_(cfg.SUPABASE_BUCKET).download("state_grid.gpkg"))

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

                single_gdf = gpd.GeoDataFrame([row.to_dict()], geometry="geometry", crs=uploaded_gdf.crs)
                user_utm = single_gdf.to_crs(epsg=32644)
                calculated_area_ha = float(user_utm.geometry.area.sum() / 10000.0)
                single_feature = {
                    "type": "Feature",
                    "properties": {"polygon_id": polygon_number, "source_file": filename},
                    "geometry": mapping(_align_gdf_crs(single_gdf.copy(), "EPSG:4326").geometry.iloc[0]),
                }

                await status_msg.edit_text(f"📍 *Processing polygon `{polygon_number}/{polygon_total}`…*", parse_mode=ParseMode.MARKDOWN)
                intersecting_cells = grid_gdf[grid_gdf.geometry.intersects(user_geom)]
                target_grid_ids = intersecting_cells["TopoSheet_No"].dropna().astype(str).unique().tolist()
                if not target_grid_ids:
                    await callback_query.message.reply_text(
                        f"⚠️ *Polygon `{polygon_number}/{polygon_total}`:* The uploaded boundary does not intersect the study framework grid area.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    continue

                fcm_intersected_gdfs: list[gpd.GeoDataFrame] = []
                ftm_intersected_gdfs: list[gpd.GeoDataFrame] = []
                dem_intersected_gdfs: list[gpd.GeoDataFrame] = []
                demr_paths: list[str] = []

                for grid_id in target_grid_ids:
                    # FCM
                    fcm_doc = getattr(db, "fcm_layers").find_one({"grid_id": grid_id, "data_type": "FCM"})
                    if fcm_doc:
                        fcm_local = tmp_workspace / f"fcm_{grid_id}.gpkg"
                        await client.download_media(fcm_doc["file_id"], file_name=str(fcm_local))
                        part_gdf = _align_gdf_crs(gpd.read_file(str(fcm_local)), uploaded_gdf.crs)
                        clip_part = part_gdf[part_gdf.geometry.intersects(user_geom)].copy()
                        if not clip_part.empty:
                            fcm_intersected_gdfs.append(clip_part)
                        fcm_local.unlink(missing_ok=True)

                    # FTM
                    ftm_doc = getattr(db, "ftm_layers").find_one({"grid_id": grid_id, "data_type": "FTM"})
                    if ftm_doc:
                        ftm_local = tmp_workspace / f"ftm_{grid_id}.gpkg"
                        await client.download_media(ftm_doc["file_id"], file_name=str(ftm_local))
                        part_gdf = _align_gdf_crs(gpd.read_file(str(ftm_local)), uploaded_gdf.crs)
                        clip_part = part_gdf[part_gdf.geometry.intersects(user_geom)].copy()
                        if not clip_part.empty:
                            ftm_intersected_gdfs.append(clip_part)
                        ftm_local.unlink(missing_ok=True)

                    # DEM vector
                    dem_doc = getattr(db, "dem_layers").find_one({"grid_id": grid_id, "data_type": "DEM"})
                    if dem_doc:
                        dem_local = tmp_workspace / f"dem_{grid_id}.gpkg"
                        await client.download_media(dem_doc["file_id"], file_name=str(dem_local))
                        part_gdf = _align_gdf_crs(gpd.read_file(str(dem_local)), uploaded_gdf.crs)
                        clip_part = part_gdf[part_gdf.geometry.intersects(user_geom)].copy()
                        if not clip_part.empty:
                            dem_intersected_gdfs.append(clip_part)
                        dem_local.unlink(missing_ok=True)

                    # DEMR raster
                    demr_doc = getattr(db, "demr_layers", None)
                    if demr_doc is not None:
                        demr_doc = demr_doc.find_one({"grid_id": grid_id, "data_type": "DEMR"})
                    if demr_doc:
                        demr_local = tmp_workspace / f"demr_{grid_id}.tif"
                        await client.download_media(demr_doc["file_id"], file_name=str(demr_local))
                        demr_paths.append(str(demr_local))

                    gc.collect()

                report_text = (
                    f"🔬 *Conditional Spatial Analysis Report for `{filename}`*\n"
                    f"🧩 *Polygon:* `{polygon_number}/{polygon_total}`\n"
                    f"📅 *Generated on:* `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                    f"📊 *Total Boundary Area:* `{calculated_area_ha:.2f} ha`\n\n"
                )

                fcm_class_summary: dict = {}
                dominant_cover_type = "NON FOREST"
                passed_fcm_list: list[gpd.GeoDataFrame] = []
                passed_ftm_list: list[gpd.GeoDataFrame] = []
                passed_dem_list: list[gpd.GeoDataFrame] = []
                passed_demr_paths: list[str] = []
                dem_metrics: dict = {}
                ftm_area_ha: Optional[float] = None

                # FCM
                if fcm_intersected_gdfs:
                    fcm_compiled = pd.concat(fcm_intersected_gdfs, ignore_index=True)
                    logger.info("FCM Columns = %s", fcm_compiled.columns.tolist())
                    logger.info("FCM Sample = %s", fcm_compiled.head(3).to_dict("records"))
                    fcm_compiled["geometry"] = fcm_compiled.geometry.intersection(user_geom)
                    fcm_compiled = fcm_compiled[~fcm_compiled.geometry.is_empty].copy()
                    fcm_utm = fcm_compiled.to_crs(epsg=32644)
                    class_col = next((c for c in fcm_utm.columns if c.lower() == "class_name"), None)
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
                            fcm_class_summary[standard_label] = {"hectares": float(class_ha), "percentage": float(c_pct)}
                            if standard_label not in {"WATER", "NO-DATA"} and class_ha > max_area:
                                max_area = class_ha
                                dominant_cover_type = standard_label
                    report_text += "🌲 *Forest Canopy Cover (FCM):*\n"
                    if fcm_class_summary:
                        for label, metrics in fcm_class_summary.items():
                            report_text += f"• {label}: `{metrics['hectares']:.2f} ha` ({metrics['percentage']:.1f}%)\n"
                        report_text += "• Processing Status: `[Natively Evaluated]` ✅\n\n"
                    else:
                        report_text += "• Processing Status: `[Evaluated - class_name column not usable]` ⚠️\n\n"
                    passed_fcm_list = [fcm_compiled]
                else:
                    report_text += "🌲 *Forest Canopy Cover (FCM):*\n"
                    report_text += "• Processing Status: `[Skipped - Layer Data Inactive/Not Found]` ⏳\n\n"

                # FTM
                if ftm_intersected_gdfs:
                    ftm_compiled = pd.concat(ftm_intersected_gdfs, ignore_index=True)
                    ftm_compiled["geometry"] = ftm_compiled.geometry.intersection(user_geom)
                    ftm_compiled = ftm_compiled[~ftm_compiled.geometry.is_empty].copy()
                    ftm_utm = ftm_compiled.to_crs(epsg=32644)
                    ftm_area_ha = float(ftm_utm.geometry.area.sum() / 10000.0)
                    report_text += "🌿 *Forest Type Mapping (FTM):*\n"
                    report_text += f"• Active Intersecting Canopy Area: `{ftm_area_ha:.2f} ha`\n"
                    report_text += "• Processing Status: `[Natively Evaluated]` ✅\n\n"
                    passed_ftm_list = [ftm_compiled]
                else:
                    report_text += "🌿 *Forest Type Mapping (FTM):*\n"
                    report_text += "• Processing Status: `[Skipped - Layer Data Inactive/Not Found]` ⏳\n\n"

                # DEM vector
                if dem_intersected_gdfs:
                    dem_compiled = pd.concat(dem_intersected_gdfs, ignore_index=True)
                    logger.info("DEM Columns = %s", dem_compiled.columns.tolist())
                    logger.info("DEM Sample = %s", dem_compiled.head(3).to_dict("records"))
                    elev_col = next((c for c in dem_compiled.columns if c.lower() in {"elevation", "elev", "contour", "z"}), None)
                    if elev_col:
                        dem_compiled["geometry"] = dem_compiled.geometry.intersection(user_geom)
                        dem_compiled = dem_compiled[~dem_compiled.geometry.is_empty].copy()
                        elev_series = pd.to_numeric(dem_compiled[elev_col], errors="coerce").dropna()
                        if not elev_series.empty:
                            dem_metrics = {
                                "elevation_min_m": round(float(elev_series.min()), 1),
                                "elevation_max_m": round(float(elev_series.max()), 1),
                                "elevation_mean_m": round(float(elev_series.mean()), 1),
                                "slope_mean_deg": "—",
                                "slope_max_deg": "—",
                            }
                            report_text += "⛰️ *Topographic Elevation Profiles (DEM):*\n"
                            report_text += f"• Intersecting Mean Elevation: `{elev_series.mean():.1f} m`\n"
                            report_text += f"• Maximum Vertex Elevation Peak: `{elev_series.max():.1f} m`\n"
                            report_text += "• Processing Status: `[Natively Evaluated]` ✅\n\n"
                            passed_dem_list = [dem_compiled]
                        else:
                            report_text += "⛰️ *Topographic Elevation Profiles (DEM):*\n"
                            report_text += "• Processing Status: `[Elevation values empty]` ⚠️\n\n"
                    else:
                        report_text += "⛰️ *Topographic Elevation Profiles (DEM):*\n"
                        report_text += "• Processing Status: `[Evaluated - No Elevation Attribute Columns Found]` ⚠️\n\n"
                else:
                    report_text += "⛰️ *Topographic Elevation Profiles (DEM):*\n"
                    report_text += "• Processing Status: `[Skipped - Layer Data Inactive/Not Found]` ⏳\n\n"

                # DEMR raster (preferred for contour rendering if present)
                if demr_paths:
                    demr_metrics, kept_paths = _summarize_raster_dem(demr_paths, user_geom)
                    if demr_metrics:
                        dem_metrics = demr_metrics
                        passed_demr_paths = kept_paths
                        report_text += "🛰️ *Raster DEM (DEMR):*\n"
                        report_text += f"• Raster tiles used: `{len(kept_paths)}`\n"
                        report_text += f"• Elevation Range: `{demr_metrics['elevation_min_m']}–{demr_metrics['elevation_max_m']} m`\n"
                        report_text += f"• Mean Elevation: `{demr_metrics['elevation_mean_m']} m`\n"
                        report_text += "• Processing Status: `[Natively Evaluated]` ✅\n\n"
                    else:
                        report_text += "🛰️ *Raster DEM (DEMR):*\n"
                        report_text += "• Processing Status: `[No raster DEM values available]` ⚠️\n\n"

                summary_en, summary_hi, key_facts_en, key_facts_hi = _build_bilingual_summary(
                    area_ha=calculated_area_ha,
                    fcm_class_summary=fcm_class_summary,
                    dominant_cover_type=dominant_cover_type,
                    dem_metrics=dem_metrics,
                    ftm_area_ha=ftm_area_ha,
                    has_raster_dem=bool(passed_demr_paths),
                )

                results = {
                    "area_ha": calculated_area_ha,
                    "centroid": [round(float(user_geom.centroid.x), 6), round(float(user_geom.centroid.y), 6)],
                    "fcm": {"dominant": dominant_cover_type, "classes": fcm_class_summary},
                    "ftm": {"area_ha": ftm_area_ha},
                    "dem": dem_metrics,
                    "summary_en": summary_en,
                    "summary_hi": summary_hi,
                    "key_facts_en": key_facts_en,
                    "key_facts_hi": key_facts_hi,
                    "_raw_fcm_gdfs": passed_fcm_list,
                    "_raw_ftm_gdfs": passed_ftm_list,
                    "_raw_dem_gdfs": passed_dem_list,
                    "_raw_demr_paths": passed_demr_paths,
                    "_map_modes": [m for m in ["fcm" if passed_fcm_list else None, "ftm" if passed_ftm_list else None, "dem" if (passed_demr_paths or passed_dem_list) else None] if m],
                    "_has_water": "WATER" in fcm_class_summary,
                    "_contour_interval_m": 20,
                }

                polygon_filename = f"{base_name}_polygon_{polygon_number}"
                log_analysis(user.id, polygon_filename, single_feature, {"status": "completed"}, results["centroid"])

                try:
                    logger.info(
                        "RENDER_TRACE | file=%s | area=%.2f | fcm_classes=%s | raw_fcm=%d | raw_ftm=%d | raw_dem=%d | raw_demr=%d",
                        polygon_filename,
                        results.get("area_ha", 0),
                        list(results.get("fcm", {}).get("classes", {}).keys()),
                        len(results.get("_raw_fcm_gdfs", [])),
                        len(results.get("_raw_ftm_gdfs", [])),
                        len(results.get("_raw_dem_gdfs", [])),
                        len(results.get("_raw_demr_paths", [])),
                    )
                    pdf_path = render_map(single_feature, results, polygon_filename, map_mode="bundle")
                    pdf_path.name = f"{polygon_filename}.pdf"
                    await client.send_chat_action(chat_id=user.id, action=ChatAction.UPLOAD_DOCUMENT)
                    await callback_query.message.reply_document(
                        document=pdf_path,
                        caption=(
                            f"📄 *Analysis PDF Report for `{polygon_filename}`*\n"
                            f"• Separate pages generated for available layers\n"
                            f"• Final bilingual summary included"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception as map_err:
                    logger.error("Map rendering module encountered non-fatal dropout error: %s", map_err, exc_info=True)
                    await callback_query.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN)

                processed_any = True
                gc.collect()

            except Exception as polygon_err:
                logger.error("Polygon %s analysis failed.", polygon_number, exc_info=True)
                await callback_query.message.reply_text(
                    f"❌ *Polygon `{polygon_number}/{polygon_total}` analysis failed:* `{polygon_err}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                continue

        if not processed_any:
            await callback_query.message.reply_text("⚠️ No valid polygon reports could be generated from the uploaded file.", parse_mode=ParseMode.MARKDOWN)
        await status_msg.delete()

    except Exception as err:
        logger.error("Error executing dynamic channel vector intersections pipeline", exc_info=True)
        if "status_msg" in locals():
            await status_msg.edit_text(f"❌ *Analysis Execution Failed:* {err}", parse_mode=ParseMode.MARKDOWN)
    finally:
        if tmp_workspace.exists():
            shutil.rmtree(tmp_workspace, ignore_errors=True)
        gc.collect()


@Client.on_message(filters.text & filters.private & ~filters.regex(r"^/"))
async def catch_all_text(client: Client, message: Message) -> None:
    await message.reply_text(
        "👋 I am ready to process your spatial layouts!\n\n"
        "Please attach a valid boundary file (.geojson, .kml, .zip) directly here. "
        "Type /help if you need formatting examples.",
        parse_mode=ParseMode.MARKDOWN,
    )
    sys.stdout.flush()


from utils import *


@Client.on_message(filters.command("testtext") & filters.user(ADMIN_IDS))
async def testtext_handler(client, message: Message):
    arg = None
    if len(message.command) > 1:
        arg = message.command[1].strip().lower()

    if arg == "png":
        out = render_texttest_png()
        out.name = "texttest.png"
        await message.reply_photo(photo=out, caption="Text shaping test PNG")
        return

    out = render_texttest_pdf()
    out.name = "texttest.pdf"
    await message.reply_document(document=out, caption="Text shaping test PDF")

@Client.on_message(filters.command("testtext"))
async def testtext_handler(client, message: Message):
    arg = None
    if len(message.command) > 1:
        arg = message.command[1].strip().lower()

    if arg == "png":
        out = render_texttest_png()
        out.name = "texttest.png"
        await message.reply_photo(photo=out, caption="Text shaping test PNG")
        return

    out = render_texttest_pdf()
    out.name = "texttest.pdf"
    await message.reply_document(document=out, caption="Text shaping test PDF")
