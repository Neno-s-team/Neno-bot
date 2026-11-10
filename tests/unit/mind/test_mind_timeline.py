"""时间线：模型看到的过去都带"离现在多久"和具体日期，隔得久的对话中间插一行提示。"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config
from app.services.mind import clock, timeline

TZ = ZoneInfo("Asia/Shanghai")


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


NOW = at("2026-11-11 21:30")  # 周三


@pytest.fixture(autouse=True)
def fixed(monkeypatch):
    monkeypatch.setattr(config, "MIND_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setattr(clock, "now", lambda: NOW)


@pytest.mark.parametrize("when,expect", [
    ("2026-11-11 21:29", "刚才"),
    ("2026-11-11 21:00", "30 分钟前"),
    ("2026-11-11 18:30", "3 小时前"),
    ("2026-11-11 09:00", "今天上午"),
    ("2026-11-10 22:00", "昨天晚上"),
    ("2026-11-09 15:00", "前天下午"),
    ("2026-11-07 15:00", "4 天前"),
    ("2026-11-02 15:00", "上周一"),
    ("2026-10-20 15:00", "10月20日"),
])
def test_ago(when, expect):
    assert timeline.ago(at(when)) == expect


def test_day_word_and_stamp():
    assert timeline.day_word(at("2026-11-12 10:00")) == "明天"
    assert timeline.day_word(at("2026-11-10 10:00")) == "昨天"
    assert timeline.stamp(at("2026-11-10 22:05")) == "昨天 22:05"
    assert timeline.stamp(at("2026-11-05 08:00")) == "11月5日 周四 08:00"
    assert timeline.today_line() == "2026年11月11日 周三 晚上21:30"


def _utc(text):
    return datetime.fromtimestamp(at(text), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_turns_mark_gaps():
    turns = [{"role": "user", "content": "晚安", "created_at": _utc("2026-11-09 23:50")},
             {"role": "assistant", "content": "晚安", "created_at": _utc("2026-11-09 23:51")},
             {"role": "user", "content": "在吗", "created_at": _utc("2026-11-11 21:00")}]
    lines = timeline.turns_with_time(turns)
    assert lines[0] == "——前天 23:50——"
    assert lines[3] == "——隔了 1 天（今天 21:00）——" and lines[4] == "对方：在吗"
    assert lines[-1] == "——到现在过了 30 分钟——"
    assert timeline.turns_with_time([{"role": "user", "content": "hi"}]) == ["对方：hi"]  # 没有时间就不编


def test_working_memory_shows_dates_and_gaps(isolated_db):
    from app.services.mind.memory import store as memory_store, wm_builder
    from app.services.mind.schemas import L1Appraisal
    memory_store.insert_episode("s", "c", 0, summary="他说周末去爬山", ts=at("2026-11-08 20:00"))
    history = [{"role": "user", "content": "晚安", "created_at": _utc("2026-11-09 23:50")},
               {"role": "user", "content": "在吗", "created_at": _utc("2026-11-11 21:00")}]
    memory = wm_builder.build_working_memory("s", l1=L1Appraisal.from_dict({"cues": ["爬山"]}), message="爬山的事",
                                             history=history, self_card="")
    text = wm_builder.render_working_memory(memory)
    assert "现在是 2026年11月11日 周三 晚上21:30" in text
    assert "[3 天前，11-08 20:00]" in text
    assert "——隔了 1 天（今天 21:00）——" in text


def test_intents_say_which_day(isolated_db):
    from app.services.mind import intents
    from app.storage.db import get_conn
    with get_conn() as conn:
        intents.add(conn, "s", "去图书馆", source="reflection", strength=0.7, due_at=at("2026-11-12 21:30"), key="k")
    assert intents.render("s")[0].startswith("去图书馆（刚才想的，打算明天做")
