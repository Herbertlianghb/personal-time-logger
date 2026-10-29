"""Lazy, single-record links for an explicitly selected legacy correction.

Preparing a link reads Base and records its provenance locally.  It does not
edit Base, invent a user report, or turn a recorded window into human effort.
"""
from __future__ import annotations

import json
import re
import uuid

from .base_adapter import ALLOWLIST, HttpBaseTransport, _canonical, _schema_fields, _schema_fingerprint
from .feishu import Feishu
from .store import digest, now_ms, packed


TABLE = """CREATE TABLE IF NOT EXISTS formal_record_links(
 activity_id TEXT PRIMARY KEY,record_id TEXT NOT NULL UNIQUE,
 snapshot_observation_id TEXT NOT NULL,initial_fields TEXT NOT NULL,
 initial_fingerprint TEXT NOT NULL,created_at_ms INTEGER NOT NULL)"""


class FormalConflict(ValueError):
    pass


def canonical_fields(fields, schema):
    return {name: _canonical(fields.get(name), schema[name]["type"])
            for name in sorted(ALLOWLIST) if name in schema}


def record_fingerprint(record_id, fields, schema):
    return digest({"record_id": record_id, "fields": canonical_fields(fields, schema)})


def get_link(store, activity_id):
    with store.lock:
        exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='formal_record_links'").fetchone()
        row = store.db.execute("SELECT * FROM formal_record_links WHERE activity_id=?", (activity_id,)).fetchone() if exists else None
    return dict(row) if row else None


def _claim(value, oid, field):
    if type(value) is not int:
        return {"value_ms": None, "kind": "unknown", "precision": "unknown", "source_id": None,
                "quote": "", "date_resolution": "unknown"}
    return {"value_ms": value, "kind": "formal_record", "precision": "millisecond", "source_id": oid,
            "quote": field, "date_resolution": "explicit"}


def _option(value):
    return value[0] if isinstance(value, list) and len(value) == 1 else value if isinstance(value, str) else None


def prepare_correction(store, config, record_id, expected_fingerprint, transport=None):
    if not isinstance(record_id, str) or not re.fullmatch(r"rec[A-Za-z0-9]{4,100}", record_id):
        raise FormalConflict("invalid_record_id")
    if not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint):
        raise FormalConflict("record_fingerprint_required")
    base = config.get("base_token")
    table = config.get("time_table_id") or config.get("table_id")
    if not base or not table:
        raise FormalConflict("base_source_not_configured")
    transport = transport or HttpBaseTransport(Feishu(config["app_id"], config["app_secret"]), base, table)
    schema_raw = transport.field_schema()
    schema = _schema_fields(schema_raw)
    remote = transport.get(record_id)
    if not remote:
        raise FormalConflict("remote_record_missing")
    fingerprint = record_fingerprint(record_id, remote["fields"], schema)
    if fingerprint != expected_fingerprint:
        raise FormalConflict("remote_record_changed_refresh_required")
    fields = canonical_fields(remote["fields"], schema)
    captured = now_ms()
    aid = str(uuid.uuid5(uuid.NAMESPACE_URL, "personal-time-logger:" + base + ":" + table + ":" + record_id))
    with store.transaction() as db:
        db.execute(TABLE)
        # A current PTL row must use its existing identity rather than acquire
        # another activity through a legacy link.
        mapped = db.execute("SELECT activity_id,last_revision,baseline FROM base_mapping WHERE remote_id=?", (record_id,)).fetchone()
        revision = 1
        if mapped:
            row = db.execute("SELECT revision,body FROM activities WHERE activity_id=? AND current_valid=1", (mapped["activity_id"],)).fetchone()
            if not row:
                raise FormalConflict("linked_activity_missing")
            if row["revision"] != mapped["last_revision"]:
                raise FormalConflict("activity_has_unsynced_revision")
            if not get_link(store, mapped["activity_id"]) or canonical_fields(json.loads(mapped["baseline"]), schema) == fields:
                return {"activity_id": mapped["activity_id"], "revision": row["revision"], "record_id": record_id,
                        "record_fingerprint": fingerprint, "name": json.loads(row["body"]).get("name", ""), "prepared_at_ms": captured}
            # Explicitly selecting the latest remote version can rebase this
            # one legacy link. Older pending model results become stale.
            aid, revision = mapped["activity_id"], row["revision"] + 1
        if not mapped and db.execute("SELECT 1 FROM formal_record_links WHERE record_id=?", (record_id,)).fetchone():
            raise FormalConflict("legacy_link_mapping_missing")
        oid = str(uuid.uuid5(uuid.NAMESPACE_URL, "personal-time-logger:formal-observation:" + record_id + ":" + fingerprint))
        mid = "formal-record:" + record_id + ":" + fingerprint
        key = digest([store.scope, mid])
        source = {"source_kind": "formal_record", "record_id": record_id,
                  "record_fingerprint": fingerprint, "captured_at_ms": captured, "fields": fields}
        content = packed(source)
        # This is a machine snapshot, not a user message or an interpretation
        # job. Raw original fields are immutable evidence for later validators.
        db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (key, store.scope, mid, "formal-base", "formal-base", "formal_record", content,
                    None, captured, captured, oid))
        db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (oid, key, "formal_snapshot", content, digest(content), None, captured, "formal_record", None, digest(["formal_snapshot", content])))
        block = {"activity_id": None, "supersedes_revision": None, "name": fields.get("时间花销") or "未命名活动",
                 "actor": "human", "role": "primary", "parent_activity_id": None,
                 "category": _option(fields.get("一级分类")), "project": _option(fields.get("二级项目")),
                 "status": "ended" if fields.get("开始时间") is not None and fields.get("结束时间") is not None else "unknown",
                 "evidence_refs": [oid], "start": _claim(fields.get("开始时间"), oid, "开始时间"),
                 "end": _claim(fields.get("结束时间"), oid, "结束时间"), "human_intervals": [],
                 "reported_duration_seconds": None, "result": fields.get("结果记录") or "",
                 "uncertainty": "既有飞书记录；区间不自动等同于已确认人工投入。", "subtasks": []}
        pid = "formal-snapshot:" + oid
        proposal = {"version": 1, "blocks": [block], "questions": [], "question_answers": []}
        db.execute("INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?)",
                   (pid, fingerprint, "none:formal-record-read", "formal-link-1.2", captured, packed(proposal), "accepted"))
        db.execute("UPDATE activities SET current_valid=0 WHERE activity_id=?", (aid,))
        db.execute("INSERT INTO activities VALUES(?,?,?,?,1,?)", (aid, revision, pid, packed(block), captured))
        db.execute("INSERT OR REPLACE INTO formal_record_links VALUES(?,?,?,?,?,?)", (aid, record_id, oid, packed(fields), fingerprint, captured))
        db.execute("INSERT OR REPLACE INTO base_mapping VALUES(?,?,?,?,?,?)", (aid, "legacy", record_id, revision, packed(fields), _schema_fingerprint(schema_raw)))
    return {"activity_id": aid, "revision": revision, "record_id": record_id,
            "record_fingerprint": fingerprint, "name": block["name"], "prepared_at_ms": captured}


def legacy_patch(store, link, block):
    """Only new explicit user evidence can change the linked formal row.

    Missing/null model values preserve the current formal fields. Free-text/category
    changes additionally have to occur literally in a new user report; an LLM
    rephrasing the old snapshot cannot silently rename or reclassify history.
    """
    with store.lock:
        mapping = store.db.execute("SELECT baseline FROM base_mapping WHERE activity_id=? AND segment_key='legacy' AND remote_id=?",
                                   (link["activity_id"], link["record_id"])).fetchone()
    baseline = json.loads(mapping["baseline"]) if mapping and mapping["baseline"] else json.loads(link["initial_fields"])
    evidence = {e["id"]: e for e in store.evidence(block.get("evidence_refs", []))}
    user_sources = {oid: source for oid, source in evidence.items()
                    if source.get("message_type") == "text" and source.get("source") != "formal_record"}
    joined = "\n".join(source.get("content") or "" for source in user_sources.values())
    patch = {}
    for local, remote in (("start", "开始时间"), ("end", "结束时间")):
        claim = block.get(local, {})
        if claim.get("kind") == "explicit_user" and claim.get("source_id") in user_sources:
            value = claim.get("value_ms")
            if type(value) is int and value != baseline.get(remote):
                patch[remote] = value
    for local, remote in (("name", "时间花销"), ("result", "结果记录")):
        value = block.get(local)
        if isinstance(value, str) and value and value != baseline.get(remote) and value in joined:
            patch[remote] = value
    for local, remote in (("category", "一级分类"), ("project", "二级项目")):
        value = block.get(local)
        if isinstance(value, str) and value and [value] != baseline.get(remote) and value in joined:
            patch[remote] = [value]
    return patch
