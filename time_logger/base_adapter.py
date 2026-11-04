"""Conservative, auditable projection into the existing time table.

Time columns preserve known activity boundaries; they do not prove continuous
human effort.  Explicit human intervals remain separate in the activity model.
The CLI has no create-idempotency flag: UUIDs are retained locally, while
uncertain creates are recovered by the durable note marker and never reissued.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from .calculator import LOCAL_TZ, interval_union
from .store import digest, now_ms, packed


ALLOWLIST = {
    "时间花销": "text", "备注": "text", "结果记录": "text",
    "开始时间": "datetime", "结束时间": "datetime",
    "一级分类": "select", "二级项目": "select", "维度": "select",
    "状态": "number", "AI 赋能比例": "number", "工作量": "number",
    "工作量单位": "text", "复盘": "text",
}
REQUIRED = {"时间花销", "备注", "开始时间", "结束时间"}
_TABLE_LOCKS = {}
_TABLE_LOCKS_GUARD = threading.Lock()


class BaseSyncError(RuntimeError):
    pass


class TransportError(BaseSyncError):
    def __init__(self, code, outcome_unknown=False):
        super().__init__(str(code))
        self.outcome_unknown = outcome_unknown


def _schema_fields(schema):
    fields = schema.get("fields", []) if isinstance(schema, dict) else schema
    if not isinstance(fields, list):
        raise BaseSyncError("invalid_field_schema")
    return {f["name"]: f for f in fields}


def _schema_fingerprint(schema, *, include_options=True):
    return digest([{k: f.get(k) for k in ("id", "name", "type", "multiple")}
                   | ({"options": sorted(o["name"] for o in f.get("options", []))} if include_options else {})
                   for f in sorted(_schema_fields(schema).values(), key=lambda f: f["name"])
                   if f["name"] in ALLOWLIST])


def _canonical(value, field_type=None):
    if value in (None, "", []):
        return None
    if field_type == "datetime" and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return value
    return value


def _split_intervals(intervals):
    result = []
    for start, end in interval_union(intervals):
        cursor = start
        while cursor < end:
            local = datetime.fromtimestamp(cursor / 1000, LOCAL_TZ)
            boundary = int((local.replace(hour=0, minute=0, second=0, microsecond=0)
                            + timedelta(days=1)).timestamp() * 1000)
            stop = min(end, boundary)
            result.append((local.date().isoformat(), cursor, stop))
            cursor = stop
    return result


def _activity_boundary(block, key):
    # Proposals have already validated explicit quotes and report-anchor clocks.
    # Only provenance-checked report/relative estimates may be saved as windows.
    claim = block.get(key) or {}
    value = claim.get("value_ms")
    supported = claim.get("kind") in {"explicit_user", "report_anchor"}
    supported = supported or (claim.get("kind") == "estimate" and isinstance(claim.get("basis"), dict))
    return value if type(value) is int and supported else None


def _non_effort_projection(fields):
    """Recognize our explicit window/audit rows; unmarked history stays protected."""
    note = fields.get("备注") or ""
    if not isinstance(note, str) or not note.startswith("[PTL:") or "\n" not in note:
        return False
    try:
        claims, _ = json.JSONDecoder().raw_decode(note.split("\n", 1)[1])
    except ValueError:
        return False
    return isinstance(claims, dict) and (
        claims.get("counting") in {"activity_window", "partial_activity_window", "unknown_effort"}
        or claims.get("status") in {"recalled", "cancelled", "superseded"})


class LarkCliTransport:
    """Official CLI user identity; no credentials are exported by this class."""

    client_token_enforced = False

    def __init__(self, profile, base_token, table_id, cwd, *, allow_formal_write=False):
        self.profile, self.base_token, self.table_id = profile, base_token, table_id
        self.cwd = str(Path(cwd).resolve())
        self.table_key = ("feishu-base", base_token, table_id)
        self.read_only = not allow_formal_write
        self.executable = shutil.which("lark-cli.exe") or shutil.which("lark-cli")
        # npm's Windows launcher is a shell wrapper; resolve its native bundled
        # executable rather than interpolate user JSON into a command shell.
        if os.name == "nt":
            npm_root = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules"
            candidate = npm_root / "@larksuite" / "cli" / "bin" / "lark-cli.exe"
            if candidate.is_file():
                self.executable = str(candidate)
        if not self.executable or Path(self.executable).suffix.lower() in {".ps1", ".cmd", ".bat"}:
            raise BaseSyncError("native_lark_cli_executable_required")

    def _call(self, command, *arguments, payload=None, write=False):
        if write and self.read_only:
            raise BaseSyncError("formal_base_read_only")
        argv = [self.executable, "--profile", self.profile, "base", command,
                "--base-token", self.base_token, "--table-id", self.table_id,
                "--as", "user", "--format", "json", *arguments]
        if payload is not None:
            argv.extend(["--json", packed(payload)])
        try:
            response = subprocess.run(argv, cwd=self.cwd, capture_output=True,
                                      encoding="utf-8", timeout=90, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransportError(type(exc).__name__, outcome_unknown=write) from exc
        try:
            envelope = json.loads(response.stdout if response.returncode == 0 else response.stderr)
        except ValueError as exc:
            raise TransportError("invalid_cli_envelope", outcome_unknown=write) from exc
        if response.returncode or envelope.get("ok") is not True:
            error = envelope.get("error", {})
            raise TransportError(error.get("code") or error.get("subtype") or "cli_failure",
                                 outcome_unknown=write)
        return envelope["data"]

    def field_schema(self):
        return self._call("+field-list")

    @staticmethod
    def _records(data):
        names = data.get("fields", [])
        ids, values = data.get("record_id_list", []), data.get("data", [])
        if len(ids) != len(values):
            raise BaseSyncError("invalid_record_matrix")
        return [{"record_id": rid, "fields": dict(zip(names, row))}
                for rid, row in zip(ids, values)]

    def find_marker(self, marker):
        selected, offset = [], 0
        while True:
            data = self._call("+record-list", "--limit", "200", "--offset", str(offset),
                              "--filter-json", packed({"logic": "and", "conditions": [
                                  ["备注", "intersects", marker]]}))
            rows = self._records(data)
            selected.extend(row for row in rows if marker in (row["fields"].get("备注") or ""))
            if not data.get("has_more"):
                return selected
            next_offset = data.get("next_offset")
            if type(next_offset) is not int or next_offset <= offset:
                raise BaseSyncError("incomplete_marker_query")
            offset = next_offset

    def get(self, record_id):
        rows = self._records(self._call("+record-get", "--record-id", record_id))
        return rows[0] if rows else None

    def create(self, fields, client_token):
        # The token is durably stored by BaseAdapter, but this CLI does not
        # expose an upstream token parameter.  Marker recovery is mandatory.
        data = self._call("+record-upsert", payload=fields, write=True)
        ids = data.get("record", {}).get("record_id_list", [])
        if len(ids) != 1:
            raise TransportError("missing_created_record_id", outcome_unknown=True)
        return ids[0]

    def update(self, record_id, fields):
        self._call("+record-upsert", "--record-id", record_id, payload=fields, write=True)


    def find_overlaps(self, start_ms, end_ms):
        # Live Base v3 predicates did not preserve sub-day ExactDate precision.
        # Push down a deliberately wider day window, then compare the actual
        # returned timestamps locally.  Long cross-day records remain covered.
        lower = datetime.fromtimestamp(start_ms / 1000, LOCAL_TZ).date() - timedelta(days=1)
        upper = datetime.fromtimestamp((end_ms - 1) / 1000, LOCAL_TZ).date() + timedelta(days=1)
        predicate = {"logic": "and", "conditions": [
            ["开始时间", "<", f"ExactDate({upper.isoformat()})"],
            ["结束时间", ">", f"ExactDate({lower.isoformat()})"]]}
        result, offset = [], 0
        while True:
            data = self._call("+record-list", "--limit", "200", "--offset", str(offset),
                              "--filter-json", packed(predicate),
                              "--field-id", "开始时间", "--field-id", "结束时间", "--field-id", "备注")
            for row in self._records(data):
                left = _canonical(row["fields"].get("开始时间"), "datetime")
                right = _canonical(row["fields"].get("结束时间"), "datetime")
                if type(left) is int and type(right) is int and max(left, start_ms) < min(right, end_ms):
                    result.append(row)
            if not data.get("has_more"):
                return result
            next_offset = data.get("next_offset")
            if type(next_offset) is not int or next_offset <= offset:
                raise BaseSyncError("incomplete_overlap_query")
            offset = next_offset


class HttpBaseTransport(LarkCliTransport):
    """Native Base v3 API over the project's existing tenant-token client.

    Requires only field:read and record:read/create/update, plus explicit edit
    access to the selected Base.  No user OAuth or Linux CLI is required.
    """

    def __init__(self, platform, base_token, table_id, *, allow_formal_write=False):
        self.platform, self.base_token, self.table_id = platform, base_token, table_id
        self.table_key = ("feishu-base", base_token, table_id)
        self.read_only = not allow_formal_write
        self.prefix = f"/base/v3/bases/{base_token}/tables/{table_id}"

    def get_many(self, record_ids):
        ids = list(dict.fromkeys(record_ids))
        rows = []
        for offset in range(0, len(ids), 50):
            data = self.platform.request("POST", self.prefix + "/records/batch_get",
                                         {"record_id_list": ids[offset:offset + 50]})["data"]
            rows.extend(self._records(data))
        return rows

    def _call(self, command, *arguments, payload=None, write=False):
        if write and self.read_only:
            raise BaseSyncError("formal_base_read_only")
        pairs = list(zip(arguments[::2], arguments[1::2]))
        options = dict(pairs)
        if command == "+field-list":
            fields, offset = [], 0
            while True:
                data = self.platform.request("GET", self.prefix + "/fields?" + urllib.parse.urlencode(
                    {"limit": 300, "offset": offset}))["data"]
                page = data.get("fields", [])
                fields.extend(page)
                offset += len(page)
                if not page or offset >= data.get("total", offset):
                    return {"fields": fields, "total": len(fields)}
        if command == "+record-list":
            params = {"limit": int(options.get("--limit", 200)), "offset": int(options.get("--offset", 0))}
            if "--filter-json" in options:
                params["filter"] = options["--filter-json"]
            projection = [value for key, value in pairs if key == "--field-id"]
            if projection:
                params["field_id"] = projection
            return self.platform.request("GET", self.prefix + "/records?" +
                                         urllib.parse.urlencode(params, doseq=True))["data"]
        if command == "+record-get":
            return self.platform.request("POST", self.prefix + "/records/batch_get",
                                         {"record_id_list": [options["--record-id"]]})["data"]
        if command == "+record-upsert":
            record_id = options.get("--record-id")
            if record_id:
                data = self.platform.request("PATCH", self.prefix + "/records/" + record_id, payload)["data"]
            else:
                data = self.platform.request("POST", self.prefix + "/records", payload)["data"]
            return {"record": data}
        raise BaseSyncError("unsupported_native_base_operation")


class BaseAdapter:
    def __init__(self, store, transport, schema):
        self.store, self.transport = store, transport
        self.fields = _schema_fields(schema)
        self.schema_fingerprint = _schema_fingerprint(schema)
        self.schema_structure_fingerprint = _schema_fingerprint(schema, include_options=False)
        if not REQUIRED.issubset(self.fields):
            raise BaseSyncError("required_fields_missing")
        for name in ALLOWLIST.keys() & self.fields.keys():
            if self.fields[name]["type"] != ALLOWLIST[name]:
                raise BaseSyncError("field_type_mismatch:" + name)
        key = getattr(transport, "table_key", id(transport))
        with _TABLE_LOCKS_GUARD:
            self.lock = _TABLE_LOCKS.setdefault(key, threading.RLock())

    def _current(self, activity_id, revision):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT body,revision FROM activities WHERE activity_id=? AND current_valid=1",
                (activity_id,)).fetchone()
        if row is None or row["revision"] != revision:
            return None
        return json.loads(row["body"])

    def _equivalent(self, actual, wanted):
        return all(_canonical(actual.get(k), self.fields[k]["type"])
                   == _canonical(v, self.fields[k]["type"]) for k, v in wanted.items())

    def _desired(self, activity_id, revision, block):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", activity_id):
            raise BaseSyncError("invalid_activity_id")
        from .formal import get_link, legacy_patch
        legacy = get_link(self.store, activity_id)
        if legacy:
            previous = self._operation(activity_id, revision, "legacy")
            # A completed update advances the baseline. Its immutable payload
            # remains the receipt for this revision when the job is replayed.
            fields = json.loads(previous["body"]) if previous and previous["status"] == "done" else legacy_patch(self.store, legacy, block)
            for name in ("一级分类", "二级项目"):
                if name in fields:
                    known = {option["name"] for option in self.fields.get(name, {}).get("options", [])}
                    if any(value not in known for value in fields[name]):
                        raise BaseSyncError("legacy_unknown_classification")
            return {"legacy": {"marker": f"[PTL:{activity_id}:legacy]", "fields": fields}}
        if block.get("actor") != "human" or block.get("role") != "primary":
            return {}
        intervals = [[i["start_ms"], i["end_ms"]] for i in block.get("human_intervals", [])]
        inactive = block.get("status") in {"recalled", "cancelled", "superseded"}
        if inactive:
            intervals = []
        if intervals and (block["start"].get("kind") != "explicit_user"
                          or block["end"].get("kind") != "explicit_user"):
            raise BaseSyncError("effort_requires_explicit_boundaries")
        # Refuse overlapping primary activities, rather than silently assigning
        # the overlap to one project and distorting the existing table's sums.
        for other in self.store.current_activities():
            if (other["activity_id"] == activity_id or other.get("actor") != "human"
                    or other.get("role") != "primary"
                    or other.get("status") in {"recalled", "cancelled", "superseded"}):
                continue
            for left, right in intervals:
                if any(max(left, x["start_ms"]) < min(right, x["end_ms"])
                       for x in other.get("human_intervals", [])):
                    raise BaseSyncError("overlapping_primary_activities")
        pieces = _split_intervals(intervals)
        if pieces:
            # Preserve existing human-interval segmentation and keys, including
            # explicit pauses and midnight splits; do not fill their gaps.
            counting = "explicit_human_interval"
            display_note = "本段有明确的本人持续投入依据；人工区间与活动报告分别保留。"
        else:
            start = None if inactive else _activity_boundary(block, "start")
            end = None if inactive else _activity_boundary(block, "end")
            if start is not None and end is not None and end < start:
                raise BaseSyncError("negative_activity_window")
            counting = ("activity_window" if start is not None and end is not None
                        else "partial_activity_window" if start is not None or end is not None
                        else "unknown_effort")
            pieces = _split_intervals([[start, end]]) if start is not None and end is not None else []
            # Reuse the original fact row when a single-day window becomes
            # known, rather than create a replacement row on every correction.
            if len(pieces) <= 1:
                pieces = [("fact", start, end)]
            display_note = ("该行仅保留撤回或取消的审计，不计有效活动时长。" if inactive
                            else "起止表示活动经过窗口，不证明连续人工投入；报告锚点仍以原始证据标明。"
                            if counting == "activity_window"
                            else "仅保留已知的一端时间；另一端未知，不计算时长。"
                            if counting == "partial_activity_window"
                            else "活动已发生，起止或投入时长未知；不以零代替未知。")
            if not inactive and any(block.get(key, {}).get("kind") in {"estimate", "report_anchor"}
                                    and _activity_boundary(block, key) is not None for key in ("start", "end")):
                display_note = "估算／报告活动窗口，非实测起止、净人工或实测睡眠时长。" + display_note
        desired, counters = {}, {}
        for day, start, end in pieces:
            index = counters.get(day, 0)
            counters[day] = index + 1
            segment = "fact" if day == "fact" else f"{day}_{index}"
            marker = f"[PTL:{activity_id}:{segment}]"
            claims = {key: block.get(key) for key in (
                "status", "start", "end", "reported_duration_seconds", "uncertainty", "subtasks")}
            claims.update({"activity_id": activity_id, "revision": revision,
                           "evidence_refs": block.get("evidence_refs", []),
                            "counting": counting, "display_note": display_note,
                            "segment_start_ms": start, "segment_end_ms": end})
            fields = {"时间花销": block.get("name", "未命名活动"),
                      "备注": marker + "\n" + packed(claims),
                      "开始时间": start, "结束时间": end}
            if "结果记录" in self.fields:
                fields["结果记录"] = block.get("result") or None
            dimension = self.fields.get("维度", {})
            if "时间管理" in {o["name"] for o in dimension.get("options", [])}:
                fields["维度"] = ["时间管理"]
            for local, remote in (("category", "一级分类"), ("project", "二级项目")):
                value = block.get(local)
                if remote not in self.fields:
                    continue
                known = {o["name"] for o in self.fields[remote].get("options", [])}
                fields[remote] = [value] if value in known else None
                if value and value not in known:
                    fields["备注"] += f"\n未映射{remote}：{value}；未创建新选项。"
            desired[segment] = {"marker": marker, "fields": fields}
        return desired

    def _mapping(self, activity_id):
        with self.store.lock:
            return {r["segment_key"]: dict(r) for r in self.store.db.execute(
                "SELECT * FROM base_mapping WHERE activity_id=?", (activity_id,))}

    def _operation(self, activity_id, revision, segment):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT * FROM sync_operations WHERE activity_id=? AND revision=? AND segment_key=?",
                (activity_id, revision, segment)).fetchone()
            return dict(row) if row else None

    def _status(self, operation_id, status, remote_id=None):
        with self.store.transaction() as db:
            db.execute("UPDATE sync_operations SET status=?,remote_id=COALESCE(?,remote_id) WHERE operation_id=?",
                       (status, remote_id, operation_id))

    def _record_success(self, op, remote_id, remote_fields):
        with self.store.transaction() as db:
            existing = db.execute("SELECT last_revision,baseline FROM base_mapping WHERE activity_id=? AND segment_key=?",
                                  (op["activity_id"], op["segment_key"])).fetchone()
            if existing and existing[0] > op["revision"]:
                raise BaseSyncError("mapping_revision_must_not_decrease")
            tracked = set(json.loads(existing[1])) if existing and existing[1] else set()
            tracked.update(json.loads(op["body"]))
            baseline = {k: remote_fields.get(k) for k in tracked}
            db.execute("INSERT OR REPLACE INTO base_mapping VALUES(?,?,?,?,?,?)",
                       (op["activity_id"], op["segment_key"], remote_id, op["revision"],
                        packed(baseline), self.schema_fingerprint))
            db.execute("UPDATE sync_operations SET status='done',remote_id=? WHERE operation_id=?",
                       (remote_id, op["operation_id"]))

    def sync(self, activity_id, revision, dry_run=True):
        with self.lock:
            block = self._current(activity_id, revision)
            if block is None:
                return {"status": "superseded", "activity_id": activity_id, "revision": revision}
            refs = block.get("evidence_refs", [])
            evidence = self.store.evidence(refs) if refs else []
            if block.get("_acceptance_test") or any(item.get("is_acceptance_test") for item in evidence):
                # Test registration is trusted local metadata.  Preserve raw
                # evidence, but reject projection before even a schema GET.
                return {"status": "test_excluded", "activity_id": activity_id, "revision": revision}
            live_schema = self.transport.field_schema()
            live_fingerprint = _schema_fingerprint(live_schema)
            if live_fingerprint != self.schema_fingerprint:
                if _schema_fingerprint(live_schema, include_options=False) != self.schema_structure_fingerprint:
                    return {"status": "schema_conflict", "activity_id": activity_id}
                # Select choices are live business data. Refresh them without
                # accepting changed field IDs, types, names or multiplicity.
                self.fields = _schema_fields(live_schema)
                self.schema_fingerprint = live_fingerprint
            if not dry_run and getattr(self.transport, "read_only", False):
                return {"status": "read_only", "activity_id": activity_id}
            desired = self._desired(activity_id, revision, block)
            mappings = self._mapping(activity_id)
            from .formal import get_link
            legacy = get_link(self.store, activity_id)
            if legacy and (set(mappings) != {"legacy"} or mappings["legacy"]["remote_id"] != legacy["record_id"]):
                return {"status": "conflict", "reason": "legacy_mapping_missing_or_changed"}
            overlap_query = getattr(self.transport, "find_overlaps", None)
            if overlap_query:
                own_prefix = f"[PTL:{activity_id}:"
                own_ids = {mapping["remote_id"] for mapping in mappings.values() if mapping["remote_id"]}
                # Activity/report windows can overlap without proving duplicate
                # human effort.  Keep the conservative guard for real human
                # intervals and unmarked historical rows only.
                intervals = [] if block.get("status") in {"recalled", "cancelled", "superseded"} else block.get("human_intervals", [])
                for interval in intervals:
                    start, end = interval["start_ms"], interval["end_ms"]
                    if end == start:
                        continue
                    existing = [r["record_id"] for r in overlap_query(start, end)
                                if r["record_id"] not in own_ids
                                and own_prefix not in (r["fields"].get("备注") or "")
                                and not _non_effort_projection(r["fields"])]
                    if existing:
                        return {"status": "conflict", "reason": "remote_time_overlap", "record_ids": existing}
            for segment, mapping in mappings.items():
                if segment not in desired and mapping["remote_id"]:
                    marker = f"[PTL:{activity_id}:{segment}]"
                    desired[segment] = {"marker": marker, "retired": True, "fields": {
                        "时间花销": block.get("name", "活动") + "（已被修订替代）",
                        "备注": marker + "\n" + packed({"revision": revision, "status": "superseded",
                            "display_note": "该行已被后续修订替代，仅保留审计，不计有效活动时长。"}),
                        "开始时间": None, "结束时间": None}}
            results = []
            for segment, candidate in desired.items():
                mapping, op = mappings.get(segment), self._operation(activity_id, revision, segment)
                fields = candidate["fields"]
                if any(name not in ALLOWLIST or name not in self.fields for name in fields):
                    raise BaseSyncError("non_allowlisted_field")
                if mapping and mapping["last_revision"] > revision:
                    return {"status": "superseded", "segments": results}
                remote_id = mapping["remote_id"] if mapping else None
                remote = self.transport.get(remote_id) if remote_id else None
                if remote_id and remote is None:
                    return {"status": "conflict", "reason": "remote_record_missing", "segment": segment}
                if not remote_id:
                    matches = self.transport.find_marker(candidate["marker"])
                    if len(matches) > 1:
                        return {"status": "conflict", "reason": "duplicate_business_marker", "segment": segment}
                    if matches:
                        remote, remote_id = matches[0], matches[0]["record_id"]
                        if op is None or not self._equivalent(remote["fields"], json.loads(op["body"])):
                            return {"status": "conflict", "reason": "untracked_or_changed_remote", "segment": segment}
                        if not dry_run:
                            self._record_success(op, remote_id, remote["fields"])
                        results.append({"segment": segment, "status": "recovered", "record_id": remote_id})
                        continue
                if mapping and remote:
                    # Missing/unmapped model classification is not an explicit
                    # request to erase an existing confirmed classification.
                    fields = dict(fields)
                    for name in ("一级分类", "二级项目"):
                        if fields.get(name) is None and remote["fields"].get(name):
                            fields.pop(name, None)
                if op and op["body"] != packed(fields):
                    return {"status": "conflict", "reason": "operation_payload_changed", "segment": segment}
                if op and op["status"] in {"inflight", "outcome_unknown"}:
                    if remote and self._equivalent(remote["fields"], fields):
                        if not dry_run:
                            self._record_success(op, remote_id, remote["fields"])
                        results.append({"segment": segment, "status": "recovered", "record_id": remote_id})
                        continue
                    return {"status": "outcome_unknown", "segment": segment,
                            "reason": "manual_reconciliation_required_before_retry"}
                if mapping and mapping["baseline"] and remote:
                    if not self._equivalent(remote["fields"], json.loads(mapping["baseline"])):
                        return {"status": "conflict", "reason": "remote_manual_change", "segment": segment}
                if op and op["status"] == "done":
                    results.append({"segment": segment, "status": "already_synced", "record_id": remote_id})
                    continue
                action = "retire" if candidate.get("retired") else "update" if remote_id else "create"
                if dry_run:
                    results.append({"segment": segment, "action": action, "record_id": remote_id,
                                    "before": remote["fields"] if remote else None, "after": fields})
                    continue
                # Check again immediately before issuing each external mutation.
                if self._current(activity_id, revision) is None:
                    return {"status": "superseded", "segments": results}
                if op is None:
                    op = {"operation_id": str(uuid.uuid4()), "activity_id": activity_id,
                          "revision": revision, "segment_key": segment, "action": action,
                          "body": packed(fields), "before_body": packed(remote["fields"]) if remote else None,
                          "client_token": str(uuid.uuid4()), "status": "inflight", "remote_id": remote_id,
                          "created_at_ms": now_ms()}
                    with self.store.transaction() as db:
                        db.execute("INSERT INTO sync_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(op.values()))
                else:
                    self._status(op["operation_id"], "inflight")
                try:
                    if not fields and remote_id:
                        self._record_success(op, remote_id, remote["fields"])
                        results.append({"segment": segment, "action": "unchanged", "status": "synced", "record_id": remote_id})
                        continue
                    if remote_id:
                        self.transport.update(remote_id, fields)
                    else:
                        remote_id = self.transport.create(fields, op["client_token"])
                    # Persist returned ID before readback; it remains useful if
                    # readback itself loses its response or the process stops.
                    self._status(op["operation_id"], "inflight", remote_id)
                    observed = None
                    for delay in (0, .15, .35, .75):
                        if delay:
                            time.sleep(delay)
                        observed = self.transport.get(remote_id)
                        if observed is not None and self._equivalent(observed["fields"], fields):
                            break
                    if observed is None or not self._equivalent(observed["fields"], fields):
                        self._status(op["operation_id"], "outcome_unknown", remote_id)
                        return {"status": "outcome_unknown", "reason": "readback_mismatch", "segment": segment}
                    self._record_success(op, remote_id, observed["fields"])
                    results.append({"segment": segment, "action": action, "status": "synced", "record_id": remote_id})
                except Exception as exc:
                    self._status(op["operation_id"], "outcome_unknown", remote_id)
                    return {"status": "outcome_unknown", "reason": getattr(exc, "category", type(exc).__name__), "segment": segment}
            return {"status": "dry_run" if dry_run else "synced", "activity_id": activity_id,
                    "revision": revision, "segments": results,
                    "client_token_enforced": getattr(self.transport, "client_token_enforced", False),
                    "concurrency": "single_writer; remote read/compare is not atomic CAS"}
