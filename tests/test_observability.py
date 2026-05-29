from __future__ import annotations

import json
import logging

from app.observability import JsonFormatter


def test_json_formatter_sanitizes_secret_fields_nested_in_lists() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.extra_fields = {
        "events": [
            {
                "authorization": "Bearer secret-token",
                "nested": {"api_key": "sk-secret", "status": "ok"},
            }
        ],
        "status": "ok",
    }

    payload = json.loads(JsonFormatter().format(record))

    assert payload["events"] == [{"nested": {"status": "ok"}}]
    assert payload["status"] == "ok"
    assert "secret-token" not in json.dumps(payload)
    assert "sk-secret" not in json.dumps(payload)
