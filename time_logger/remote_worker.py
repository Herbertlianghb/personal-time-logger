"""Private SSH pull -> local Codex -> private SSH import, with quiet stdout."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from .model_runner import CodexInterpreter, ModelUnavailable, _strict_json


class WorkerError(RuntimeError):
    pass


PERMANENT_REJECTIONS = {"StaleBatch", "BatchConflict", "InvalidProposal", "InvalidModelProposal"}


@contextmanager
def _single_worker(config_path):
    name = hashlib.sha256(str(Path(config_path).resolve()).encode("utf-8")).hexdigest()[:20]
    path = Path(tempfile.gettempdir()) / ("time-logger-worker-" + name + ".lock")
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError):
            raise WorkerError("worker_already_running") from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _remote(config, action, body=None, batch_id=None):
    executable = shutil.which(config.get("ssh_executable", "ssh"))
    ssh_config = Path(config["ssh_config"]).resolve()
    if not executable or not ssh_config.is_file():
        raise WorkerError("ssh_configuration_unavailable")
    if config.get("host", "personal-time-logger") != "personal-time-logger":
        raise WorkerError("unexpected_ssh_host")
    if action not in {"export-batch", "import-proposal"}:
        raise WorkerError("unsupported_remote_operation")
    arguments = [config.get("remote_python", "/opt/personal-time-logger/.venv/bin/python"),
                 "-m", "time_logger", "--config", config.get("remote_config", "/var/lib/personal-time-logger/config.json"), action]
    if batch_id is not None:
        try:
            uuid.UUID(batch_id)
        except (ValueError, TypeError, AttributeError):
            raise WorkerError("invalid_remote_batch_id") from None
        arguments += ["--batch-id", batch_id]
    remote_command = "cd " + shlex.quote(config.get("remote_cwd", "/opt/personal-time-logger")) + " && " + shlex.join(arguments)
    command = [executable, "-F", str(ssh_config), "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "personal-time-logger", remote_command]
    try:
        result = subprocess.run(command, input="" if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                timeout=60, shell=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise WorkerError("ssh_operation_timed_out") from None
    except OSError:
        raise WorkerError("ssh_process_unavailable") from None
    if result.returncode:
        # Never echo a remote traceback, authentication diagnostic or activity text.
        try:
            diagnostic = _strict_json(result.stdout)
            category = diagnostic.get("error_class") if isinstance(diagnostic, dict) else None
        except (ValueError, TypeError):
            category = None
        if category in PERMANENT_REJECTIONS:
            raise WorkerError(category)
        raise WorkerError("remote_operation_failed")
    if len(result.stdout.encode("utf-8")) > 1024 * 1024:
        raise WorkerError("remote_batch_too_large")
    try:
        value = _strict_json(result.stdout)
    except (ValueError, TypeError):
        raise WorkerError("remote_response_not_json") from None
    if not isinstance(value, dict):
        raise WorkerError("remote_response_not_object")
    return value


def _summary(result):
    return {key: result[key] for key in ("status", "processed", "activities_changed", "questions_added", "answers_saved", "accepted", "replayed") if key in result}


def _open_state(config):
    # The CLI always supplies this path next to its private configuration.
    # Direct programmatic callers can omit persistence only for isolated tests.
    if not config.get("state_file"):
        return None
    path = Path(config["state_file"]).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=3)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA synchronous=EXTRA")
    database.execute("""CREATE TABLE IF NOT EXISTS model_outbox(
      batch_id TEXT PRIMARY KEY,body TEXT NOT NULL,status TEXT NOT NULL,
      last_error_class TEXT,result TEXT)""")
    database.commit()
    return database


def _transmit(config, batch_id, payload, state):
    try:
        result = _remote(config, "import-proposal", payload, batch_id)
        if result.get("status") == "rejected" and result.get("error_class") in PERMANENT_REJECTIONS:
            raise WorkerError(result["error_class"])
        if result.get("status") not in {"imported", "proposed"}:
            raise WorkerError("remote_import_not_acknowledged")
    except WorkerError as error:
        if state:
            with state:
                state.execute("UPDATE model_outbox SET status=?,last_error_class=? WHERE batch_id=?",
                              ("rejected" if str(error) in PERMANENT_REJECTIONS else "pending", str(error), batch_id))
        raise
    if state:
        with state:
            state.execute("UPDATE model_outbox SET status='imported',last_error_class=NULL,result=? WHERE batch_id=?",
                          (json.dumps(_summary(result), ensure_ascii=False), batch_id))
    return _summary(result)


def run_once(config):
    state = _open_state(config)
    try:
        pending = state.execute("SELECT batch_id,body FROM model_outbox WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone() if state else None
        if pending:
            return _transmit(config, pending["batch_id"], json.loads(pending["body"]), state)
        bundle = _remote(config, "export-batch")
        if not bundle.get("batch_id"):
            return {"status": bundle.get("status", "idle"), "processed": 0,
                    "attachments_deferred": bundle.get("attachments_deferred", 0)}
        if state:
            existing = state.execute("SELECT body,status FROM model_outbox WHERE batch_id=?", (bundle["batch_id"],)).fetchone()
            if existing:
                if existing["status"] == "rejected":
                    # An old/unupgraded server must not make us spend model
                    # quota again and then collide with this rejected PK.
                    raise WorkerError("BatchConflict")
                return _transmit(config, bundle["batch_id"], json.loads(existing["body"]), state)
        interpreter = CodexInterpreter(executable=config.get("codex_executable", "codex"))
        proposal = interpreter.interpret(bundle["evidence"], bundle["current_blocks"], bundle["categories"],
                                         bundle["projects"], questions=bundle.get("questions", []))
        payload = {"proposal": proposal, "metadata": {**interpreter.last_run_metadata, "batch_input_hash": bundle["input_hash"]}}
        if state:
            with state:
                state.execute("INSERT INTO model_outbox(batch_id,body,status) VALUES(?,?,'pending')",
                              (bundle["batch_id"], json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        return _transmit(config, bundle["batch_id"], payload, state)
    finally:
        if state:
            state.close()


def main():
    parser = argparse.ArgumentParser(description="Run one private local interpretation batch")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("worker configuration must be an object")
        config.setdefault("state_file", str(Path(args.config).resolve().parent / "worker-state.sqlite3"))
        with _single_worker(args.config):
            result = run_once(config)
    except (WorkerError, ModelUnavailable, KeyError, OSError, ValueError, sqlite3.Error) as error:
        category = str(error) if isinstance(error, WorkerError) and str(error) in PERMANENT_REJECTIONS else type(error).__name__
        result = {"status": "deferred", "processed": 0, "error_class": category}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
