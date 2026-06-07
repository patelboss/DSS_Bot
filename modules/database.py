"""
modules/database.py — All MongoDB Atlas interactions.

Collections
-----------
users          : One document per Telegram user_id.
analysis_logs  : Append-only record of every spatial analysis run.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient, GEOSPHERE
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from config import cfg

log = logging.getLogger(__name__)

# ── Connection (lazily re-used across requests) ───────────────────────────────
_client: MongoClient | None = None


def _get_db():
    global _client
    if _client is None:
        _client = MongoClient(cfg.MONGO_URI, serverSelectionTimeoutMS=5_000)
        _ensure_indexes(_client[cfg.MONGO_DB])
        log.info("MongoDB connection established.")
    return _client[cfg.MONGO_DB]


def _ensure_indexes(db) -> None:
    """Create indexes once on first connection."""
    users: Collection = db[cfg.MONGO_COLLECTION_USERS]
    users.create_index("telegram_id", unique=True)

    logs: Collection = db[cfg.MONGO_COLLECTION_LOGS]
    logs.create_index("telegram_id")
    logs.create_index("created_at")
    # Geospatial index so we can later query by polygon centroid
    logs.create_index([("centroid", GEOSPHERE)])


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
    geojson:   dict,          # The uploaded polygon as GeoJSON Feature
    results:   dict,          # Computed metrics dict from spatial_analysis
    centroid:  tuple[float, float],  # (lon, lat)
) -> str:
    """
    Persist one analysis result. Returns the inserted document _id as string.
    """
    db = _get_db()
    doc: dict[str, Any] = {
        "telegram_id": user_id,
        "filename":    filename,
        "created_at":  _now(),
        "geometry":    geojson.get("geometry"),      # GeoJSON geometry object
        "centroid": {                                 # For 2dsphere index
            "type":        "Point",
            "coordinates": list(centroid),
        },
        "results": results,
    }
    result = db[cfg.MONGO_COLLECTION_LOGS].insert_one(doc)
    increment_run_count(user_id)
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
