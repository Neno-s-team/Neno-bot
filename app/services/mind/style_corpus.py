"""说话方式语料：措辞层（E2）的参考片段，只影响"怎么说"。

和人设分开：不进记忆、不进 L1/L2、不进自我卡；换人设不用动它，换语料也不影响她是谁。
语料是本地 jsonl（MIND_STYLE_CORPUS_PATH，默认 data/corpus/style.jsonl，data/ 不进 git），
每行 {"lines": [[说话人, 文本], ...], "tags": [...]}。说话人只是"甲/乙"，不分谁是谁。

挑片段：按当前情境（对方消息 + 想说的要点）的词重叠打分，加按 cycle 派生的小抖动避免每次同几段；
偏好短段、去掉长串连发，免得示例把条数带多。文件不存在就什么都不加。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from app import config
from app.services.mind.memory.tokenize import to_index_text


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        lines = [(str(s), str(t)) for s, t in row.get("lines", []) if t]
        if len(lines) >= 2:
            terms = set(to_index_text(" ".join(t for _, t in lines)).split())
            rows.append({"lines": lines, "terms": terms, "run": _longest_run(lines)})
    return rows


def _longest_run(lines) -> int:
    best = cur = 0
    last = None
    for speaker, _ in lines:
        cur = cur + 1 if speaker == last else 1
        last = speaker
        best = max(best, cur)
    return best


def corpus() -> list[dict]:
    path = Path(config.MIND_STYLE_CORPUS_PATH)
    if not config.MIND_STYLE_EXAMPLES or not path.is_file():
        return []
    return _load(str(path), path.stat().st_mtime)


def pick(situation: str, seed: str, k: int | None = None) -> list[list[tuple[str, str]]]:
    rows = corpus()
    k = int(config.MIND_STYLE_EXAMPLES if k is None else k)
    if not rows or k <= 0:
        return []
    probe = set(to_index_text(situation).split())

    def score(index_row):
        index, row = index_row
        jitter = int.from_bytes(hashlib.sha256(f"{seed}:{index}".encode()).digest()[:4], "big") / 2**32
        overlap = len(probe & row["terms"])
        return overlap + jitter * 1.5 - 0.5 * max(0, row["run"] - 3)

    ranked = sorted(enumerate(rows), key=score, reverse=True)
    return [row["lines"][:10] for _, row in ranked[:k]]


def render(examples: list[list[tuple[str, str]]]) -> str:
    if not examples:
        return ""
    blocks = ["\n".join(f"{s}：{t}" for s, t in lines) for lines in examples]
    return ("真人聊天的说话样子（甲乙是两个普通人，与你无关；只学语气、断句、长短、表情用法，"
            "不要照抄内容，也不要把里面的事当成你的经历）：\n\n" + "\n\n".join(blocks))
