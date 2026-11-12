"""一条命令把 Neno 跑起来：python run.py

第一次运行会：从 .env.example 生成 .env → 问你用哪家的模型（默认 DeepSeek，国内直连）→ 要一次密钥并当场测一下
→ 启动后端（世界引擎 + 大脑）→ 进入聊天。.env 里填了 TELEGRAM_BOT_TOKEN 就走 Telegram 机器人，没填就在这个终端里聊。
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


# 第一次运行时的两个选项：(档位, 说明, 要的密钥, 去哪申请)
CHOICES = [
    ("cn", "DeepSeek —— 国内直连，支付宝/微信充值，充 10 块钱能用很久", "DEEPSEEK_API_KEY",
     "https://platform.deepseek.com/api_keys"),
    ("intl", "OpenRouter —— 要能连上海外网络，一个密钥能用各家模型", "OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
]
WHERE = {key: url for _, _, key, url in CHOICES}


def models(values: dict[str, str]) -> dict[str, str]:
    """各层最后用什么模型：设了档位就是档位 + 单独覆盖的；没设就是 .env 里各自写的。"""
    from app.llm import presets

    name = values.get("MODEL_PRESET", "").strip().lower()
    if name in presets.PRESETS:
        return presets.models(name, values)
    return {env: values.get(env, "").strip() for env in presets.LAYERS}


def keys_needed(values: dict[str, str]) -> list[str]:
    from app.llm import providers

    return providers.needed_keys(list(models(values).values())) or ["OPENROUTER_API_KEY"]


def try_keys(values: dict[str, str], post=None) -> str:
    """每个要用的提供方各发一次最小的请求。都通了返回空串，否则返回一句人话。"""
    import requests

    from app.llm import providers

    post = post or requests.post
    seen = set()
    for model in models(values).values():
        name = providers.split(model)[0] or "openrouter"
        if not model or name in seen:
            continue
        seen.add(name)
        spec = providers.PROVIDERS[name]
        url = values.get(spec["url_env"], "").strip() or spec["url"]
        key = values.get(spec["key_env"], "").strip()
        body = providers.adapt(name, {"model": providers.split(model)[1], "max_tokens": 16,
                                      "messages": [{"role": "user", "content": "回一个字：好"}]})
        proxy = values.get("OPENROUTER_PROXY", "").strip() if name == "openrouter" else ""
        try:
            resp = post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body,
                        timeout=40, proxies={"http": proxy, "https": proxy} if proxy else None)
        except Exception as exc:
            return f"连不上 {spec['label']}（{type(exc).__name__}）。检查网络" + ("或代理 OPENROUTER_PROXY。" if name == "openrouter" else "。")
        if resp.status_code == 401:
            return f"{spec['label']} 说这个密钥不对。"
        if resp.status_code == 402:
            return f"{spec['label']} 的账户里没有余额，先充值。"
        if resp.status_code != 200:
            return f"{spec['label']} 返回了 {resp.status_code}：{resp.text[:120]}"
    return ""


def ensure_env(ask=input, path: Path = ENV, example: Path | None = None, check=try_keys) -> dict[str, str]:
    """保证 .env 能用：没有就从 .env.example 生成；内部令牌自动生成；
    缺模型密钥就问用哪家、要密钥、当场测一次（测不过可以重输，也可以先跳过）。"""
    example = example or path.with_name(".env.example")
    if not path.exists():
        shutil.copyfile(example, path)
        print("已从 .env.example 生成 .env。")
    values = read_env(path)
    for key in ("NENO_INTERNAL_TOKEN", "ADMIN_TOKEN"):      # 后端和通道之间的暗号，自己生成，不用管
        if not values.get(key):
            set_env(key, secrets.token_urlsafe(24), path)
    missing = [key for key in keys_needed(values) if not values.get(key)]
    if not missing:
        return read_env(path)
    if values.get("MODEL_PRESET", "").strip().lower() in ("cn", "intl"):     # 只要一个密钥的档位：先问用哪家
        print("\n她需要一个大模型来想事情。用哪家的？")
        for i, (_, label, _, _) in enumerate(CHOICES, 1):
            print(f"  {i}. {label}")
        pick = ask("输入 1 或 2，直接回车选 1：").strip()
        preset, _, key, _ = CHOICES[1 if pick == "2" else 0]
        set_env("MODEL_PRESET", preset, path)
        missing = [key]
    for attempt in range(3):
        for key in missing:
            print(f"\n需要 {key}。去 {WHERE.get(key, '对应的平台')} 建一个密钥。")
            value = ask("把密钥粘贴到这里，回车：").strip()
            if not value:
                raise SystemExit(f"没有密钥，先不启动。之后把它写进 .env 的 {key} 再运行。")
            set_env(key, value, path)
        print("测试一下……")
        wrong = check(read_env(path))
        if not wrong:
            print("通了。")
            break
        print(wrong)
        if attempt == 2 or ask("重新输入密钥按回车；输入 s 先跳过（之后可能聊不了）：").strip().lower() == "s":
            break
    return read_env(path)


def cities(root: Path = ROOT) -> list[tuple[str, str]]:
    """有哪些城市可选：[(名字, 显示名)]，杭州排第一（它是做得最细的那座）。"""
    import json

    found = []
    for path in sorted((root / "content" / "world" / "city").glob("*/pack.json")):
        if (path.parent / "map.json").is_file():
            found.append((path.parent.name, json.loads(path.read_text(encoding="utf-8")).get("label") or path.parent.name))
    return sorted(found, key=lambda item: (item[0] != "hangzhou", item[0]))


def ensure_city(ask=input, path: Path = ENV, root: Path = ROOT) -> str:
    """她住在哪座城市：只在世界还没建的时候问一次（建好以后改了也不算数——中途换城等于搬家）。返回选中的城市名。"""
    values = read_env(path)
    if values.get("WORLD_CITY"):
        return values["WORLD_CITY"]
    options = cities(root)
    if (root / "data" / "bot.db").exists() or len(options) <= 1:
        return options[0][0] if options else ""
    print("\n她住在哪座城市？选离你近的，你们就在同一片天气里，能看同一场电影和展览。")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    pick = ask("输入序号，直接回车选 1：").strip()
    name = options[int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(options) else 0][0]
    set_env("WORLD_CITY", name, path)
    return name


def problems(values: dict[str, str]) -> list[str]:
    """启动前能看出来的配置问题。"""
    from app.llm import presets

    found = []
    preset = values.get("MODEL_PRESET", "").strip().lower()
    if preset and preset not in presets.PRESETS:
        found.append(f"MODEL_PRESET={preset} 不认识，可选：{' / '.join(presets.PRESETS)}")
    found += [f"{key} 没填" for key in keys_needed(values) if not values.get(key)]
    if values.get("MIND_ENABLED", "").lower() != "true" or values.get("MIND_ASYNC_DELIVERY", "").lower() != "true":
        found.append("MIND_ENABLED / MIND_ASYNC_DELIVERY 要是 true（她的大脑和分条发送）")
    if not models(values).get("MIND_L1_MODEL"):
        found.append("MIND_L1_MODEL 没填（第一反应用的小模型）")
    if values.get("WORLD_ENABLED", "").lower() == "true" and not values.get("MIND_WORLD_ID"):
        found.append("开了世界（WORLD_ENABLED）但没填 MIND_WORLD_ID")
    return found


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    os.chdir(ROOT)
    ensure_env()
    ensure_city()
    values = read_env()
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
