from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .store import now_ms


class PlatformError(RuntimeError):
    def __init__(self, category, retry_after=0):
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after


class Feishu:
    def __init__(self, app_id, app_secret):
        self.app_id, self.app_secret = app_id, app_secret
        self.token, self.expires_at = None, 0

    def request(self, method, path, body=None, authenticated=True):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            if time.monotonic() >= self.expires_at:
                auth = self.request("POST", "/auth/v3/tenant_access_token/internal", {"app_id": self.app_id, "app_secret": self.app_secret}, False)
                self.token = auth["tenant_access_token"]
                self.expires_at = time.monotonic() + max(1, auth["expire"] - 120)
            headers["Authorization"] = "Bearer " + self.token
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request("https://open.feishu.cn/open-apis" + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise PlatformError("rate_limited", int(exc.headers.get("Retry-After", "60"))) from None
            if exc.code in {401, 403}:
                self.expires_at = 0
                raise PlatformError("permission_or_authentication") from None
            raise PlatformError("http_server" if exc.code >= 500 else "http_rejected") from None
        except (OSError, ValueError):
            raise PlatformError("transport_outcome_unknown" if method != "GET" else "transport_failed") from None
        if result.get("code") != 0:
            code = result.get("code")
            raise PlatformError("business_code_" + str(code))
        return result

    def receipt(self, chat_id, created_at_ms, client_token, text=None):
        from datetime import datetime
        from .calculator import LOCAL_TZ
        time_text = datetime.fromtimestamp(created_at_ms / 1000, LOCAL_TZ).strftime("%H:%M")
        content = text or f"已保存原文｜报告时间 {time_text}｜整理结果稍后更新"
        return self.request("POST", "/im/v1/messages?receive_id_type=chat_id", {
            "receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": content}, ensure_ascii=False), "uuid": client_token,
        })["data"]["message_id"]

    def history_page(self, chat_id, start_ms, end_ms, page_token=None):
        params = {"container_id_type": "chat", "container_id": chat_id, "start_time": start_ms // 1000,
                  "end_time": (end_ms + 999) // 1000, "page_size": 50, "sort_type": "ByCreateTimeAsc"}
        if page_token:
            params["page_token"] = page_token
        return self.request("GET", "/im/v1/messages?" + urllib.parse.urlencode(params))["data"]

    def send_desktop_mirror(self, chat_id, text, client_token):
        data = self.request("POST", "/im/v1/messages?receive_id_type=chat_id", {
            "receive_id": chat_id, "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False), "uuid": client_token,
        })["data"]
        return {"message_id": data.get("message_id"), "sent_at_ms": data.get("create_time")}

    def get_message(self, message_id):
        data = self.request("GET", "/im/v1/messages/" + urllib.parse.quote(message_id, safe=""))["data"]
        items = data.get("items", [])
        return items[0] if len(items) == 1 else None


def normalize_history(item, app_scope, chat_id, allowed_senders):
    sender = item.get("sender", {})
    if sender.get("sender_type") != "user" or sender.get("id") not in allowed_senders:
        return None
    deleted = item.get("deleted", False)
    return {"app_scope": app_scope, "message_id": item["message_id"], "chat_id": chat_id,
            "sender_key": sender["id"], "sender_type": "user", "message_type": item.get("msg_type", "unknown"),
            "created_at_ms": int(item["create_time"]), "updated_at_ms": int(item["update_time"]) if item.get("update_time") else None,
            "content": None if deleted else item.get("body", {}).get("content", ""),
            "source": "history", "kind": "recall" if deleted else "receive"}


def reconcile(store, platform, chat_id, initial_start_ms, fixed_end_ms=None, overlap_ms=300000, scan_key=None):
    """Complete one fixed range. Failure never advances the completion watermark."""
    scan_key = scan_key or chat_id
    with store.transaction() as db:
        state = db.execute("SELECT * FROM scans WHERE chat_id=?", (scan_key,)).fetchone()
        old_watermark = state["completed_through_ms"] if state else None
        end = (state["fixed_end_ms"] if state and state["status"] == "scanning" else None) or fixed_end_ms or now_ms()
        start = max(0, (old_watermark if old_watermark is not None else initial_start_ms) - overlap_ms)
        # Restart a failed or expired cursor from the last fully completed range.
        db.execute("INSERT INTO scans VALUES(?,?,?,NULL,'scanning',NULL) ON CONFLICT(chat_id) DO UPDATE SET fixed_end_ms=excluded.fixed_end_ms,cursor=NULL,status='scanning',last_error_class=NULL",
                   (scan_key, old_watermark, end))
    count, token, seen_tokens = 0, None, set()
    try:
        while True:
            page = platform.history_page(chat_id, start, end, token)
            for item in page.get("items", []):
                normalized = normalize_history(item, store.scope, chat_id, store.allowed_senders)
                if normalized:
                    result = store.ingest(normalized)
                    count += not result["duplicate"]
            if not page.get("has_more"):
                break
            token = page.get("page_token")
            if not token or token in seen_tokens:
                raise PlatformError("invalid_history_pagination")
            seen_tokens.add(token)
            with store.transaction() as db:
                db.execute("UPDATE scans SET cursor=? WHERE chat_id=?", (token, scan_key))
        with store.transaction() as db:
            db.execute("UPDATE scans SET completed_through_ms=?,fixed_end_ms=NULL,cursor=NULL,status='scan_success_completeness_limited',last_error_class=NULL WHERE chat_id=?", (end, scan_key))
            if count:
                store.enqueue(db, "receipt", "history-summary:" + scan_key + ":" + str(end),
                              {"chat_id": chat_id, "created_at_ms": end,
                               "text": f"已补回{count}条消息证据｜保留原平台时间｜整理结果稍后更新"})
        return {"new_observations": count, "completed_through_ms": end,
                "limitations": ["only bound authorized p2p chat", "unobserved edits and platform-deleted messages may be unrecoverable"]}
    except Exception as exc:
        with store.transaction() as db:
            db.execute("UPDATE scans SET status='failed',last_error_class=? WHERE chat_id=?", (type(exc).__name__, scan_key))
        raise
