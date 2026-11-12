"""简单通道：终端、Telegram 机器人；以及 run.py 的一条命令启动。都不碰真网络。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

import run
from channels import terminal
from channels.core import Core, pump
from channels.telegram_bot import Bot


class FakeCore:
    session_id = "me"

    def __init__(self, batches=()):
        self.batches, self.acks, self.said = list(batches), [], []

    def due(self, limit=50):
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, Exception):
            raise batch
        return batch

    def ack(self, delivery_id, status="sent", error=None):
        self.acks.append((delivery_id, status, error))

    def say(self, text, request_id=None, source="local"):
        self.said.append((text, request_id, source))
        return {"status": "accepted"}


def _run(core, show, rounds):
    left = [rounds]

    def running():
        left[0] -= 1
        return left[0] >= 0

    pump(core, show, running, sleep=lambda s: None)


def test_pump_hands_each_item_to_the_channel_and_reports_back():
    text = {"id": 1, "kind": "text", "payload": {"text": "在"}}
    sticker = {"id": 2, "kind": "sticker", "payload": {"name": "猫猫"}}
    boom = {"id": 3, "kind": "text", "payload": {"text": "炸"}}
    core = FakeCore([[text, sticker, boom], requests.ConnectionError("后端断了"), [text]])   # 第三轮：回执丢了，后端又给了一次
    seen = []

    def show(item):
        seen.append(item["id"])
        if item["id"] == 3:
            raise RuntimeError("发不出去")
        return item["kind"] == "text"

    _run(core, show, 3)
    assert seen == [1, 2, 3]                                               # 第二次给的同一条没有再发一遍
    assert core.acks == [(1, "sent", None), (2, "failed", None), (3, "failed", "RuntimeError: 发不出去"), (1, "sent", None)]


def test_core_talks_to_the_backend_as_the_home_session(monkeypatch):
    monkeypatch.setenv("NENO_INTERNAL_TOKEN", "暗号".encode().hex())
    monkeypatch.delenv("MIND_HOME_SESSION", raising=False)
    monkeypatch.delenv("NENO_CORE_URL", raising=False)
    core = Core()
    assert core.session_id == "me" and core.url == "http://127.0.0.1:8000" and core.http.trust_env is False
    assert core.http.headers["X-Internal-Token"] == "暗号".encode().hex()
    sent = {}

    class Reply:
        def raise_for_status(self): pass
        def json(self): return {"status": "accepted"}

    monkeypatch.setattr(core.http, "post", lambda url, **kw: sent.update(url=url, **kw) or Reply())
    assert core.say("在吗", request_id="r1", source="terminal") == {"status": "accepted"}
    assert sent["url"].endswith("/chat") and sent["json"] == {
        "message": "在吗", "session_id": "me", "source": "terminal", "request_id": "r1", "attachments": []}
    monkeypatch.setenv("MIND_HOME_SESSION", "tg:42")
    assert Core().session_id == "tg:42"


def test_terminal_shows_what_arrives():
    assert terminal.render({"kind": "text", "payload": {"text": "刚下课"}}) == "Neno：刚下课"
    assert terminal.render({"kind": "sticker", "payload": {"name": "猫猫"}}) == "Neno：[表情包：猫猫]"
    assert "正在输入" in terminal.render({"kind": "typing", "payload": {"duration": 3}})
    assert terminal.render({"kind": "mark_read", "payload": {}}) is None


def test_terminal_says_when_she_will_see_it_only_if_it_takes_a_while():
    busy = {"activity": "上课", "phone": "包里", "muted": True, "level": 1, "sees_in": 3000}
    assert terminal.hint(busy) == "　（她在上课，手机在包里，静音。大概 50 分钟后会看到）"
    assert terminal.hint({**busy, "sees_in": 61, "muted": False, "phone": ""}) == "　（她在上课。大概 2 分钟后会看到）"
    assert terminal.hint({**busy, "activity": "睡觉", "sees_in": 6 * 3600}).endswith("大概 6 小时后会看到）")
    assert terminal.hint({**busy, "sees_in": 15}) is None and terminal.hint({**busy, "sees_in": None}) is None


def test_backend_tells_what_she_is_doing_and_when_she_will_look(isolated_db, monkeypatch):
    from app.routers import mind as mind_router
    from app.services.mind import clock, schedule
    from app.storage.db import execute_write

    monkeypatch.setattr(clock, "now", lambda: 1000.0)
    monkeypatch.setattr(schedule, "segment_at", lambda *a, **k: {"activity": "在路上", "level": 3, "phone": "手里", "muted": False})
    assert mind_router.now("me") == {"activity": "在路上", "place": "", "phone": "手里", "level": 3, "muted": False, "sees_in": None}
    for trace, glance, status, session in (("a", 1456.0, "pending", "me"), ("b", 1200.0, "opened", "me"), ("c", 1100.0, "pending", "other")):
        execute_write("INSERT INTO mind_perception(session_id, trace_id, received_at, glance_at, status) VALUES (?, ?, 1000, ?, ?)",
                      (session, trace, glance, status))
    assert mind_router.now("me")["sees_in"] == 456                          # 只看这个会话里还没点开的


@pytest.fixture()
def bot(tmp_path):
    core = FakeCore()
    made = Bot("TOKEN", core, state_path=tmp_path / "data" / "channel.json", sleep=lambda s: made.calls.append(("sleep", s)))
    made.calls = []
    made.call = lambda method, params=None, timeout=30: made.calls.append((method, params)) or []
    return made


def _msg(update_id, chat_id, text=None, kind="private", **extra):
    message = {"chat": {"id": chat_id, "type": kind}, **extra}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def test_bot_only_listens_to_whoever_sent_the_pairing_code(bot, tmp_path):
    assert bot.owner is None and len(bot.code) == 6 and bot.code.isdigit()
    bot.handle(_msg(1, 7, "你好"))
    assert bot.calls[-1][1]["text"].startswith("还没绑定") and bot.core.said == [] and bot.code not in str(bot.calls)
    bot.handle(_msg(2, 7, bot.code))
    assert bot.owner == 7 and bot.calls[-1][1]["text"].startswith("绑定好了")
    assert json.loads((tmp_path / "data" / "channel.json").read_text(encoding="utf-8")) == {"owner": 7}
    again = Bot("TOKEN", bot.core, state_path=tmp_path / "data" / "channel.json")
    assert again.owner == 7                                                 # 重启后还记得
    bot.handle(_msg(3, 7, "在干嘛"))
    bot.handle(_msg(4, 99, "让我也聊聊"))                                   # 别人：不理
    bot.handle(_msg(5, 7, "/start"))                                        # Telegram 的开场命令：不算跟她说的话
    bot.handle(_msg(6, 7, "群里的话", kind="group"))
    bot.handle(_msg(7, 7, photo=[{"file_id": "x"}]))
    assert bot.core.said == [("在干嘛", "tgbot:3", "telegram_bot"), ("[发来一张图片，这里看不了]", "tgbot:7", "telegram_bot")]


def test_bot_ignores_someone_guessing_the_code(bot):
    for i in range(10):
        bot.handle(_msg(i, 5, "000000" if bot.code != "000000" else "111111"))
    assert bot.owner is None and len([c for c in bot.calls if c[0] == "sendMessage"]) == 6


def test_bot_sends_what_it_can(bot):
    assert bot.show({"kind": "text", "payload": {"text": "在"}}) is False     # 还没绑定：没处发
    bot.state["owner"] = 7
    bot.calls.clear()
    assert bot.show({"kind": "text", "payload": {"text": "刚下课"}}) is True
    assert bot.calls == [("sendMessage", {"chat_id": 7, "text": "刚下课"})]
    bot.calls.clear()
    assert bot.show({"kind": "typing", "payload": {"duration": 9.0}}) is True  # 打字久："正在输入"要续上
    assert bot.calls == [("sendChatAction", {"chat_id": 7, "action": "typing"}), ("sleep", 4)] * 2 + [
        ("sendChatAction", {"chat_id": 7, "action": "typing"})]
    assert bot.show({"kind": "mark_read", "payload": {}}) is True
    assert bot.show({"kind": "sticker", "payload": {"name": "猫猫"}}) is False


def test_bot_does_not_lose_a_message_when_the_backend_is_down(bot):
    bot.state["owner"] = 7
    updates = [[_msg(10, 7, "第一句"), _msg(11, 7, "第二句")], [_msg(10, 7, "第一句"), _msg(11, 7, "第二句")], []]
    bot.call = lambda method, params=None, timeout=30: updates.pop(0) if method == "getUpdates" else []
    down = [True]

    def say(text, request_id=None, source="local"):
        if down[0]:
            down[0] = False
            raise requests.ConnectionError("后端没起来")
        bot.core.said.append(text)

    bot.core.say = say
    left = [3]

    def running():
        left[0] -= 1
        return left[0] >= 0

    bot.listen(running)
    assert bot.core.said == ["第一句", "第二句"] and bot.offset == 12


# ── run.py：一条命令启动 ────────────────────────────────────────────────────

def test_first_run_makes_a_working_env_and_asks_for_the_key_once(tmp_path, capsys):
    example = Path(run.ROOT / ".env.example")
    env = tmp_path / ".env"
    asked = []
    values = run.ensure_env(ask=lambda prompt: asked.append(prompt) or " sk-or-test ", path=env, example=example)
    assert len(asked) == 1 and values["OPENROUTER_API_KEY"] == "sk-or-test"
    assert len(values["NENO_INTERNAL_TOKEN"]) >= 24 and values["ADMIN_TOKEN"] != values["NENO_INTERNAL_TOKEN"]
    assert run.problems(values) == []                                       # 模板本身就是一份能用的配置
    assert values["MIND_HOME_SESSION"] == "me" and values["MIND_WORLD_ID"] == "neno-life" and values["TELEGRAM_BOT_TOKEN"] == ""
    again = run.ensure_env(ask=lambda prompt: pytest.fail("不该再问"), path=env, example=example)
    assert again == values                                                  # 第二次运行：不再问，也不重新生成暗号
    with pytest.raises(SystemExit):
        run.ensure_env(ask=lambda prompt: "", path=tmp_path / "other.env", example=example)


def test_set_env_changes_one_line_and_leaves_the_rest(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# 注释\nA=1\nB = old\n", encoding="utf-8")
    run.set_env("B", "new", env)
    run.set_env("C", "3", env)
    assert env.read_text(encoding="utf-8") == "# 注释\nA=1\nB=new\nC=3\n"
    assert run.read_env(env) == {"A": "1", "B": "new", "C": "3"}


def test_problems_are_spotted_before_starting():
    ok = {"OPENROUTER_API_KEY": "k", "MIND_ENABLED": "true", "MIND_ASYNC_DELIVERY": "true", "MIND_L1_MODEL": "m"}
    assert run.problems(ok) == []
    assert run.problems({**ok, "OPENROUTER_API_KEY": ""}) == ["OPENROUTER_API_KEY 没填"]
    assert len(run.problems({**ok, "MIND_ASYNC_DELIVERY": "false", "MIND_L1_MODEL": ""})) == 2
    assert "MIND_WORLD_ID" in run.problems({**ok, "WORLD_ENABLED": "true"})[0]
