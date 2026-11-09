"""以用户自己的 TG 账号做真号实测：发消息、看她的已读/输入中/回复时机。

独立 TDLib 库（data/telegram/tester），不碰 Neno 网关的会话。
  python scripts/tg_tester.py login            # 交互登录（手机号/验证码只在本终端输入）
  python scripts/tg_tester.py chats            # 列出私聊，找到 Neno
  python scripts/tg_tester.py target <chat_id> # 记住 Neno 的 chat_id
  python scripts/tg_tester.py say --gap 1 --watch 240 "第一条" "第二条"
  python scripts/tg_tester.py watch --watch 120
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram_gateway.auth import authorize  # noqa: E402
from telegram_gateway.config import GatewayConfig  # noqa: E402
from telegram_gateway.tdlib_client import TdlibClient  # noqa: E402

BASE = ROOT / "data" / "telegram" / "tester"
TARGET = BASE / "target.txt"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


async def _client(interactive: bool) -> TdlibClient:
    cfg = GatewayConfig.from_env()
    cfg = dataclasses.replace(cfg, database_directory=BASE / "database", files_directory=BASE / "files")
    cfg.database_directory.mkdir(parents=True, exist_ok=True)
    cfg.files_directory.mkdir(parents=True, exist_ok=True)
    client = TdlibClient(cfg.sidecar_binary, cfg.tdjson_library)
    await client.start()
    await client.send({"@type": "setLogVerbosityLevel", "new_verbosity_level": 1})
    await client.send(cfg.tdlib_parameters_request())
    await authorize(client, interactive=interactive,
                    database_encryption_key_b64=cfg.database_encryption_key_b64())
    return client


def _describe(update: dict, chat_id: int, sent: dict[int, str]) -> str | None:
    kind = update.get("@type")
    if kind == "updateChatReadOutbox" and update.get("chat_id") == chat_id:
        return f"已读 ✓✓ 到 msg {update.get('last_read_outbox_message_id')}"
    if kind == "updateChatAction" and update.get("chat_id") == chat_id:
        action = (update.get("action") or {}).get("@type", "")
        return "正在输入…" if action == "chatActionTyping" else f"动作 {action}"
    if kind == "updateNewMessage":
        msg = update.get("message") or {}
        if msg.get("chat_id") != chat_id:
            return None
        content = msg.get("content") or {}
        ctype = content.get("@type")
        who = "我" if msg.get("is_outgoing") else "她"
        if msg.get("is_outgoing"):
            return None
        if ctype == "messageText":
            return f"{who}：{content['text']['text']}"
        if ctype == "messageSticker":
            return f"{who}：[表情包 {(content.get('sticker') or {}).get('emoji', '')}]"
        return f"{who}：[{ctype}]"
    if kind == "updateMessageSendSucceeded":
        msg = update.get("message") or {}
        if msg.get("chat_id") == chat_id:
            sent[msg["id"]] = "ok"
            return f"我发出 msg {msg['id']}"
    if kind == "updateDeleteMessages" and update.get("chat_id") == chat_id and update.get("is_permanent"):
        return f"消息被删除 {update.get('message_ids')}"
    return None


async def _watch(client: TdlibClient, chat_id: int, seconds: float, sent: dict[int, str], t0: float) -> None:
    end = time.time() + seconds
    updates = client.updates()
    while time.time() < end:
        try:
            update = await asyncio.wait_for(anext(updates), timeout=max(0.1, end - time.time()))
        except asyncio.TimeoutError:
            break
        line = _describe(update, chat_id, sent)
        if line:
            print(f"[{_ts()} +{time.time() - t0:6.1f}s] {line}", flush=True)


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["login", "chats", "target", "say", "watch"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--gap", type=float, default=1.0)
    ap.add_argument("--watch", type=float, default=180.0)
    opts = ap.parse_args()

    client = await _client(interactive=opts.cmd == "login")
    try:
        if opts.cmd == "login":
            me = await client.request({"@type": "getMe"})
            print(f"登录完成：user_id={me.get('id')}")
            return
        if opts.cmd == "chats":
            res = await client.request({"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": 30})
            for cid in res.get("chat_ids", []):
                chat = await client.request({"@type": "getChat", "chat_id": cid})
                if (chat.get("type") or {}).get("@type") == "chatTypePrivate":
                    print(cid, chat.get("title"))
            return
        if opts.cmd == "target":
            TARGET.write_text(str(int(opts.args[0])), encoding="utf-8")
            print("已记住", opts.args[0])
            return
        chat_id = int(TARGET.read_text(encoding="utf-8").strip())
        await client.request({"@type": "getChat", "chat_id": chat_id})
        await client.request({"@type": "openChat", "chat_id": chat_id})
        sent: dict[int, str] = {}
        t0 = time.time()
        if opts.cmd == "say":
            for i, text in enumerate(opts.args):
                if i:
                    await _watch(client, chat_id, opts.gap, sent, t0)
                await client.request({"@type": "sendMessage", "chat_id": chat_id, "input_message_content": {
                    "@type": "inputMessageText", "text": {"@type": "formattedText", "text": text}}})
                print(f"[{_ts()} +{time.time() - t0:6.1f}s] 我：{text}", flush=True)
        await _watch(client, chat_id, opts.watch, sent, t0)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
