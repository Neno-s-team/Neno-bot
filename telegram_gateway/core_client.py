from __future__ import annotations

from typing import Any

import httpx


class NenoCoreError(RuntimeError):
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
        if not isinstance(payload, dict) or not str(payload.get("reply") or "").strip():
            raise NenoCoreError("Neno Core returned an empty reply")
        return payload
