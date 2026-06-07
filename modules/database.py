"""
modules/database.py — All MongoDB Atlas interactions.

Collections
-----------
users          : One document per Telegram user_id.
analysis_logs  : Append-only record of every spatial analysis run.
"""

import logging
import sys
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient, GEOSPHERE
from pymongo.collection import Collection

from config import cfg

# ── Force Stream / Unbuffered Stdout Logging Setup for Koyeb Console ─────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(stdout_handler)

# ── Connection (lazily re-used across requests) ───────────────────────────────
_client: MongoClient | None = None


def _get_db():
    global _client
    if _client is None:
        if not cfg.MONGO_URI:
            log.critical("❌ Missing MONGO_URI in environment config parameters.")
            sys.stdout.flush()
            raise ValueError("Invalid MongoDB configuration parameters detected.")
        try:
            _client = MongoClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5000)
            # Force connection verification immediately on launch
            _client.admin.command('ping')
            _ensure_indexes(_client[cfg.MONGO_DB])
            log.info("✅ MongoDB Atlas connection successfully established.")
            sys.stdout.flush()
        except Exception as e:
            log.error(f"❌ Failed to connect to MongoDB Atlas cluster instance: {str(e)}")
            sys.stdout.flush()
            raise
    return _client[cfg.MONGO_DB]


def _ensure_indexes(db) -> None:
    """Create indexes once on first connection."""
    try:
        users: Collection = db[cfg.MONGO_COLLECTION_USERS]
        users.create_index("telegram_id", unique=True)

        logs: Collection = db[cfg.MONGO_COLLECTION_LOGS]
        logs.create_index("telegram_id")
        logs.create_index("created_at")
        # Geospatial index so we can later query by polygon centroid
        logs.create_index([("centroid", GEOSPHERE)])
        log.info("📊 Database index validation checks passed cleanly.")
        sys.stdout.flush()
    except Exception as e:
        log.error(f"⚠️ Index configuration check encountered a hitch: {str(e)}")
        sys.stdout.flush()


# ── User helpers ──────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str | None, full_name: str) -> None:
    """Create or update a user record; bump last_seen timestamp."""
    db = _get_db()
    db[cfg.MONGO_COLLECTION_USERS].update_one(
        {"telegram_id": user_id},
        {
            "$set": {
                "username":  username,
                "full_name": full_name,
                "last_seen": _now(),
            },
            "$setOnInsert": {
                "telegram_id": user_id,
                "created_at":  _now(),
                "run_count":   0,
            },
        },
        upsert=True,
    )


def increment_run_count(user_id: int) -> None:
    db = _get_db()
    db[cfg.MONGO_COLLECTION_USERS].update_one(
        {"telegram_id": user_id},
        {"$inc": {"run_count": 1}},
    )


def get_user(user_id: int) -> dict | None:
    db = _get_db()
    return db[cfg.MONGO_COLLECTION_USERS].find_one({"telegram_id": user_id})


# ── Analysis log helpers ──────────────────────────────────────────────────────

def log_analysis(
    user_id:   int,
    filename:  str,
    geojson:   Any,           # The uploaded polygon as GeoJSON Feature or dict geometry
    results:   dict,          # Computed metrics dict from spatial_analysis
    centroid:  tuple[float, float],  # (lon, lat)
) -> str:
    """
    Persist one analysis result. Returns the inserted document _id as string.
    """
    db = _get_db()

    # 🚀 ADAPTIVE EXTRACTOR: Gracefully maps geometry whether full Feature block or raw structure geometry is passed
    if isinstance(geojson, dict):
        extracted_geometry = geojson.get("geometry", geojson)
    else:
        extracted_geometry = geojson

    doc: dict[str, Any] = {
        "telegram_id": user_id,
        "filename":    filename,
        "created_at":  _now(),
        "geometry":    extracted_geometry,
        "centroid": {                                 # For 2dsphere index compatibility
            "type":        "Point",
            "coordinates": list(centroid),
        },
        "results": results,
    }
    
    result = db[cfg.MONGO_COLLECTION_LOGS].insert_one(doc)
    increment_run_count(user_id)
    
    log.info(f"💾 Spatial log metrics saved cleanly for user [{user_id}] under tracking ID: {result.inserted_id}")
    sys.stdout.flush()
    return str(result.inserted_id)


def get_user_history(user_id: int, limit: int = 5) -> list[dict]:
    """Return the most recent N analysis logs for a user."""
    db = _get_db()
    cursor = (
        db[cfg.MONGO_COLLECTION_LOGS]
        .find({"telegram_id": user_id}, {"_id": 0, "geometry": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    return list(cursor)


# ── Utility ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
    
