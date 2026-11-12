"""终端里跟她聊：python -m channels.terminal

什么账号都不用。她不是一问一答的机器人：可能过一会儿才看到、分几条发、也可能自己来找你，
所以她的话是"到了才显示"，不是你回车后立刻出来。输入 /quit 退出。
"""

from __future__ import annotations

import sys
import threading
import time

from channels.core import Core, pump

NAME = "Neno"


def render(item: dict) -> str | None:
    """一项投递 → 终端里的一行；不用显示的返回 None。"""
    kind, payload = item.get("kind"), item.get("payload") or {}
    if kind == "text":
        return f"{NAME}：{payload.get('text', '')}"
    if kind == "sticker":
        return f"{NAME}：[表情包：{payload.get('name', '')}]"
    if kind == "typing":
        return "　（对方正在输入…）"
    return None                 # mark_read：终端里没有"已读"


def hint(now: dict) -> str | None:
    """刚发完消息时说一句她在干嘛、大概多久会看到——只在要等一阵的时候说。"""
    wait = now.get("sees_in")
    if wait is None or wait <= 20:
        return None
    phone = f"，手机在{now['phone']}" if now.get("phone") else ""
    when = f"{-(-wait // 60)} 分钟" if wait < 5400 else f"{round(wait / 3600)} 小时"
    return f"　（她在{now.get('activity') or '忙'}{phone}{'，静音' if now.get('muted') else ''}。大概 {when}后会看到）"


def main(core: Core | None = None) -> int:
    core = core or Core()
    if not core.wait(60):
        print(f"连不上后端（{core.url}）。先启动后端，或者直接用 python run.py。")
        return 1
    print(f"已连上。直接打字跟 {NAME} 说话，/quit 退出。她回得有快有慢，也可能自己来找你。\n")
    alive = True

    def show(item: dict) -> bool:
        line = render(item)
        if line:
            sys.stdout.write(f"\r{line}\n> ")
            sys.stdout.flush()
        return True

    threading.Thread(target=pump, args=(core, show, lambda: alive), daemon=True).start()
    try:
        while True:
            text = input("> ").strip()
            if text in ("/quit", "/exit", "/q"):
                break
            if text:
                try:
                    reply = core.say(text, source="terminal")
                except Exception as exc:
                    print(f"　（没发出去：{type(exc).__name__}）")
                    continue
                if reply.get("reply"):      # 后端没开异步投递时：当场回
                    print(f"{NAME}：{reply['reply']}")
                    continue
                try:
                    line = hint(core.now())
                except Exception:
                    line = None
                if line:
                    print(line)
    except (EOFError, KeyboardInterrupt):
        print()
    alive = False
    time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
