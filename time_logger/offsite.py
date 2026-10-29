"""Pull a consistent server backup to this separate computer; no automatic deletion."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid

from .store import digest, packed


def verify_backup(path, manifest):
    path = Path(path)
    if not path.is_file():
        raise ValueError("backup_missing")
    db = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup_integrity_failed")
        actual = {"messages": db.execute("SELECT count(*) FROM messages").fetchone()[0],
                  "observations": db.execute("SELECT count(*) FROM observations").fetchone()[0],
                  "message_hash": digest(db.execute("SELECT message_key,first_content,message_created_at_ms FROM messages ORDER BY message_key").fetchall()),
                  "revision_hash": digest(db.execute("SELECT activity_id,revision,body,current_valid FROM activities ORDER BY activity_id,revision").fetchall()),
                  "jobs_hash": digest(db.execute("SELECT job_id,status,payload FROM jobs ORDER BY job_id").fetchall()),
                  "watermark_hash": digest(db.execute("SELECT * FROM scans ORDER BY chat_id").fetchall())}
        if any(actual[key] != manifest[key] for key in actual):
            raise ValueError("backup_manifest_mismatch")
        return actual
    finally:
        db.close()


def pull(config):
    directory = Path(config["offsite_directory"]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "last-backup.json"
    day = datetime.now(timezone.utc).date().isoformat()
    if receipt.exists() and json.loads(receipt.read_text(encoding="utf-8")).get("utc_day") == day:
        return {"status": "already_backed_up_today"}
    host = config.get("ssh_host", "personal-time-logger")
    if host != "personal-time-logger":
        raise ValueError("unapproved_ssh_target")
    prefix = ["ssh", "-F", config["ssh_config"], host]
    command = "/opt/personal-time-logger/.venv/bin/python -m time_logger --config /var/lib/personal-time-logger/config.json backup"
    result = subprocess.run(prefix + ["cd /opt/personal-time-logger && " + command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=120)
    if result.returncode:
        raise RuntimeError("remote_backup_failed")
    manifest = json.loads(result.stdout)
    source = manifest["destination"]
    if not source.startswith("/var/lib/personal-time-logger/backups/") or ".." in source:
        raise ValueError("unexpected_backup_path")
    local = directory / Path(source).name
    if local.exists():
        raise ValueError("refuse_backup_overwrite")
    for remote, destination in [(source, local), (source + ".manifest.json", local.with_suffix(".sqlite3.manifest.json"))]:
        copied = subprocess.run(["scp", "-F", config["ssh_config"], host + ":" + remote, str(destination)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if copied.returncode:
            raise RuntimeError("backup_transfer_failed")
    verify_backup(local, manifest)
    receipt.write_text(packed({"utc_day": day, "status": "verified", "source": source,
                               "local": str(local), "created_at_ms": manifest["created_at_ms"],
                               "message_hash": manifest["message_hash"], "revision_hash": manifest["revision_hash"]}), encoding="utf-8")
    return {"status": "verified", "message_count": manifest["messages"], "local": str(local)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        result = pull(json.loads(Path(args.config).read_text(encoding="utf-8-sig")))
    except Exception as exc:
        print(packed({"status": "failed", "error_class": type(exc).__name__}))
        raise SystemExit(1)
    print(packed(result))


if __name__ == "__main__":
    main()
