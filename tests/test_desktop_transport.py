"""Pinned SSH names must match Paramiko's non-default-port lookup."""
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from time_logger.desktop_setup import binding_values, new_binding
from time_logger.desktop_transport import DeviceTransport


class TransportBindingTests(unittest.TestCase):
    def test_non_default_port_uses_the_same_pin_name_as_sshclient(self):
        host_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")
        for port, name in ((22, "logger.example.test"), (2222, "[logger.example.test]:2222")):
            with self.subTest(port=port):
                config, secret = new_binding(binding_values("logger.example.test", port, host_key))
                client = Mock()
                with patch("time_logger.desktop_transport.paramiko.SSHClient", return_value=client):
                    transport = DeviceTransport(config, secret_reader=lambda target: secret)
                    transport._connect()
                self.assertEqual(client.get_host_keys().add.call_args.args[:2], (name, "ssh-ed25519"))
                self.assertEqual(client.connect.call_args.kwargs["port"], port)
                self.assertFalse(client.connect.call_args.kwargs["allow_agent"])
                self.assertFalse(client.connect.call_args.kwargs["look_for_keys"])


if __name__ == "__main__":
    unittest.main()
