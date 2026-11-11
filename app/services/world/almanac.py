"""日历（E0；模块叫 almanac，免得和标准库 calendar 撞名）：这一天是什么日子——周末、法定假日、节日、学期/考试周/寒暑假。

数据在 content/world/calendar/<name>.json；世界的作息配置里 `calendar.name` 指定用哪份（没配就只有周几）。
别处用 `tags`：weekend / workday / holiday / festival / term / exam / break / off（不用上课的日子）。
- 作息表条目可写 `except: ["holiday", "break"]` / `only: ["term"]`：那天不适用。
- 空档选项可写 `when: {"weekend": 0.3, "exam": 0.5}`：那天对这件事的兴致加减。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

CONTENT = Path(__file__).resolve().parents[3] / "content" / "world" / "calendar"
_WEEK = "一二三四五六日"


@lru_cache(maxsize=4)
def load(name: str) -> dict:
    return json.loads((CONTENT / f"{name}.json").read_text(encoding="utf-8"))


def _config(w) -> dict | None:
    return (w["settings"].get("rhythms") or {}).get("calendar")


def local_date(w, ts: float) -> date:
    offset = w["settings"].get("utc_offset_minutes", 0) * 60
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ts + offset)).date()


def _in(span: dict, iso: str) -> bool:
    return span["from"] <= iso <= span["to"]


def day(w, ts: float) -> dict:
    """ts 所在的本地这一天：{"date", "weekday"(1–7), "tags", "names"(节日/假期名), "term"(学期阶段名)}。"""
    return on(w, local_date(w, ts))


def on(w, d: date) -> dict:
    cfg = _config(w)
    iso = d.isoformat()
    weekday = d.isoweekday()
    tags: set[str] = set()
    names: list[str] = []
    term = None
    weekend = weekday >= 6
    if cfg:
        data = load(cfg["name"])
        if iso in data.get("makeup_workdays", []):
            weekend = False  # 调休上班
        for h in data.get("holidays", []):
            if _in(h, iso):
                tags.add("holiday")
                if h["name"] not in names:
                    names.append(h["name"] + "假期" if not h["name"].endswith("假期") else h["name"])
        festival = data.get("festivals", {}).get(iso)
        if festival:
            tags.add("festival")
            names = [n for n in names if not n.startswith(festival)]
            names.insert(0, festival)
        for t in data.get("terms", []):
            if _in(t, iso):
                tags.add(t["tag"])
                term = t["name"]
                break
    tags.add("weekend" if weekend else "workday")
    if weekend or tags & {"holiday", "break"}:
        tags.add("off")
    return {"date": iso, "weekday": weekday, "tags": tags, "names": names, "term": term}


def allows(w, d: date, entry: dict) -> bool:
    """作息条目这天适不适用（only / except）。"""
    if not entry.get("only") and not entry.get("except"):
        return True
    tags = on(w, d)["tags"]
    if entry.get("only") and not tags & set(entry["only"]):
        return False
    return not tags & set(entry.get("except", []))


def boost(w, ts: float, when: dict | None) -> float:
    """选项的 `when` 在这一天加起来是多少。"""
    if not when:
        return 0.0
    tags = day(w, ts)["tags"]
    return sum(value for tag, value in when.items() if tag in tags)


def note(w, ts: float) -> str:
    """给她的脑子：今天是什么日子（"周六；冬至；寒假"）。平常的教学日只有周几。"""
    info = day(w, ts)
    parts = [f"周{_WEEK[info['weekday'] - 1]}" + ("（周末）" if "weekend" in info["tags"] else "")]
    parts += info["names"]
    if info["term"] and not info["tags"] & {"term"}:
        parts.append(info["term"])
    return "；".join(parts)
