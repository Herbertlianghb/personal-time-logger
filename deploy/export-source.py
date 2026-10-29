"""Export only public source files; never traverse private delivery materials."""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


PUBLIC_FILES = (
    ".gitignore", "README.md", "LICENSE", "desktop_entry.py",
    "requirements.lock.txt", "requirements.desktop.lock.txt",
    "config/config.example.json", "config/client.example.json",
    "config/device.example.json", "config/ssh-config.example",
    "config/field-contract.example.json", "config/model-proposal.schema.json",
    "config/interpreter-prompt.txt", "tests/controlled-corpus.json",
    "docs/SETUP.md", "docs/SERVER.md", "docs/CONFIGURATION.md",
    "docs/AGENT-SETUP.md", "docs/AGENT-PROMPTS.md", "docs/DISTRIBUTION.md", "docs/RELEASE-1.4.7.md",
    "deploy/build-desktop.ps1", "deploy/package-desktop.py",
    "deploy/install-desktop.ps1", "deploy/install.vbs", "deploy/install-client-task.ps1",
    "deploy/install_units.py", "deploy/provision-desktop.py",
    "deploy/set_flags.py", "deploy/set-desktop-options.py",
    "deploy/generate-desktop-icon.py", "deploy/desktop-version.txt",
    "deploy/personal-time-logger.service",
    "deploy/personal-time-logger-backup.service",
    "deploy/personal-time-logger-backup.timer", "deploy/journal-limits.conf",
    "deploy/export-source.py", "deploy/licenses/GPL-3.0-only.txt",
    "deploy/licenses/LGPL-3.0-only.txt",
)


def public_members(root):
    paths = [root / name for name in PUBLIC_FILES]
    for directory in ("time_logger", "tests"):
        paths.extend((root / directory).glob("*.py"))
    for path in paths:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise ValueError("public_source_symlink_not_allowed")
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("public_source_missing_or_outside_root:" + str(path.relative_to(root)))
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.suffix.lower() != ".zip" or output.exists():
        raise SystemExit("Output must be a new .zip file; existing targets are unchanged")
    members = public_members(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, arcname="personal-time-logger/" + path.relative_to(root).as_posix())
    print("Public source archive: " + str(output))
    print("Files: " + str(len(members)))


if __name__ == "__main__":
    main()
