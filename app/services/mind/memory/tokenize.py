"""FTS 索引分词：中文按相邻两字切，英文/数字按词转小写。

中文没有空格，unicode61 分词器会把整句当一个 token，所以写入前先切成
相邻二字组（"吃饭了" → "吃饭 饭了"）；单字保留（查询线索常常就是一个字）。
存入 FTS 的文本和查询词都走同一个函数，保证切分口径一致。
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def to_index_text(text: str) -> str:
    """把文本转成 FTS 索引/查询用的 token 串（空格连接）。"""
    raw = str(text or "")
    if not raw.strip():
        return ""
    terms: list[str] = []
    for segment in re.split(r"\s+", raw):
        if not segment:
            continue
        for word in _WORD_RE.findall(segment):
            terms.append(word.lower())
        # 中文相邻两字切；单字也收（线索可能是单字）
        for run in re.findall(r"[\u4e00-\u9fff]+", segment):
            if len(run) == 1:
                terms.append(run)
                continue
            for index in range(len(run) - 1):
                terms.append(run[index:index + 2])
    return " ".join(terms)


def to_query_text(text: str) -> str:
    """查询串：与索引同一分词，OR 连接，每个词双引号转义。"""
    terms = [term for term in to_index_text(text).split(" ") if term]
    if not terms:
        return ""
    # FTS 里双引号转义为两个双引号；token 已过滤为汉字/字母数字，这里仅兜底。
    return " OR ".join('"%s"' % term.replace('"', '""') for term in terms)
