"""记忆检索：FTS 候选 + 加权打分，取工作记忆条目。"""

from __future__ import annotations

import time
from typing import Any

from app import config
from app.services.mind.memory import store as memory_store
from app.services.mind.memory.tokenize import to_query_text


def _recency_factor(ts: float, now: float) -> float:
    days = max(0.0, (now - float(ts or 0.0)) / 86400.0)
    half_life = float(config.MIND_RECENCY_HALF_LIFE_DAYS) or 7.0
    return 2 ** (-days / half_life)


def _eff_strength(episode: dict[str, Any], now: float) -> float:
    """记忆强度：strength 从"上次回忆或创建"起按半衰期衰减。"""
    anchor = max(
        float(episode.get("last_recalled_at") or 0.0),
        float(episode.get("ts") or 0.0),
        float(episode.get("created_at") or 0.0),
    )
    days = max(0.0, (now - anchor) / 86400.0)
    half_life = float(config.MIND_MEMORY_HALF_LIFE_DAYS) or 14.0
    return float(episode.get("strength") or 1.0) * (2 ** (-days / half_life))


def _intensity(episode: dict[str, Any]) -> float:
    return min(1.0, abs(float(episode.get("valence") or 0.0))
               + max(float(episode.get("arousal") or 0.0), 0.0))


def _weights() -> dict[str, float]:
    raw = config.MIND_RETRIEVAL_WEIGHTS or {}
    return {
        "rel": float(raw.get("rel", 1)),
        "rec": float(raw.get("rec", 1)),
        "imp": float(raw.get("imp", 1)),
        "emo": float(raw.get("emo", 1)),
        "str": float(raw.get("str", 1)),
    }


def _score_candidates(candidates: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    if not candidates:
        return []
    ranks = [item["rank"] for item in candidates]
    lowest, highest = min(ranks), max(ranks)
    weights = _weights()
    scored: list[dict[str, Any]] = []
    for item in candidates:
        if highest > lowest:
            # bm25 越小越相关；min-max 归一化后翻转
            relevance = 1.0 - (item["rank"] - lowest) / (highest - lowest)
        else:
            relevance = 1.0
        score = (
            weights["rel"] * relevance
            + weights["rec"] * _recency_factor(item["ts"], now)
            + weights["imp"] * float(item["importance"] or 0.0)
            + weights["emo"] * _intensity(item)
            + weights["str"] * _eff_strength(item, now)
        )
        enriched = dict(item)
        enriched["score"] = score
        enriched["eff_strength"] = _eff_strength(item, now)
        scored.append(enriched)
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    return scored


def retrieve_episodes(
    session_id: str,
    cues: list[str],
    message: str,
    *,
    limit: int | None = None,
    apply_recall_boost: bool = True,
) -> list[dict[str, Any]]:
    """取前 N 段经历；只对真正入选的经历做回忆增强（续跑回放不增强）。"""
    resolved_limit = limit if limit is not None else max(1, int(config.MIND_WM_EPISODES))
    now = time.time()
    query_parts = [str(cue) for cue in (cues or []) if str(cue).strip()]
    query_parts.append(str(message or ""))
    query = to_query_text(" ".join(query_parts))

    candidates = memory_store.search_episodes(session_id, query, limit=30) if query else []
    if candidates:
        selected = _score_candidates(candidates, now)[:resolved_limit]
    else:
        # 没有线索/命中：退化为按新近 + 重要性排序
        weight = _weights()
        fallback = memory_store.list_episodes(session_id, limit=50)
        for item in fallback:
            item["score"] = (
                weight["rec"] * _recency_factor(item["ts"], now)
                + weight["imp"] * float(item["importance"] or 0.0)
            )
            item["eff_strength"] = _eff_strength(item, now)
        fallback.sort(key=lambda entry: entry["score"], reverse=True)
        selected = fallback[:resolved_limit]

    if not apply_recall_boost:
        return selected
    for item in selected:
        new_strength = min(1.0, item["eff_strength"] + 0.1)
        memory_store.update_episode_memory(
            item["id"],
            strength=new_strength,
            recall_count=int(item["recall_count"] or 0) + 1,
            last_recalled_at=now,
        )
        item["strength"] = new_strength
        item["recall_count"] = int(item["recall_count"] or 0) + 1
        item["last_recalled_at"] = now
    return selected


def retrieve_beliefs(
    session_id: str,
    cues: list[str],
    message: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """认识：FTS 命中按 confidence 排序；无命中时退化为高可信度优先。"""
    resolved_limit = limit if limit is not None else max(1, int(config.MIND_WM_BELIEFS))
    query_parts = [str(cue) for cue in (cues or []) if str(cue).strip()]
    query_parts.append(str(message or ""))
    query = to_query_text(" ".join(query_parts))

    hits = memory_store.search_beliefs(session_id, query, limit=30) if query else []
    if hits:
        hits.sort(key=lambda item: (float(item["confidence"] or 0.0), -float(item["rank"])),
                  reverse=True)
        return hits[:resolved_limit]
    beliefs = memory_store.list_beliefs(session_id, limit=resolved_limit)
    return beliefs[:resolved_limit]
