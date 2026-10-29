"""Durable interpretation batches shared by the collector and private worker."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .model_runner import InvalidModelProposal, _check_schema, _strict_json
from .proposal import InvalidProposal, validate_proposal
from .store import digest, now_ms, packed


class BatchConflict(ValueError):
    pass


class StaleBatch(BatchConflict):
    pass


TABLE = """CREATE TABLE IF NOT EXISTS interpretation_batches(
 batch_id TEXT PRIMARY KEY,created_at_ms INTEGER NOT NULL,status TEXT NOT NULL,
 bundle TEXT NOT NULL,input_hash TEXT NOT NULL,proposal_id TEXT,proposal_hash TEXT,
 metadata TEXT,result TEXT,last_error_class TEXT)"""


def _ensure_schema(store):
    with store.transaction() as db:
        db.execute(TABLE)


def _question_context(db):
    rows = db.execute("""SELECT q.*,p.created_at_ms FROM questions q
      JOIN proposals p USING(proposal_id)
      ORDER BY CASE q.status WHEN 'pending' THEN 0 ELSE 1 END,q.rowid DESC LIMIT 40""").fetchall()
    return [{**json.loads(row["body"]), "question_id": row["question_id"],
             "status": row["status"], "asked_at_ms": row["asked_at_ms"],
             "created_at_ms": row["created_at_ms"],
             "answer_observation_id": row["answer_observation_id"]} for row in rows]


def _acceptance_context(store, item):
    if item.get("_acceptance_test"):
        return True
    refs = item.get("evidence_refs", [])
    return bool(refs) and any(source.get("is_acceptance_test") for source in store.evidence(refs))


def _classification_options(config):
    """Use the same live options as the destination, outside SQLite locks."""
    base = config.get("base_token")
    table = config.get("time_table_id") or config.get("table_id")
    if not base or not table:
        return config.get("categories", []), config.get("projects", [])
    from .base_adapter import BaseSyncError, HttpBaseTransport, _schema_fields
    from .feishu import Feishu
    transport = HttpBaseTransport(Feishu(config["app_id"], config["app_secret"]), base, table)
    fields = _schema_fields(transport.field_schema())
    options = []
    for name in ("一级分类", "二级项目"):
        field = fields.get(name, {})
        if field.get("type") != "select":
            raise BaseSyncError("classification_schema_missing")
        options.append(list(dict.fromkeys(item["name"] for item in field.get("options", []))))
    return tuple(options)


def export_batch(store, config) -> dict:
    if not config.get("parse", False):
        return {"status": "disabled", "batch_id": None, "processed": 0}
    categories, projects = _classification_options(config)
    _ensure_schema(store)
    with store.transaction() as db:
        existing = db.execute("SELECT * FROM interpretation_batches WHERE status='exported' ORDER BY created_at_ms LIMIT 1").fetchone()
        if existing:
            bundle = json.loads(existing["bundle"])
            observed = store.evidence(bundle["evidence_ids"])
            valid = len(observed) == len(bundle["evidence_ids"]) and all(item["id"] == item["current_observation_id"] for item in observed)
            valid = valid and type(bundle.get("is_acceptance_test")) is bool
            valid = valid and all(bool(item.get("is_acceptance_test")) == bundle.get("is_acceptance_test") for item in observed)
            valid = valid and set(bundle.get("categories", [])) == set(categories)
            valid = valid and set(bundle.get("projects", [])) == set(projects)
            for aid, revision in bundle["revisions"].items():
                current = db.execute("SELECT revision FROM activities WHERE activity_id=? AND current_valid=1", (aid,)).fetchone()
                valid = valid and current is not None and current[0] == revision
            if valid:
                for job_id in bundle["job_ids"]:
                    db.execute("UPDATE jobs SET lease_until_ms=? WHERE job_id=? AND status='inflight'", (now_ms() + 600000, job_id))
                return bundle
            _invalidate(store, bundle["batch_id"], "snapshot_changed_before_export")
        now = now_ms()
        rows = db.execute("""SELECT * FROM jobs WHERE job_type='interpret' AND
          ((status IN ('pending','retry') AND next_at_ms<=?) OR
           (status='inflight' AND lease_until_ms<=?)) ORDER BY next_at_ms,job_id LIMIT 300""", (now, now)).fetchall()
        selected, source_ids, batch_is_test = [], [], None
        deferred = db.execute("SELECT count(*) FROM jobs WHERE job_type='attachment' AND status IN ('pending','retry','inflight','conflict')").fetchone()[0]
        for row in rows:
            if len(selected) >= 30:
                break
            payload = json.loads(row["payload"])
            oid = payload.get("observation_id")
            source = db.execute("""SELECT o.*,m.current_observation_id,m.message_type,m.message_created_at_ms,
              EXISTS(SELECT 1 FROM acceptance_tests t WHERE t.app_scope=m.app_scope AND t.message_id=m.message_id) AS is_acceptance_test
              FROM observations o JOIN messages m USING(message_key) WHERE o.observation_id=?""", (oid,)).fetchone()
            if not source or source["current_observation_id"] != oid:
                db.execute("UPDATE jobs SET status='superseded',lease_until_ms=NULL,last_error_class='superseded_observation' WHERE job_id=?", (row["job_id"],))
                continue
            if source["message_type"] != "text" and source["kind"] != "recall":
                store.enqueue(db, "attachment", "attachment:" + oid,
                              {"observation_id": oid, "message_type": source["message_type"], "status": "source_saved_extraction_pending"})
                db.execute("UPDATE jobs SET status='superseded',lease_until_ms=NULL,last_error_class='requires_attachment_extraction' WHERE job_id=?", (row["job_id"],))
                deferred += 1
                continue
            source_is_test = bool(source["is_acceptance_test"])
            if source["source"] == "desktop":
                desktop = store.evidence([oid])[0].get("desktop_context", {})
                if desktop.get("target_activity_id"):
                    target = db.execute("SELECT revision FROM activities WHERE activity_id=? AND current_valid=1", (desktop["target_activity_id"],)).fetchone()
                    if not target or target["revision"] != desktop.get("expected_revision"):
                        db.execute("UPDATE jobs SET status='conflict',lease_until_ms=NULL,last_error_class='desktop_target_revision_changed' WHERE job_id=?", (row["job_id"],))
                        continue
            if batch_is_test is None:
                batch_is_test = source_is_test
            if source_is_test != batch_is_test:
                continue  # other class is not claimed; it remains eligible
            selected.append(row["job_id"])
            source_ids.append(oid)
        if not selected:
            return {"status": "idle", "batch_id": None, "processed": 0, "attachments_deferred": deferred}

        # Bounded recent activity context, plus activities referencing an older
        # observation of a newly edited/recalled logical message.
        recent = db.execute("SELECT * FROM activities WHERE current_valid=1 ORDER BY created_at_ms DESC LIMIT 30").fetchall()
        related_ids = db.execute("""SELECT observation_id FROM observations WHERE message_key IN
          (SELECT message_key FROM observations WHERE observation_id IN (""" + ",".join("?" for _ in source_ids) + ")) LIMIT 100", source_ids).fetchall()
        related = []
        if related_ids:
            filters = " OR ".join("body LIKE ?" for _ in related_ids)
            related = db.execute("SELECT * FROM activities WHERE current_valid=1 AND (" + filters + ") ORDER BY created_at_ms DESC LIMIT 30",
                                 ['%"' + row[0] + '"%' for row in related_ids]).fetchall()
        desktop_targets = {item.get("desktop_context", {}).get("target_activity_id") for item in store.evidence(source_ids)} - {None}
        if desktop_targets:
            marks = ",".join("?" for _ in desktop_targets)
            related = list(db.execute("SELECT * FROM activities WHERE current_valid=1 AND activity_id IN (" + marks + ")", list(desktop_targets))) + list(related)
        blocks = []
        seen = set()
        for row in list(related) + list(recent):
            if row["activity_id"] not in seen and len(blocks) < 40:
                context = {**json.loads(row["body"]), "activity_id": row["activity_id"], "revision": row["revision"]}
                if _acceptance_context(store, context) != batch_is_test:
                    continue
                seen.add(row["activity_id"])
                blocks.append(context)
        evidence_ids = list(source_ids)
        required_refs = set()
        for block in blocks:
            required_refs.update(block.get("evidence_refs", []))
        if required_refs:
            marks = ",".join("?" for _ in required_refs)
            counterparts = db.execute("SELECT DISTINCT m.current_observation_id FROM observations o JOIN messages m USING(message_key) WHERE o.observation_id IN (" + marks + ")", list(required_refs)).fetchall()
            evidence_ids.extend(row[0] for row in counterparts if row[0])
        completed_context = db.execute("SELECT payload FROM jobs WHERE job_type='interpret' AND status='done' ORDER BY next_at_ms DESC LIMIT 30").fetchall()
        evidence_ids.extend(json.loads(row[0]).get("observation_id") for row in completed_context)
        evidence_ids = [oid for oid in evidence_ids if oid]
        evidence_ids = list(dict.fromkeys(evidence_ids))[:100]
        evidence = []
        for item in store.evidence(evidence_ids):
            if item["current_observation_id"] != item["id"] or (item["message_type"] not in {"text", "formal_record"} and item["kind"] != "recall"):
                continue
            if bool(item.get("is_acceptance_test")) != batch_is_test:
                continue
            evidence.append({**item, "content": item["content"] or "", "new_for_batch": item["id"] in source_ids})
        batch_id = str(uuid.uuid4())
        bundle = {"version": 1, "status": "exported", "batch_id": batch_id, "created_at_ms": now,
                  "job_ids": selected, "new_evidence_ids": source_ids,
                  "is_acceptance_test": batch_is_test,
                  "evidence": evidence, "evidence_ids": [item["id"] for item in evidence],
                  "current_blocks": blocks, "revisions": {item["activity_id"]: item["revision"] for item in blocks},
                  "questions": [item for item in _question_context(db) if _acceptance_context(store, item) == batch_is_test],
                  "categories": categories,
                  "projects": projects, "attachments_deferred": deferred}
        input_hash = digest(bundle)
        bundle["input_hash"] = input_hash
        db.execute("INSERT INTO interpretation_batches(batch_id,created_at_ms,status,bundle,input_hash) VALUES(?,?,'exported',?,?)",
                   (batch_id, now, packed(bundle), input_hash))
        for job_id in selected:
            db.execute("UPDATE jobs SET status='inflight',attempts=attempts+1,lease_until_ms=? WHERE job_id=?", (now + 600000, job_id))
        return bundle


def _metadata(value):
    if not isinstance(value, dict) or not isinstance(value.get("model"), str) or not isinstance(value.get("prompt_version"), str):
        raise InvalidModelProposal("model_and_prompt_metadata_required")
    allowed = {"model", "provider", "prompt_version", "prompt_sha256", "input_sha256", "cli_version",
               "usage", "elapsed_seconds", "status", "exit_code", "batch_input_hash"}
    return {key: value[key] for key in allowed if key in value}


def _invalidate(store, batch_id, reason, status="stale"):
    with store.transaction() as db:
        row = db.execute("SELECT bundle,status FROM interpretation_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row or row["status"] in {"imported", "rejected", "stale"}:
            return
        bundle = json.loads(row["bundle"])
        db.execute("UPDATE interpretation_batches SET status=?,last_error_class=? WHERE batch_id=?", (status, reason, batch_id))
        for job_id in bundle["job_ids"]:
            # A review-only proposal has not applied facts, even though its
            # original jobs were marked done.  Rejection must release those
            # jobs too; an imported batch is protected by the guard above.
            terminal = "('superseded')" if row["status"] == "proposed" else "('done','superseded')"
            db.execute("UPDATE jobs SET status='retry',next_at_ms=?,lease_until_ms=NULL,last_error_class=? WHERE job_id=? AND status NOT IN " + terminal,
                       (now_ms(), reason, job_id))


def import_proposal(store, batch_id, proposal, metadata, accept=True) -> dict:
    _ensure_schema(store)
    try:
        schema = _strict_json((Path(__file__).resolve().parent.parent / "config/model-proposal.schema.json").read_text(encoding="utf-8"))
        _check_schema(proposal, schema, schema)
        metadata = _metadata(metadata)
        body_hash = digest(proposal)
        with store.transaction() as db:
            batch = db.execute("SELECT * FROM interpretation_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not batch:
                raise BatchConflict("unknown_batch")
            if batch["status"] == "imported" or (batch["status"] == "proposed" and not accept):
                if batch["proposal_hash"] != body_hash:
                    raise BatchConflict("accepted_batch_proposal_is_immutable")
                return {**json.loads(batch["result"]), "replayed": True}
            if batch["status"] not in {"exported", "proposed"}:
                raise StaleBatch("batch_no_longer_current")
            bundle = json.loads(batch["bundle"])
            if metadata.get("batch_input_hash", batch["input_hash"]) != batch["input_hash"]:
                raise BatchConflict("batch_input_hash_mismatch")
            batch_is_test = bundle.get("is_acceptance_test")
            if type(batch_is_test) is not bool:
                raise StaleBatch("batch_missing_trusted_test_classification")
            current = [item for item in store.current_activities()
                       if _acceptance_context(store, item) == batch_is_test]
            revisions = {item["activity_id"]: item["revision"] for item in current}
            if any(revisions.get(aid) != revision for aid, revision in bundle["revisions"].items()):
                raise StaleBatch("activity_revision_changed")
            evidence = store.evidence(bundle["evidence_ids"])
            if len(evidence) != len(bundle["evidence_ids"]) or any(item["id"] != item["current_observation_id"] for item in evidence):
                raise StaleBatch("observation_changed")
            if any(bool(item.get("is_acceptance_test")) != batch_is_test for item in evidence):
                raise StaleBatch("acceptance_test_classification_changed")
            source_map = {item["id"]: item for item in evidence}
            fresh = set(bundle["new_evidence_ids"])
            for block in proposal["blocks"]:
                if not fresh.intersection(block["evidence_refs"]):
                    raise InvalidProposal("activity_must_include_new_batch_evidence")
            validate_proposal(proposal, evidence, current)
            answers = proposal["question_answers"]
            questions = {item["question_id"]: item for item in bundle["questions"]}
            answered_ids = set()
            for answer in answers:
                qid, oid = answer["question_id"], answer["answer_evidence_id"]
                question = questions.get(qid)
                row = db.execute("SELECT * FROM questions WHERE question_id=?", (qid,)).fetchone()
                source = source_map.get(oid)
                if qid in answered_ids or not question or question["status"] != "pending" or not row or row["status"] != "pending":
                    raise InvalidProposal("question_answer_not_pending_or_duplicate")
                if oid not in fresh or not source or source["kind"] == "recall" or source["message_type"] != "text":
                    raise InvalidProposal("answer_requires_new_text_evidence")
                asked_after = question["asked_at_ms"] or question["created_at_ms"]
                if source["observed_at_ms"] < asked_after or (question["asked_at_ms"] and (source["created_at_ms"] or 0) < question["asked_at_ms"]):
                    raise InvalidProposal("answer_precedes_question")
                answered_ids.add(qid)
            pid = store.save_proposal(proposal, evidence, metadata["model"], metadata["prompt_version"])
            persisted = db.execute("SELECT body FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
            if digest(json.loads(persisted[0])) != body_hash:
                raise BatchConflict("saved_proposal_differs_from_import")
            question_count = db.execute("SELECT count(*) FROM questions").fetchone()[0]
            changed = store.accept_proposal(pid) if accept else []
            if accept:
                for answer in answers:
                    if not store.resolve_question(answer["question_id"], answer["answer_evidence_id"], skipped=answer["disposition"] == "skipped"):
                        raise StaleBatch("question_state_changed")
            result = {"status": "imported" if accept else "proposed", "batch_id": batch_id, "proposal_id": pid,
                      "processed": len(bundle["job_ids"]), "activities_changed": len(changed),
                      "questions_added": db.execute("SELECT count(*) FROM questions").fetchone()[0] - question_count,
                      "answers_saved": len(answers) if accept else 0,
                      "accepted": bool(accept), "replayed": False}
            for job_id in bundle["job_ids"]:
                store.finish_job(job_id, "done")
            db.execute("UPDATE interpretation_batches SET status=?,proposal_id=?,proposal_hash=?,metadata=?,result=?,last_error_class=NULL WHERE batch_id=?",
                       (result["status"], pid, body_hash, packed(metadata), packed(result), batch_id))
            return result
    except StaleBatch as error:
        _invalidate(store, batch_id, str(error))
        raise
    except (InvalidProposal, InvalidModelProposal, BatchConflict) as error:
        # Permanently rejected candidate contents must not pin the oldest
        # exported batch forever.  Keep its audit record, requeue its evidence
        # under a fresh batch ID, and never invalidate accepted facts.
        _invalidate(store, batch_id, str(error), status="rejected")
        raise
