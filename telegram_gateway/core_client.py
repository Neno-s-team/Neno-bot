from __future__ import annotations

from typing import Any

import httpx


class NenoCoreError(RuntimeError):
    pass


class NenoCoreNotFound(NenoCoreError):
    pass


class NenoCoreClient:
    def __init__(self, base_url: str, internal_token: str = ""):
        headers = {"X-Internal-Token": internal_token} if internal_token else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=300)

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        *,
        session_id: str,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/chat",
            json={
                "message": message,
                "session_id": session_id,
                "source": "telegram",
                "request_id": request_id,
                "attachments": attachments or [],
            },
        )
        if response.status_code >= 400:
            raise NenoCoreError(f"Neno Core returned HTTP {response.status_code}")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("status") == "accepted":
            # 异步模式：core 已受理（感知/待想队列接管），不要求 reply
            return payload
        if not isinstance(payload, dict) or not str(payload.get("reply") or "").strip():
            raise NenoCoreError("Neno Core returned an empty reply")
        return payload

    async def mind_due(self, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """GET /mind/deliveries/due：core 侧原子认领后返回的待执行项。"""
        response = await self._client.get(
            "/mind/deliveries/due",
            params={"session_id": session_id, "limit": limit},
        )
        if response.status_code >= 400:
            raise NenoCoreError(f"Neno Core returned HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def mind_ack(self, delivery_id: int, status: str, error: str | None = None) -> dict[str, Any]:
        """POST /mind/deliveries/{id}/ack。duplicate 也算成功。"""
        response = await self._client.post(
            f"/mind/deliveries/{delivery_id}/ack",
            json={"status": status, "error": error},
        )
        if response.status_code >= 400:
            raise NenoCoreError(f"Neno Core returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise NenoCoreError("Neno Core ack did not return ok")
        return payload

    async def mind_retract(self, trace_id: str) -> dict[str, Any]:
        """POST /mind/retract。404 视为已完成（消息已不在感知里）。"""
        response = await self._client.post("/mind/retract", json={"trace_id": trace_id})
        if response.status_code == 404:
            return {"ok": True, "trace_id": trace_id, "not_found": True}
        if response.status_code >= 400:
            raise NenoCoreError(f"Neno Core returned HTTP {response.status_code}")
        return response.json()
