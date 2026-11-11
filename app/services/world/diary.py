"""它的日记（P1）：另一个世界里的同一个人设，把自己过的日子写成一条条帖子。

"它"不是 Neno：它有自己的世界、自己的账号；读不到 Neno 的记忆和聊天，也不用那份私人语料（口吻只从人设来）。
这一层只管三件事，不碰联网和账号：
- candidates：从世界里新发生的事里挑出"可能值得发"的，每件带一个想说的程度；
- compose：用它的口吻把一件事写成一条（一个模型调用）；
- gate：发出去之前的闸——长度、不带链接 / @ / 话题标签、和最近发过的不重样。
发到哪、要不要人先看一眼，在外面一层。
"""
from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

from app.services.world.real import clean

ROOT = Path(__file__).resolve().parents[3]
MAX_WEIGHTED = 280      # X 的长度上限（中日韩文字一个算两个）
GOOD_LENGTH = 90        # 再长就不像随手发的了


def card() -> str:
    """它的人设：和 Neno 同一张自我卡，去掉只属于私下那段关系的几行。"""
    path = ROOT / "prompts" / "mind" / "self_card.md"
    if not path.is_file():
        return ""
    keep = [line for line in path.read_text(encoding="utf-8").splitlines()
            if not any(word in line for word in ("和对方的关系", "抑制基线", "【你和他】"))]
    return "\n".join(keep)[:600]


def request(messages, schema, name, max_tokens) -> str:
    """写帖子的那个模型：WORLD_DIARY_MODEL（用户定：google/gemini-4-carbon），走 WORLD_DIARY_URL / WORLD_DIARY_API_KEY，
    没设就走 OpenRouter 那一套。连模型都没设时，退回上网用的那个便宜模型。"""
    model = os.getenv("WORLD_DIARY_MODEL", "").strip()
    if not model:
        from app.services.world import surf
        return surf.request(messages, schema, name, max_tokens)
    from app.llm.openrouter_client import chat_with_openrouter
    return chat_with_openrouter(
        api_key=os.getenv("WORLD_DIARY_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip(),
        url=os.getenv("WORLD_DIARY_URL", "").strip() or os.getenv("OPENROUTER_URL", "").strip(),
        model_name=model, messages=messages, timeout=90, max_tokens=max_tokens, purpose="world_diary",
        extra_body={**json.loads(os.getenv("WORLD_DIARY_EXTRA_BODY", "") or "{}"),
                    "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}})


def situation(day_note: str, clock: str, place: str, doing: str, sky: str = "") -> str:
    """她此刻的处境，给写帖子的模型当背景。天气不是每次都给——给了它就爱往帖子里写。"""
    return f"{day_note} {clock}；她在{place}，{doing}" + (f"；外面{sky}" if sky else "")


def weighted_len(text: str) -> int:
    return sum(2 if ord(ch) > 0x10FF else 1 for ch in text)


# ── 挑：哪些事可能值得发 ─────────────────────────────────────────────────────

def candidates(events: list[dict], actor: str = "neno") -> list[dict]:
    """世界里新发生的事 → [{at, what, weight, kind}]。weight 是"有多想说"（0–1）。平淡的事不进来。"""
    found = []
    for event in events:
        p, kind = event["payload"], event["kind"]
        if event.get("actor_id") not in (actor, None):
            continue
        what, weight = None, 0.0
        if kind == "life.moment" and p.get("kind") != "surf":
            what, weight = p.get("text"), float(p.get("share", 0.3))
        elif kind == "life.attended" and p.get("times", 1) == 1:
            what, weight = f"去了{p.get('title')}" + (f"（{p['plot']}）" if p.get("plot") else ""), 0.7
        elif kind == "life.visit" and p.get("visits") == 1:
            what, weight = f"第一次去{p.get('name')}", 0.4
        elif kind == "weather.changed" and p.get("weather") == "snow":
            what, weight = f"下雪了（{p.get('to')}）", 0.9
        elif kind == "task.done":
            what = f"{p.get('title')}弄完交了" + ("，拖了几天" if p.get("late") else "，赶在最后一刻" if p.get("rushed") else "")
            weight = 0.6
        elif kind == "task.crunch":
            what, weight = f"熬夜赶{p.get('title')}", 0.6
        elif kind == "task.missed":
            what, weight = f"{p.get('title')}没弄完就到期了", 0.45
        elif kind == "perturb.happened":
            broke = [x["label"] for x in p.get("applied", []) if x.get("label") and x.get("broke")]
            if broke:
                what, weight = f"{broke[0]}坏了", 0.55
        elif kind == "life.fixed":
            what, weight = f"把{p.get('label')}修好了", 0.5
        elif kind == "life.skipped_plan":
            what, weight = f"本来要去{p.get('title')}，{p.get('reason')}，没去", 0.4
        if what:
            found.append({"at": event["sim_time"], "what": clean(what, 120), "weight": max(0.0, min(1.0, weight)),
                          "kind": kind})
    return found


# ── 写：用它的口吻写成一条 ───────────────────────────────────────────────────

PROMPT = """你在替一个人写她自己随手发的一条动态。她是谁见自我卡。这条是她自己想发的，没人要求她发。
写法：
- 第一人称，口语，像随手打出来的。一句到两句，最多三句，四十个字上下最好。
- 只说给你的这件事。"此刻"那一行只是让你知道她在哪、在干嘛，不用写进去，更不要每条都提天气。
- 别的细节不要编：没给的人、没给的店、没给的数字都不要写。
- 不总结、不升华、不讲道理、不卖惨。别用"今天"开头。别写成日记体的"我今天做了什么"。
- 她嘴硬、不说肉麻话，情绪藏着：有感觉也是带过一句，或者只说事不说感觉。
- 不带话题标签，不带链接，不 @ 任何人，表情符号最多一个，多数时候不用。
- 和她最近发过的别重样：换个说法，换个角度，或者干脆别发。
如果这件事实在没什么可说的，post 给空字符串。
只输出 JSON，样子像这样：{"post": "路边蹲着只橘猫，胖得离谱。它看我，我看它，谁也没让路。"}"""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["post"],
          "properties": {"post": {"type": "string"}}}


def compose(ask, what: str, context: str, recent: list[str]) -> str:
    """把一件事写成一条。ask(messages, schema, name, max_tokens) → 模型的回答。写不出来 / 不想发返回空串。"""
    content = (f"自我卡：\n{card()}\n\n此刻：{context}\n\n她想说的这件事：{what}\n\n她最近发过的：\n"
               + ("\n".join(f"- {text}" for text in recent[-8:]) or "（还没发过）"))
    try:
        raw = str(ask([{"role": "system", "content": PROMPT}, {"role": "user", "content": content}], SCHEMA, "diary_post", 2000))
        data, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
        return clean(str(data.get("post") or ""), 200).strip("“”\"' ")
    except Exception:
        return ""


# ── 闸：发出去之前 ───────────────────────────────────────────────────────────

def gate(text: str, recent: list[str]) -> str:
    """能发返回空串；不能发返回原因。"""
    if not text or len(text) < 4:
        return "太短或是空的"
    if weighted_len(text) > MAX_WEIGHTED or len(text) > GOOD_LENGTH:
        return "太长"
    if re.search(r"https?://|www\.|\.com\b", text, re.I):
        return "带了链接"
    if re.search(r"[@＠]\S|[#＃]\S", text):
        return "带了 @ 或话题标签"
    if re.search(r"作为(一个)?AI|语言模型|人设|自我卡|提示词", text, re.I):
        return "出戏了"
    for old in recent[-30:]:
        if SequenceMatcher(None, text, old).ratio() > 0.6:
            return "和最近发过的太像"
    return ""
