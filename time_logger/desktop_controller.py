"""Quiet background uploads; refreshing never invokes an interpreter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time

from PySide6.QtCore import QObject, Signal

from .desktop_local import LocalState
from .desktop_transport import DeviceTransport, SyncUnavailable

LOCAL_TZ = timezone(timedelta(hours=8))
MIRROR_PENDING = frozenset({"queued", "sending", "retry"})


def chat_mirror_status(response):
    state = response.get("chat_mirror_state")
    if state == "sent":
        return "已同步到飞书聊天"
    if state in MIRROR_PENDING:
        return "聊天同步中"
    if state in {"outcome_unknown", "blocked"}:
        return "聊天同步待确认，原文已保存"
    return ""


class DesktopController(QObject):
    snapshot = Signal(dict)
    state = Signal(str)
    pending = Signal(list)
    questions = Signal(list)
    sync_busy = Signal(bool)
    heatmap_snapshot = Signal(dict)
    heatmap_state = Signal(str)
    daily_progress = Signal(dict)
    daily_progress_state = Signal(str)

    def __init__(self, config, state_path, transport=None):
        super().__init__()
        self.local = LocalState(state_path)
        self.transport = transport or DeviceTransport(config)
        self.day = datetime.now(LOCAL_TZ).date().isoformat()
        self.visible = False
        self.wakeup = threading.Event()
        self.closed = threading.Event()
        self.thread = None
        self.refresh_requested = False
        self.refresh_generation = 0
        self.last_snapshot = None
        self.remote_pending = []
        self.mirror_watch = {}
        self.mirror_next_poll = 0
        self._retry_lock = threading.Lock()
        self._retry_requested = False
        self.heatmap_month = None
        self.heatmap_scope = "month"
        self.heatmap_visible = False
        self.heatmap_requested = False
        self.heatmap_generation = 0
        self.heatmap_force = False
        self.daily_progress_requested = True
        self.daily_progress_refreshed_at = 0

    def get_draft(self):
        return self.local.preference("draft", "")

    def set_draft(self, text):
        self.local.set_preference("draft", text)

    def get_ui_state(self):
        return self.local.preference("ui_state", {})

    def set_ui_state(self, value):
        self.local.set_preference("ui_state", value)

    def submit(self, text, activity_id=None, question_id=None, answer_action=None, record_id=None, expected_fingerprint=None):
        context = {}
        if activity_id:
            activities = (self.last_snapshot or {}).get("activities", [])
            item = next((item for item in activities if item.get("activity_id") == activity_id), None)
            if not item or not isinstance(item.get("revision"), int):
                raise ValueError("该活动暂不能自动更正，请在原文中写明日期与活动")
            context.update(activity_id=activity_id, expected_revision=item["revision"])
        if record_id:
            context.update(record_id=record_id, expected_fingerprint=expected_fingerprint)
        if question_id:
            context.update(question_id=question_id, disposition="skipped" if answer_action in {"skip", "skipped"} else "answered")
        self.local.submit(text, **context)
        self.state.emit("已存本机，待上传")
        self._emit_pending()
        self.wakeup.set()
        return True

    def _emit_pending(self):
        own = self.local.list_submissions()
        ids = {item["response"].get("observation_id") for item in own if item.get("response")}
        queued = [{**item, "source": "desktop", "text": item["text"],
                   "chat_mirror_status": chat_mirror_status(item.get("response") or {})} for item in own
                  if item["stage"] != "archived"
                  and not (item["stage"] == "server_saved" and (item.get("response") or {}).get("processing_enabled") is False)
                  and not (item["stage"] == "organized" and (item.get("response") or {}).get("base_required") is False)]
        queued.extend(item for item in self.remote_pending if item.get("observation_id") not in ids)
        self.pending.emit(queued)

    def latest_save_status(self):
        latest = self.local.list_submissions(limit=1)
        if not latest:
            return "已读取最新结果；记录后会显示保存状态"
        item = latest[0]
        stage = item["stage"]
        if stage == "organized":
            base = "已整理，等待飞书核验" if (item.get("response") or {}).get("base_required") else "已整理"
        else:
            base = {"local_saved": "已存本机，待上传", "server_saved": "服务器已保存，待整理",
                    "archived": "已归档，飞书已核验", "needs_review": "服务器已保存原文，更正对象有更新",
                    "local_needs_review": "本机原文已保留，更正对象有更新"}.get(stage, "原文已保留，等待处理")
        if stage == "server_saved" and (item.get("response") or {}).get("processing_enabled") is False:
            base = "服务器已保存原文"
        mirror = chat_mirror_status(item.get("response") or {})
        return base + (" · " + mirror if mirror else "")

    def _watch_chat_mirror(self, sid, response, *, uploaded=False, now=None):
        now = time.monotonic() if now is None else now
        if response.get("chat_mirror_state") not in MIRROR_PENDING:
            self.mirror_watch.pop(sid, None)
        elif uploaded and sid not in self.mirror_watch:
            # Bound the fast watch to thirty seconds after actual upload ACK.
            # A queued response never renews this deadline, even across refreshes.
            self.mirror_watch[sid] = now + 30
            self.mirror_next_poll = min(self.mirror_next_poll or now + 5, now + 5)

    def _poll_chat_mirror(self, now=None):
        now = time.monotonic() if now is None else now
        self.mirror_watch = {sid: deadline for sid, deadline in self.mirror_watch.items() if deadline > now}
        if not self.visible or not self.mirror_watch or now < self.mirror_next_poll:
            return False
        self.mirror_next_poll = now + 5
        try:
            result = self.transport.request("status", client_submission_ids=sorted(self.mirror_watch)[:100])
            for item in result.get("submissions", []):
                sid = item["client_submission_id"]
                self.local.acknowledge(sid, {**item, "status_as_of_ms": result["server_now_ms"]})
                self._watch_chat_mirror(sid, item, now=now)
            self.state.emit(self.latest_save_status())
            self._emit_pending()
            return True
        except Exception:
            # A failed chat-status read cannot revoke the durable save receipt,
            # alter the queue, or put ordinary uploads into global backoff.
            self.state.emit(self.latest_save_status() + "（聊天状态查询暂时受阻，原文已保存）")
            return False

    def load_cached(self, day):
        cached = self.local.cache(day)
        if cached:
            cached = {**cached, "cache": {**cached.get("cache", {}), "local_cached": True, "stale": True}}
            self.last_snapshot = cached
            self.snapshot.emit(cached)
        return cached

    def refresh(self, day):
        self.day = str(day)
        self.load_cached(self.day)
        self.refresh_requested = True
        self.refresh_generation += 1
        self.daily_progress_requested = True
        self.wakeup.set()

    def request_heatmap(self, month, *, refresh=False, scope="month"):
        # Reuse the existing cache DB and serial network worker. This cache
        # contains derived display data, never a second activity ledger.
        self.heatmap_month = str(month)
        if scope not in {"month", "year"}:
            raise ValueError("invalid_heatmap_scope")
        self.heatmap_scope = scope
        self.heatmap_visible = True
        self.heatmap_requested = True
        self.heatmap_force = bool(refresh)
        self.heatmap_generation += 1
        key = "heatmap:" + ("year:" if scope == "year" else "") + self.heatmap_month
        cached = self.local.preference(key)
        if cached:
            self.heatmap_snapshot.emit({**cached, "cache": {**cached.get("cache", {}), "state": "stale", "local_cached": True}})
        self.heatmap_state.emit("正在读取统计范围内的记录…")
        self.wakeup.set()

    def set_heatmap_visible(self, visible):
        self.heatmap_visible = bool(visible)

    def _refresh_heatmap(self):
        month, scope, generation = self.heatmap_month, self.heatmap_scope, self.heatmap_generation
        result = self.transport.request("heatmap", month=month, scope=scope, refresh=self.heatmap_force)
        value = result.get("heatmap", result)
        if value.get("month") != month or not isinstance(value.get("days"), list):
            raise ValueError("heatmap_month_mismatch")
        if value.get("scope", "month") != scope:
            raise ValueError("heatmap_scope_not_supported")
        if value.get("as_of_ms") and value.get("cache", {}).get("state") != "stale" and not value.get("error"):
            key = "heatmap:" + ("year:" if scope == "year" else "") + month
            self.local.set_preference(key, value)
        if month == self.heatmap_month and scope == self.heatmap_scope:
            self.heatmap_snapshot.emit(value)
            self.heatmap_state.emit("")
        self._publish_daily_progress(value)
        self.heatmap_requested = generation != self.heatmap_generation
        if not self.heatmap_requested:
            self.heatmap_force = False

    def _publish_daily_progress(self, value):
        today = datetime.now(LOCAL_TZ).date().isoformat()
        if not any(item.get("date") == today for item in value.get("days", [])):
            return False
        self.daily_progress.emit(value)
        cache = value.get("cache", {})
        if value.get("error") or cache.get("stale") or cache.get("local_cached") or cache.get("state") == "stale":
            self.daily_progress_state.emit("缓存待刷新")
        else:
            self.daily_progress_requested = False
            self.daily_progress_refreshed_at = time.monotonic()
            self.daily_progress_state.emit("")
        return True

    def _refresh_daily_progress(self):
        month = datetime.now(LOCAL_TZ).strftime("%Y-%m")
        result = self.transport.request("heatmap", month=month, scope="month", refresh=False)
        value = result.get("heatmap", result)
        if value.get("month") != month or not isinstance(value.get("days"), list):
            raise ValueError("daily_progress_month_mismatch")
        if not self._publish_daily_progress(value):
            raise ValueError("daily_progress_day_missing")
        if value.get("as_of_ms") and not value.get("error") and value.get("cache", {}).get("state") != "stale":
            self.local.set_preference("heatmap:" + month, value)

    def set_visible(self, value):
        self.visible = bool(value)
        if value:
            month = datetime.now(LOCAL_TZ).strftime("%Y-%m")
            cached = self.local.preference("heatmap:" + month)
            if cached:
                self._publish_daily_progress({**cached, "cache": {**cached.get("cache", {}), "local_cached": True}})
            self.daily_progress_requested = True
            self.refresh_requested = True
            self.refresh_generation += 1
            self.wakeup.set()

    def retry_sync(self):
        """Request one immediate worker pass without touching its connection."""
        with self._retry_lock:
            if self.closed.is_set() or self._retry_requested:
                return False
            self._retry_requested = True
        self.wakeup.set()
        return True

    def start(self):
        if self.thread:
            return
        self._emit_pending()
        self.thread = threading.Thread(target=self._run, name="desktop-sync", daemon=True)
        self.thread.start()

    def stop(self):
        self.closed.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=1)
        # A blocked network call may complete after quit. Its durable local
        # queue survives; avoid closing a connection under its worker thread.
        if not self.thread or not self.thread.is_alive():
            self.transport.close()
            self.local.close()

    def _refresh_history(self):
        requested_day = self.day
        requested_generation = self.refresh_generation
        own_ids = [item["client_submission_id"] for item in self.local.list_submissions()]
        result = self.transport.request("snapshot", client_submission_ids=own_ids)
        for item in result.get("submissions", []):
            self.local.acknowledge(item["client_submission_id"], {**item, "status_as_of_ms": result["server_now_ms"]})
            self._watch_chat_mirror(item["client_submission_id"], item)
        self.remote_pending = result.get("pending", [])
        self.questions.emit(result.get("questions", []))
        history = self.transport.request("history", date=requested_day)
        value = history.get("history", history)
        if value.get("date") != requested_day:
            raise ValueError("history_date_mismatch")
        while value.get("next_cursor"):
            page = self.transport.request("history", date=requested_day, cursor=value["next_cursor"])
            page = page.get("history", page)
            if page.get("data_revision") != value.get("data_revision") or page.get("date") != requested_day:
                raise ValueError("history_page_version_changed")
            value["items"] = value.get("items", value.get("activities", [])) + page.get("items", page.get("activities", []))
            value["activities"] = value["items"]
            value["next_cursor"] = page.get("next_cursor")
            if len(value["items"]) > 20000:
                raise ValueError("history_page_bound")
        if not self.local.save_history(value):
            cached = self.local.cache(requested_day)
            if cached and cached.get("as_of_ms", 0) > value.get("as_of_ms", 0):
                value = {**cached, "cache": {**cached.get("cache", {}), "stale": True, "local_cached": True}}
        if requested_day == self.day:
            self.last_snapshot = value
            self.snapshot.emit(value)
        self.refresh_requested = requested_day != self.day or requested_generation != self.refresh_generation
        self.state.emit(self.latest_save_status())

    def _run(self):
        last_refresh = upload_retry_after = refresh_retry_after = heatmap_retry_after = progress_retry_after = 0
        upload_failures = refresh_failures = heatmap_failures = progress_failures = 0
        while not self.closed.is_set():
            self.wakeup.clear()
            now = time.monotonic()
            with self._retry_lock:
                retry = self._retry_requested
                self._retry_requested = False
            if retry:
                upload_retry_after = refresh_retry_after = heatmap_retry_after = progress_retry_after = 0
                self.refresh_requested = True
                self.refresh_generation += 1
                self.daily_progress_requested = True
            queued = self.local.queued() if now >= upload_retry_after else []
            refresh_due = self.visible and now >= refresh_retry_after and (
                self.refresh_requested or now - last_refresh >= 60)
            mirror_due = self.visible and now >= self.mirror_next_poll and any(
                deadline > now for deadline in self.mirror_watch.values())
            heatmap_due = self.heatmap_visible and self.heatmap_requested and now >= heatmap_retry_after
            progress_due = self.visible and now >= progress_retry_after and (
                self.daily_progress_requested or now - self.daily_progress_refreshed_at >= 60)
            if not (queued or refresh_due or mirror_due or heatmap_due or progress_due or retry):
                self.wakeup.wait(timeout=5)
                continue
            self.sync_busy.emit(True)
            try:
                uploaded = upload_failed = history_failed = False
                if queued:
                    try:
                        for body in queued:
                            try:
                                if body.get("record_id") and not body.get("activity_id"):
                                    target = self.transport.request("prepare_correction", record_id=body["record_id"], expected_fingerprint=body["expected_fingerprint"])
                                    body = self.local.bind_target(body["client_submission_id"], target["activity_id"], target["revision"])
                                op = "question_answer" if body.get("question_id") else "submit"
                                result = self.transport.request(op, **body)
                                self.local.acknowledge(body["client_submission_id"], {**result["submission"], "status_as_of_ms": result["server_now_ms"]})
                                self._watch_chat_mirror(body["client_submission_id"], result["submission"], uploaded=True)
                                uploaded = True
                                upload_failures = 0
                                upload_retry_after = 0
                                self.state.emit(self.latest_save_status())
                            except SyncUnavailable as exc:
                                if str(exc) in {"formal_record_changed", "formal_snapshot_changed", "record_changed", "record_fingerprint_conflict",
                                                "legacy_record_changed", "invalid_text", "submission_id_payload_conflict", "activity_not_found",
                                                "question_not_available", "target_revision_required", "remote_record_changed_refresh_required",
                                                "remote_record_missing", "record_fingerprint_required", "invalid_record_id", "linked_activity_missing",
                                                "activity_has_unsynced_revision", "legacy_link_mapping_missing", "invalid_reported_time",
                                                "invalid_question_id", "invalid_question_disposition", "target_activity_required", "question_id_required"}:
                                    self.local.failed(body["client_submission_id"], "活动已有更新；本机原文已保留，请重新选择更正对象", permanent=True)
                                    continue
                                self.local.failed(body["client_submission_id"], "sync_unavailable")
                                raise
                            except Exception:
                                self.local.failed(body["client_submission_id"], "sync_unavailable")
                                raise
                    except Exception:
                        upload_failed = True
                        upload_failures += 1
                        upload_retry_after = time.monotonic() + min(30, 5 * 2 ** min(upload_failures - 1, 3))
                        self.state.emit(self.latest_save_status() + "（上传暂时同步受阻；本机待上传原文保留，恢复后自动重试）")
                # A read failure has its own backoff: it cannot hold up a new
                # queued upload or revoke an already durable server receipt.
                should_refresh = not upload_failed and self.visible and time.monotonic() >= refresh_retry_after and (
                    self.refresh_requested or uploaded or time.monotonic() - last_refresh >= 60)
                if should_refresh:
                    try:
                        self._refresh_history()
                        last_refresh = time.monotonic()
                        refresh_failures = 0
                        refresh_retry_after = 0
                    except Exception:
                        history_failed = True
                        refresh_failures += 1
                        refresh_retry_after = time.monotonic() + min(30, 5 * 2 ** min(refresh_failures - 1, 3))
                        self.state.emit(self.latest_save_status() + "（历史查询暂时受阻，稍后自动重试）")
                        self.load_cached(self.day)
                elif not upload_failed:
                    self._poll_chat_mirror()
                if not upload_failed and heatmap_due:
                    try:
                        self._refresh_heatmap()
                        heatmap_failures = 0
                        heatmap_retry_after = 0
                    except Exception:
                        heatmap_failures += 1
                        heatmap_retry_after = time.monotonic() + min(30, 5 * 2 ** min(heatmap_failures - 1, 3))
                        self.heatmap_state.emit("热力图暂时无法读取，将自动重试；请确认服务端已升级且已配置时间表。已有缓存仅供参考。")
                if (not self.closed.is_set() and not upload_failed and not history_failed and self.visible
                        and not self.local.queued() and time.monotonic() >= progress_retry_after) and (
                        self.daily_progress_requested or uploaded or time.monotonic() - self.daily_progress_refreshed_at >= 60):
                    try:
                        self._refresh_daily_progress()
                        progress_failures = 0
                        progress_retry_after = time.monotonic() + 60
                    except Exception:
                        progress_failures += 1
                        progress_retry_after = time.monotonic() + min(30, 5 * 2 ** min(progress_failures - 1, 3))
                        self.daily_progress_state.emit("进度暂未更新")
                self._emit_pending()
            finally:
                self.sync_busy.emit(False)
            self.wakeup.wait(timeout=5)
        self.transport.close()
