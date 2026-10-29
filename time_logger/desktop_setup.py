"""First binding for a new Windows user; existing bindings are never replaced."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QStackedWidget, QTextEdit, QVBoxLayout, QWidget)

from .desktop_credentials import write_secret


def binding_values(host, port, host_public_key):
    host = host.strip()
    if not host or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9_.:\-]+", host):
        raise ValueError("请填写服务器域名或 IP 地址，不含 ssh://、用户名或路径。")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SSH 端口须为 1 到 65535。")
    try:
        key = serialization.load_ssh_public_key(host_public_key.strip().encode("ascii"))
    except (UnicodeError, ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("请粘贴服务器的完整 ssh-ed25519 主机公钥。") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("当前客户端只支持 ssh-ed25519 主机公钥。")
    public = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")
    return {"host": host, "port": port, "host_public_key": public}


def new_binding(values):
    device = "windows-" + uuid.uuid4().hex[:16]
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                               serialization.PublicFormat.OpenSSH).decode("ascii")
    secret = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                   serialization.NoEncryption())
    return {**values, "device_id": device, "credential_target": "PersonalTimeLogger/device/" + device,
            "device_public_key": public}, secret


def save_binding(path, config, secret, secret_writer=write_secret):
    """Exclusive create protects a binding written while the setup was open."""
    path = Path(path)
    if path.exists():
        raise FileExistsError("existing_binding_preserved")
    secret_writer(config["credential_target"], secret)
    with path.open("x", encoding="utf-8") as output:
        json.dump(config, output, ensure_ascii=False, indent=2)


def setup_document():
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return root / "docs" / "SETUP.md"


class SetupDialog(QDialog):
    def __init__(self, config_path, parent=None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.config = self.secret = None
        self.setWindowTitle("个人时间记录 · 首次配置")
        self.resize(620, 480)
        layout = QVBoxLayout(self)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages)
        first, second = QWidget(), QWidget()
        self.pages.addWidget(first)
        self.pages.addWidget(second)
        form = QFormLayout(first)
        intro = QLabel("连接你自己的时间记录服务端。\n需要先按配置说明准备 Linux 服务器和飞书应用；桌面端不包含这些账号或服务。")
        intro.setWordWrap(True)
        form.addRow(intro)
        self.host = QLineEdit()
        self.host.setPlaceholderText("你的服务器域名或 IP")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.host_key = QTextEdit()
        self.host_key.setPlaceholderText("ssh-ed25519 AAAA…（从你控制的服务器取得）")
        self.host_key.setMaximumHeight(120)
        form.addRow("服务器", self.host)
        form.addRow("SSH 端口", self.port)
        form.addRow("服务器主机公钥", self.host_key)
        note = QLabel("请从服务器控制台读取 /etc/ssh/ssh_host_ed25519_key.pub。\n不要用未经核对的网络扫描结果代替可信公钥。")
        note.setWordWrap(True)
        form.addRow(note)
        finish = QVBoxLayout(second)
        instructions = QLabel("将下面的设备信息按配置说明登记到你自己的服务器。\n登记设备并限制此公钥只能执行该设备的 desktop_bridge 后，点击“保存并打开”。")
        instructions.setWordWrap(True)
        finish.addWidget(instructions)
        self.public_info = QTextEdit()
        self.public_info.setReadOnly(True)
        finish.addWidget(self.public_info)
        copy = QPushButton("复制设备信息")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.public_info.toPlainText()))
        finish.addWidget(copy)
        location = QLabel("设备私钥将保存在当前 Windows 用户的凭据管理器；配置和记录位于：\n" + str(self.config_path.parent))
        location.setWordWrap(True)
        finish.addWidget(location)
        buttons = QHBoxLayout()
        docs = QPushButton("打开配置说明")
        docs.clicked.connect(self.open_document)
        buttons.addWidget(docs)
        buttons.addStretch()
        cancel = QPushButton("稍后配置")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.next_button = QPushButton("生成设备信息")
        self.next_button.clicked.connect(self.advance)
        buttons.addWidget(self.next_button)
        layout.addLayout(buttons)

    def open_document(self):
        path = setup_document()
        if not path.is_file() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(self, "配置说明", "请打开下载包中的 docs/SETUP.md，或请 Agent 按仓库中的同名文档协助配置。")

    def advance(self):
        if self.pages.currentIndex() == 0:
            try:
                values = binding_values(self.host.text(), self.port.value(), self.host_key.toPlainText())
            except ValueError as exc:
                QMessageBox.warning(self, "检查配置", str(exc))
                return
            self.config, self.secret = new_binding(values)
            self.public_info.setPlainText("device_id: " + self.config["device_id"] + "\n\n设备公钥（可登记到服务器）：\n" +
                                          self.config["device_public_key"] + " desktop-" + self.config["device_id"])
            self.pages.setCurrentIndex(1)
            self.next_button.setText("保存并打开")
            return
        try:
            save_binding(self.config_path, self.config, self.secret)
        except FileExistsError:
            QMessageBox.warning(self, "已有配置", "配置文件已存在，本次没有替换。请关闭此窗口并重新打开程序。")
            return
        except (OSError, RuntimeError):
            QMessageBox.warning(self, "未能保存", "无法保存设备配置或 Windows 凭据。请确认使用当前 Windows 用户运行，并按配置说明排查。")
            return
        self.secret = None
        self.accept()
