"""和后端说话的最小客户端，加一个"取到点该发的 → 交给通道去发 → 回执"的循环。"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Callable

import requests


class Core:
    def __init__(self, url: str | None = None, token: str | None = None, session_id: str | None = None):
        self.url = (url or os.getenv("NENO_CORE_URL", "") or "http://127.0.0.1:8000").rstrip("/")
        self.session_id = session_id or os.getenv("MIND_HOME_SESSION", "").strip() or "me"
        self.http = requests.Session()
        self.http.trust_env = False            # 后端在本机，不走系统代理
        token = token if token is not None else os.getenv("NENO_INTERNAL_TOKEN", "").strip()
        if token:
            self.http.headers["X-Internal-Token"] = token

    def healthy(self) -> bool:
        try:
            return self.http.get(f"{self.url}/health", timeout=3).status_code == 200
        except requests.RequestException:
            return False

    def wait(self, seconds: float = 60.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.healthy():
                return True
            time.sleep(1.0)
        return False

    def say(self, text: str, request_id: str | None = None, source: str = "local") -> dict[str, Any]:
        """把一句话交给后端。异步模式下后端只回"收到了"，她的回复之后从 due() 里出来。"""
        resp = self.http.post(f"{self.url}/chat", timeout=300, json={
            "message": text[:2000], "session_id": self.session_id, "source": source,
            "request_id": request_id or uuid.uuid4().hex, "attachments": []})
        resp.raise_for_status()
        return resp.json()

    def due(self, limit: int = 50) -> list[dict[str, Any]]:
        resp = self.http.get(f"{self.url}/mind/deliveries/due", params={"session_id": self.session_id, "limit": limit},
                             timeout=30)
        resp.raise_for_status()
        return list(resp.json().get("items") or [])

    def ack(self, delivery_id: int, status: str = "sent", error: str | None = None) -> None:
        self.http.post(f"{self.url}/mind/deliveries/{delivery_id}/ack", json={"status": status, "error": error},
                       timeout=30).raise_for_status()


def pump(core: Core, show: Callable[[dict[str, Any]], bool], running: Callable[[], bool] = lambda: True,
         poll: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> None:
    """循环：取到点该发的，一项项交给 show(item)。show 返回 True 记"已发"，False 记"失败"，抛异常也记失败。
    item: {"id", "kind": mark_read | typing | text | sticker, "payload": {...}}
    一句话发失败时，后端会把这一轮剩下的作废（不会隔很久突然补半句）。"""
    shown: set[int] = set()                 # 已经发出去的：回执没送到时后端会再给一次，不能再发一遍
    while running():
        try:
            items = core.due()
        except requests.RequestException:
            sleep(poll * 3)
            continue
        for item in items:
            key = int(item["id"])
            if key in shown:
                ok, error = True, None
            else:
                try:
                    ok, error = bool(show(item)), None
                except Exception as exc:    # 通道出错不该让循环死掉
                    ok, error = False, f"{type(exc).__name__}: {exc}"[:200]
                if ok:
                    shown.add(key)
            try:
                core.ack(key, "sent" if ok else "failed", error if not ok else None)
            except requests.RequestException:
                pass                        # 回执没送到：租约过期后后端会再给一次
        sleep(poll)
