from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from telegram_gateway.tdlib_client import TdlibClient, TdlibProcessError


def _prompt(name: str) -> str:
    value = input(f"Telegram {name}: ").strip()
    if not value:
        raise RuntimeError(f"Telegram {name} must not be blank")
    return value


async def authorize(
    client: TdlibClient,
    *,
    interactive: bool,
    prompt: Callable[[str], str] = _prompt,
    timeout: float = 300,
    database_encryption_key_b64: str = "",
) -> None:
    """Drive TDLib's authorization state machine without logging secrets."""
    async for update in _updates_until_ready(client, timeout=timeout):
        if update.get("@type") != "updateAuthorizationState":
            continue
        state = update.get("authorization_state") or {}
        state_type = str(state.get("@type") or "")
        if state_type == "authorizationStateReady":
            return
        if state_type == "authorizationStateWaitTdlibParameters":
            continue
        if state_type == "authorizationStateWaitEncryptionKey":
            await client.request(
                {
                    "@type": "checkDatabaseEncryptionKey",
                    "encryption_key": database_encryption_key_b64,
                },
                timeout=30,
            )
            continue
        if not interactive:
            raise TdlibProcessError(
                f"Telegram authorization requires interactive login ({state_type}); "
                "run: python -m telegram_gateway --login"
            )
        if state_type == "authorizationStateWaitPhoneNumber":
            await client.request(
                {
                    "@type": "setAuthenticationPhoneNumber",
                    "phone_number": prompt("phone number"),
                    "settings": {
                        "@type": "phoneNumberAuthenticationSettings",
                        "allow_flash_call": False,
                        "allow_missed_call": False,
                        "is_current_phone_number": False,
                        "allow_sms_retriever_api": False,
                    },
                },
                timeout=60,
            )
        elif state_type == "authorizationStateWaitCode":
            await client.request({"@type": "checkAuthenticationCode", "code": prompt("login code")}, timeout=60)
        elif state_type == "authorizationStateWaitPassword":
            await client.request({"@type": "checkAuthenticationPassword", "password": prompt("2FA password")}, timeout=60)
        elif state_type == "authorizationStateWaitRegistration":
            raise TdlibProcessError("the configured phone number is not registered in Telegram")
        elif state_type == "authorizationStateWaitOtherDeviceConfirmation":
            link = str(state.get("link") or "")
            raise TdlibProcessError(
                "Telegram requires another-device confirmation"
                + (f"; open the displayed link: {link}" if link else "")
            )
        elif state_type in {
            "authorizationStateWaitEmailAddress",
            "authorizationStateWaitEmailCode",
            "authorizationStateWaitRegistration",
        }:
            raise TdlibProcessError(f"unsupported Telegram authorization state: {state_type}")
        elif state_type in {"authorizationStateClosing", "authorizationStateClosed"}:
            raise TdlibProcessError("Telegram authorization closed before becoming ready")


async def _updates_until_ready(client: TdlibClient, *, timeout: float):
    while True:
        try:
            update = await asyncio.wait_for(anext(client.updates()), timeout=timeout)
        except StopAsyncIteration:
            return
        yield update
