"""手机上的入口：准备好工作目录和配置，在调用它的线程里把后端跑起来。由 BackendService 调用。

后端代码和电脑版是同一份（构建时从仓库根目录拷进来）。这里只做手机上不一样的三件事：
- 后端用的都是相对路径（data/、prompts/、content/）：把工作目录切到 App 自己的可写目录，
  随包带来的 prompts/ 和 content/ 每次启动同步过去（包里的那份升级时会被系统换掉，不能往里写）。
- 没有 .env：默认值取随包带的 env.example，再盖上界面里填的档位和密钥。
- uvicorn 用纯 Python 的那套（asyncio + h11），只监听本机。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_server = None


def prepare(home: str, settings_json: str = "{}") -> str:
    """切工作目录、同步随包内容、把配置放进环境。返回工作目录。"""
    work = Path(home) / "neno"
    work.mkdir(parents=True, exist_ok=True)
    for name in ("prompts", "content"):
        if (HERE / name).exists():
            shutil.copytree(HERE / name, work / name, dirs_exist_ok=True)
    os.chdir(work)
    import run      # 仓库根目录的 run.py：只用它读配置的那个小函数

    values = run.read_env(HERE / "env.example")
    values.update({key: str(value) for key, value in json.loads(settings_json or "{}").items() if value not in (None, "")})
    # 用量和收发账本默认放在代码旁边；手机上代码目录升级时会被换掉，改放到可写的数据目录里
    (work / "data").mkdir(exist_ok=True)
    values["NENO_OPERATIONS_DB"] = str(work / "data" / "operations.db")
    os.environ.update(values)
    return str(work)


def start(home: str, settings_json: str, port: int) -> None:
    """把后端跑起来（阻塞，直到 stop()）。"""
    global _server
    prepare(home, settings_json)
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=int(port), loop="asyncio", http="h11", ws="none",
                            lifespan="on", access_log=False, log_level="info")
    _server = uvicorn.Server(config)
    _server.run()


def stop() -> None:
    if _server is not None:
        _server.should_exit = True


def name() -> str:
    """她叫什么：取自人设卡里"名字："那一行。界面的标题和通知用。"""
    try:
        for line in (HERE / "prompts" / "mind" / "self_card.md").read_text(encoding="utf-8").splitlines():
            if "名字" in line and ("：" in line or ":" in line):
                return line.replace(":", "：").split("：", 1)[1].strip()
    except OSError:
        pass
    return ""


def cities() -> str:
    """第一次打开时"住在"那一行：能选的城市，JSON [[名字, 显示名, 时区], …]，杭州排第一。只有一座时界面只显示、不让选。
    时区是给界面算"她那边现在是早上还是晚上"用的。"""
    import run

    found = []
    for name, label in run.cities(HERE):
        pack = json.loads((HERE / "content" / "world" / "city" / name / "pack.json").read_text(encoding="utf-8"))
        found.append([name, label, pack.get("timezone") or "Asia/Shanghai"])
    return json.dumps(found, ensure_ascii=False)


def personas() -> str:
    """第一次打开时能选谁，JSON。现在只有"她"；角色卡把"他"做成配置之后，在这里加上 "he"，界面上那张卡就能选了。"""
    return json.dumps(["she"])


def try_keys(settings_json: str) -> str:
    """第一次设置时真发一次最小的请求，看密钥通不通。通了返回空串，否则返回一句人话（不带报错原文）。"""
    import run

    values = run.read_env(HERE / "env.example")
    values.update({key: str(value) for key, value in json.loads(settings_json or "{}").items() if value not in (None, "")})
    kind, detail = run.key_problem(values)
    if kind:
        print("key test:", detail)      # 原文只进日志（adb logcat）
    return {"": "", "network": "连不上这家的服务器。看一下网络，再点一次。",
            "key": "这家说这串密钥不对。可能没复制全，或者这串已经被删掉了。",
            "balance": "密钥是对的，但账户里还没有钱。充一点再回来点一次。"}.get(kind, "这家暂时没答上来。等一会儿再点一次。")


def export(home: str, dest: str) -> str:
    """设置里的"导出数据"：把聊天记录和她的世界存成一个 zip。成了返回空串，否则返回一句人话。

    数据库正在被后端写着，不能直接拷文件：每个库先用 SQLite 自己的备份做一份完整的，再放进压缩包。
    密钥不在里面——它存在 App 的系统设置里，不在数据目录。
    """
    import tempfile
    import zipfile

    data = Path(home) / "neno" / "data"
    try:
        with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
            for index, path in enumerate(sorted(data.rglob("*"))):
                rel = path.relative_to(data)
                if not path.is_file() or rel.parts[0] in ("tmp", "logs", "cache") or path.name.endswith(("-wal", "-shm", "-journal")):
                    continue
                if path.suffix == ".db":
                    copy = Path(tmp) / f"{index}.db"
                    source, target = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True), sqlite3.connect(copy)
                    try:
                        source.backup(target)
                    finally:
                        source.close()
                        target.close()
                    out.write(copy, "data/" + rel.as_posix())
                else:
                    out.write(path, "data/" + rel.as_posix())
        return ""
    except Exception as exc:
        print("export failed:", repr(exc))      # 原文只进日志
        return "没存成。看看手机空间够不够，再试一次。"


def probe() -> str:
    """M0 要验的几条里能在机器上直接看的：解释器、原生依赖装没装上、SQLite 带不带全文检索。返回 JSON。"""
    found: dict[str, object] = {"python": sys.version.split()[0], "platform": sys.platform, "sqlite": sqlite3.sqlite_version}
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        conn.execute("INSERT INTO t(body) VALUES ('她 在 图书馆 写 论文')")
        found["fts5"] = conn.execute("SELECT COUNT(*) FROM t WHERE t MATCH '图书馆'").fetchone()[0] == 1
    except Exception as exc:
        found["fts5"] = f"{type(exc).__name__}: {exc}"
    for name in ("pydantic", "pydantic_core", "fastapi", "uvicorn", "PIL", "tzdata"):
        try:
            module = __import__(name)
            found[name] = getattr(module, "__version__", "ok")
        except Exception as exc:
            found[name] = f"{type(exc).__name__}: {exc}"
    try:
        from zoneinfo import ZoneInfo

        found["timezone"] = str(ZoneInfo("Asia/Shanghai"))
    except Exception as exc:
        found["timezone"] = f"{type(exc).__name__}: {exc}"
    return json.dumps(found, ensure_ascii=False)
