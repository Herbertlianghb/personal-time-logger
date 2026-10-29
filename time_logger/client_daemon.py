"""One quiet scheduled run on the existing signed-in Windows computer."""
import argparse
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

from .offsite import pull
from .store import packed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    path = Path(args.config).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    logger = logging.getLogger("time-logger-client")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(path.parent / "client-status.log", maxBytes=262144, backupCount=2, encoding="utf-8")
    logger.addHandler(handler)
    started = time.monotonic()
    result = {"as_of": datetime.now(timezone.utc).isoformat(), "human_minutes": None}
    python = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable
    try:
        ran = subprocess.run([python, "-m", "time_logger.remote_worker", "--config", str(path)], cwd=Path(__file__).resolve().parent.parent,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=360,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        value = json.loads(ran.stdout)
        result["interpretation"] = {key: value[key] for key in ("status", "processed", "activities_changed", "accepted", "replayed", "error_class") if key in value}
        result["exit_code"] = ran.returncode
    except Exception as exc:
        result["interpretation"] = {"status": "failed", "error_class": type(exc).__name__}
        result["exit_code"] = 1
    try:
        result["offsite_backup"] = pull(config)
    except Exception as exc:
        result["offsite_backup"] = {"status": "failed", "error_class": type(exc).__name__}
    result["ai_background_elapsed_seconds"] = round(time.monotonic() - started, 2)
    temp = path.parent / "last-client-run.tmp"
    temp.write_text(packed(result), encoding="utf-8")
    os.replace(temp, path.parent / "last-client-run.json")
    logger.info(packed(result))
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
