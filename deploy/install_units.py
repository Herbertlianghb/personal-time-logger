"""Install only this project's new systemd files; never start a service."""
import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import subprocess
import sys


SOURCE = Path("/opt/personal-time-logger/deploy")
NAMESPACE = Path("/etc/systemd/journald@personal-time-logger.conf.d")
CONFIG = Path("/var/lib/personal-time-logger/config.json")
BACKUPS = Path("/var/lib/personal-time-logger/backups")
FILES = (
    ("personal-time-logger.service", Path("/etc/systemd/system/personal-time-logger.service")),
    ("personal-time-logger-backup.service", Path("/etc/systemd/system/personal-time-logger-backup.service")),
    ("personal-time-logger-backup.timer", Path("/etc/systemd/system/personal-time-logger-backup.timer")),
    ("journal-limits.conf", NAMESPACE / "limits.conf"),
)


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return {"returncode": result.returncode, "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-host", required=True, help="Hostname of your own intended deployment server")
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != args.expect_host:
        raise RuntimeError("Unexpected host or identity")
    user = pwd.getpwnam("time-logger")
    conflicts, payloads = [], {}
    if SOURCE.resolve() != SOURCE:
        conflicts.append("Unexpected source path")
    for name, target in FILES:
        source = SOURCE / name
        data = source.read_bytes()
        if source.is_symlink() or not source.is_file():
            conflicts.append("Source is not a regular file: " + name)
        if target.is_symlink() or (target.exists() and
                                  (not target.is_file() or target.read_bytes() != data)):
            conflicts.append("Destination conflict: " + str(target))
        payloads[target] = data
    for parent in (Path("/etc/systemd/system"), Path("/var/lib/personal-time-logger")):
        if not parent.is_dir() or parent.resolve() != parent:
            conflicts.append("Unexpected parent: " + str(parent))
    if NAMESPACE.is_symlink() or (NAMESPACE.exists() and not NAMESPACE.is_dir()):
        conflicts.append("Namespace directory conflict")
    if CONFIG.is_symlink() or not CONFIG.is_file():
        conflicts.append("Config missing or symlink")
    if BACKUPS.is_symlink() or (BACKUPS.exists() and
                               (not BACKUPS.is_dir() or BACKUPS.stat().st_uid != user.pw_uid)):
        conflicts.append("Backups directory conflict")
    if conflicts:
        print(json.dumps({"unit_setup_status": "STOPPED", "conflicts": conflicts}, indent=2))
        return 2

    if not NAMESPACE.exists():
        NAMESPACE.mkdir(mode=0o755)
    created = []
    for target, data in payloads.items():
        if target.exists():
            continue
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chown(target, 0, 0)
        os.chmod(target, 0o644)
        created.append(str(target))
    os.chown(CONFIG, user.pw_uid, user.pw_gid)
    os.chmod(CONFIG, 0o600)
    if not BACKUPS.exists():
        BACKUPS.mkdir(mode=0o700)
    os.chown(BACKUPS, user.pw_uid, user.pw_gid)
    os.chmod(BACKUPS, 0o700)

    targets = [str(target) for name, target in FILES
               if name.endswith((".service", ".timer"))]
    verify = run(["systemd-analyze", "verify"] + targets)
    report = {"unit_setup_status": "INSTALLED", "created": created, "unit_verify": verify,
              "config_metadata": {"uid": CONFIG.stat().st_uid, "gid": CONFIG.stat().st_gid,
                                  "mode": oct(stat.S_IMODE(CONFIG.stat().st_mode))},
              "backups_metadata": {"path": str(BACKUPS), "uid": BACKUPS.stat().st_uid,
                                   "mode": oct(stat.S_IMODE(BACKUPS.stat().st_mode))},
              "start_or_enable_requested": False}
    if verify["returncode"] == 0:
        report["daemon_reload"] = run(["systemctl", "daemon-reload"])
    report["units"] = {
        name: {"active": run(["systemctl", "is-active", name])["stdout"],
               "enabled": run(["systemctl", "is-enabled", name])["stdout"]}
        for name, _ in FILES if name.endswith((".service", ".timer"))
    }
    print(json.dumps(report, indent=2))
    return verify["returncode"] or report.get("daemon_reload", {}).get("returncode", 0)


if __name__ == "__main__":
    sys.exit(main())
