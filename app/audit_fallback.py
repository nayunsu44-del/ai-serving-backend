from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_audit_fallback_lock = threading.Lock()


def write_audit_fallback(path: Path, fields: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(fields, default=str, ensure_ascii=False) + "\n"
    with _audit_fallback_lock:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
