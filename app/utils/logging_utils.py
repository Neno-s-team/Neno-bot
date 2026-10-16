import logging
import json
import re
import uuid
from urllib.parse import unquote
from typing import Any

SECRET_KEY_PARTS = ("token", "secret", "cookie", "api_key", "authorization")
IDENTIFIER_KEY_PARTS = ("user_id", "session_id")
BODY_KEY_PARTS = ("prompt", "message", "response", "reply", "content")
MAX_FIELD_LENGTH = 200
_LOGGING_READY = False
_ACCESS_FILTER_READY = False


class _SafeAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        if not isinstance(record.args, tuple) or len(record.args) < 3:
            return True

        args = list(record.args)
        args[2] = sanitize_access_path(str(args[2]))
        record.args = tuple(args)
        return True


def new_trace_id() -> str:
    return uuid.uuid4().hex[:8]


def _mask_plain_identifier(text: str) -> str:
    if not text:
        return ""
    if "..." in text:
        return text
    if len(text) <= 8:
        return text
    return f"{text[:4]}...{text[-4:]}"


def mask_sensitive(value: Any) -> Any:
    if value is None:
        return None

    text = str(value).strip()
    if ":" not in text:
        return _mask_plain_identifier(text)

    parts = text.split(":")
    if len(parts) >= 3:
        masked_parts = parts[:2] + [_mask_plain_identifier(part) for part in parts[2:]]
        return ":".join(masked_parts)
    return ":".join(_mask_plain_identifier(part) for part in parts)


def _truncate(value: Any, limit: int = MAX_FIELD_LENGTH) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _is_length_field(key: str) -> bool:
    return key.endswith("_len") or key.endswith("_count") or key in {"latency_ms", "status_code"}


def _sanitize_field_value(key: str, value: Any) -> Any:
    lower_key = key.lower()

    if any(part in lower_key for part in SECRET_KEY_PARTS):
        return "[REDACTED]"

    if any(part in lower_key for part in IDENTIFIER_KEY_PARTS):
        return mask_sensitive(value)

    if any(part in lower_key for part in BODY_KEY_PARTS) and not _is_length_field(lower_key):
        if value is None:
            return None
        if "error" in lower_key:
            return _truncate(value)
        return f"[{len(str(value))} chars]"

    if isinstance(value, dict):
        return {str(child_key): _sanitize_field_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_field_value(key, item) for item in value[:20]]
    if isinstance(value, str):
        return _truncate(value)
    return value


def sanitize_log_fields(fields: dict) -> dict:
    sanitized = {}
    for raw_key, value in fields.items():
        key = str(raw_key)
        sanitized[key] = _sanitize_field_value(key, value)
    return sanitized


def _format_log_value(value: Any) -> str:
    if value is None:
        return "None"
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if not text:
        return '""'
    if any(char.isspace() for char in text):
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def sanitize_access_path(path: str) -> str:
    def replace_query_value(match: re.Match) -> str:
        key = match.group(1)
        raw_value = match.group(2)
        return f"{key}{mask_sensitive(unquote(raw_value))}"

    sanitized = re.sub(
        r"((?:admin_)?token|secret|cookie|api_key|authorization)=([^&\s]+)",
        r"\1=[REDACTED]",
        path,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"((?:session_id|user_id)=)([^&\s]+)",
        replace_query_value,
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def configure_safe_logging() -> None:
    global _ACCESS_FILTER_READY, _LOGGING_READY
    if not _LOGGING_READY:
        logging.getLogger().setLevel(logging.INFO)
        _LOGGING_READY = True

    if not _ACCESS_FILTER_READY:
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.addFilter(_SafeAccessLogFilter())
        _ACCESS_FILTER_READY = True


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _persist_debug_event(
    *,
    module: str,
    event: str,
    trace_id: str | None,
    level: str,
    fields: dict,
) -> None:
    try:
        from app.storage.db import add_debug_event

        add_debug_event(
            trace_id=trace_id,
            module=module,
            event=event,
            level=level,
            success=_coerce_bool(fields.get("success")),
            skipped=_coerce_bool(fields.get("skipped")),
            action=None if fields.get("action") is None else str(fields.get("action"))[:120],
            reason=None if fields.get("reason") is None else str(fields.get("reason"))[:240],
            target_label=None if fields.get("target_label") is None else str(fields.get("target_label"))[:120],
            candidate_id=_coerce_int(fields.get("candidate_id")),
            metadata_json=json.dumps(fields, ensure_ascii=False, default=str),
        )
    except Exception:
        return


def log_event(
    module: str,
    event: str,
    trace_id: str | None = None,
    level: str = "info",
    **fields: Any,
) -> None:
    configure_safe_logging()

    safe_fields = sanitize_log_fields(fields)
    safe_level = str(level or "info").strip().lower()[:32] or "info"
    if safe_level == "info" and ("error" in event.lower() or "failed" in event.lower()):
        safe_level = "error"
    parts = [
        "[neno]",
        f"trace={trace_id or '-'}",
        f"module={module}",
        f"event={event}",
        f"level={safe_level}",
    ]
    parts.extend(f"{key}={_format_log_value(value)}" for key, value in safe_fields.items())
    logging.info(" ".join(parts))
    _persist_debug_event(
        module=module,
        event=event,
        trace_id=trace_id,
        level=safe_level,
        fields=safe_fields,
    )
