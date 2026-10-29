"""Durable, independent desktop-to-bot chat mirroring.

The original desktop event is the only input evidence.  Bot mirror messages
are a delivery projection, never another user event or another model job.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import sqlite3
from pathlib import Path

from .calculator import LOCAL_TZ
from .feishu import PlatformError
from .store import Store, digest, now_ms, packed


JOB_TYPE = "desktop_chat_mirror"
# Feishu documents a one-hour uuid deduplication window. Keep ten minutes of
# margin and a finite send count; beyond it only read-side recovery is allowed.
SAFE_UUID_RETRY_MS = 50 * 60000
MAX_SEND_ATTEMPTS = 8
MAX_HISTORY_PAGES = 5
SCHEMA = """CREATE TABLE IF NOT EXISTS desktop_chat_mirrors(
 observation_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,chat_id TEXT NOT NULL,
 marker TEXT NOT NULL UNIQUE,body_text TEXT NOT NULL,body_hash TEXT NOT NULL,
 provider_uuid TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
 queued_at_ms INTEGER NOT NULL,send_started_at_ms INTEGER,send_attempts INTEGER NOT NULL DEFAULT 0,
 platform_message_id TEXT,sent_at_ms INTEGER,confirmed_at_ms INTEGER,last_error TEXT,
 accepted_at_ms INTEGER)"""


class MirrorError(ValueError):
    pass


def ensure_schema(store):
    with store.lock:
        store.db.execute(SCHEMA)


def _exists(store):
    with store.lock:
        return bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_chat_mirrors'").fetchone())


def _is_test(store, observation_id):
    evidence = store.evidence([observation_id])
    return not evidence or evidence[0].get("is_acceptance_test", False)


def enqueue_mirror(store, config, submission):
    """Called inside the same transaction as raw evidence, after its insert.

    The worker cannot see this job until COMMIT. No network call happens here.
    """
    if not config.get("desktop_chat_mirror", False) or not submission["raw_text"].strip():
        return False
    oid = submission["observation_id"]
    if _is_test(store, oid):
        return False
    context = json.loads(submission["context_json"])
    stamp = datetime.fromtimestamp(submission["reported_at_ms"] / 1000, LOCAL_TZ).strftime("%H:%M")
    heading = "电脑记录 · " + stamp
    if context.get("report_time_trust") != "near_server_clock":
        heading += "（设备报告时间）"
    if context.get("target_activity_name"):
        heading += "\n更正对象：" + context["target_activity_name"]
    elif context.get("question_text"):
        heading += "\n回答：" + context["question_text"]
    marker = "[记录索引 " + digest(["desktop-chat-mirror", oid])[:16] + "]"
    body = heading + "\n" + submission["raw_text"] + "\n\n" + marker
    chat_id = config.get("chat_id") or store.get_setting("bound_chat_id")
    binding = bool(chat_id and config.get("binding_verified", False)
                   and chat_id == store.get_setting("bound_chat_id"))
    with store.transaction() as db:
        store.enqueue(db, JOB_TYPE, "desktop-chat-mirror:" + oid, {"observation_id": oid})
        job = db.execute("SELECT job_id,client_token FROM jobs WHERE dedup_key=?", ("desktop-chat-mirror:" + oid,)).fetchone()
        db.execute("INSERT OR IGNORE INTO desktop_chat_mirrors(observation_id,device_id,chat_id,marker,body_text,body_hash,provider_uuid,job_id,state,queued_at_ms,last_error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (oid, submission["device_id"], chat_id or "", marker, body, digest(body), job["client_token"], job["job_id"],
                    "queued" if binding else "blocked", now_ms(), None if binding else "owner_chat_binding_unverified"))
    return True


def mirror_status(store, config, submission):
    row = None
    if _exists(store):
        with store.lock:
            record = store.db.execute("SELECT state,platform_message_id,sent_at_ms,confirmed_at_ms,last_error FROM desktop_chat_mirrors WHERE observation_id=?", (submission["observation_id"],)).fetchone()
            row = dict(record) if record else None
    enabled = bool(config.get("desktop_chat_mirror", False))
    state = (row["state"] if row else "not_applicable" if not submission["raw_text"].strip() or _is_test(store, submission["observation_id"])
             else "disabled" if not enabled else "blocked")
    return {"chat_mirror_state": state, "chat_mirror_enabled": enabled,
            "chat_mirror_paused": not enabled and bool(row and row["state"] != "sent"),
            "chat_mirror_message_id": row["platform_message_id"] if row else None,
            "chat_mirror_sent_at_ms": row["sent_at_ms"] if row else None,
            "chat_mirror_confirmed_at_ms": row["confirmed_at_ms"] if row else None,
            "chat_mirror_error": row["last_error"] if row else "mirror_not_queued" if state == "blocked" else None}


def mark_recalled_mirror(store, message_id):
    if not _exists(store):
        return False
    with store.transaction() as db:
        changed = db.execute("UPDATE desktop_chat_mirrors SET state='blocked',last_error='chat_mirror_recalled' WHERE platform_message_id=?", (message_id,)).rowcount
    return bool(changed)


def _platform_time(value):
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        return None
    return parsed if type(value) is not bool and 946684800000 <= parsed <= 4102444800000 else None


def _match_message(store, row, item, *, require_chat=False):
    sender = item.get("sender", {})
    if (sender.get("sender_type") != "app" or sender.get("id_type") != "app_id"
            or sender.get("id") != store.scope or item.get("msg_type") != "text" or item.get("deleted")
            or require_chat and item.get("chat_id") != row["chat_id"]):
        return None
    if not require_chat and item.get("chat_id") not in {None, row["chat_id"]}:
        return None
    try:
        text = json.loads(item.get("body", {}).get("content", ""))["text"]
    except (ValueError, TypeError, KeyError):
        return None
    sent_at = _platform_time(item.get("create_time"))
    if (not isinstance(text, str) or row["marker"] not in text or digest(text) != row["body_hash"]
            or sent_at is None or not isinstance(item.get("message_id"), str)):
        return None
    return {"message_id": item["message_id"], "sent_at_ms": sent_at}


def _recover(store, platform, row):
    if row["platform_message_id"]:
        item = platform.get_message(row["platform_message_id"])
        result = _match_message(store, row, item, require_chat=True) if isinstance(item, dict) else None
        if result is None or result["message_id"] != row["platform_message_id"]:
            raise MirrorError("mirror_readback_mismatch")
        return result
    start, end, token, tokens, matches = max(0, row["send_started_at_ms"] - 60000), now_ms(), None, set(), {}
    for page_number in range(MAX_HISTORY_PAGES):
        page = platform.history_page(row["chat_id"], start, end, token)
        for item in page.get("items", []):
            match = _match_message(store, row, item)
            if match and start <= match["sent_at_ms"] <= end:
                matches[match["message_id"]] = match
        if not page.get("has_more"):
            if len(matches) > 1:
                raise MirrorError("duplicate_mirror_messages")
            return next(iter(matches.values())) if matches else None
        token = page.get("page_token")
        if not isinstance(token, str) or not token or token in tokens:
            raise MirrorError("incomplete_mirror_history")
        tokens.add(token)
    raise MirrorError("mirror_history_page_bound")


def _state(store, oid, state, error=None):
    with store.transaction() as db:
        db.execute("UPDATE desktop_chat_mirrors SET state=?,last_error=? WHERE observation_id=?", (state, error, oid))


def _success(store, job, result):
    with store.transaction() as db:
        db.execute("UPDATE desktop_chat_mirrors SET state='sent',platform_message_id=?,sent_at_ms=?,confirmed_at_ms=?,last_error=NULL WHERE observation_id=?",
                   (result["message_id"], result.get("sent_at_ms"), now_ms(), job["payload"]["observation_id"]))
        store.finish_job(job["job_id"], "done", remote_id=result["message_id"])


def process_mirror(store, config, platform, job):
    """One bounded delivery/recovery attempt, independent of model/Base work."""
    oid = job["payload"]["observation_id"]
    if not config.get("desktop_chat_mirror", False):
        store.finish_job(job["job_id"], "retry", "desktop_chat_mirror_paused")
        return
    with store.lock:
        saved = store.db.execute("SELECT * FROM desktop_chat_mirrors WHERE observation_id=?", (oid,)).fetchone()
        row = dict(saved) if saved else None
    if row is None:
        store.finish_job(job["job_id"], "conflict", "mirror_mapping_missing")
        return
    if _is_test(store, oid):
        _state(store, oid, "not_applicable")
        store.finish_job(job["job_id"], "superseded", "acceptance_test_excluded")
        return
    if row["state"] == "sent":
        store.finish_job(job["job_id"], "done", remote_id=row["platform_message_id"])
        return
    if row["last_error"] == "chat_mirror_recalled":
        store.finish_job(job["job_id"], "superseded", "chat_mirror_recalled")
        return
    if (not config.get("binding_verified", False) or row["chat_id"] != config.get("chat_id")
            or row["chat_id"] != store.get_setting("bound_chat_id")):
        _state(store, oid, "blocked", "owner_chat_binding_unverified")
        store.finish_job(job["job_id"], "retry", "owner_chat_binding_unverified")
        return
    if row["provider_uuid"] != job["client_token"] or digest(row["body_text"]) != row["body_hash"]:
        _state(store, oid, "blocked", "mirror_payload_or_uuid_changed")
        store.finish_job(job["job_id"], "conflict", "mirror_payload_or_uuid_changed")
        return
    ambiguous_before = row["send_started_at_ms"] is not None
    try:
        if row["platform_message_id"]:
            _success(store, job, _recover(store, platform, row))
            return
        if ambiguous_before:
            recovered = _recover(store, platform, row)
            if recovered:
                _success(store, job, recovered)
                return
            age = now_ms() - row["send_started_at_ms"]
            if not 0 <= age <= SAFE_UUID_RETRY_MS or row["send_attempts"] >= MAX_SEND_ATTEMPTS:
                _state(store, oid, "outcome_unknown", "mirror_delivery_unconfirmed")
                store.finish_job(job["job_id"], "retry", "mirror_delivery_unconfirmed")
                return
        # Persist the send boundary before touching the network. A process
        # crash from here onward always takes the recovery/idempotent path.
        with store.transaction() as db:
            db.execute("UPDATE desktop_chat_mirrors SET state='sending',send_started_at_ms=COALESCE(send_started_at_ms,?),send_attempts=send_attempts+1,last_error=NULL WHERE observation_id=?", (now_ms(), oid))
        result = platform.send_desktop_mirror(row["chat_id"], row["body_text"], row["provider_uuid"])
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), str) or not result["message_id"]:
            raise PlatformError("transport_outcome_unknown")
        with store.transaction() as db:
            db.execute("UPDATE desktop_chat_mirrors SET platform_message_id=?,sent_at_ms=?,accepted_at_ms=? WHERE observation_id=?",
                       (result["message_id"], _platform_time(result.get("sent_at_ms")), now_ms(), oid))
        row["platform_message_id"] = result["message_id"]
        # Persist the returned id before GET. Even a crash or failed GET now
        # recovers this exact message and never sends another POST.
        _success(store, job, _recover(store, platform, row))
    except (PlatformError, MirrorError) as exc:
        category = exc.category if isinstance(exc, PlatformError) else str(exc)
        definitive_rejection = isinstance(exc, PlatformError) and category in {"rate_limited", "permission_or_authentication", "http_rejected"}
        if definitive_rejection and not ambiguous_before and not row["platform_message_id"]:
            with store.transaction() as db:
                db.execute("UPDATE desktop_chat_mirrors SET state='retry',send_started_at_ms=NULL,last_error=? WHERE observation_id=?", (category, oid))
        else:
            _state(store, oid, "blocked" if isinstance(exc, MirrorError) and category == "duplicate_mirror_messages" else "outcome_unknown", category)
        store.finish_job(job["job_id"], "retry", category)
    except Exception as exc:
        # A generic transport exception can occur after the server accepted a
        # message. Preserve uncertainty, the payload and the original uuid.
        _state(store, oid, "outcome_unknown", type(exc).__name__)
        store.finish_job(job["job_id"], "retry", type(exc).__name__)


def backfill_mirrors(store, config, device_id, *, apply=False, maximum=10):
    """Explicit one-device backfill; default is a read-only count."""
    with store.lock:
        device = store.db.execute("SELECT enabled FROM desktop_devices WHERE device_id=?", (device_id,)).fetchone()
        if not device or not device[0]:
            raise MirrorError("bound_device_required")
        has_mirrors = _exists(store)
        condition = " AND NOT EXISTS(SELECT 1 FROM desktop_chat_mirrors r WHERE r.observation_id=s.observation_id)" if has_mirrors else ""
        rows = store.db.execute("""SELECT s.* FROM desktop_submissions s WHERE s.device_id=? AND length(trim(s.raw_text))>0
          AND NOT EXISTS(SELECT 1 FROM observations o JOIN messages m USING(message_key)
            JOIN acceptance_tests t ON t.app_scope=m.app_scope AND t.message_id=m.message_id WHERE o.observation_id=s.observation_id)"""
            + condition + " ORDER BY s.server_saved_at_ms", (device_id,)).fetchall()
        rows = [dict(row) for row in rows]
    if len(rows) > maximum:
        raise MirrorError("backfill_exceeds_explicit_bound")
    result = {"device_id": device_id, "eligible_count": len(rows), "applied": False, "queued_count": 0}
    if not apply:
        return result
    if not config.get("desktop_chat_mirror", False):
        raise MirrorError("desktop_chat_mirror_disabled")
    ensure_schema(store)
    with store.transaction():
        result["queued_count"] = sum(bool(enqueue_mirror(store, config, row)) for row in rows)
    return {**result, "applied": True}


def main():
    parser = argparse.ArgumentParser(description="Count or enqueue the explicitly authorized device's prior desktop reports")
    parser.add_argument("--config", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if args.apply:
        store = Store(config["database"], config["app_id"], [config["owner_open_id"]])
    else:
        # Counting must not run Store schema/pragma initialization.
        store = object.__new__(Store)
        import threading
        store.lock = threading.RLock()
        store.db = sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True)
        store.db.row_factory = sqlite3.Row
    try:
        print(packed(backfill_mirrors(store, config, args.device_id, apply=args.apply)))
    finally:
        store.close()


if __name__ == "__main__":
    main()
