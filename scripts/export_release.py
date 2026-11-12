"""把当前提交导出成一份干净的发布版，并检查没有带出私人信息。

用法：python scripts/export_release.py <目标目录>

- 只导出已提交的内容（git archive HEAD）：工作区里没提交的改动、没入库的文件不会被带出去。
- 只导出白名单里的路径：开发笔记、内部计划、个人化的东西留在开发仓库。
- 导出后扫一遍：本机 .env 里的密钥/账号、本机数据库里记的账号名，只要在导出的文件里出现就报错；
  另外按通用格式找密钥和邮箱。有发现就退出码 1，不要发布。
- 目标目录里已有的 .git 会保留（发布仓库的历史只有一次次快照）。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 整个目录
DIRS = ["app/", "channels/", "content/", "prompts/mind/", "telegram_gateway/", "telegram_sidecar/", "tests/"]
# 单个文件
FILES = [
    "README.md", "LICENSE", ".env.example", ".gitignore", ".gitattributes", "requirements.txt", "pytest.ini",
    "run.py", "start.bat", "start.sh",
    "docs/telegram-user-account.md", "docs/how-it-works.md",
    "scripts/build-tdlib.ps1", "scripts/build-telegram-sidecar.ps1", "scripts/start-telegram-gateway.ps1",
    "scripts/start-neno-backend.ps1", "scripts/start-neno-stack.ps1", "scripts/open-neno-browser.ps1",
    "scripts/corpus_wechat.py", "scripts/stickers2webp.ps1", "scripts/export_release.py",
]
PRIVATE_KEYS = re.compile(r"KEY|TOKEN|HASH|SECRET|SESSION|CHAT_IDS|API_ID|PASSWORD|PROXY")
PATTERNS = {
    "OpenRouter 密钥": re.compile(r"sk-or-v1-[0-9a-f]{20,}"),
    "GitHub 令牌": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "Telegram 机器人 token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "邮箱": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}
OK_EMAILS = re.compile(r"@(example\.(com|org)|anthropic\.com|github\.com|users\.noreply\.github\.com)$|^git@")


def selected(paths: list[str]) -> list[str]:
    return sorted(p for p in paths if p in FILES or any(p.startswith(d) for d in DIRS))


def private_values(env: Path, db: Path) -> set[str]:
    """本机的私人值：.env 里的密钥和账号、数据库里记的账号名。只用来比对，不写到任何地方。"""
    found: set[str] = set()
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if PRIVATE_KEYS.search(key):
                    for part in re.split(r"[,\s]+", value.strip().strip("'\"")):
                        found.add(part.removeprefix("tg:").lstrip("-"))
    if db.exists():
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                for (settings,) in conn.execute("SELECT settings FROM world_worlds"):
                    for friend in ((json.loads(settings).get("surf") or {}).get("friends") or []):
                        found.update(str(v).lstrip("@") for v in friend.values())
        except (sqlite3.Error, ValueError):
            pass
    return {v for v in found if len(v) >= 6 and not v.startswith(("http://127.", "http://localhost"))}


def scan(root: Path, needles: set[str]) -> list[str]:
    hits = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        name = path.relative_to(root).as_posix()
        for needle in needles:
            if needle in text:
                hits.append(f"{name}: 出现了本机的私人值（{needle[:3]}…，{len(needle)} 个字符）")
        for label, pattern in PATTERNS.items():
            for match in pattern.findall(text):
                value = match if isinstance(match, str) else match[0]
                if label == "邮箱" and OK_EMAILS.search(value):
                    continue
                hits.append(f"{name}: {label} {value[:6]}…")
    return hits


def export(target: Path, root: Path = ROOT) -> list[str]:
    tracked = subprocess.run(["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"], cwd=root, capture_output=True,
                             check=True).stdout.decode("utf-8").split("\0")
    paths = selected([p for p in tracked if p])
    missing = [f for f in FILES if f not in paths]
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():          # 清掉上一次的导出，留着 .git
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD", "--", *paths], cwd=root, capture_output=True,
                             check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    print(f"导出 {len(paths)} 个文件到 {target}")
    for name in missing:
        print(f"注意：白名单里的 {name} 还没提交，这次没导出")
    return scan(target, private_values(root / ".env", root / "data" / "bot.db"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    problems = export(Path(sys.argv[1]).resolve())
    for line in problems:
        print("！" + line)
    print("没有发现私人信息。" if not problems else f"\n发现 {len(problems)} 处，先处理再发布。")
    sys.exit(1 if problems else 0)
