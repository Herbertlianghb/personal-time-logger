import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .service import Service, interpret_pending, load_config
from .store import Store
from .calculator import summarize


def main():
    parser = argparse.ArgumentParser(description="Personal time logger operations")
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("health")
    sub.add_parser("interpret")
    summary_parser = sub.add_parser("summary")
    summary_parser.add_argument("--date")
    metrics_parser = sub.add_parser("metrics")
    metrics_parser.add_argument("--date")
    sub.add_parser("report-days")
    mark = sub.add_parser("mark-reported")
    mark.add_argument("--date", required=True)
    mark.add_argument("--data-revision", required=True)
    tests = sub.add_parser("tag-test-messages")
    tests.add_argument("--message-id", action="append", required=True)
    sub.add_parser("export-batch")
    importer = sub.add_parser("import-proposal")
    importer.add_argument("--batch-id", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--destination")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "serve":
        Service(config).run()
        return
    if args.command == "interpret":
        print(json.dumps(interpret_pending(config), ensure_ascii=False))
        return
    store = Store(config["database"], config["app_id"], [config["owner_open_id"]])
    try:
        if args.command == "health":
            result = store.health()
            health_path = Path(config.get("health_file", str(config["database"]) + ".health.json"))
            result["service"] = json.loads(health_path.read_text(encoding="utf-8")) if health_path.is_file() else None
            result["configured_flags"] = {key: bool(config.get(key, False)) for key in ("ingest", "parse", "base_sync", "notion_sync")}
        elif args.command in {"summary", "metrics"}:
            from .reporting import report
            result = report(store, args.date)
            if args.command == "metrics":
                result = {key: result[key] for key in ("as_of_ms", "date", "data_revision", "metrics", "health")}
        elif args.command == "backup":
            destination = args.destination or str(Path(config["database"]).parent / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8] + ".sqlite3"))
            result = store.backup(destination)
            store.set_setting("last_backup_at", result["created_at_ms"])
            result["destination"] = destination
        elif args.command == "export-batch":
            from .batch import export_batch
            result = export_batch(store, config)
        elif args.command == "report-days":
            with store.lock:
                result = {"dates": [row[0] for row in store.db.execute("SELECT day FROM report_dirty ORDER BY day")]}
        elif args.command == "tag-test-messages":
            result = store.tag_test_messages(args.message_id)
        elif args.command == "mark-reported":
            from .reporting import mark_reported
            result = mark_reported(store, args.date, args.data_revision)
        elif args.command == "import-proposal":
            from .batch import import_proposal
            data = json.load(sys.stdin)
            result = import_proposal(store, args.batch_id, data["proposal"], data["metadata"], accept=config.get("accept_model_proposals", True))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        rejected = type(exc).__name__ in {"StaleBatch", "BatchConflict", "InvalidProposal", "InvalidModelProposal"}
        print(json.dumps({"status": "rejected" if rejected else "failed", "error_class": type(exc).__name__}))
        raise SystemExit(1)
