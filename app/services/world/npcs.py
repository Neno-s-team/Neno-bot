"""W4 NPC：她身边的人有自己的一天，偶尔来找她（打电话/发消息）。

- 规则决定"今天会不会来找她、大概几点"（按每周频率抽签）；模型只负责"内容"。
- 每个核心/次要 NPC 每天醒来做一次廉价模型调用：今天干嘛（几个片段）、要不要找她、找她想说什么、记一笔。
- 到点找她：她在睡觉/上课/组会就晚点再说（未接来电后来回过去），可推迟三次。
  一次调用模拟双方几句话（双人对话单次调用），结果按各自视角记：NPC 记一笔，她得到一段"今天和谁聊了什么"。
- 关系锁（用户定：完全不碰）：提示词不许、输出再过滤——任何涉及她感情线的内容（恋爱、对象、表白、约会、暧昧……）
  整段作废并留痕，不进世界。
- 模型调用一律在数据库事务之外：到期任务只把请求排进 world_llm_calls（queued），后台 step() 领取→调用→落地。
- 每天调用上限 WORLD_NPC_MAX_CALLS_PER_DAY；失败退避沿用世界认知的 provider_failure 规则。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.world import rhythms
from app.storage.world_store import WorldError, emit, encode, entity, job, save_entity, world

CONTENT = Path(__file__).resolve().parents[3] / "content/world/npcs"
_ROMANCE = re.compile(r"恋爱|对象|男朋友|女朋友|男友|女友|表白|约会|相亲|暧昧|追你|追求|喜欢你|处对象|脱单|心动|结婚")
_ROMANCE_NON_KIN = re.compile(r"爱你|想你了|宝贝")


def load(name: str) -> dict | None:
    path = CONTENT / (name + ".json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def config(w: dict) -> dict | None:
    return w["settings"].get("npcs")


def setup(c, w, name: str) -> None:
    cfg = load(name)
    if not cfg:
        return
    w["settings"]["npcs"] = cfg
    for npc_id in cfg["npcs"]:
        _schedule_wake(c, w, npc_id, w["sim_time"])


def _local(w, t) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc) + timedelta(minutes=w["settings"]["utc_offset_minutes"])


def _at(w, day_start: float, hhmm: str) -> float:
    h, m = hhmm.split(":")
    return day_start + int(h) * 3600 + int(m) * 60


def _schedule_wake(c, w, npc_id, after) -> None:
    day, start = rhythms._local_day(w, after)
    spec = config(w)["npcs"][npc_id]
    for d in (day, day + 1):
        base = start + (d - day) * 86400
        when = _at(w, base, spec.get("wake", "08:00")) + rhythms._draw(w, f"wake:{npc_id}:{d}", "j", -1800, 1800)
        if when > after:
            job(c, w["world_id"], f"npc_wake:{npc_id}:{d}", when, "npc_wake", {"npc": npc_id, "day": d})
            return


# ── 到期任务（事务内，只排队不调模型）───────────────────────────────────────

def _queue(c, w, kind, subject, context) -> int:
    cursor = c.execute(
        "INSERT INTO world_llm_calls(world_id,kind,subject,status,created_at,sim_time,model,context) VALUES (?,?,?,'queued',?,?,?,?)",
        (w["world_id"], kind, subject, time.time(), w["sim_time"], _model(), encode(context)))
    return int(cursor.lastrowid)


def _memory(w, npc_id) -> list[str]:
    return w["settings"].setdefault("npc_memory", {}).setdefault(npc_id, [])


def _wake(c, w, task) -> None:
    npc_id, d = task["payload"]["npc"], task["payload"]["day"]
    spec = config(w)["npcs"][npc_id]
    per_week = spec.get("contact", {}).get("per_week", [0, 0])
    rate = rhythms._draw(w, f"rate:{npc_id}:{d // 7}", "r", *per_week) / 7.0
    contact_today = rhythms._draw(w, f"contact:{npc_id}:{d}", "c") < rate
    local = _local(w, w["sim_time"])
    context = {"npc": npc_id, "label": spec["label"], "card": spec["card"], "relation": spec["relation"],
               "date": local.strftime("%Y-%m-%d"), "weekday": "一二三四五六日"[local.weekday()],
               "weather": _weather(c, w), "memory": _memory(w, npc_id)[-6:], "contact_today": contact_today,
               "contact_hours": spec.get("contact", {}).get("hours", [9, 21]), "day": d}
    _queue(c, w, "npc_plan", npc_id, context)
    emit(c, w, task["job_id"], "npc.wake", {"contact_today": contact_today}, npc_id)
    _schedule_wake(c, w, npc_id, task["run_at"] + 600)


def _weather(c, w) -> str | None:
    row = c.execute("SELECT state FROM world_entities WHERE world_id=? AND entity_id='street'", (w["world_id"],)).fetchone()
    return json.loads(row[0]).get("weather") if row else None


_BUSY = {"sleep", "class", "meeting"}


def _contact_due(c, w, task) -> None:
    p = task["payload"]
    neno = entity(c, w["world_id"], "neno")
    doing = (neno["state"].get("doing") or {}).get("activity")
    tries = int(p.get("tries", 0))
    if doing in _BUSY and tries < 3:
        delay = rhythms._draw(w, task["job_id"], "later", 40, 120) * 60
        missed = p.get("missed") or (p["channel"] == "call")
        job(c, w["world_id"], f"{task['job_id']}:retry{tries + 1}", w["sim_time"] + delay, "npc_contact",
            {**p, "tries": tries + 1, "missed": missed})
        emit(c, w, task["job_id"] + ":busy", "npc.contact_deferred", {"npc": p["npc"], "she_was": doing}, p["npc"])
        return
    spec = config(w)["npcs"][p["npc"]]
    needs = {n: round(rhythms._value_at(r, w["sim_time"]), 2) for n, r in rhythms._rows(c, w["world_id"], "neno").items()}
    context = {"npc": p["npc"], "label": spec["label"], "card": spec["card"], "relation": spec["relation"],
               "kin": spec.get("kin", any(x in spec["relation"] for x in ("妈", "爸", "家人"))),
               "channel": p["channel"], "about": p.get("about", ""), "missed": bool(p.get("missed")),
               "npc_memory": _memory(w, p["npc"])[-4:], "neno_card": _neno_card(),
               "neno_doing": (neno["state"].get("doing") or {}).get("label", "空闲"),
               "neno_body": [x["label"] for x in neno["state"].get("conditions", [])],
               "neno_tired": needs.get("fatigue", 0) > 0.6, "time": _local(w, w["sim_time"]).strftime("%H:%M")}
    _queue(c, w, "npc_contact", p["npc"], context)


def _neno_card() -> str:
    path = Path(__file__).resolve().parents[3] / "prompts/mind/self_card.md"
    return path.read_text(encoding="utf-8")[:500] if path.is_file() else ""


def handle(c, w, task) -> bool:
    if task["kind"] == "npc_wake":
        if config(w):
            _wake(c, w, task)
        return True
    if task["kind"] == "npc_contact":
        if config(w):
            _contact_due(c, w, task)
        return True
    return False


# ── 模型调用（事务外）──────────────────────────────────────────────────────

def _model() -> str:
    return os.getenv("WORLD_NPC_MODEL", "").strip() or os.getenv("WORLD_LLM_MODEL", "").strip() or "openai/gpt-6-luna"


PLAN_PROMPT = """你在模拟一个普通人一天的大致安排。不是聊天，不是写小说。
只根据给的角色卡、日期、天气、记忆，给出这个人今天大概干了什么（3–5 个片段，每段一句话、带大概时刻）。
平淡是常态，不要制造戏剧性。不要提到任何恋爱、对象、暧昧相关的事。
如果 contact_today 为 true：这个人今天会联系"她"（{relation}的那个女生），给出几点（HH:MM，在 contact_hours 内）、
用 call 还是 message、想说的事（一句话，日常的：问近况、转发东西、催进度、约吃饭等）。为 false 就都填 null。
memory_note：一句话，记下今天值得这个人记住的事（可以为空字符串）。
只输出 JSON。"""

PLAN_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["day", "contact_time", "contact_channel", "contact_about", "memory_note"],
               "properties": {
                   "day": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                   "contact_time": {"type": ["string", "null"]},
                   "contact_channel": {"type": ["string", "null"], "enum": ["call", "message", None]},
                   "contact_about": {"type": ["string", "null"], "description": "想说的事，一句话"},
                   "memory_note": {"type": "string"}}}

CONTACT_PROMPT = """模拟两个人的一小段日常交流（{channel_word}），一次写完双方。
A 是 {label}（{relation}），角色卡：{card}
B 是"她"，自我卡：{neno_card}
B 此刻：{neno_doing}{body_line}。{missed_line}（只知道这些；她今天别的事不知道就别替她编，比如在哪、吃了什么。）
A 想说的事：{about}
要求：真实、平淡、短。电话写 4–8 句来回；消息写 1–4 条（B 可能只回一两句，也可能没空回）。
B 的口气按她的自我卡：嘴硬心软、独立、情绪藏得住。不要提任何恋爱、对象、暧昧相关的事。
输出 JSON：lines 是 [[说话人 "A"或"B", 内容], ...]；npc_note 是 A 事后记下的一句话；neno_note 是 B 视角的一句话总结（"妈打电话来问……"这种）。"""

CONTACT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lines", "npc_note", "neno_note"],
                  "properties": {
                      "lines": {"type": "array", "maxItems": 10, "items": {"type": "array", "items": {"type": "string"},
                                                                          "minItems": 2, "maxItems": 2}},
                      "npc_note": {"type": "string"}, "neno_note": {"type": "string"}}}


def _request(messages, schema, name, max_tokens) -> str:
    from app.llm.openrouter_client import chat_with_openrouter

    extra = json.loads(os.getenv("WORLD_LLM_EXTRA_BODY", "") or "{}")
    extra["response_format"] = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
    return chat_with_openrouter(
        api_key=os.getenv("WORLD_LLM_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip(),
        url=os.getenv("WORLD_LLM_URL", "").strip() or os.getenv("OPENROUTER_URL", "").strip(),
        model_name=_model(), messages=messages, timeout=45, max_tokens=max_tokens, extra_body=extra, purpose="world_npc")


def romance_hit(text: str, kin: bool) -> bool:
    """关系锁：她的感情线世界不碰。家人说"想你了/宝贝"是亲情，放行。"""
    if _ROMANCE.search(text or ""):
        return True
    return (not kin) and bool(_ROMANCE_NON_KIN.search(text or ""))


def _messages(row) -> list[dict]:
    ctx = json.loads(row["context"])
    if row["kind"] == "npc_plan":
        return [{"role": "system", "content": PLAN_PROMPT.format(relation=ctx["relation"])},
                {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}]
    return [{"role": "user", "content": CONTACT_PROMPT.format(
        channel_word="打电话" if ctx["channel"] == "call" else "微信消息", label=ctx["label"], relation=ctx["relation"],
        card=ctx["card"], neno_card=ctx["neno_card"], neno_doing=ctx["neno_doing"],
        body_line=("，身上：" + "、".join(ctx["neno_body"])) if ctx["neno_body"] else "",
        missed_line="（刚才 A 打来她没接到，现在是她回过去/看到消息）" if ctx["missed"] else "",
        about=ctx["about"] or "随便问问近况")}]


def step(store, max_calls_per_day: int | None = None) -> dict:
    """领一条排队的 NPC 请求 → 调模型（不持锁）→ 落地。后台循环反复调。"""
    limit = max_calls_per_day if max_calls_per_day is not None else int(os.getenv("WORLD_NPC_MAX_CALLS_PER_DAY", "40"))
    now = time.time()
    with store.transaction(write=True) as c:
        used = c.execute("SELECT count(*) FROM world_llm_calls WHERE kind LIKE 'npc_%' AND status NOT IN ('queued','dropped')"
                         " AND created_at >= ?", (int(now // 86400) * 86400,)).fetchone()[0]
        row = c.execute("SELECT * FROM world_llm_calls WHERE status='queued' AND (kind LIKE 'npc_%' OR kind LIKE 'neno_%')"
                        " ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return {"idle": "nothing_queued"}
        streak = 0
        for r in c.execute("SELECT status FROM world_llm_calls WHERE kind LIKE 'npc_%' AND status IN ('applied','failed')"
                           " ORDER BY id DESC LIMIT 5"):
            if r[0] != "failed":
                break
            streak += 1
        last_fail = c.execute("SELECT max(finished_at) FROM world_llm_calls WHERE kind LIKE 'npc_%' AND status='failed'").fetchone()[0]
        if streak and last_fail and now - last_fail < min(3600, 120 * 2 ** (streak - 1)):
            return {"idle": "provider_failure_backoff"}
        if used >= limit and row["kind"].startswith("npc_"):
            c.execute("UPDATE world_llm_calls SET status='dropped', finished_at=?, result=? WHERE id=?",
                      (now, encode({"reason": "daily_npc_budget"}), row["id"]))
            return {"idle": "daily_npc_budget"}
        c.execute("UPDATE world_llm_calls SET status='requesting', created_at=? WHERE id=?", (now, row["id"]))
        row = dict(row)
    raw, error = None, None
    try:
        if row["kind"].startswith("neno_"):
            from app.services.world import self_mind
            ctx = json.loads(row["context"])
            if row["kind"] == "neno_decide":
                raw = self_mind._request(self_mind.decide_prompt(ctx), self_mind.DECIDE_SCHEMA, "neno_decide")
            else:
                raw = self_mind._request(self_mind.reply_prompt(ctx, ctx["their_lines"]), self_mind.REPLY_SCHEMA, "neno_reply")
        else:
            schema, name, tokens = ((PLAN_SCHEMA, "npc_day", 600) if row["kind"] == "npc_plan"
                                    else (CONTACT_SCHEMA, "npc_contact", 900))
            raw = _request(_messages(row), schema, name, tokens)
    except Exception as exc:
        error = type(exc).__name__
    return finish(store, row, raw, error)


def finish(store, row: dict, raw: str | None, error: str | None) -> dict:
    with store.transaction(write=True) as c:
        w = world(c, row["world_id"])
        ctx = json.loads(row["context"])
        status, result = "failed", {}
        try:
            if error:
                raise ValueError(f"provider:{error}")
            text = (raw or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(text)
            kin = bool(ctx.get("kin"))
            if romance_hit(json.dumps(data, ensure_ascii=False), kin):
                emit(c, w, f"npc-lock:{row['id']}", "npc.relationship_lock", {"npc": row["subject"], "call_id": row["id"]},
                     row["subject"])
                raise WorldError("relationship lock: romance content dropped")
            appliers = {"npc_plan": _apply_plan, "npc_contact": _apply_contact,
                        "neno_reply": _apply_reply, "neno_decide": _apply_decide}
            result = appliers[row["kind"]](c, w, row, ctx, data)
            status = "applied"
        except (ValueError, WorldError, KeyError, TypeError) as exc:
            result = {"reason": str(exc)[:300]}
            if error:
                result["provider_error"] = True
        c.execute("UPDATE world_llm_calls SET status=?, finished_at=?, response=?, result=? WHERE id=?",
                  (status, time.time(), (raw or "")[:20000], encode(result), row["id"]))
        if status != "applied" and row["kind"] == "neno_decide":
            # 她没想成（模型失败/输出坏）：别让她干等着，回到规则挑事
            from app.services.world import life
            life.schedule_choice(c, w, json.loads(row["context"])["aid"], 1, tag=f":fallback{row['id']}")
            status_save = True
        else:
            status_save = status == "applied"
        if status_save:
            from app.services.world.engine import WorldEngine
            WorldEngine._save_world(c, w)
        return {"call_id": row["id"], "kind": row["kind"], "status": status, **result}


def land_contact(c, w, ctx, lines, note, key) -> int:
    """一次联络落地：她记一句（进【当前情境】"今天：…"），打电话缓解社交需求。"""
    neno = entity(c, w["world_id"], "neno")
    inbox = neno["state"].setdefault("recent_contacts", [])
    inbox.append({"from": ctx["label"], "channel": ctx["channel"], "at": w["sim_time"], "note": note,
                  "lines": [[ctx["label"] if s == "A" else "我", t] for s, t in lines]})
    neno["state"]["recent_contacts"] = inbox[-6:]
    save_entity(c, neno)
    if ctx["channel"] == "call":
        rows = rhythms.settle(c, w, "neno")
        if "social" in rows:
            rows["social"]["value"] = max(0.0, rows["social"]["value"] - 0.25)
            rhythms._reschedule(c, w, rows["social"])
    return emit(c, w, key, "npc.contacted",
                {"npc": ctx.get("npc_id") or ctx["npc"], "channel": ctx["channel"], "note": note, "lines": lines}, "neno")


def _apply_plan(c, w, row, ctx, data) -> dict:
    npc_id = row["subject"]
    if data.get("memory_note"):
        _memory(w, npc_id).append(f"{ctx['date']}：{data['memory_note'][:80]}")
        del _memory(w, npc_id)[:-12]
    emit(c, w, f"npc-day:{row['id']}", "npc.day", {"day": [str(x)[:60] for x in data.get("day", [])][:5]}, npc_id)
    if not ctx.get("contact_today") or not data.get("contact_time"):
        return {"contact": None}
    try:
        hh, mm = [int(x) for x in str(data["contact_time"]).split(":")[:2]]
    except ValueError:
        hh, mm = ctx["contact_hours"][0], 0
    low, high = ctx["contact_hours"]
    hh = max(low, min(high - 1, hh))
    _, start = rhythms._local_day(w, w["sim_time"])
    when = start + hh * 3600 + mm * 60
    if when <= w["sim_time"]:
        when = w["sim_time"] + 1800
    channel = data.get("contact_channel") or "message"
    job(c, w["world_id"], f"npc_contact:{npc_id}:{ctx['day']}", when, "npc_contact",
        {"npc": npc_id, "channel": channel, "about": str(data.get("contact_about") or "")[:80]})
    return {"contact": {"at": when, "channel": channel}}


def _apply_contact(c, w, row, ctx, data) -> dict:
    npc_id = row["subject"]
    lines = [[("A" if s == "A" else "B"), str(t)[:120]] for s, t in data.get("lines", [])][:10]
    if not lines:
        raise ValueError("empty exchange")
    from app.services.world import self_mind
    if self_mind.enabled(w) and self_mind.used_today(c, w) < self_mind.per_day() * 2:
        # 她有自己的脑子：NPC 只留自己的台词，她那边交给她自己回
        theirs = [t for s, t in lines if s == "A"][:4]
        if data.get("npc_note"):
            _memory(w, npc_id).append(f"{ctx['time']}：{data['npc_note'][:80]}")
            del _memory(w, npc_id)[:-12]
        reply_ctx = {**ctx, "their_lines": theirs, "npc_id": npc_id}
        c.execute("INSERT INTO world_llm_calls(world_id,kind,subject,status,created_at,sim_time,model,context)"
                  " VALUES (?,?,?,'queued',?,?,?,?)",
                  (w["world_id"], "neno_reply", npc_id, time.time(), w["sim_time"], self_mind._model(), encode(reply_ctx)))
        return {"handoff": "neno_reply", "their_lines": len(theirs)}
    if data.get("npc_note"):
        _memory(w, npc_id).append(f"{ctx['time']}：{data['npc_note'][:80]}")
        del _memory(w, npc_id)[:-12]
    note = str(data.get("neno_note") or "")[:120]
    event = land_contact(c, w, ctx, lines, note, f"npc-contact:{row['id']}")
    return {"event_id": event, "lines": len(lines)}


def _apply_reply(c, w, row, ctx, data) -> dict:
    """她自己回了：双方台词交错落地（对方先说，她接；多出来的放后面）。"""
    theirs, mine = ctx["their_lines"], [str(x)[:120] for x in (data.get("replies") or [])][:4]
    lines = []
    for i in range(max(len(theirs), len(mine))):
        if i < len(theirs):
            lines.append(["A", theirs[i]])
        if i < len(mine):
            lines.append(["B", mine[i]])
    event = land_contact(c, w, ctx, lines, str(data.get("note") or "")[:120], f"neno-reply:{row['id']}")
    return {"event_id": event, "replies": len(mine)}


def _apply_decide(c, w, row, ctx, data) -> dict:
    from app.services.world import self_mind
    return self_mind.apply_decision(c, w, row, data)
