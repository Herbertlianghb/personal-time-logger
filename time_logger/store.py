from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def now_ms():
    return time.time_ns() // 1_000_000


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(packed(value).encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
 message_key TEXT PRIMARY KEY,app_scope TEXT NOT NULL,message_id TEXT NOT NULL,
 chat_id TEXT NOT NULL,sender_key TEXT NOT NULL,message_type TEXT NOT NULL,
 first_content TEXT,message_created_at_ms INTEGER,first_received_at_ms INTEGER NOT NULL,
 first_persisted_at_ms INTEGER NOT NULL,current_observation_id TEXT,
 UNIQUE(app_scope,message_id));
CREATE TABLE IF NOT EXISTS observations(
 observation_id TEXT PRIMARY KEY,message_key TEXT NOT NULL REFERENCES messages(message_key),
 kind TEXT NOT NULL,content TEXT,content_hash TEXT NOT NULL,updated_at_ms INTEGER,
 observed_at_ms INTEGER NOT NULL,source TEXT NOT NULL,provider_event_id TEXT,
 fingerprint TEXT NOT NULL,UNIQUE(message_key,fingerprint));
CREATE TABLE IF NOT EXISTS jobs(
 job_id TEXT PRIMARY KEY,job_type TEXT NOT NULL,dedup_key TEXT UNIQUE NOT NULL,
 payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
 next_at_ms INTEGER NOT NULL,lease_until_ms INTEGER,client_token TEXT NOT NULL,
 last_error_class TEXT,remote_id TEXT);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,next_at_ms);
CREATE TABLE IF NOT EXISTS proposals(
 proposal_id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,model_id TEXT NOT NULL,
 prompt_version TEXT NOT NULL,created_at_ms INTEGER NOT NULL,body TEXT NOT NULL,
 status TEXT NOT NULL,UNIQUE(input_hash,model_id,prompt_version));
CREATE TABLE IF NOT EXISTS activities(
 activity_id TEXT NOT NULL,revision INTEGER NOT NULL,proposal_id TEXT NOT NULL,
 body TEXT NOT NULL,current_valid INTEGER NOT NULL,created_at_ms INTEGER NOT NULL,
 PRIMARY KEY(activity_id,revision));
CREATE UNIQUE INDEX IF NOT EXISTS one_current_activity ON activities(activity_id) WHERE current_valid=1;
CREATE TABLE IF NOT EXISTS questions(
 question_id TEXT PRIMARY KEY,semantic_key TEXT UNIQUE NOT NULL,proposal_id TEXT NOT NULL,
 body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',asked_at_ms INTEGER,
 answer_observation_id TEXT);
CREATE TABLE IF NOT EXISTS scans(
 chat_id TEXT PRIMARY KEY,completed_through_ms INTEGER,fixed_end_ms INTEGER,
 cursor TEXT,status TEXT NOT NULL DEFAULT 'unknown',last_error_class TEXT);
CREATE TABLE IF NOT EXISTS base_mapping(
 activity_id TEXT NOT NULL,segment_key TEXT NOT NULL,remote_id TEXT,
 last_revision INTEGER NOT NULL DEFAULT 0,baseline TEXT,schema_fingerprint TEXT,
 PRIMARY KEY(activity_id,segment_key));
CREATE TABLE IF NOT EXISTS sync_operations(
 operation_id TEXT PRIMARY KEY,activity_id TEXT NOT NULL,revision INTEGER NOT NULL,
 segment_key TEXT NOT NULL,action TEXT NOT NULL,body TEXT NOT NULL,before_body TEXT,
 client_token TEXT NOT NULL,status TEXT NOT NULL,remote_id TEXT,created_at_ms INTEGER NOT NULL,
 UNIQUE(activity_id,revision,segment_key));
CREATE TABLE IF NOT EXISTS report_dirty(day TEXT PRIMARY KEY,changed_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS acceptance_tests(app_scope TEXT NOT NULL,message_id TEXT NOT NULL,PRIMARY KEY(app_scope,message_id));
"""


class Store:
    """One serialized writer. A return from ingest means COMMIT succeeded."""

    def __init__(self, path, app_scope, allowed_senders, allowed_chats=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = app_scope
        self.allowed_senders = frozenset(allowed_senders)
        self.allowed_chats = frozenset(allowed_chats)
        if not self.scope or not self.allowed_senders:
            raise ValueError("explicit application scope and sender allowlist required")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=1, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=EXTRA")
        self.db.execute("PRAGMA busy_timeout=750")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('schema_version','1')")
        self.commit_latencies_ms = []

    @contextmanager
    def transaction(self):
        with self.lock:
            nested = self.db.in_transaction
            savepoint = "sp_" + uuid.uuid4().hex
            self.db.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("RELEASE SAVEPOINT " + savepoint if nested else "COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    if nested:
                        self.db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                        self.db.execute("RELEASE SAVEPOINT " + savepoint)
                    else:
                        self.db.execute("ROLLBACK")
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def get_setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, packed(value)))

    def tag_test_messages(self, message_ids):
        if not all(isinstance(mid, str) and mid.startswith("om_") for mid in message_ids):
            raise ValueError("explicit_platform_test_message_ids_required")
        with self.transaction() as db:
            db.executemany("INSERT OR IGNORE INTO acceptance_tests VALUES(?,?)", [(self.scope, mid) for mid in message_ids])
        return {"tagged": len(message_ids)}

    @staticmethod
    def enqueue(db, kind, key, payload):
        db.execute(
            "INSERT OR IGNORE INTO jobs(job_id,job_type,dedup_key,payload,next_at_ms,client_token) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), kind, key, packed(payload), now_ms(), str(uuid.uuid4())),
        )

    def ingest(self, message, *, enqueue_processing=True):
        started = time.monotonic_ns()
        if message.get("app_scope") != self.scope:
            raise PermissionError("wrong_app_scope")
        if message.get("sender_key") not in self.allowed_senders or message.get("sender_type", "user") != "user":
            raise PermissionError("unauthorized_sender")
        chat_id = message.get("chat_id")
        if not chat_id or (self.allowed_chats and chat_id not in self.allowed_chats):
            raise PermissionError("unauthorized_chat")
        mid = message.get("message_id")
        if not isinstance(mid, str) or not mid:
            raise ValueError("missing_message_id")
        kind = message.get("kind", "receive")
        if kind not in {"receive", "edit", "recall"}:
            raise ValueError("observation_kind")
        created = message.get("created_at_ms")
        updated = message.get("updated_at_ms")
        for ts in (created, updated):
            if ts is not None and (type(ts) is not int or not 946684800000 <= ts <= 4102444800000):
                raise ValueError("platform_timestamp_must_be_utc_milliseconds")
        if kind != "recall" and created is None:
            raise ValueError("platform_creation_time_required")
        content = message.get("content")
        if not isinstance(content, (str, type(None))):
            raise ValueError("content_must_be_text")
        source = message.get("source", "ws")
        if source not in {"ws", "history", "authorized_import", "desktop"}:
            raise ValueError("observation_source")
        key = digest([self.scope, mid])
        fingerprint = digest([kind, content, updated])
        observed = now_ms()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, self.scope, mid, chat_id, message["sender_key"], message.get("message_type", "text"),
                 content, created, message.get("received_at_ms", observed), observed, None),
            )
            row = db.execute("SELECT * FROM messages WHERE message_key=?", (key,)).fetchone()
            # A recall may arrive first; later evidence fills only previously unknown initial facts.
            if row["message_created_at_ms"] is None and created is not None:
                db.execute("UPDATE messages SET message_created_at_ms=?,first_content=COALESCE(first_content,?) WHERE message_key=?",
                           (created, content, key))
            previous = db.execute("SELECT * FROM observations WHERE observation_id=?", (row["current_observation_id"],)).fetchone()
            duplicate = db.execute("SELECT observation_id FROM observations WHERE message_key=? AND fingerprint=?", (key, fingerprint)).fetchone()
            if duplicate:
                return {"saved": True, "duplicate": True, "observation_id": duplicate[0], "message_id": mid}
            oid = str(uuid.uuid4())
            db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (oid, key, kind, content, digest(content), updated, observed, source, message.get("event_id"), fingerprint))
            conflict = False
            current = previous is None or kind == "recall"
            if previous and previous["kind"] != "recall" and kind != "recall":
                prev_version = previous["updated_at_ms"]
                if updated is not None and (prev_version is None or updated > prev_version):
                    current = True
                elif content != previous["content"] and (updated is None or updated == prev_version):
                    conflict = True
                    self.enqueue(db, "reconcile", "conflict:" + oid, {"chat_id": chat_id})
            if previous and previous["kind"] == "recall":
                current = False
            if current:
                db.execute("UPDATE messages SET current_observation_id=? WHERE message_key=?", (oid, key))
                if enqueue_processing:
                    task_type = "interpret" if row["message_type"] == "text" else "attachment"
                    self.enqueue(db, task_type, task_type + ":" + oid, {"observation_id": oid})
            if previous is None and source == "ws" and kind != "recall":
                self.enqueue(db, "receipt", "receipt:" + key, {"message_id": mid, "chat_id": chat_id, "created_at_ms": created})
            db.execute("INSERT OR REPLACE INTO settings VALUES('last_commit_at',?)", (packed(observed),))
        self.commit_latencies_ms.append((time.monotonic_ns() - started) / 1_000_000)
        self.commit_latencies_ms = self.commit_latencies_ms[-2000:]
        return {"saved": True, "duplicate": False, "conflict": conflict, "observation_id": oid, "message_id": mid}

    def evidence(self, ids=None, limit=100):
        with self.lock:
            if ids:
                rows = self.db.execute(
                    "SELECT o.*,m.message_id,m.message_created_at_ms,m.chat_id,m.current_observation_id,m.message_type FROM observations o JOIN messages m USING(message_key) WHERE o.observation_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY m.message_created_at_ms,o.observed_at_ms", ids).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT o.*,m.message_id,m.message_created_at_ms,m.chat_id,m.current_observation_id,m.message_type FROM observations o JOIN messages m USING(message_key) WHERE o.observation_id=m.current_observation_id ORDER BY m.message_created_at_ms DESC LIMIT ?", (limit,)).fetchall()
                rows = list(reversed(rows))
        with self.lock:
            test_ids = {row[0] for row in self.db.execute("SELECT message_id FROM acceptance_tests WHERE app_scope=?", (self.scope,))}
        result = []
        for row in rows:
            entry = {**dict(row), "id": row["observation_id"], "created_at_ms": row["message_created_at_ms"]}
            entry["is_acceptance_test"] = row["message_id"] in test_ids
            if row["source"] == "formal_record" and row["message_type"] == "formal_record":
                entry["formal_record"] = json.loads(row["content"])
            if row["source"] == "desktop":
                with self.lock:
                    desktop = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='desktop_submissions'").fetchone()
                    metadata = self.db.execute("SELECT context_json FROM desktop_submissions WHERE observation_id=?", (row["observation_id"],)).fetchone() if desktop else None
                if metadata:
                    entry["desktop_context"] = json.loads(metadata[0])
            if row["message_type"] == "text" and row["content"]:
                try:
                    decoded = json.loads(row["content"])
                    if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
                        entry["content"] = decoded["text"]
                except ValueError:
                    pass
            result.append(entry)
        return result

    def current_activities(self):
        with self.lock:
            return [{**json.loads(r["body"]), "activity_id": r["activity_id"], "revision": r["revision"]}
                    for r in self.db.execute("SELECT * FROM activities WHERE current_valid=1")]

    def claim_job(self, types=None, lease_ms=600000):
        now = now_ms()
        with self.transaction() as db:
            where = "(status IN ('pending','retry') AND next_at_ms<=? OR status='inflight' AND lease_until_ms<=?)"
            args = [now, now]
            if types:
                where += " AND job_type IN (" + ",".join("?" for _ in types) + ")"
                args.extend(types)
            row = db.execute("SELECT * FROM jobs WHERE " + where + " ORDER BY next_at_ms LIMIT 1", args).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='inflight',lease_until_ms=?,attempts=attempts+1 WHERE job_id=?", (now + lease_ms, row["job_id"]))
            return {**dict(row), "payload": json.loads(row["payload"]), "attempts": row["attempts"] + 1}

    def finish_job(self, job_id, status="done", error_class=None, remote_id=None):
        if status not in {"done", "retry", "conflict", "dead", "outcome_unknown", "superseded"}:
            raise ValueError("job_status")
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            delay = min(3600000, 1000 * 2 ** min(row[0], 12))
            db.execute("UPDATE jobs SET status=?,next_at_ms=?,lease_until_ms=NULL,last_error_class=?,remote_id=COALESCE(?,remote_id) WHERE job_id=?",
                       (status, now_ms() + delay, error_class, remote_id, job_id))

    def save_proposal(self, body, evidence, model_id, prompt_version):
        input_hash = digest({"evidence": [(e["id"], e["content_hash"]) for e in evidence],
                             "context": [(b["activity_id"], b["revision"]) for b in self.current_activities()]})
        with self.transaction() as db:
            existing = db.execute("SELECT proposal_id FROM proposals WHERE input_hash=? AND model_id=? AND prompt_version=?", (input_hash, model_id, prompt_version)).fetchone()
            if existing:
                return existing[0]
            pid = str(uuid.uuid4())
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?)", (pid, input_hash, model_id, prompt_version, now_ms(), packed(body), "proposed"))
            return pid

    def accept_proposal(self, proposal_id, *, enqueue_base_sync=True):
        from .proposal import validate_proposal
        with self.transaction() as db:
            p = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if not p:
                raise KeyError(proposal_id)
            if p["status"] == "accepted":
                return []
            body = json.loads(p["body"])
            refs = set()
            for block in body["blocks"]:
                refs.update(block["evidence_refs"])
            for question in body["questions"]:
                refs.update(question["evidence_refs"])
            evidence = self.evidence(list(refs)) if refs else []
            validate_proposal(body, evidence, self.current_activities())
            changed = []
            for block in body["blocks"]:
                if any(item["is_acceptance_test"] for item in self.evidence(block["evidence_refs"])):
                    block["_acceptance_test"] = True
                aid = block.get("activity_id") or str(uuid.uuid4())
                previous = db.execute("SELECT revision,body FROM activities WHERE activity_id=? AND current_valid=1", (aid,)).fetchone()
                revision = previous[0] + 1 if previous else 1
                db.execute("UPDATE activities SET current_valid=0 WHERE activity_id=?", (aid,))
                db.execute("INSERT INTO activities VALUES(?,?,?,?,1,?)", (aid, revision, proposal_id, packed(block), now_ms()))
                if enqueue_base_sync:
                    self.enqueue(db, "base_sync", f"base:{aid}:{revision}", {"activity_id": aid, "revision": revision})
                changed.append(aid)
                from .reporting import affected_days
                old_block = json.loads(previous[1]) if previous else None
                for day in affected_days([b for b in (old_block, block) if b], self):
                    db.execute("INSERT OR REPLACE INTO report_dirty VALUES(?,?)", (day, now_ms()))
            for question in body["questions"]:
                db.execute("INSERT OR IGNORE INTO questions(question_id,semantic_key,proposal_id,body) VALUES(?,?,?,?)",
                           (str(uuid.uuid4()), question["semantic_key"], proposal_id, packed(question)))
            db.execute("UPDATE proposals SET status='accepted' WHERE proposal_id=?", (proposal_id,))
            return changed

    def resolve_question(self, question_id, answer_observation_id, skipped=False):
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM observations WHERE observation_id=?", (answer_observation_id,)).fetchone():
                raise ValueError("answer_evidence_missing")
            changed = db.execute("UPDATE questions SET status=?,answer_observation_id=? WHERE question_id=? AND status='pending'",
                                 ("skipped" if skipped else "answered", answer_observation_id, question_id)).rowcount
            return bool(changed)

    def backup(self, destination):
        target = Path(destination).resolve()
        if target == self.path.resolve() or target.exists():
            raise ValueError("backup_requires_new_independent_target")
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(target)
        try:
            with self.lock:
                self.db.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup_integrity_failed")
            manifest = {"created_at_ms": now_ms(), "schema_version": 1,
                        "messages": dest.execute("SELECT count(*) FROM messages").fetchone()[0],
                        "observations": dest.execute("SELECT count(*) FROM observations").fetchone()[0],
                        "message_hash": digest(dest.execute("SELECT message_key,first_content,message_created_at_ms FROM messages ORDER BY message_key").fetchall()),
                        "revision_hash": digest(dest.execute("SELECT activity_id,revision,body,current_valid FROM activities ORDER BY activity_id,revision").fetchall()),
                        "jobs_hash": digest(dest.execute("SELECT job_id,status,payload FROM jobs ORDER BY job_id").fetchall()),
                        "watermark_hash": digest(dest.execute("SELECT * FROM scans ORDER BY chat_id").fetchall())}
        finally:
            dest.close()
        target.with_suffix(target.suffix + ".manifest.json").write_text(packed(manifest), encoding="utf-8")
        return manifest

    def health(self):
        with self.lock:
            counts = dict(self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status"))
            oldest = self.db.execute("SELECT min(next_at_ms) FROM jobs WHERE status IN ('pending','retry','inflight')").fetchone()[0]
            latest = self.db.execute("SELECT max(message_created_at_ms) FROM messages").fetchone()[0]
            scans = [dict(row) for row in self.db.execute("SELECT status,completed_through_ms,last_error_class FROM scans ORDER BY completed_through_ms DESC LIMIT 5")]
        latencies = sorted(self.commit_latencies_ms)
        return {"last_event_at": latest, "last_commit_at": self.get_setting("last_commit_at"),
                "jobs": counts, "recovery_state": scans, "oldest_pending_age_ms": None if oldest is None else max(0, now_ms() - oldest),
                "commit_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * .95))] if latencies else None}
