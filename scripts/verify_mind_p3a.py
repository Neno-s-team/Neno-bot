"""P3a 端到端验收：假时钟跑完"上课收消息 → 下课回复"全流程，打印时间线。

不调真实模型、不发 TG、不重启服务；跑完自动清理临时库。
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zoneinfo import ZoneInfo  # noqa: E402

workdir = Path(tempfile.mkdtemp(prefix="neno-p3a-"))
(workdir / "data").mkdir()

import app.config as config  # noqa: E402
import app.storage.db as db  # noqa: E402
import app.storage.operations as operations_storage  # noqa: E402
import app.services.mind.clock as mind_clock  # noqa: E402

config.MIND_ENABLED = True
config.MIND_ASYNC_DELIVERY = True
config.MIND_L1_MODEL = "fake/l1"
config.MIND_FOCUS_WINDOW_MIN = 0
config.MIND_L0_LAMBDA_PER_MIN = {"0": 0, "1": 30.0, "2": 0.12, "3": 0.35, "4": 3.0}
schedule_path = workdir / "schedule.json"
schedule_path.write_text(json.dumps({
    "sleep": {"start": "00:30", "end": "08:00"},
    "weekly": [
        {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "09:40",
         "activity": "上课", "level": 1, "phone": "包里", "muted": True},
        {"days": [1, 2, 3, 4, 5], "start": "10:00", "end": "11:40",
         "activity": "上课", "level": 1, "phone": "包里", "muted": True},
    ],
    "default": {"activity": "空闲", "level": 3, "phone": "身上", "muted": False},
}, ensure_ascii=False), encoding="utf-8")
config.MIND_SCHEDULE_PATH = str(schedule_path)

db.DB_DIR = workdir / "data"
db.DB_PATH = workdir / "data" / "bot.db"
operations_storage._instance = None

from app.storage.db import add_message, init_db, fetch_all  # noqa: E402
from app.services.mind import orchestration, perception, schedule, store  # noqa: E402
from app.services.mind import scheduler as mind_scheduler  # noqa: E402

T_MON_0810 = datetime(2026, 10, 12, 8, 10).timestamp()
T_MON_0941 = datetime(2026, 10, 12, 9, 41).timestamp()
clock_state = {"t": T_MON_0810}
mind_clock.now = lambda: clock_state["t"]
mind_clock.local_now = lambda: datetime.fromtimestamp(clock_state["t"], ZoneInfo("Asia/Shanghai"))


class FakeProvider:
    def __init__(self):
        self.calls = {"mind_l1": 0, "mind_expr": 0}

    def __call__(self, *, purpose, **_kw):
        if purpose in self.calls:
            self.calls[purpose] += 1
        if purpose == "mind_l1":
            return json.dumps({
                "category": "分享", "gut": "想他", "affect_delta": {"valence": 0.2, "arousal": 0.1},
                "urgency": 0.1, "cues": [], "depth": "normal", "reply_will": "later",
            }, ensure_ascii=False)
        if purpose == "mind_expr":
            return "好哦，下课啦"
        raise AssertionError(purpose)


provider = FakeProvider()
import app.services.mind.appraisal as appraisal_mod  # noqa: E402
import app.services.mind.expression as expression_mod  # noqa: E402

appraisal_mod.request_model_response = provider
expression_mod.request_model_response = provider

init_db()


def ts_to_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")


def main() -> int:
    session = "verify-p3a"
    add_message(session, "user", "下课一起吃饭吗", trace_id="t1")
    record = perception.create_perception(session, "t1")
    print(f"[{ts_to_str(T_MON_0810)}] 收到消息（上课时段 level={schedule.segment_at(T_MON_0810)['level']}）")
    print(f"  -> 抽样的看手机时刻 glance_at = {ts_to_str(record['glance_at'])}")

    clock_state["t"] = record["glance_at"] + 1
    mind_scheduler.process_glance(session, "t1")
    record = store.get_perception_by_trace("t1")
    print(f"[{ts_to_str(clock_state['t'])}] 到点：seen={record['seen']}（上课只看预览，不标已读）")
    assert record["seen"] == "preview"
    assert record["l1_preview_json"], "预览应跑一次 L1"

    deliveries = fetch_all("SELECT kind FROM mind_delivery WHERE session_id=?", (session,))
    assert not any(row["kind"] == "mark_read" for row in deliveries), "预览不标已读"
    print("  -> 没有 mark_read（预览不标已读）OK")

    clock_state["t"] = T_MON_0941
    print(f"[{ts_to_str(T_MON_0941)}] 下课：level={schedule.segment_at(T_MON_0941)['level']}")
    orchestration.handle_opened(session, ["t1"])
    pending = [
        dict(row) for row in fetch_all(
            "SELECT * FROM mind_delivery WHERE session_id=? AND status='pending' ORDER BY seq",
            (session,),
        )
    ]
    assert pending, "下课后应生成投递计划"
    kinds = [row["kind"] for row in pending]
    print(f"  -> 投递计划：{kinds}")
    assert kinds[0] == "mark_read"
    assert "text" in kinds
    print("  -> mark_read 在 text 之前 OK")

    cycle = store.get_cycle(orchestration.derive_cycle_id(session, "async:t1"))
    wm = json.loads(cycle["wm_json"])
    print(f"  -> 工作记忆时间：{wm['situation']['local_time']}（应为 09:4x）")
    assert wm["situation"]["local_time"].startswith("2026-10-12 09:4")

    print()
    print(f"L1 调用 {provider.calls['mind_l1']} 次（预览 1 + 决策 1 + 周期内全文 1）")
    print(f"E2 调用 {provider.calls['mind_expr']} 次")
    print("PASS: verify_mind_p3a")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
