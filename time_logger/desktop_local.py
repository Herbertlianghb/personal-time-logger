"""Durable desktop draft, transport queue and replaceable history cache.

This is not an activity ledger. Only the server/formal Base owns activities.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def ms():
    return time.time_ns() // 1_000_000


class LocalState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=EXTRA")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS submissions(
            id TEXT PRIMARY KEY,body TEXT NOT NULL,created_at_ms INTEGER NOT NULL,
            stage TEXT NOT NULL DEFAULT 'local_saved',response TEXT,error TEXT);
          CREATE TABLE IF NOT EXISTS history(day TEXT PRIMARY KEY,body TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def preference(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_preference(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def submit(self, text, **context):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 16000:
            raise ValueError("invalid_submission")
        if not text.strip() and context.get("disposition") != "skipped":
            raise ValueError("empty_submission")
        sid, created = str(uuid.uuid4()), ms()
        body = {"client_submission_id": sid, "reported_at_ms": created, "text": text, **context}
        with self.lock, self.db:
            self.db.execute("INSERT INTO submissions(id,body,created_at_ms) VALUES(?,?,?)",
                            (sid, json.dumps(body, ensure_ascii=False), created))
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES('draft','\"\"')")
        return body

    def queued(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM submissions WHERE stage='local_saved' ORDER BY created_at_ms LIMIT 30")]

    def list_submissions(self, limit=100):
        with self.lock:
            return [{**json.loads(row["body"]), "stage": row["stage"],
                     "response": json.loads(row["response"]) if row["response"] else None,
                     "error": row["error"]} for row in self.db.execute(
                         "SELECT * FROM submissions ORDER BY created_at_ms DESC LIMIT ?", (limit,))]

    def acknowledge(self, sid, response):
        if response.get("stage") not in {"server_saved", "organized", "archived", "needs_review"}:
            raise ValueError("server_did_not_confirm_save")
        with self.lock, self.db:
            row = self.db.execute("SELECT stage,response FROM submissions WHERE id=?", (sid,)).fetchone()
            old = json.loads(row["response"]) if row and row["response"] else {}
            if response.get("status_as_of_ms", 0) < old.get("status_as_of_ms", 0):
                return
            rank = {"local_saved": 0, "server_saved": 1, "organized": 2, "archived": 3}
            if row and response["stage"] in rank and row["stage"] in rank and rank[response["stage"]] < rank[row["stage"]]:
                return
            self.db.execute("UPDATE submissions SET stage=?,response=?,error=NULL WHERE id=?",
                            (response["stage"], json.dumps(response, ensure_ascii=False), sid))

    def bind_target(self, sid, activity_id, revision):
        with self.lock, self.db:
            row = self.db.execute("SELECT body FROM submissions WHERE id=? AND stage='local_saved'", (sid,)).fetchone()
            if not row:
                raise ValueError("queued_submission_not_found")
            body = json.loads(row[0])
            body.update(activity_id=activity_id, expected_revision=revision)
            self.db.execute("UPDATE submissions SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), sid))
            return body

    def failed(self, sid, category, *, permanent=False):
        with self.lock, self.db:
            self.db.execute("UPDATE submissions SET error=?,stage=CASE WHEN ? THEN 'local_needs_review' ELSE stage END WHERE id=?", (category, permanent, sid))

    def cache(self, day):
        with self.lock:
            row = self.db.execute("SELECT body FROM history WHERE day=?", (day,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_history(self, snapshot):
        if not isinstance(snapshot, dict) or not snapshot.get("as_of_ms") or not snapshot.get("date"):
            raise ValueError("incomplete_history_response")
        if snapshot.get("cache", {}).get("stale") or snapshot.get("cache", {}).get("state") == "stale" or snapshot.get("error"):
            return False  # failed remote read cannot erase the last good version
        with self.lock, self.db:
            old = self.db.execute("SELECT body FROM history WHERE day=?", (snapshot["date"],)).fetchone()
            if old and json.loads(old[0]).get("as_of_ms", 0) > snapshot["as_of_ms"]:
                return False
            self.db.execute("INSERT OR REPLACE INTO history VALUES(?,?)", (snapshot["date"], json.dumps(snapshot, ensure_ascii=False)))
        return True
