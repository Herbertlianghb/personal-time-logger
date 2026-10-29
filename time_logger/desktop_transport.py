"""Pinned SSH with a device-only forced command. Secrets stay in memory."""
from __future__ import annotations

import base64
import io
import json
import logging
import socket
import time

import paramiko

from .desktop_credentials import read_secret

logging.getLogger("paramiko").addHandler(logging.NullHandler())
logging.getLogger("paramiko").propagate = False


class SyncUnavailable(RuntimeError):
    pass


class DeviceTransport:
    def __init__(self, config, secret_reader=read_secret):
        self.config, self.secret_reader = config, secret_reader
        self.client = None

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def _connect(self):
        if self.client and self.client.get_transport() and self.client.get_transport().is_active():
            return
        self.close()
        client = paramiko.SSHClient()
        public = self.config["host_public_key"].split()
        key = paramiko.Ed25519Key(data=base64.b64decode(public[1]))
        port = self.config.get("port", 22)
        pinned_name = self.config["host"] if port == 22 else "[" + self.config["host"] + "]:" + str(port)
        client.get_host_keys().add(pinned_name, public[0], key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        secret = self.secret_reader(self.config["credential_target"])
        private = paramiko.Ed25519Key.from_private_key(io.StringIO(secret.decode("ascii")))
        client.connect(self.config["host"], port=port,
                       username="time-logger", pkey=private, look_for_keys=False, allow_agent=False,
                       timeout=10, auth_timeout=10, banner_timeout=10, channel_timeout=30)
        client.get_transport().set_keepalive(30)
        self.client = client

    def request(self, op, **fields):
        body = {"version": 1, "op": op, "device_id": self.config["device_id"], **fields}
        encoded = (json.dumps(body, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("submission_too_large")
        try:
            self._connect()
            channel = self.client.get_transport().open_session(timeout=15)
            channel.settimeout(35)
            channel.exec_command("desktop-v1")
            channel.sendall(encoded)
            channel.shutdown_write()
            data, started = bytearray(), time.monotonic()
            try:
                while True:
                    part = channel.recv(65536)
                    if not part:
                        break
                    data.extend(part)
                    if len(data) > 4 * 1024 * 1024 or time.monotonic() - started > 45:
                        raise SyncUnavailable("bounded_response_exceeded")
                result = json.loads(data.decode("utf-8"))
                if not isinstance(result, dict) or not result.get("ok"):
                    raise SyncUnavailable(result.get("error", "server_rejected") if isinstance(result, dict) else "invalid_response")
                return result
            finally:
                channel.close()
        except (paramiko.SSHException, socket.timeout, OSError, ValueError, EOFError) as exc:
            self.close()
            raise SyncUnavailable(type(exc).__name__) from None
