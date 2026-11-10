"""她在世界里自己拿主意（用她聊天的深思模型，和跟用户聊天时是同一个脑子）。

两件事：
1. 别人找她（npc.contacted 之前）：NPC 那边只写对方的话，她这边由她自己回——
   这里把 NPC 写好的对方台词交给她，她用自己的口吻接，记一句自己视角的话。
2. 关键时刻（规则挑事时检测到"冲突"：生病还有义务、下雨还想出门、打算与身体打架）：
   规则列出候选，她选一个，留一句心里话。日常小事不打扰她（人大部分时候按惯性过）。

全部走世界侧排队（world_llm_calls，kind=neno_*），后台 step 领取；不在世界事务里调模型。
每天上限 MIND_WORLD_DECISIONS_PER_DAY（默认 6），超了就退回规则。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.world import rhythms
from app.storage.world_store import encode, entity, emit, save_entity, world

DECIDE_PROMPT = """你在过自己的日子，现在要决定接下来干什么。

此刻：{now}，在{place}。身上：{body}。
{conflict}
你今天的打算：{plans}

可选（只能从这里选一个 id）：
{options}

像平时那样凭感觉选，不用讲道理。输出 JSON：choice（id），murmur（选的时候心里嘀咕的一句，十几个字以内，你自己的口吻）。"""

REPLY_PROMPT = """{who}{how}来了。

你此刻：{doing}{body}。
对方说的（按顺序）：
{their_lines}

用你平时的口吻接，像真人那样短，可以只回一两句，可以敷衍、也可以多说两句，看你此刻的状态。
不知道自己今天具体干了什么就别编。输出 JSON：replies（你说的话，按顺序，1–4 条），note（你事后会记得的一句话，你的视角）。"""

# 字段别叫 thought：严格 schema 里叫 thought 会被服务商当成在套取思考过程而拦截
DECIDE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["choice", "murmur"],
                 "properties": {"choice": {"type": "string"}, "murmur": {"type": "string"}}}
REPLY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["replies", "note"],
                "properties": {"replies": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                               "note": {"type": "string"}}}


def per_day() -> int:
    return int(os.getenv("MIND_WORLD_DECISIONS_PER_DAY", "6"))


def enabled(w) -> bool:
    return bool(w["settings"].get("mind_linked")) and os.getenv("MIND_WORLD_DECISIONS", "false").lower() in {"1", "true", "yes", "on"}


def _card() -> str:
    from pathlib import Path
    path = Path(__file__).resolve().parents[3] / "prompts/mind/self_card.md"
    return path.read_text(encoding="utf-8")[:800] if path.is_file() else ""


def used_today(c, w) -> int:
    day_start = int(time.time() // 86400) * 86400
    return c.execute("SELECT count(*) FROM world_llm_calls WHERE world_id=? AND kind LIKE 'neno_%' AND created_at>=?"
                     " AND status NOT IN ('dropped')", (w["world_id"], day_start)).fetchone()[0]


def conflict(c, w, aid, options) -> str | None:
    """这个空档值不值得让她自己想：生病/很累/下雨碰上要出门的事，或者打算和身体打架。"""
    actor = entity(c, w["world_id"], aid)
    needs = {n: rhythms._value_at(r, w["sim_time"]) for n, r in rhythms._rows(c, w["world_id"], aid).items()}
    sick = [x["label"] for x in actor["state"].get("conditions", []) if x["until"] > w["sim_time"]]
    outdoor = [o for o in options if o[1] not in {"my_room", "home_living", "home_kitchen", "home_bath"}]
    weather = None
    row = c.execute("SELECT state FROM world_entities WHERE world_id=? AND entity_id='street'", (w["world_id"],)).fetchone()
    if row:
        weather = json.loads(row[0]).get("weather")
    top = sorted(options, key=lambda o: -o[2])[:2]
    if sick and outdoor:
        return f"你{sick[0]}，但有想出门的事"
    if needs.get("fatigue", 0) > 0.8 and any(o[0].get("activity") == "study" for o in top):
        return "你很累了，可是事情还没做完"
    if weather == "rain" and any(o in outdoor for o in top):
        return "外面在下雨，你本来想出去"
    if len(top) == 2 and abs(top[0][2] - top[1][2]) < 0.15 and top[0][0].get("plan_boost") and needs.get("hunger", 0) > 0.7:
        return "有打算要做，但你饿了"
    return None


def queue_decision(c, w, aid, options, reason, key) -> int:
    actor = entity(c, w["world_id"], aid)
    place = entity(c, w["world_id"], actor["parent_id"])["label"]
    from app.services.world import life
    ctx = {"aid": aid, "key": key, "reason": reason, "place": place,
           "body": "、".join([x["label"] for x in actor["state"].get("conditions", [])]
                            + list(rhythms.felt(c, w, aid).values())) or "还行",
           "now": time.strftime("%H:%M", time.gmtime(w["sim_time"] + w["settings"]["utc_offset_minutes"] * 60)),
           "plans": "；".join(_plans()) or "没特别的",
           "options": [{"id": ch["id"], "what": _label(c, w, ch), "where": entity(c, w["world_id"], pl)["label"]}
                       for ch, pl, _ in sorted(options, key=lambda o: -o[2])[:5]]}
    cursor = c.execute(
        "INSERT INTO world_llm_calls(world_id,kind,subject,status,created_at,sim_time,model,context) VALUES (?,?,?,'queued',?,?,?,?)",
        (w["world_id"], "neno_decide", aid, time.time(), w["sim_time"], _model(), encode(ctx)))
    emit(c, w, key + ":ask", "life.ask_self", {"reason": reason, "options": [o["id"] for o in ctx["options"]]}, aid)
    return int(cursor.lastrowid)


def _label(c, w, choice) -> str:
    acts = rhythms.config(w)["activities"]
    if choice.get("activity") in acts:
        return acts[choice["activity"]]["label"]
    return "吃点东西"


def _plans() -> list[str]:
    try:
        from app.services.mind import intents
        return [r["text"] for r in intents.open_intents()[:3]]
    except Exception:
        return []


def _model() -> str:
    from app import config
    return config.MIND_THINK_MODEL or config.CHAT_MODEL_NAME


def _request(prompt: str, schema: dict, name: str) -> str:
    from app import config
    from app.services.chat.llm_gateway import request_model_response
    # 和她聊天的深思层同一个模型、同样的思考设置；思考会吃掉输出额度，给足
    extra = {**config.MIND_THINK_EXTRA_BODY, "response_format": {"type": "json_schema", "json_schema": {
        "name": name, "strict": True, "schema": schema}}, "provider": {"require_parameters": True}}
    from app.services.mind.deliberation import strip_think
    # 和反思/深思一样：自我卡放 system，情境放 user
    messages = [{"role": "system", "content": _card() or "你是一个普通人，在过自己的日子。"},
                {"role": "user", "content": prompt}]
    return strip_think(request_model_response(model_name=_model(), messages=messages,
                                              extra_body=extra, timeout=config.LLM_TIMEOUT_SECONDS,
                                              max_tokens=config.MIND_THINK_MAX_TOKENS, purpose="mind_world"))


def reply_prompt(ctx: dict, their_lines: list[str]) -> str:
    return REPLY_PROMPT.format(who=ctx["label"], how="打电话" if ctx["channel"] == "call" else "发消息",
                               doing=ctx["neno_doing"],
                               body=("，身上：" + "、".join(ctx["neno_body"])) if ctx["neno_body"] else "",
                               their_lines="\n".join(f"- {t}" for t in their_lines))


def decide_prompt(ctx: dict) -> str:
    return DECIDE_PROMPT.format(now=ctx["now"], place=ctx["place"], body=ctx["body"],
                                conflict=f"（{ctx['reason']}）", plans=ctx["plans"],
                                options="\n".join(f"- {o['id']}：在{o['where']}{o['what']}" for o in ctx["options"]))


def apply_decision(c, w, row, data) -> dict:
    from app.services.world import life
    ctx = json.loads(row["context"])
    aid = ctx["aid"]
    ids = [o["id"] for o in ctx["options"]]
    choice_id = data.get("choice") if data.get("choice") in ids else ids[0]
    actor = entity(c, w["world_id"], aid)
    if actor["state"].get("doing"):
        return {"skipped": "already busy"}
    choice = next(ch for ch in rhythms.config(w)["choices"] if ch["id"] == choice_id)
    place = actor["parent_id"] if choice["place"] == "here" else choice["place"]
    deadline = life._next_obligation(c, w, aid)
    room = (deadline - w["sim_time"]) / 3600 if deadline else 24.0
    thought = str(data.get("murmur") or "")[:40]
    emit(c, w, ctx["key"] + ":self", "life.self_chose", {"choice": choice_id, "thought": thought, "reason": ctx["reason"]}, aid)
    life._do(c, w, aid, actor, choice, place, room, ctx["key"] + ":self")
    if thought:
        actor = entity(c, w["world_id"], aid)
        actor["state"]["last_thought"] = {"at": w["sim_time"], "text": thought}
        save_entity(c, actor)
    return {"choice": choice_id, "thought": thought}
