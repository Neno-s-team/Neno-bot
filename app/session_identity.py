from __future__ import annotations


class SessionIdentityError(ValueError):
    pass


def telegram_chat_id(session_id: str) -> int | None:
    if not session_id.startswith("tg:"):
        return None
    suffix = session_id[3:]
    try:
        value = int(suffix)
    except (TypeError, ValueError):
        return None
    if not -(2**63) <= value <= 2**63 - 1 or str(value) != suffix:
        return None
    return value


def canonical_chat_session_id(value: str) -> str:
    session_id = (value or "").strip()
    if not session_id:
        raise SessionIdentityError("session_id must not be blank")
    if session_id.startswith("tg:") and telegram_chat_id(session_id) is None:
        raise SessionIdentityError("Telegram session_id must be canonical tg:<signed-64-bit-chat-id>")
    return session_id
