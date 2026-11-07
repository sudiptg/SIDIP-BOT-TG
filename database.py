"""Persistence layer.

* Default backend: a local JSON file (``DATA_DIR/database.json``) - fine for a VPS.
* Optional backend: MongoDB (set ``MONGO_URI``) - REQUIRED on Render Free because its
  filesystem is wiped on every restart / redeploy / spin-down.

All data lives in memory and is flushed in the background (debounced), so handlers stay fast.
An old ``database.json`` from the previous bot version is migrated automatically.
"""
import asyncio
import copy
import json
import logging
import os
from pathlib import Path

import config

log = logging.getLogger("database")
_DOC_ID = "botdata"


def _default_global() -> dict:
    return {
        "auth_users": [],
        "max_tasks": config.MAX_CONCURRENT_TASKS,
        "stats": {"done": 0, "failed": 0, "bytes": 0},
    }


class Database:
    def __init__(self) -> None:
        self.data = {"users": {}, "global": _default_global()}
        self.backend = "json"
        self._client = None
        self._coll = None
        self._dirty = False
        self._lock = asyncio.Lock()
        self._flusher = None

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        raw = None
        if config.MONGO_URI:
            try:
                from pymongo import AsyncMongoClient

                self._client = AsyncMongoClient(config.MONGO_URI, serverSelectionTimeoutMS=8000)
                self._coll = self._client[config.MONGO_DB_NAME]["bot_data"]
                doc = await self._coll.find_one({"_id": _DOC_ID})
                raw = (doc or {}).get("data") or {}
                self.backend = "mongodb"
            except Exception as exc:
                log.error("MongoDB unavailable (%s). Falling back to the local JSON file.", exc)
                self._coll = None
                self._client = None
        if not raw:
            raw = await asyncio.to_thread(self._read_json)
        self._merge(raw or {})
        log.info("Database ready (backend=%s, users=%d)", self.backend, len(self.data["users"]))

    @staticmethod
    def _read_json() -> dict:
        for path in (config.DB_FILE, Path("database.json")):  # second one = legacy location
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.error("Could not read %s: %s", path, exc)
        return {}

    def _merge(self, raw: dict) -> None:
        if "user_settings" in raw or "metadata_db" in raw or "autorename_format" in raw:
            raw = self._migrate_legacy(raw)
        users = raw.get("users", {})
        glob = raw.get("global", {})
        self.data = {"users": users, "global": {**_default_global(), **glob}}
        self.data["global"]["stats"] = {**_default_global()["stats"], **self.data["global"].get("stats", {})}

    @staticmethod
    def _migrate_legacy(raw: dict) -> dict:
        users: dict = {}
        for uid, old in raw.get("user_settings", {}).items():
            settings = dict(old)
            if isinstance(settings.get("sub"), str):
                settings["sub"] = settings["sub"] == "on"
            users.setdefault(uid, {})["settings"] = settings
        for uid, meta in raw.get("metadata_db", {}).items():
            users.setdefault(uid, {})["metadata"] = meta
        for uid, fmt in raw.get("autorename_format", {}).items():
            users.setdefault(uid, {})["rename_format"] = fmt
        log.info("Migrated legacy database.json (%d users)", len(users))
        return {"users": users, "global": {}}

    # ------------------------------------------------------------------ #
    # Saving
    # ------------------------------------------------------------------ #
    def touch(self) -> None:
        """Mark data as changed; a debounced background flush will persist it."""
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._flusher is None or self._flusher.done():
            self._flusher = loop.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(1.5)
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            snapshot = json.dumps(self.data, ensure_ascii=False)
            try:
                if self._coll is not None:
                    await self._coll.replace_one(
                        {"_id": _DOC_ID}, {"_id": _DOC_ID, "data": json.loads(snapshot)}, upsert=True
                    )
                else:
                    await asyncio.to_thread(self._write_json, snapshot)
            except Exception as exc:
                self._dirty = True
                log.error("Could not save database: %s", exc)

    @staticmethod
    def _write_json(payload: str) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.DB_FILE.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, config.DB_FILE)

    async def close(self) -> None:
        await self.flush()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #
    def user(self, uid) -> dict:
        record = self.data["users"].setdefault(str(uid), {})
        record.setdefault("settings", {})
        record.setdefault("metadata", {})
        record.setdefault("rename_format", "")
        record.setdefault("thumb_file_id", "")
        record.setdefault("stats", {"files": 0, "bytes": 0})
        return record

    def settings(self, uid) -> dict:
        """Live settings dict (defaults filled in). Call ``touch()`` after changing it."""
        current = self.user(uid)["settings"]
        for key, value in config.DEFAULT_SETTINGS.items():
            current.setdefault(key, copy.deepcopy(value))
        return current

    # ------------------------------------------------------------------ #
    # Access control + statistics
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_owner(uid) -> bool:
        return int(uid) == int(config.OWNER_ID)

    def is_authorized(self, uid) -> bool:
        uid = int(uid)
        return (self.is_owner(uid) or uid in config.AUTH_USERS
                or uid in self.data["global"]["auth_users"])

    def auth_list(self) -> list:
        return list(self.data["global"]["auth_users"])

    def add_auth(self, uid: int) -> None:
        if uid not in self.data["global"]["auth_users"]:
            self.data["global"]["auth_users"].append(uid)
            self.touch()

    def remove_auth(self, uid: int) -> bool:
        if uid in self.data["global"]["auth_users"]:
            self.data["global"]["auth_users"].remove(uid)
            self.touch()
            return True
        return False

    def record_result(self, uid, ok: bool, nbytes: int = 0) -> None:
        stats = self.data["global"]["stats"]
        stats["done" if ok else "failed"] += 1
        if ok:
            stats["bytes"] += nbytes
            user_stats = self.user(uid)["stats"]
            user_stats["files"] += 1
            user_stats["bytes"] += nbytes
        self.touch()


db = Database()
