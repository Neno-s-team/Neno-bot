from __future__ import annotations

import asyncio

from telegram_gateway.auth import authorize
from telegram_gateway.config import GatewayConfig
from telegram_gateway.tdlib_client import TdlibClient


async def login() -> None:
    config = GatewayConfig.from_env()
    client = TdlibClient(config.sidecar_binary, config.tdjson_library)
    try:
        await client.start()
        await client.request(config.tdlib_parameters_request(), timeout=60)
        await authorize(
            client,
            interactive=True,
            database_encryption_key_b64=config.database_encryption_key_b64(),
        )
        print("Telegram authorization completed. The session is stored in TDLib's database directory.")
    finally:
        await client.close()
