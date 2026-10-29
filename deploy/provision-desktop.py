"""One-time, same-user binding. Never writes a private key to a file."""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from time_logger.desktop_app import data_directory
from time_logger.desktop_credentials import write_secret, read_secret, CredentialUnavailable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--public-key-out", required=True)
    args = parser.parse_args()
    folder = data_directory()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "device.json"
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        read_secret(config["credential_target"])
        print(json.dumps({"status": "already_bound", "device_id": config["device_id"]}))
        return
    entries = Path(args.known_hosts).read_text(encoding="utf-8").splitlines()
    public = next(" ".join(line.split()[1:3]) for line in entries if line.startswith(args.host + " ssh-ed25519 "))
    device = "windows-" + uuid.uuid4().hex[:16]
    target = "PersonalTimeLogger/device/" + device
    private = Ed25519PrivateKey.generate()
    secret = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
    write_secret(target, secret)
    if read_secret(target) != secret:
        raise CredentialUnavailable("device_credential_readback_failed")
    key = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")
    Path(args.public_key_out).write_text(key + " desktop-" + device + "\n", encoding="ascii")
    path.write_text(json.dumps({"device_id": device, "host": args.host, "port": 22,
                                "host_public_key": public, "credential_target": target}, indent=2), encoding="utf-8")
    print(json.dumps({"status": "bound_to_windows_credential_manager", "device_id": device, "configuration": str(path)}))


if __name__ == "__main__":
    main()
