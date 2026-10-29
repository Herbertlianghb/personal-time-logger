"""First-binding contracts use temporary paths and an in-memory credential writer."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from time_logger.desktop_setup import binding_values, new_binding, save_binding


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.host_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")

    def test_host_key_is_a_real_ed25519_key_with_comments_removed(self):
        values = binding_values("logger.example.test", 2222, self.host_key + " host-comment")
        self.assertEqual(values["host_public_key"], self.host_key)
        self.assertEqual(values["port"], 2222)
        for key in ("ssh-ed25519 not-a-key", "", "ssh-rsa AAAA", "ssh-future AAAA"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                binding_values("logger.example.test", 22, key)

    def test_connection_fields_cannot_be_shell_or_url_fragments(self):
        for host in ("ssh://logger.example.test", "user@logger.example.test", "example.test\nother", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                binding_values(host, 22, self.host_key)
        for port in (0, 65536, True, "22"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                binding_values("logger.example.test", port, self.host_key)

    def test_private_key_never_enters_device_configuration(self):
        config, secret = new_binding(binding_values("logger.example.test", 22, self.host_key))
        private = serialization.load_ssh_private_key(secret, password=None)
        expected = private.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                     serialization.PublicFormat.OpenSSH).decode("ascii")
        self.assertEqual(config["device_public_key"], expected)
        self.assertNotIn("PRIVATE KEY", json.dumps(config))
        self.assertEqual(config["credential_target"], "PersonalTimeLogger/device/" + config["device_id"])

    def test_existing_binding_is_not_overwritten_or_recredentialed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "device.json"
            original = '{"existing": true}'
            path.write_text(original, encoding="utf-8")
            writer = Mock()
            with self.assertRaises(FileExistsError):
                save_binding(path, {"credential_target": "unused"}, b"unused", writer)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            writer.assert_not_called()

    def test_success_stores_only_public_configuration_in_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "device.json"
            config, secret = new_binding(binding_values("logger.example.test", 22, self.host_key))
            writer = Mock()
            save_binding(path, config, secret, writer)
            writer.assert_called_once_with(config["credential_target"], secret)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), config)
            self.assertNotIn("PRIVATE KEY", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
