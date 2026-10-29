from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .calculator import LOCAL_TZ, summarize
from .feishu import Feishu, PlatformError, reconcile
from .store import Store, digest, now_ms, packed


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not config.get("owner_open_id"):
        raise ValueError("owner_identity_not_verified")
    if not config.get("app_id") or not config.get("app_secret"):
        raise ValueError("application_credentials_missing")
    if config.get("base_sync") and not (config.get("base_token") and (config.get("time_table_id") or config.get("table_id"))):
        raise ValueError("base_source_not_configured")
    return config


class Service:
    def __init__(self, config):
        self.config = config
        self.store = Store(config["database"], config["app_id"], [config["owner_open_id"]], [config["chat_id"]] if config.get("chat_id") else [])
        self.platform = Feishu(config["app_id"], config["app_secret"])
        self.mirror_platform = None
        if config.get("desktop_chat_mirror", False):
            from .desktop_mirror import ensure_schema
            ensure_schema(self.store)
            self.mirror_platform = Feishu(config["app_id"], config["app_secret"])
        self.stop_event = threading.Event()
        self.ws_state = "not_started"
        self.last_error = None
        self.base_adapter = None
        self.initial_start = self.store.get_setting("initial_start_ms")
        if self.initial_start is None:
            self.initial_start = config.get("history_start_ms", now_ms())
            self.store.set_setting("initial_start_ms", self.initial_start)
        if config.get("chat_id") and config.get("binding_verified"):
            self.store.set_setting("bound_chat_id", config["chat_id"])
            self.store.set_setting("binding_receipt_sent", True)

    def receive(self, event):
        message, sender = event.event.message, event.event.sender
        if message.chat_type != "p2p" or sender.sender_type != "user":
            return
        if sender.sender_id.open_id != self.config["owner_open_id"]:
            return
        if not self.config.get("ingest", True):
            raise RuntimeError("ingestion_disabled")
        result = self.store.ingest({"app_scope": self.config["app_id"], "message_id": message.message_id,
            "chat_id": message.chat_id, "sender_key": sender.sender_id.open_id, "sender_type": sender.sender_type,
            "message_type": message.message_type, "created_at_ms": int(message.create_time),
            "updated_at_ms": int(message.update_time) if getattr(message, "update_time", None) else None,
            "content": message.content, "source": "ws", "kind": "receive",
            "event_id": event.header.event_id})
        if not self.store.get_setting("bound_chat_id"):
            self.store.set_setting("bound_chat_id", message.chat_id)
        return None

    def recalled(self, event):
        chat = self.store.get_setting("bound_chat_id")
        if not chat or event.event.chat_id != chat:
            return
        from .desktop_mirror import mark_recalled_mirror
        if mark_recalled_mirror(self.store, event.event.message_id):
            return
        with self.store.lock:
            known_user_message = self.store.db.execute("SELECT 1 FROM messages WHERE app_scope=? AND message_id=?", (self.store.scope, event.event.message_id)).fetchone()
        if not known_user_message:
            # Recall events omit sender identity. An unknown bot projection
            # must not be relabelled as a user report. History supplies the
            # authenticated sender and can recover a recall-first user event.
            with self.store.transaction() as db:
                self.store.enqueue(db, "reconcile", "unknown-recall:" + event.event.message_id, {"chat_id": chat})
            return
        self.store.ingest({"app_scope": self.config["app_id"], "message_id": event.event.message_id,
            "chat_id": chat, "sender_key": self.config["owner_open_id"], "created_at_ms": None,
            "updated_at_ms": int(event.event.recall_time), "content": None,
            "source": "ws", "kind": "recall", "event_id": event.header.event_id})

    def write_health(self):
        health = self.store.health()
        health.update({"as_of_ms": now_ms(), "ws_state": self.ws_state, "last_error_class": self.last_error,
                       "bound": bool(self.store.get_setting("binding_receipt_sent")),
                       "disk_free_bytes": shutil.disk_usage(Path(self.config["database"]).parent).free,
                       "last_backup_at": self.store.get_setting("last_backup_at"),
                       "model_enabled": bool(self.config.get("parse", False)),
                       "base_sync_enabled": bool(self.config.get("base_sync", False)),
                       "desktop_chat_mirror_enabled": bool(self.config.get("desktop_chat_mirror", False)),
                       "notion_sync_enabled": bool(self.config.get("notion_sync", False))})
        health["base_sync_state"] = self.store.get_setting("base_sync_state")
        health["desktop_chat_mirror_state"] = self.store.get_setting("desktop_chat_mirror_state")
        path = Path(self.config.get("health_file", str(self.config["database"]) + ".health.json"))
        temp = path.with_suffix(".tmp")
        temp.write_text(packed(health), encoding="utf-8")
        os.replace(temp, path)

    def process_receipt(self, job):
        p = job["payload"]
        try:
            remote_id = self.platform.receipt(p["chat_id"], p["created_at_ms"], job["client_token"], text=p.get("text"))
            self.store.finish_job(job["job_id"], remote_id=remote_id)
            self.store.set_setting("binding_receipt_sent", True)
        except PlatformError as exc:
            status = "outcome_unknown" if exc.category == "transport_outcome_unknown" else "retry"
            self.store.finish_job(job["job_id"], status, exc.category)
            self.last_error = exc.category

    def queue_questions(self):
        if not self.config.get("parse", False):
            return
        chat = self.store.get_setting("bound_chat_id")
        if not chat:
            return
        day = datetime.now(LOCAL_TZ).date().isoformat()
        if self.store.get_setting("question_round_day") == day:
            return
        with self.store.transaction() as db:
            rows = db.execute("SELECT question_id,body FROM questions WHERE status='pending' AND asked_at_ms IS NULL ORDER BY rowid LIMIT 2").fetchall()
            if not rows:
                return
            questions = [json.loads(row["body"]) for row in rows]
            text = "有两处需要确认：" if len(rows) == 2 else "有一处需要确认："
            text += "\n" + "\n".join(str(i + 1) + ". " + q["text"] + "（要确认的是：" + q["ambiguity"] + "）" for i, q in enumerate(questions))
            text += "\n直接回复即可；不知道、先不管或合在一起算也可以。"
            self.store.enqueue(db, "question", "question-round:" + day,
                               {"chat_id": chat, "created_at_ms": now_ms(), "text": text,
                                "question_ids": [row["question_id"] for row in rows]})
            for row in rows:
                db.execute("UPDATE questions SET asked_at_ms=? WHERE question_id=?", (now_ms(), row["question_id"]))
            db.execute("INSERT OR REPLACE INTO settings VALUES('question_round_day',?)", (packed(day),))

    def process_question(self, job):
        p = job["payload"]
        # Another input (including the desktop) may answer while delivery is
        # queued/retrying. Never send a resolved question again.
        question_ids = p.get("question_ids", [])
        if question_ids:
            with self.store.lock:
                marks = ",".join("?" for _ in question_ids)
                rows = self.store.db.execute("SELECT question_id,body FROM questions WHERE status='pending' AND question_id IN (" + marks + ")", question_ids).fetchall()
            if not rows:
                self.store.finish_job(job["job_id"], "superseded")
                return
            if len(rows) != len(question_ids):
                pending = {row["question_id"]: json.loads(row["body"]) for row in rows}
                texts = [pending[qid] for qid in question_ids if qid in pending]
                text = "有两处需要确认：" if len(texts) == 2 else "有一处需要确认："
                text += "\n" + "\n".join(str(i + 1) + ". " + q["text"] + "（要确认的是：" + q["ambiguity"] + "）" for i, q in enumerate(texts))
                text += "\n直接回复即可；不知道、先不管或合在一起算也可以。"
                p = {**p, "text": text}
        try:
            mid = self.platform.receipt(p["chat_id"], p["created_at_ms"], job["client_token"], text=p["text"])
            self.store.finish_job(job["job_id"], remote_id=mid)
        except PlatformError as exc:
            self.store.finish_job(job["job_id"], "outcome_unknown" if exc.category == "transport_outcome_unknown" else "retry", exc.category)
            self.last_error = exc.category

    def worker(self):
        next_history = 0
        next_health = 0
        while not self.stop_event.is_set():
            job = None
            try:
                job = self.store.claim_job(["receipt", "reconcile", "question"])
                if job and job["job_type"] == "receipt":
                    self.process_receipt(job)
                elif job and job["job_type"] == "question":
                    self.process_question(job)
                elif job:
                    try:
                        reconcile(self.store, self.platform, job["payload"]["chat_id"], self.initial_start)
                        self.store.finish_job(job["job_id"])
                    except Exception as exc:
                        self.store.finish_job(job["job_id"], "retry", type(exc).__name__)
                self.queue_questions()
                chat = self.store.get_setting("bound_chat_id")
                if chat and time.monotonic() >= next_history:
                    try:
                        reconcile(self.store, self.platform, chat, self.initial_start)
                        local_day = datetime.now(LOCAL_TZ).date().isoformat()
                        if self.store.get_setting("last_wide_scan_day") != local_day:
                            reconcile(self.store, self.platform, chat, max(self.initial_start, now_ms() - 48 * 3600000),
                                      scan_key=chat + ":wide:" + local_day)
                            self.store.set_setting("last_wide_scan_day", local_day)
                        self.last_error = None
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                    next_history = time.monotonic() + self.config.get("history_interval_seconds", 3600)
                if job or time.monotonic() >= next_health:
                    self.write_health()
                    next_health = time.monotonic() + 30
            except Exception as exc:
                self.last_error = type(exc).__name__
            self.stop_event.wait(.5 if job else 2)

    def sync_worker(self):
        from .base_adapter import BaseAdapter, HttpBaseTransport
        while not self.stop_event.is_set():
            job = None
            try:
                job = self.store.claim_job(["base_sync"])
                if job:
                    if self.base_adapter is None:
                        transport = HttpBaseTransport(self.platform,
                            self.config["base_token"],
                            self.config.get("time_table_id") or self.config["table_id"], allow_formal_write=True)
                        self.base_adapter = BaseAdapter(self.store, transport, transport.field_schema())
                    p = job["payload"]
                    result = self.base_adapter.sync(p["activity_id"], p["revision"], dry_run=False)
                    status = result["status"]
                    job_status = "done" if status in {"synced", "test_excluded"} else "superseded" if status == "superseded" else "retry" if status == "outcome_unknown" else "conflict"
                    self.store.finish_job(job["job_id"], job_status, None if status == "synced" else result.get("reason", status))
                    self.store.set_setting("base_sync_state", {"as_of_ms": now_ms(), "status": status,
                                         "reason": result.get("reason"), "activity_id": p["activity_id"]})
            except Exception as exc:
                self.last_error = type(exc).__name__
                if job:
                    self.store.finish_job(job["job_id"], "retry", type(exc).__name__)
            self.stop_event.wait(.5 if job else 5)

    def mirror_worker(self):
        from .desktop_mirror import JOB_TYPE, process_mirror
        while not self.stop_event.is_set():
            job = None
            try:
                job = self.store.claim_job([JOB_TYPE], lease_ms=120000)
                if job:
                    process_mirror(self.store, self.config, self.mirror_platform, job)
                    with self.store.lock:
                        mirror = self.store.db.execute("SELECT state,last_error FROM desktop_chat_mirrors WHERE observation_id=?", (job["payload"]["observation_id"],)).fetchone()
                    self.store.set_setting("desktop_chat_mirror_state", {"as_of_ms": now_ms(),
                                           "state": mirror["state"] if mirror else "blocked",
                                           "last_error": mirror["last_error"] if mirror else "mirror_mapping_missing"})
            except Exception as exc:
                # Delivery trouble is independent of ingest/model/Base health.
                if job:
                    try:
                        self.store.finish_job(job["job_id"], "retry", type(exc).__name__)
                    except Exception:
                        pass
            self.stop_event.wait(.5 if job else 2)

    def run(self):
        # Imports only in the network receiver, keeping offline/store operations small.
        import lark_oapi as lark
        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self.receive).register_p2_im_message_recalled_v1(self.recalled).build()
        service = self

        class Client(lark.ws.Client):
            async def _connect(self):
                await super()._connect()
                service.ws_state = "connected"

        # The SDK may log event payload at DEBUG; keep all SDK messages off.
        logging.getLogger("Lark").setLevel(logging.CRITICAL)
        self.ws_state = "connecting"
        thread = threading.Thread(target=self.worker, name="outbox", daemon=True)
        thread.start()
        base_thread = None
        if self.config.get("base_sync", False):
            base_thread = threading.Thread(target=self.sync_worker, name="base-sync", daemon=True)
            base_thread.start()
        mirror_thread = None
        if self.config.get("desktop_chat_mirror", False):
            mirror_thread = threading.Thread(target=self.mirror_worker, name="desktop-chat-mirror", daemon=True)
            mirror_thread.start()
        client = Client(self.config["app_id"], self.config["app_secret"], event_handler=handler, log_level=lark.LogLevel.ERROR)
        logging.getLogger("Lark").setLevel(logging.CRITICAL)
        client.on_reconnecting = lambda: setattr(service, "ws_state", "reconnecting")
        def reconnected():
            service.ws_state = "connected"
            chat = service.store.get_setting("bound_chat_id")
            if chat:
                with service.store.transaction() as db:
                    service.store.enqueue(db, "reconcile", "reconnect:" + str(now_ms()), {"chat_id": chat})
        client.on_reconnected = reconnected
        try:
            client.start()
        finally:
            self.stop_event.set()
            thread.join(20)
            if base_thread:
                base_thread.join(20)
            if mirror_thread:
                mirror_thread.join(20)
            self.store.close()


def interpret_pending(config):
    from .model_runner import CodexInterpreter
    from .proposal import validate_proposal
    store = Store(config["database"], config["app_id"], [config["owner_open_id"]])
    try:
        pending = []
        while len(pending) < 50:
            job = store.claim_job(["interpret"])
            if not job:
                break
            pending.append(job)
        if not pending:
            return {"processed": 0}
        # A bounded context includes previous current observations for merge/correction.
        evidence = store.evidence(limit=100)
        normalized = []
        for item in evidence:
            if item["message_type"] == "text" and item["created_at_ms"] is not None:
                try:
                    content = json.loads(item["content"] or "{}").get("text", item["content"])
                except ValueError:
                    content = item["content"]
                normalized.append({**item, "content": content or ""})
        interpreter = CodexInterpreter(executable=config.get("codex_executable", "codex"))
        try:
            proposal = interpreter.interpret(normalized, store.current_activities(), config.get("categories", []), config.get("projects", []))
            validate_proposal(proposal, normalized, store.current_activities())
            pid = store.save_proposal(proposal, evidence, interpreter.model, interpreter.last_run_metadata["prompt_version"])
            if config.get("accept_model_proposals", False):
                store.accept_proposal(pid)
            for job in pending:
                store.finish_job(job["job_id"])
            return {"processed": len(pending), "proposal_id": pid, "accepted": bool(config.get("accept_model_proposals", False)),
                    "model_run": interpreter.last_run_metadata}
        except Exception as exc:
            for job in pending:
                store.finish_job(job["job_id"], "retry", type(exc).__name__)
            raise
    finally:
        store.close()
