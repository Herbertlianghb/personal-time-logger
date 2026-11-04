"""Bounded JSON RPC reached only through a device-bound SSH forced command.

This process is not a second collector.  It adds evidence and jobs to the same
Store used by the Feishu collector. Optional bot chat mirroring is a separate
delivery outbox and never enters this evidence pipeline a second time.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from .calculator import LOCAL_TZ
from .service import load_config
from .store import Store, digest, now_ms, packed


MAX_REQUEST_BYTES = 65536
MAX_TEXT_BYTES = 16000
DEVICE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{7,79}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS desktop_devices(
 device_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,created_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS desktop_submissions(
 device_id TEXT NOT NULL REFERENCES desktop_devices(device_id),
 client_submission_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
 observation_id TEXT NOT NULL UNIQUE REFERENCES observations(observation_id),
 reported_at_ms INTEGER NOT NULL,server_saved_at_ms INTEGER NOT NULL,
 raw_text TEXT NOT NULL,context_json TEXT NOT NULL,
 PRIMARY KEY(device_id,client_submission_id));
"""


class BridgeError(ValueError):
    """A stable, non-sensitive error code suitable for an end-user client."""


def _ensure_schema(store):
    with store.lock:
        store.db.executescript(SCHEMA)


def _device_id(value):
    if not isinstance(value, str) or not DEVICE_ID.fullmatch(value):
        raise BridgeError("invalid_device_id")
    return value


def _submission_id(value):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("invalid_submission_id") from None
    if str(parsed) != value:
        raise BridgeError("invalid_submission_id")
    return value


def register_device(store, device_id, enabled=True):
    """Administrative entry point, never exposed as an RPC operation."""
    device_id = _device_id(device_id)
    _ensure_schema(store)
    with store.transaction() as db:
        db.execute("INSERT INTO desktop_devices VALUES(?,?,?) ON CONFLICT(device_id) DO UPDATE SET enabled=excluded.enabled",
                   (device_id, int(enabled), now_ms()))
    return {"device_id": device_id, "enabled": bool(enabled)}


def _date_range(value):
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise BridgeError("invalid_date") from None
    return (int(datetime.combine(day, time(), LOCAL_TZ).timestamp() * 1000),
            int(datetime.combine(day + timedelta(days=1), time(), LOCAL_TZ).timestamp() * 1000))


class DesktopBridge:
    def __init__(self, store, config, device_id):
        self.store, self.config = store, config
        self.device_id = _device_id(device_id)
        _ensure_schema(store)
        if config.get("desktop_chat_mirror", False):
            from .desktop_mirror import ensure_schema
            ensure_schema(store)

    def owner_identity(self):
        return self.config["owner_open_id"]

    def ingest(self, message):
        return self.store.ingest(message)

    def _authorize(self, request):
        if not isinstance(request, dict) or type(request.get("version")) is not int or request.get("version") != 1:
            raise BridgeError("protocol_version")
        if request.get("device_id") != self.device_id:
            raise BridgeError("device_mismatch")
        if not self.config.get("desktop_enabled", False):
            raise BridgeError("desktop_disabled")
        with self.store.lock:
            row = self.store.db.execute("SELECT enabled FROM desktop_devices WHERE device_id=?", (self.device_id,)).fetchone()
        if not row or not row[0]:
            raise BridgeError("device_revoked")

    def handle(self, request):
        self._authorize(request)
        operations = {"submit": self.submit, "status": self.status,
                      "pending": self.pending, "questions": self.questions,
                      "question_answer": self.submit, "history": self.history,
                      "health": self.health, "snapshot": self.snapshot,
                      "heatmap": self.heatmap,
                      "prepare_correction": self.prepare_correction}
        operation = operations.get(request.get("op"))
        if operation is None:
            raise BridgeError("operation_not_allowed")
        result = operation(request)
        return {**result, "ok": True, "version": 1, "server_now_ms": now_ms()}

    def _context(self, request, reported, received):
        context = {"source": "desktop", "device_id": self.device_id,
                   "device_reported_at_ms": reported, "server_received_at_ms": received,
                   "report_time_trust": "near_server_clock" if abs(received - reported) <= 300000 else "device_clock_unverified"}
        target = request.get("activity_id")
        revision = request.get("expected_revision")
        if target is not None:
            if not isinstance(target, str) or type(revision) is not int or revision < 1:
                raise BridgeError("target_revision_required")
            row = self.store.db.execute("SELECT body,revision FROM activities WHERE activity_id=? AND current_valid=1", (target,)).fetchone()
            if not row:
                raise BridgeError("activity_not_found")
            context.update(target_activity_id=target, expected_revision=revision,
                           target_activity_name=json.loads(row["body"]).get("name", ""),
                           current_revision_at_submit=row["revision"])
        elif revision is not None:
            raise BridgeError("target_activity_required")
        qid = request.get("question_id")
        if qid is not None:
            if not isinstance(qid, str):
                raise BridgeError("invalid_question_id")
            row = self.store.db.execute("SELECT * FROM questions WHERE question_id=?", (qid,)).fetchone()
            if not row or row["asked_at_ms"] is None:
                raise BridgeError("question_not_available")
            disposition = request.get("disposition", "answered")
            if disposition not in {"answered", "skipped"}:
                raise BridgeError("invalid_question_disposition")
            context.update(question_id=qid, question_text=json.loads(row["body"])["text"],
                           question_disposition=disposition, question_state_before=row["status"])
        elif request.get("op") == "question_answer":
            raise BridgeError("question_id_required")
        return context

    def submit(self, request):
        if not self.config.get("ingest", True):
            raise BridgeError("ingestion_disabled")
        sid = _submission_id(request.get("client_submission_id"))
        text = request.get("text")
        explicit_skip = request.get("question_id") is not None and request.get("disposition") == "skipped"
        if not isinstance(text, str) or (not text.strip() and not explicit_skip) or len(text.encode("utf-8")) > MAX_TEXT_BYTES or "\x00" in text:
            raise BridgeError("invalid_text")
        reported = request.get("reported_at_ms")
        if type(reported) is not int or not 946684800000 <= reported <= 4102444800000:
            raise BridgeError("invalid_reported_time")
        payload_hash = digest({key: request.get(key) for key in
                               ("text", "reported_at_ms", "activity_id", "expected_revision", "question_id", "disposition")})
        # One outer transaction includes raw evidence, interpretation job,
        # device mapping and any explicit question disposition. A response is
        # sent only after this transaction has committed.
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM desktop_submissions WHERE device_id=? AND client_submission_id=?", (self.device_id, sid)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise BridgeError("submission_id_payload_conflict")
                if self.config.get("desktop_chat_mirror", False):
                    from .desktop_mirror import enqueue_mirror
                    enqueue_mirror(self.store, self.config, dict(old))
                return {"submission": self._submission(dict(old)), "duplicate": True}
            received = now_ms()
            context = self._context(request, reported, received)
            result = self.ingest({"app_scope": self.store.scope,
                "message_id": "desktop:" + self.device_id + ":" + sid,
                "chat_id": self.config.get("chat_id") or self.store.get_setting("bound_chat_id") or "desktop-owner",
                "sender_key": self.owner_identity(), "sender_type": "user", "message_type": "text",
                "created_at_ms": reported, "received_at_ms": received, "content": text,
                "source": "desktop", "kind": "receive", "event_id": sid})
            oid = result["observation_id"]
            db.execute("INSERT INTO desktop_submissions VALUES(?,?,?,?,?,?,?,?)",
                       (self.device_id, sid, payload_hash, oid, reported, received, text, packed(context)))
            if context.get("target_activity_id") and context["expected_revision"] != context["current_revision_at_submit"]:
                db.execute("UPDATE jobs SET status='conflict',last_error_class='desktop_target_revision_changed' WHERE dedup_key=?", ("interpret:" + oid,))
            if context.get("question_id") and context["question_state_before"] == "pending":
                self.store.resolve_question(context["question_id"], oid, skipped=context["question_disposition"] == "skipped")
            if explicit_skip and not text.strip():
                # An explicit skip is already deterministic; an empty action
                # is not a new activity to send to the model.
                db.execute("UPDATE jobs SET status='done' WHERE dedup_key=?", ("interpret:" + oid,))
            row = dict(db.execute("SELECT * FROM desktop_submissions WHERE observation_id=?", (oid,)).fetchone())
            if self.config.get("desktop_chat_mirror", False):
                from .desktop_mirror import enqueue_mirror
                enqueue_mirror(self.store, self.config, row)
        return {"submission": self._submission(row), "duplicate": False}

    def _evidence_stage(self, oid):
        with self.store.lock:
            job = self.store.db.execute("SELECT status,last_error_class FROM jobs WHERE dedup_key=?", ("interpret:" + oid,)).fetchone()
            candidates = self.store.db.execute("SELECT * FROM activities WHERE current_valid=1 AND body LIKE ?", ('%"' + oid + '"%',)).fetchall()
            blocks = [{**json.loads(row["body"]), "activity_id": row["activity_id"], "revision": row["revision"]}
                      for row in candidates if oid in json.loads(row["body"]).get("evidence_refs", [])]
            mappings = []
            base_blocks = [block for block in blocks if block.get("actor") == "human" and block.get("role") == "primary"]
            archived = bool(base_blocks)
            for block in base_blocks:
                rows = self.store.db.execute("SELECT * FROM base_mapping WHERE activity_id=?", (block["activity_id"],)).fetchall()
                current = [dict(row) for row in rows if row["last_revision"] == block["revision"] and row["remote_id"]]
                archived = archived and bool(current) and all(row["last_revision"] == block["revision"] for row in rows)
                mappings.extend(current)
        stage = "server_saved"
        if job and job["status"] == "conflict":
            stage = "needs_review"
        elif archived:
            stage = "archived"
        elif blocks or (job and job["status"] == "done"):
            stage = "organized"
        return {"stage": stage, "activity_ids": [b["activity_id"] for b in blocks],
                "record_ids": list(dict.fromkeys(row["remote_id"] for row in mappings)),
                "processing_error": job["last_error_class"] if job else None,
                "base_required": bool(base_blocks),
                "base_verification": "previous_readback" if archived else None}

    def _submission(self, row):
        from .desktop_mirror import mirror_status
        context = json.loads(row["context_json"])
        return {"client_submission_id": row["client_submission_id"], "device_id": row["device_id"],
                "observation_id": row["observation_id"], "reported_at_ms": row["reported_at_ms"],
                "server_saved_at_ms": row["server_saved_at_ms"], "text": row["raw_text"],
                "report_time_trust": context["report_time_trust"],
                "context": context, **self._evidence_stage(row["observation_id"]), **mirror_status(self.store, self.config, row)}

    def status(self, request):
        ids = request.get("client_submission_ids")
        if ids is None:
            ids = [request.get("client_submission_id")]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise BridgeError("submission_ids_required")
        ids = list(dict.fromkeys(_submission_id(value) for value in ids))
        marks = ",".join("?" for _ in ids)
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute("SELECT * FROM desktop_submissions WHERE device_id=? AND client_submission_id IN (" + marks + ")", [self.device_id, *ids])]
        found = {row["client_submission_id"] for row in rows}
        return {"submissions": [self._submission(row) for row in rows], "not_found": [value for value in ids if value not in found]}

    def pending(self, request):
        limit = request.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError("invalid_limit")
        day = request.get("date")
        bounds = _date_range(day) if day is not None else None
        with self.store.lock:
            condition = " AND m.message_created_at_ms>=? AND m.message_created_at_ms<?" if bounds else ""
            rows = self.store.db.execute("""SELECT o.*,m.message_id,m.message_created_at_ms FROM observations o
                JOIN messages m USING(message_key) WHERE o.observation_id=m.current_observation_id AND m.message_type!='formal_record'
                AND NOT EXISTS(SELECT 1 FROM acceptance_tests t WHERE t.app_scope=m.app_scope AND t.message_id=m.message_id)"""
                + condition + " ORDER BY o.observed_at_ms DESC LIMIT ?", [*(bounds or []), limit]).fetchall()
            rows = [dict(row) for row in rows]
        items = []
        for row in rows:
            if row["kind"] == "recall":
                continue
            stage = self._evidence_stage(row["observation_id"])
            if stage["stage"] == "archived" or (stage["stage"] == "organized" and not stage["base_required"]):
                continue
            text = row["content"] or ""
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
                    text = decoded["text"]
            except ValueError:
                pass
            items.append({"observation_id": row["observation_id"], "source": "desktop" if row["source"] == "desktop" else "feishu",
                          "text": text, "reported_at_ms": row["message_created_at_ms"], "server_saved_at_ms": row["observed_at_ms"], **stage})
        return {"pending": items, "limit": limit, "may_have_more": len(rows) == limit}

    def questions(self, request):
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM questions WHERE status='pending' AND asked_at_ms IS NOT NULL ORDER BY asked_at_ms,question_id LIMIT 2").fetchall()
            values = [{**json.loads(row["body"]), "question_id": row["question_id"],
                       "status": row["status"], "asked_at_ms": row["asked_at_ms"]} for row in rows]
        # Test questions are never mixed into a real user's question budget UI.
        values = [value for value in values if not any(e.get("is_acceptance_test") for e in self.store.evidence(value.get("evidence_refs", [])))]
        return {"questions": values}

    def health(self, request):
        path = Path(self.config.get("health_file", str(self.config["database"]) + ".health.json"))
        service = None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            service = {key: saved.get(key) for key in ("as_of_ms", "ws_state", "last_error_class", "model_enabled", "base_sync_enabled", "notion_sync_enabled", "desktop_chat_mirror_enabled")}
        except (OSError, ValueError):
            pass
        return {"health": {"last_commit_at": self.store.get_setting("last_commit_at"),
                           "parse_enabled": bool(self.config.get("parse", False)),
                           "base_sync_enabled": bool(self.config.get("base_sync", False)), "service": service}}

    def snapshot(self, request):
        result = {**self.pending(request), **self.questions(request), **self.health(request)}
        if request.get("client_submission_ids"):
            result.update(self.status(request))
        return result

    def history(self, request):
        from .timeline import timeline_history
        day = request.get("date")
        _date_range(day)
        limit = request.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError("invalid_limit")
        cursor = request.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
            raise BridgeError("invalid_cursor")
        refresh = request.get("refresh", False)
        if type(refresh) is not bool:
            raise BridgeError("invalid_refresh")
        return timeline_history(self.store, self.config, day, cursor, limit, refresh=refresh,
                                day_start_hour=request.get("day_start_hour", 0))

    def prepare_correction(self, request):
        from .formal import FormalConflict, prepare_correction
        try:
            return prepare_correction(self.store, self.config, request.get("record_id"), request.get("expected_fingerprint"))
        except FormalConflict as exc:
            raise BridgeError(str(exc)) from None

    def heatmap(self, request):
        from .heatmap import month_bounds, monthly_heatmap
        month, refresh = request.get("month"), request.get("refresh", False)
        scope = request.get("scope", "month")
        if scope not in ("month", "year"):
            raise BridgeError("invalid_scope")
        try:
            month_bounds(month)
        except ValueError:
            raise BridgeError("invalid_month") from None
        if type(refresh) is not bool:
            raise BridgeError("invalid_refresh")
        return monthly_heatmap(self.store, self.config, month, refresh=refresh, scope=scope)


def read_request(stream):
    raw = stream.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise BridgeError("request_too_large_or_unterminated")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise BridgeError("invalid_json") from None
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description="Device-bound desktop bridge (SSH forced command)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--device-id", required=True)
    admin = parser.add_mutually_exclusive_group()
    admin.add_argument("--register-device", action="store_true")
    admin.add_argument("--disable-device", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    store = Store(config["database"], config["app_id"], [config["owner_open_id"]])
    try:
        if args.register_device or args.disable_device:
            result = register_device(store, args.device_id, not args.disable_device)
        else:
            result = DesktopBridge(store, config, args.device_id).handle(read_request(sys.stdin.buffer))
        print(packed(result), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        code = str(exc) if isinstance(exc, BridgeError) else "storage_unavailable" if isinstance(exc, sqlite3.Error) else "request_failed"
        print(packed({"ok": False, "error": code, "error_class": type(exc).__name__}), flush=True)
        raise SystemExit(1)
