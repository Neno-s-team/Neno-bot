"""发布版导出：只带白名单里的文件，带出私人信息要能查出来。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("export_release", Path(__file__).resolve().parents[2] / "scripts" / "export_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_only_whitelisted_paths_go_out():
    paths = ["app/main.py", "channels/core.py", "content/world/life.json", "prompts/mind/self_card.md", "tests/unit/x.py",
             "README.md", "LICENSE", "run.py", "docs/how-it-works.md", "scripts/build-tdlib.ps1",
             # 下面这些留在开发仓库
             "docs/HANDOFF.md", "docs/mind-plan.md", ".claude/skills/x.md", ".cursorrules", "prompts/system.txt",
             "scripts/tg_tester.py", "scripts/mcp5323.ps1", "assets/neno/a.png", "memory-mcp-notes.md"]
    assert release.selected(paths) == sorted(paths[:10])


def test_local_secrets_and_common_key_shapes_are_caught(tmp_path):
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "OPENROUTER_API_KEY=sk-or-v1-" + "ab12" * 8,
        "TELEGRAM_ALLOWED_CHAT_IDS=123456789,-100987654321",
        "MIND_HOME_SESSION=tg:123456789",
        "OPENROUTER_PROXY=http://127.0.0.1:7890",
        "MIND_L1_MODEL=anthropic/claude-haiku-5.5",     # 不是私人值
        "ADMIN_TOKEN=abc",                              # 太短，不拿来比对（会到处误报）
    ]), encoding="utf-8")
    needles = release.private_values(env, tmp_path / "none.db")
    assert needles == {"sk-or-v1-" + "ab12" * 8, "123456789", "100987654321"}
    out = tmp_path / "out"
    (out / "docs").mkdir(parents=True)
    (out / ".git").mkdir()
    (out / ".git" / "config").write_text("123456789", encoding="utf-8")                 # 发布仓库自己的 .git 不查
    (out / "a.py").write_text("ALLOWED = [123456789]  # 手滑写进来的\n", encoding="utf-8")
    (out / "docs" / "b.md").write_text("联系 someone" + "@" + "gmail.com；Co-Authored-By: x <noreply@anthropic.com>；git@github.com:a/b\n",
                                       encoding="utf-8")
    (out / "c.txt").write_text("模型 anthropic/claude-haiku-5.5；token 12345678:" + "A" * 35 + "\n", encoding="utf-8")
    (out / "ok.py").write_text("print('没什么特别的')\n", encoding="utf-8")
    hits = release.scan(out, needles)
    assert len(hits) == 3
    assert hits[0].startswith("a.py: 出现了本机的私人值（123…，9 个字符）")              # 报出来的只有开头几个字符
    assert hits[1].startswith("c.txt: Telegram 机器人 token") and hits[2].startswith("docs/b.md: 邮箱 someon")
