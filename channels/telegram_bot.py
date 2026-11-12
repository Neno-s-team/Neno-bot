"""Telegram 机器人通道：python -m channels.telegram_bot

去 @BotFather 发 /newbot 拿一个 token，写进 .env 的 TELEGRAM_BOT_TOKEN，就这一步。
第一次启动会在终端里显示一个 6 位配对码，在 Telegram 里把它发给机器人，你就成了"主人"；
之后它只理你一个人（别人发什么都不理，免得被人拿你的额度聊天）。
绑定信息存在 data/channel.json，删掉这个文件可以重新配对。

机器人做不到的：显示"已读"、发表情包（表情包表在真人号网关里）、看图片。
她主动找你没问题，前提是你先跟机器人说过话（Telegram 的规矩）。
在国内要代理：TELEGRAM_BOT_PROXY=http://127.0.0.1:7890（不填就用系统代理）。
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

from channels.core import Core, pump


class Bot:
    def __init__(self, token: str, core: Core, state_path: str | Path = "data/channel.json", proxy: str = "",
                 sleep: Callable[[float], None] = time.sleep):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.core = core
        self.sleep = sleep
        self.http = requests.Session()
        if proxy:
            self.http.proxies = {"http": proxy, "https": proxy}
        self.path = Path(state_path)
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.code = "".join(secrets.choice("0123456789") for _ in range(6))
        self.tries: dict[int, int] = {}
        self.offset = 0

    @property
    def owner(self) -> int | None:
        return self.state.get("owner")

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> Any:
        data = self.http.post(self.api + method, json=params or {}, timeout=timeout).json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    def handle(self, update: dict[str, Any]) -> None:
        """一条来信。没绑定时只认配对码；绑定后只理主人。"""
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        chat_id = chat.get("id")
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if self.owner is None:
            self.tries[chat_id] = self.tries.get(chat_id, 0) + 1
            if self.tries[chat_id] > 6:
                return                          # 乱猜配对码的，不理了
            if text == self.code:
                self.state["owner"] = chat_id
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.state), encoding="utf-8")
                self.call("sendMessage", {"chat_id": chat_id, "text": "绑定好了。从现在起只有你能跟她说话。"})
            else:
                self.call("sendMessage", {"chat_id": chat_id, "text": "还没绑定。把启动窗口里显示的 6 位配对码发给我。"})
            return
        if chat_id != self.owner or text.startswith("/start"):
            return
        if not text:
            text = "[发来一张图片，这里看不了]" if msg.get("photo") else "[发来一条这里看不了的消息]"
        self.core.say(text, request_id=f"tgbot:{update['update_id']}", source="telegram_bot")

    def show(self, item: dict[str, Any]) -> bool:
        """把一项投递发出去。返回 False = 这个通道发不了（比如表情包）。"""
        kind, payload = item.get("kind"), item.get("payload") or {}
        if self.owner is None:
            return False
        if kind == "text":
            self.call("sendMessage", {"chat_id": self.owner, "text": str(payload.get("text", ""))[:4096]})
            return True
        if kind == "typing":                    # "正在输入"一次只亮 5 秒，打字久就续上
            left = float(payload.get("duration") or 0)
            while True:
                self.call("sendChatAction", {"chat_id": self.owner, "action": "typing"})
                if left <= 4:
                    return True
                self.sleep(4)
                left -= 4
        return kind == "mark_read"              # 已读：机器人没有这个概念，当它做了；表情包：发不了

    def listen(self, running: Callable[[], bool] = lambda: True) -> None:
        """长轮询收消息。后端暂时不通就等一等再试同一条，不丢。"""
        failures = 0
        while running():
            try:
                updates = self.call("getUpdates", {"offset": self.offset, "timeout": 25,
                                                   "allowed_updates": ["message"]}, timeout=40)
            except Exception as exc:
                print(f"　（连不上 Telegram：{type(exc).__name__}，3 秒后重试）")
                self.sleep(3)
                continue
            for update in updates:
                try:
                    self.handle(update)
                except Exception as exc:
                    failures += 1
                    if failures < 5:
                        print(f"　（这条没交到后端：{type(exc).__name__}，稍后重试）")
                        self.sleep(3)
                        break
                    print("　（这条一直交不过去，跳过）")
                failures = 0
                self.offset = int(update["update_id"]) + 1


def main(core: Core | None = None) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("没有 TELEGRAM_BOT_TOKEN。去 @BotFather 发 /newbot 拿一个，写进 .env。")
        return 1
    core = core or Core()
    if not core.wait(60):
        print(f"连不上后端（{core.url}）。先启动后端，或者直接用 python run.py。")
        return 1
    bot = Bot(token, core, proxy=os.getenv("TELEGRAM_BOT_PROXY", "").strip())
    try:
        name = bot.call("getMe").get("username", "")
    except Exception as exc:
        print(f"连不上 Telegram（{type(exc).__name__}）。检查 token；在国内要设 TELEGRAM_BOT_PROXY。")
        return 1
    print(f"机器人 @{name} 已上线。按 Ctrl+C 停止。")
    if bot.owner is None:
        print(f"\n    配对码：{bot.code}\n\n在 Telegram 里把这 6 位数字发给 @{name}，完成绑定。")
    threading.Thread(target=pump, args=(core, bot.show), daemon=True).start()
    try:
        bot.listen()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    sys.exit(main())
