"""把微信私聊导出洗成"说话方式"语料：连续小段、去垃圾、脱敏、打场景标签。只在本地，data/ 已 gitignore。

用法：python scripts/corpus_wechat.py <文字消息-分类 目录> <输出名> <学谁:them|me> <会话名前缀...>
输出 data/corpus/wechat/<输出名>.jsonl，每行一段：{"source","lines":[["她"|"他", 文本],...],"tags":[...]}
"她" = 要学的那一侧，"他" = 另一侧。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime

LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*?): (.*)$")
DROP = re.compile(r"https?://|<msg|<appmsg|当前版本不支持|\[通话\]|转账|红包|已支付|撤回了一条消息|拍了拍|"
                  r"\[(图片|语音|视频|文件|链接|位置|名片|小程序|聊天记录)\]")
CODE = re.compile(r"[A-Za-z0-9\-]{14,}")
PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
NUMBER_ID = re.compile(r"(?<!\d)\d{6,}(?!\d)")
GAP_MIN = 25
MIN_LINES, MAX_LINES = 4, 12
TAGS = {
    "晚安": "晚安|睡了|睡觉|困了|好困|早点睡|熬夜", "早安": "早安|早上好|醒了|起床",
    "吃": "吃|饭|奶茶|咖啡|拿铁|外卖|饿|好吃", "游戏": "打游戏|上分|开黑|排位|王牌|段位|刷|副本|一把",
    "撒娇": "嘛|嘿嘿|哼|要抱|想你|陪我|求求", "吐槽": "无语|绷不住|离谱|服了|烦死|气死|什么鬼",
    "安慰": "没事|别难过|抱抱|辛苦|累了|别哭|会好的", "约": "明天|周末|几点|一起|出来|在哪|来吗",
    "拌嘴": "你才|滚|不理你|哼|算了|随便你", "追剧": "电视剧|追剧|好看|看了没|电影",
}


def clean(text: str) -> str | None:
    text = text.strip()
    if not text or DROP.search(text):
        return None
    if text.startswith("[引用]"):
        quoted, _, reply = text[4:].partition("|")
        quoted, reply = quoted.strip(), reply.strip()
        if not reply or "<" in quoted:
            return clean(reply) if reply else None
        text = f"（回复“{quoted[:15]}”）{reply}"
    if CODE.search(text):
        return None
    text = PHONE.sub("<手机号>", text)
    text = NUMBER_ID.sub("<数字>", text)
    return text[:120]


def segments(path: str, target_is_me: bool):
    rows = []
    for raw in open(path, encoding="utf-8"):
        m = LINE.match(raw.rstrip("\n"))
        if m:
            rows.append((datetime.fromisoformat(m.group(1)), m.group(2), m.group(3)))
    chunk, last = [], None
    for when, who, text in rows:
        if last is not None and (when - last).total_seconds() > GAP_MIN * 60:
            yield chunk
            chunk = []
        last = when
        text = clean(text)
        if text is None:
            continue
        side = "她" if (who == "我") == target_is_me else "他"
        if len(chunk) >= 2 and chunk[-1] == [side, text] and chunk[-2] == [side, text]:
            continue  # 连发同一句只留两次
        chunk.append([side, text])
    yield chunk


def windows(chunk):
    for start in range(0, len(chunk), MAX_LINES):
        part = chunk[start:start + MAX_LINES]
        if len(part) >= MIN_LINES and sum(1 for s, _ in part if s == "她") >= 2 and any(s == "他" for s, _ in part):
            yield part


def main():
    root, name, side, prefixes = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    out = os.path.join(os.path.dirname(__file__), "..", "data", "corpus", "wechat", name + ".jsonl")
    kept = lines = 0
    with open(out, "w", encoding="utf-8") as f:
        for path in sorted(glob.glob(os.path.join(root, "私聊", "*.txt"))):
            title = open(path, encoding="utf-8").readline().strip("# \n")
            if not any(title.startswith(p) for p in prefixes):
                continue
            source = f"s{prefixes.index(next(p for p in prefixes if title.startswith(p)))}"
            for chunk in segments(path, side == "me"):
                for part in windows(chunk):
                    joined = " ".join(t for _, t in part)
                    tags = [k for k, pat in TAGS.items() if re.search(pat, joined)]
                    f.write(json.dumps({"source": source, "lines": part, "tags": tags}, ensure_ascii=False) + "\n")
                    kept += 1
                    lines += len(part)
    print(f"{name}: {kept} 段 / {lines} 行 -> {os.path.normpath(out)}")


if __name__ == "__main__":
    main()
