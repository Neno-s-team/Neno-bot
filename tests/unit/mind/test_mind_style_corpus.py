"""说话方式语料：只进措辞层；按情境挑片段、有抖动、避开长串连发；没有语料时什么都不加。"""
from __future__ import annotations

import json

from app import config
from app.services.mind import expression, style_corpus
from app.services.mind.schemas import E1Plan

ROWS = [
    {"lines": [["乙", "我想喝奶茶"], ["甲", "喝咖啡"], ["甲", "就不困难了"], ["乙", "可是没有瑞幸"]]},
    {"lines": [["乙", "上课好无聊"], ["乙", "[裂开]"], ["甲", "下一个金铲铲"]]},
    {"lines": [["甲", "晚安"], ["乙", "嗯嗯 晚安"]]},
    {"lines": [["乙", "奶茶"], ["乙", "奶茶"], ["乙", "奶茶"], ["乙", "奶茶"], ["乙", "奶茶"], ["甲", "好"]]},
] + [{"lines": [["甲", f"随便聊{i}"], ["乙", f"嗯{i}"]]} for i in range(20)]


def _corpus(tmp_path, monkeypatch, k=2):
    path = tmp_path / "style.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS), encoding="utf-8")
    monkeypatch.setattr(config, "MIND_STYLE_CORPUS_PATH", str(path))
    monkeypatch.setattr(config, "MIND_STYLE_EXAMPLES", k)


def test_picks_relevant_and_avoids_long_bursts(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch)
    picked = style_corpus.pick("想喝奶茶 选择困难", "seed")
    assert picked[0][0] == ("乙", "我想喝奶茶")
    assert all(lines[0][1] != "奶茶" for lines in picked)  # 五连发那段被压下去


def test_seed_varies_the_filler(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch, k=4)
    picks = {tuple(map(tuple, (tuple(x) for x in style_corpus.pick("随便", f"s{i}")))) for i in range(6)}
    assert len(picks) > 1
    assert style_corpus.pick("随便", "same") == style_corpus.pick("随便", "same")


def test_examples_go_to_expression_only_and_are_marked_not_hers(tmp_path, monkeypatch):
    _corpus(tmp_path, monkeypatch)
    seen = {}

    def fake(**kwargs):
        seen["messages"] = kwargs["messages"]
        return "要的要的"

    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake)
    expression.run_e2(E1Plan(say_points=["想喝奶茶"], n_messages=1), "风格卡", "", [], "喝奶茶吗", style_seed="c1")
    system = seen["messages"][0]["content"]
    assert system[0]["cache_control"] and "cache_control" not in system[-1]
    assert "我想喝奶茶" in system[-1]["text"] and "不要把里面的事当成你的经历" in system[-1]["text"]


def test_no_corpus_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MIND_STYLE_CORPUS_PATH", str(tmp_path / "missing.jsonl"))
    monkeypatch.setattr(config, "MIND_STYLE_EXAMPLES", 6)
    assert style_corpus.pick("随便", "s") == [] and style_corpus.render([]) == ""
