from __future__ import annotations


def accepts_chat(chat_id: int, allowed_chat_ids: frozenset[int]) -> bool:
    """Fail closed: an empty allowlist accepts nobody."""
    return bool(allowed_chat_ids) and chat_id in allowed_chat_ids
