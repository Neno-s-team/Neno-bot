"""测试里起整个 app，不能把线上世界带起来。"""
from __future__ import annotations


def test_app_under_test_never_starts_the_live_world(client):
    """.env 里世界是开着的；测试进程要是把它启动了，就会在真实数据库上推进世界、联网、调模型。"""
    state = client.app.state
    assert state.world_engine is None and state.world_task is None
    assert getattr(state, "world_real_task", None) is None and getattr(state, "world_weather_task", None) is None
