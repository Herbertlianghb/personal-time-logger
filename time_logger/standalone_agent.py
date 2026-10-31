"""File exchange for the user's Agent via the existing device-only credential."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

from .calculator import LOCAL_TZ
from .desktop_transport import DeviceTransport
from .model_runner import _strict_json


def run(config_path, *, export_path=None, day=None, import_path=None, result_path=None):
    if (export_path is None) == (import_path is None):
        raise ValueError("exactly_one_agent_operation_required")
    destination = Path(export_path if export_path is not None else result_path).resolve()
    # Reserve a new result before any server action. A failed response cannot
    # silently overwrite the proposal, device config, or a prior save receipt.
    with destination.open("x", encoding="utf-8") as output:
        try:
            config = _strict_json(Path(config_path).read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                raise ValueError("device_config_invalid")
            candidate = None
            if import_path is not None:
                path = Path(import_path)
                if path.stat().st_size > 60000:
                    raise ValueError("agent_proposal_too_large")
                candidate = _strict_json(path.read_text(encoding="utf-8-sig"))
                if not isinstance(candidate, dict) or set(candidate) != {"batch_id", "proposal", "metadata"}:
                    raise ValueError("agent_proposal_envelope_invalid")
            transport = DeviceTransport(config)
            try:
                value = (transport.request("agent_export", date=day or datetime.now(LOCAL_TZ).date().isoformat())
                         if candidate is None else transport.request("agent_import", **candidate))
            finally:
                transport.close()
            json.dump(value, output, ensure_ascii=False, allow_nan=False, indent=2)
            return value
        except Exception as exc:
            # No exception text: it may contain personal text or credentials.
            code = str(exc) if re.fullmatch(r"[a-z][a-z_]{0,79}", str(exc)) else "agent_exchange_failed"
            json.dump({"ok": False, "error": code, "error_class": type(exc).__name__,
                       "retry": "使用新的输出文件；已导入的同一候选可安全重放。"}, output, ensure_ascii=False)
            raise
