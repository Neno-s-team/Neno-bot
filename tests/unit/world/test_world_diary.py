"""它的日记：从世界里的事挑值得说的，用它的口吻写成一条，发之前过一道闸。
它不是 Neno：人设是同一份，但不带私下那段关系；这一层不联网、不碰账号。"""
from __future__ import annotations

import json

from app.services.world import diary


def _event(kind, payload, at=1000.0, actor="neno"):
    return {"kind": kind, "payload": payload, "sim_time": at, "actor_id": actor}


def test_card_is_her_persona_without_the_private_relationship():
    card = diary.card()
    assert "Neno" in card and "嘴硬心软" in card
    assert "和对方的关系" not in card and "你和他" not in card and "抑制基线" not in card


def test_only_things_worth_saying_become_candidates():
    events = [
        _event("life.moment", {"text": "路边蹲着一只很胖的橘猫，盯着我看", "share": 0.55}),
        _event("life.moment", {"text": "刷手机看到《某篇文章》：有意思", "share": 0.8, "kind": "surf"}),   # 上网看到的不算她的日子
        _event("activity.started", {"activity": "study"}),                                             # 平常的事不进来
        _event("life.attended", {"title": "《神探之痕迹》", "times": 1, "plot": "一个痕检专家破案的故事。"}),
        _event("life.attended", {"title": "学校的银杏黄了", "times": 2}),                                 # 第二回去不再说
        _event("life.visit", {"name": "温州面馆", "visits": 1}),
        _event("life.visit", {"name": "温州面馆", "visits": 4}),
        _event("task.done", {"title": "课程小论文", "rushed": True}),
        _event("perturb.happened", {"label": "停电", "applied": [{"label": "台灯", "broke": True}]}),
        _event("weather.changed", {"weather": "snow", "to": "小雪"}, actor=None),
        _event("weather.changed", {"weather": "rain", "to": "小雨"}, actor=None),
        _event("life.moment", {"text": "别人的事", "share": 0.9}, actor="mom"),
    ]
    found = diary.candidates(events)
    assert [c["what"] for c in found] == [
        "路边蹲着一只很胖的橘猫，盯着我看", "去了《神探之痕迹》（一个痕检专家破案的故事。）", "第一次去温州面馆",
        "课程小论文弄完交了，赶在最后一刻", "台灯坏了", "下雪了（小雪）"]
    assert found[0]["weight"] == 0.55 and found[-1]["weight"] == 0.9 and all(0 <= c["weight"] <= 1 for c in found)


def test_compose_gives_the_model_her_card_the_event_and_recent_posts():
    seen = []

    def ask(messages, schema, name, max_tokens):
        seen.append((messages, max_tokens))
        return '好的：{"post": "“路边蹲着只橘猫，胖得离谱。”"} 以上。'

    text = diary.compose(ask, "路边蹲着一只很胖的橘猫，盯着我看", diary.situation("周四", "16:10", "政苑路", "在路上"),
                         ["咖啡洒了半杯。"])
    assert text == "路边蹲着只橘猫，胖得离谱。"                       # 回答外面多了话、带引号：抠出来
    content = seen[0][0][1]["content"]
    assert "嘴硬心软" in content and "橘猫" in content and "- 咖啡洒了半杯。" in content and "周四 16:10；她在政苑路，在路上" in content
    assert "外面" not in content and seen[0][1] >= 1500                 # 没给天气就不提；给会思考的模型留够余量
    assert "外面小雨" in diary.situation("周四", "16:10", "家", "歇着", "小雨")
    assert diary.compose(lambda *a: "不是 JSON", "事", "此刻", []) == ""
    assert diary.compose(lambda *a: json.dumps({"post": ""}), "事", "此刻", []) == ""   # 模型觉得没什么可说


def test_gate_stops_what_should_not_go_out():
    recent = ["咖啡洒了半杯，擦桌子擦到一半，突然不想动了。"]
    assert diary.gate("一脚踩进水坑，鞋里能养鱼了。", recent) == ""
    assert diary.gate("", recent) and diary.gate("嗯", recent)
    assert diary.gate("想" * 91, recent) == "太长"
    assert diary.gate("这个不错 https://example.com/x", recent) == "带了链接"
    assert diary.gate("谢谢 @someone 的推荐", recent) == "带了 @ 或话题标签"
    assert diary.gate("下雪了 #初雪", recent) == "带了 @ 或话题标签"
    assert diary.gate("作为一个AI，我今天也在努力。", recent) == "出戏了"
    assert diary.gate("咖啡洒了半杯，擦桌子擦到一半，忽然不想动了。", recent) == "和最近发过的太像"
    assert diary.weighted_len("ab你好") == 6
