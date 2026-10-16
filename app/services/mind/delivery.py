"""投递计划：E3 之后生成带发送时间的 outbox 项；ack(sent) 才写 assistant 历史。

所有随机以 seed = sha256(cycle_id) 派生，同一 cycle 重复生成结果一致；
seq 上有 UNIQUE(cycle_id, seq)，续跑不会重复插入。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any

from app import config
from app.services.mind import clock, store

_STICKER_MARK = re.compile(r"^\[表情包：(.+)\]$")


def _derive_cycle_id(session_id: str, key: str) -> str:
    return hashlib.sha256(f"{session_id}:{key}".encode()).hexdigest()[:32]


def _config_range(name: str, default: tuple[float, float]) -> tuple[float, float]:
    raw = getattr(config, name, None)
    try:
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return float(raw[0]), float(raw[1])
        if isinstance(raw, str):
            lo, hi = raw.split(",")
            return float(lo.strip()), float(hi.strip())
    except Exception:
        pass
    return default


def plan_deliveries(
    session_id: str,
    final_text: str,
    *,
    cycle_id: str,
    base_ts: float | None = None,
    trace_ids: list[str] | None = None,
    hesitate: bool = False,
) -> list[dict[str, Any]]:
    """生成投递项并落库（幂等）。返回所有 pending 项（按 seq）。"""
    t0 = clock.now() if base_ts is None else base_ts
    rng = random.Random(hashlib.sha256(cycle_id.encode()).hexdigest())
    cps = max(0.1, float(config.MIND_TYPING_CPS))
    gap_lo, gap_hi = _config_range("MIND_MSG_GAP_SEC", (1.0, 4.0))
    hesitate_lo, hesitate_hi = _config_range("MIND_HESITATE_PAUSE_SEC", (5.0, 20.0))

    items: list[dict[str, Any]] = []
    seq = 0

    def add(kind: str, payload: dict[str, Any], send_at: float, duration: float | None = None) -> None:
        nonlocal seq
        if duration is not None:
            payload = {**payload, "duration": round(duration, 3)}
        items.append({
            "session_id": session_id,
            "cycle_id": cycle_id,
            "seq": seq,
            "kind": kind,
            "payload": payload,
            "send_at": send_at,
        })
        seq += 1

    if trace_ids:
        add("mark_read", {"trace_ids": list(trace_ids)}, t0)

    clock_ts = t0

    def typing_for(text: str) -> float:
        jitter = rng.uniform(0.8, 1.2)
        return min(15.0, max(1.0, len(text) / cps * jitter))

    messages = [line for line in final_text.split("\n") if line.strip()]
    if hesitate and messages:
        add("typing", {"state": "typing"}, clock_ts, duration=3.0)
        clock_ts += 3.0 + rng.uniform(hesitate_lo, hesitate_hi)

    first = True
    for line in messages:
        line = line.strip()
        if not line:
            continue
        if not first:
            clock_ts += rng.uniform(gap_lo, gap_hi)
        first = False
        sticker = _STICKER_MARK.match(line)
        if sticker:
            add("typing", {"state": "typing"}, clock_ts, duration=typing_for(sticker.group(1)))
            clock_ts += items[-1]["payload"]["duration"]
            add("sticker", {"name": sticker.group(1)}, clock_ts)
        else:
            duration = typing_for(line)
            add("typing", {"state": "typing"}, clock_ts, duration=duration)
            clock_ts += duration
            add("text", {"text": line}, clock_ts)

    for item in items:
        store.insert_delivery(item)
    return pending_for_cycle(cycle_id)


def pending_for_cycle(cycle_id: str) -> list[dict[str, Any]]:
    from app.storage.db import fetch_all
    import json as _json

    rows = fetch_all(
        "SELECT * FROM mind_delivery WHERE cycle_id = ? AND status = 'pending' ORDER BY seq ASC",
        (cycle_id,),
    )
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = _json.loads(item.pop("payload_json") or "{}")
        except ValueError:
            item["payload"] = {}
        items.append(item)
    return items
