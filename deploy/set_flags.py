"""Atomically change only the four documented switches of this project."""
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
for flag in ("ingest", "parse", "base_sync", "notion_sync"):
    parser.add_argument("--" + flag.replace("_", "-"), choices=("true", "false"))
args = parser.parse_args()
path = Path("/var/lib/personal-time-logger/config.json")
if path.is_symlink() or path.resolve() != path or path.stat().st_uid != os.geteuid():
    raise SystemExit("configuration_target_mismatch")
config = json.loads(path.read_text(encoding="utf-8-sig"))
changed = {}
for key, value in vars(args).items():
    if value is not None:
        config[key] = value == "true"
        changed[key] = config[key]
temp = path.with_suffix(".next.json")
fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(config, stream, ensure_ascii=False)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temp, path)
print(json.dumps({"changed_flags": changed, "service_restart_required": True}))
