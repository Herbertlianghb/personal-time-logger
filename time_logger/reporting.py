"""Versioned local projections; a synchronized projection is not a fresh Base read."""
from datetime import date, datetime, time, timedelta
import json

from .calculator import LOCAL_TZ, summarize, split_days
from .store import digest, now_ms


def affected_days(blocks, store):
    days = set()
    for block in blocks:
        known = [block.get(key, {}).get("value_ms") for key in ("start", "end")]
        dates = {datetime.fromtimestamp(value / 1000, LOCAL_TZ).date().isoformat() for value in known if value is not None}
        intervals = [(i["start_ms"], i["end_ms"]) for i in block.get("human_intervals", [])]
        dates.update(split_days(intervals))
        if not dates:
            refs = block.get("evidence_refs", [])
            for source in store.evidence(refs) if refs else []:
                if source.get("created_at_ms") is not None:
                    dates.add(datetime.fromtimestamp(source["created_at_ms"] / 1000, LOCAL_TZ).date().isoformat())
        days.update(dates)
    return sorted(days)


def mark_reported(store, day, data_revision):
    date.fromisoformat(day)
    with store.transaction() as db:
        if report(store, day)["data_revision"] != data_revision:
            raise ValueError("report_revision_changed")
        db.execute("DELETE FROM report_dirty WHERE day=?", (day,))
    return {"status": "reported", "date": day, "data_revision": data_revision}


def report(store, target_date=None):
    day = date.fromisoformat(target_date) if target_date else datetime.now(LOCAL_TZ).date()
    left = int(datetime.combine(day, time(), LOCAL_TZ).timestamp() * 1000)
    right = int(datetime.combine(day + timedelta(days=1), time(), LOCAL_TZ).timestamp() * 1000)
    blocks = store.current_activities()
    selected = []
    for block in blocks:
        if block.get("_acceptance_test"):
            continue
        refs = block.get("evidence_refs", [])
        if refs and any(item.get("is_acceptance_test") for item in store.evidence(refs)):
            continue
        start, end = block["start"]["value_ms"], block["end"]["value_ms"]
        intersects = start is not None and end is not None and start < right and end > left
        boundary = any(value is not None and left <= value < right for value in (start, end))
        evidence = store.evidence(block.get("evidence_refs", [])) if block.get("evidence_refs") else []
        report_day = any(e["created_at_ms"] is not None and left <= e["created_at_ms"] < right for e in evidence)
        if intersects or boundary or (start is None and end is None and report_day):
            selected.append(block)
    summary = summarize(selected)
    summary["human_effort_seconds"] = summary["human_by_day"].get(day.isoformat(), 0)
    ai_intervals = [(b["start"]["value_ms"], b["end"]["value_ms"]) for b in selected
                    if b["actor"] == "ai" and b["role"] == "primary" and b.get("status") not in {"recalled", "cancelled"}
                    and b["start"]["value_ms"] is not None and b["end"]["value_ms"] is not None]
    summary["ai_background_union_seconds"] = split_days(ai_intervals).get(day.isoformat(), 0)
    with store.lock:
        saved = store.db.execute("SELECT count(*) FROM messages WHERE message_created_at_ms>=? AND message_created_at_ms<?", (left, right)).fetchone()[0]
        rounds = store.db.execute("SELECT payload FROM jobs WHERE job_type='question' AND status='done'").fetchall()
        sent_questions = [json.loads(row[0]) for row in rounds if left <= json.loads(row[0])["created_at_ms"] < right]
        answered = store.db.execute("SELECT count(*) FROM questions q JOIN observations o ON q.answer_observation_id=o.observation_id WHERE o.observed_at_ms>=? AND o.observed_at_ms<?", (left, right)).fetchone()[0]
        revisions = store.db.execute("SELECT count(*) FROM activities WHERE revision>1 AND created_at_ms>=? AND created_at_ms<?", (left, right)).fetchone()[0]
        confirmed = {(r["activity_id"], r["last_revision"]) for r in store.db.execute("SELECT activity_id,last_revision FROM base_mapping")}
        records = {}
        for row in store.db.execute("SELECT activity_id,remote_id,last_revision,segment_key FROM base_mapping WHERE remote_id IS NOT NULL"):
            records.setdefault(row["activity_id"], []).append({"record_id": row["remote_id"], "last_synced_revision": row["last_revision"], "segment": row["segment_key"]})
    return {"as_of_ms": now_ms(), "date": day.isoformat(), "timezone": "Asia/Taipei",
            "data_revision": digest([(b["activity_id"], b["revision"]) for b in selected]),
            "completeness": "partial", "statistics_scope": "confirmed interval union; never a claim of a complete day",
            "local_accepted_projection": summary,
            "activities": [{**b, "base_sync_previously_verified": (b["activity_id"], b["revision"]) in confirmed,
                            "base_records": records.get(b["activity_id"], [])} for b in selected],
            "formal_base_status": "requires fresh guide/schema/record read before reporting formal totals",
            "metrics": {"saved_logical_messages_including_acceptance_tests": saved,
                        "question_rounds_sent": len(sent_questions),
                        "questions_sent": sum(len(q["question_ids"]) for q in sent_questions),
                        "answers_or_skips_saved": answered, "activity_revisions_created": revisions,
                        "user_manual_minutes": None, "model_accuracy": None, "human_time_boundary_coverage": None},
            "health": store.health()}
