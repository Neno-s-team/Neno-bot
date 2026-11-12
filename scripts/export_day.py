"""把她某一天导出成一份 JSON：时间、地点、在干嘛、刷到的、听说的、去了哪家店、起过的念头（发了的和没发的）、睡前对这天说的一句。

做视频的素材从这里来——界面上没有"她的一天"这一页。只读数据库，后端开着也能跑。

用法：python scripts/export_day.py [YYYY-MM-DD] [输出文件]
  不给日期就是今天；不给输出文件就打印出来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")          # 要在导入 app.config 之前：会话名、世界名都从环境里取
    from fastapi import HTTPException

    from app import config
    from app.routers import mind

    date = argv[0] if argv else None
    try:
        day = mind.day(config.MIND_HOME_SESSION, date)
    except HTTPException as exc:
        print(f"日期要写成 2026-11-12 这样：{exc.detail}", file=sys.stderr)
        return 1
    text = json.dumps(day, ensure_ascii=False, indent=1)
    if len(argv) > 1:
        Path(argv[1]).write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"{day['date']}：{len(day['items'])} 件事、{len(day['thoughts'])} 个念头 → {argv[1]}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
