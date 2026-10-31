"""Opt-in desktop-only SSH endpoint, with a separate, owner-bound raw Store.

No receiver, Feishu credentials, model worker or formal Base are used. The
normal desktop_bridge entry point and its configuration remain unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from .desktop_bridge import DesktopBridge, BridgeError, _date_range, read_request, register_device
from .store import Store, packed


OWNER_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{7,79}$")
OWNER_SETTING = "desktop_only_owner_v1"


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or config.get("mode") != "desktop_only":
        raise ValueError("desktop_only_mode_required")
    owner = config.get("owner_id")
    if not isinstance(owner, str) or not OWNER_ID.fullmatch(owner):
        raise ValueError("desktop_only_owner_required")
    database = config.get("database")
    if not isinstance(database, str) or not Path(database).is_absolute():
        raise ValueError("desktop_only_absolute_database_required")
    if Path(database).resolve() == Path(path).resolve():
        raise ValueError("desktop_only_database_is_config")
    for flag in ("desktop_enabled", "ingest"):
        if type(config.get(flag)) is not bool:
            raise ValueError("desktop_only_boolean_required:" + flag)
    if type(config.get("manual_organization", False)) is not bool:
        raise ValueError("desktop_only_boolean_required:manual_organization")
    for key in ("parse", "base_sync", "notion_sync", "desktop_chat_mirror"):
        if key in config and config[key] is not False:
            raise ValueError("desktop_only_feature_not_supported:" + key)
    for key in ("app_id", "app_secret", "owner_open_id", "chat_id", "base_token", "time_table_id", "table_id"):
        if config.get(key):
            raise ValueError("desktop_only_external_identity_not_allowed")
    return config


def open_store(config):
    """Refuse an existing unbound or differently owned DB before Store writes.

An absent DB is claimed with O_EXCL; an existing empty/partially initialized
file is never silently adopted. This prevents pointing this endpoint at the
normal Feishu database by mistake. It is not an OS tenant isolation boundary.
"""
    path = Path(config["database"])
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("desktop_only_database_symlink")
    owner = config["owner_id"]
    scope = "desktop-only:" + owner
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not path.is_file():
            raise ValueError("desktop_only_database_not_regular") from None
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (OWNER_SETTING,)).fetchone()
            if row is None or json.loads(row[0]) != owner:
                raise ValueError("desktop_only_database_owner_mismatch")
            foreign = connection.execute(
                "SELECT 1 FROM messages WHERE app_scope<>? OR sender_key<>? LIMIT 1", (scope, owner)).fetchone()
            if foreign:
                raise ValueError("desktop_only_database_identity_mismatch")
        except sqlite3.Error:
            raise ValueError("desktop_only_database_not_initialized") from None
        finally:
            connection.close()
        created = False
    else:
        os.close(fd)
        created = True
    store = Store(path, scope, [owner])
    try:
        if created:
            store.set_setting(OWNER_SETTING, owner)
        # The identity check above happens before any schema initialization.
        if store.get_setting(OWNER_SETTING) != owner:
            raise ValueError("desktop_only_database_owner_mismatch")
        return store
    except BaseException:
        store.close()
        raise


class StandaloneBridge(DesktopBridge):
    def __init__(self, store, config, device_id):
        if (config.get("mode") != "desktop_only"
                or store.get_setting(OWNER_SETTING) != config.get("owner_id")
                or store.scope != "desktop-only:" + config["owner_id"]
                or store.allowed_senders != frozenset([config["owner_id"]])):
            raise ValueError("desktop_only_store_identity_mismatch")
        super().__init__(store, config, device_id)

    def owner_identity(self):
        return self.config["owner_id"]

    def handle(self, request):
        if isinstance(request, dict) and request.get("op") in {"agent_export", "agent_import"}:
            from .standalone_organize import accept, export_day
            self._authorize(request)
            value = (export_day(self.store, self.config, request.get("date")) if request["op"] == "agent_export"
                     else accept(self.store, self.config, request))
            from .store import now_ms
            return {**value, "ok": True, "version": 1, "server_now_ms": now_ms()}
        return super().handle(request)

    def ingest(self, message):
        return self.store.ingest(message, enqueue_processing=False)

    def _context(self, request, reported, received):
        if any(request.get(key) is not None for key in
               ("activity_id", "expected_revision", "record_id", "expected_fingerprint", "question_id", "disposition")):
            raise BridgeError("desktop_only_raw_records_only")
        if request.get("op") == "question_answer":
            raise BridgeError("desktop_only_raw_records_only")
        return super()._context(request, reported, received)

    def _evidence_stage(self, oid):
        if self.config.get("manual_organization"):
            accepted = [block for block in self.store.current_activities() if oid in block.get("evidence_refs", [])]
            if accepted:
                return {"stage": "organized", "activity_ids": [b["activity_id"] for b in accepted],
                        "record_ids": [], "processing_error": None, "processing_enabled": False,
                        "base_required": False, "base_verification": None, "mode": "desktop_only"}
        return {"stage": "server_saved", "activity_ids": [], "record_ids": [],
                "processing_error": None, "processing_enabled": False,
                "base_required": False, "base_verification": None, "mode": "desktop_only"}

    def pending(self, request):
        limit = request.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError("invalid_limit")
        if request.get("date") is not None:
            _date_range(request["date"])
        # Raw records belong to history, not to a nonexistent model's backlog.
        return {"pending": [], "limit": limit, "may_have_more": False}

    def questions(self, request):
        return {"questions": []}

    def health(self, request):
        return {"health": {"mode": "desktop_only", "last_commit_at": self.store.get_setting("last_commit_at"),
                           "parse_enabled": False, "base_sync_enabled": False, "service": None,
                           "capabilities": {"raw_history": True, "daily_record_counts": True,
                                            "model": False, "formal_corrections": False,
                                            "manual_organization": bool(self.config.get("manual_organization"))}}}

    def history(self, request):
        from .standalone_views import history
        _date_range(request.get("date"))
        limit, cursor, refresh = request.get("limit", 100), request.get("cursor"), request.get("refresh", False)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError("invalid_limit")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 4096):
            raise BridgeError("invalid_cursor")
        if type(refresh) is not bool:
            raise BridgeError("invalid_refresh")
        return history(self.store, request["date"], cursor, limit)

    def heatmap(self, request):
        from .standalone_views import heatmap
        if type(request.get("refresh", False)) is not bool:
            raise BridgeError("invalid_refresh")
        return heatmap(self.store, request.get("month"), request.get("scope", "month"))

    def prepare_correction(self, request):
        raise BridgeError("desktop_only_raw_records_only")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Desktop-only, device-bound SSH bridge")
    parser.add_argument("--config", required=True)
    parser.add_argument("--device-id", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--register-device", action="store_true")
    actions.add_argument("--disable-device", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    store = open_store(config)
    try:
        if args.register_device or args.disable_device:
            result = register_device(store, args.device_id, not args.disable_device)
        else:
            result = StandaloneBridge(store, config, args.device_id).handle(read_request(sys.stdin.buffer))
        print(packed(result), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        from .batch import BatchConflict
        from .model_runner import InvalidModelProposal
        from .proposal import InvalidProposal
        code = str(exc) if isinstance(exc, (BridgeError, BatchConflict, InvalidProposal, InvalidModelProposal)) else "storage_unavailable" if isinstance(exc, sqlite3.Error) else "request_failed"
        print(packed({"ok": False, "error": code, "error_class": type(exc).__name__}), flush=True)
        raise SystemExit(1)
