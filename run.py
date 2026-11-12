"""一条命令把 Neno 跑起来：python run.py

第一次运行会：从 .env.example 生成 .env → 问你要一次 OpenRouter 的密钥 → 启动后端（世界引擎 + 大脑）
→ 进入聊天。.env 里填了 TELEGRAM_BOT_TOKEN 就走 Telegram 机器人，没填就在这个终端里聊。
Ctrl+C（或聊天里输入 /quit）一起停掉。日志在 logs/backend.log。

python run.py --check   只检查配置、不启动
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"


def read_env(path: Path = ENV) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    return values


def set_env(key: str, value: str, path: Path = ENV) -> None:
    """改 .env 里的一行（没有就加在末尾），别的行原样不动。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*=", line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_env(ask=input, path: Path = ENV, example: Path | None = None) -> dict[str, str]:
    """保证 .env 能用：没有就从 .env.example 生成；内部令牌自动生成；模型密钥没有就问一次。"""
    example = example or path.with_name(".env.example")
    if not path.exists():
        shutil.copyfile(example, path)
        print("已从 .env.example 生成 .env。")
    values = read_env(path)
    for key in ("NENO_INTERNAL_TOKEN", "ADMIN_TOKEN"):      # 后端和通道之间的暗号，自己生成，不用管
        if not values.get(key):
            set_env(key, secrets.token_urlsafe(24), path)
    if not values.get("OPENROUTER_API_KEY"):
        print("\n需要一个模型密钥。去 https://openrouter.ai/keys 建一个（sk-or- 开头），充几美元就够用很久。")
        key = ask("把密钥粘贴到这里，回车：").strip()
        if not key:
            raise SystemExit("没有密钥，先不启动。之后把它写进 .env 的 OPENROUTER_API_KEY 再运行。")
        set_env("OPENROUTER_API_KEY", key, path)
    return read_env(path)


def problems(values: dict[str, str]) -> list[str]:
    """启动前能看出来的配置问题。"""
    found = []
    if not values.get("OPENROUTER_API_KEY"):
        found.append("OPENROUTER_API_KEY 没填")
    if values.get("MIND_ENABLED", "").lower() != "true" or values.get("MIND_ASYNC_DELIVERY", "").lower() != "true":
        found.append("MIND_ENABLED / MIND_ASYNC_DELIVERY 要是 true（她的大脑和分条发送）")
    if not values.get("MIND_L1_MODEL"):
        found.append("MIND_L1_MODEL 没填（第一反应用的小模型）")
    if values.get("WORLD_ENABLED", "").lower() == "true" and not values.get("MIND_WORLD_ID"):
        found.append("开了世界（WORLD_ENABLED）但没填 MIND_WORLD_ID")
    return found


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    os.chdir(ROOT)
    values = ensure_env()
    found = problems(values)
    for item in found:
        print("配置问题：" + item)
    if "--check" in argv:
        print("配置没问题。" if not found else "")
        return 1 if found else 0
    if found:
        return 1
    os.environ.update({k: v for k, v in values.items() if k not in os.environ})
    port = values.get("NENO_PORT", "8000")
    os.environ.setdefault("NENO_CORE_URL", f"http://127.0.0.1:{port}")
    (ROOT / "logs").mkdir(exist_ok=True)
    log = open(ROOT / "logs" / "backend.log", "a", encoding="utf-8")
    print("正在启动后端（世界引擎 + 大脑）…")
    backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port],
                               cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        from channels.core import Core

        core = Core()
        if not core.wait(90) or backend.poll() is not None:
            print("后端没起来。看 logs/backend.log 最后几行：")
            print("".join((ROOT / "logs" / "backend.log").read_text(encoding="utf-8", errors="replace").splitlines(True)[-15:]))
            return 1
        if values.get("TELEGRAM_BOT_TOKEN"):
            from channels import telegram_bot
            return telegram_bot.main(core)
        from channels import terminal
        return terminal.main(core)
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=10)
        except subprocess.TimeoutExpired:
            backend.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
