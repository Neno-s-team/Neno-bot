"""把微信私聊导出洗成"说话方式"语料：连续小段、去垃圾、脱敏、打场景标签。只在本地，data/ 已 gitignore。

用法：python scripts/corpus_wechat.py <文字消息-分类 目录> <输出名> <学谁:them|me|both> <会话名前缀...>
输出 data/corpus/<输出名>.jsonl，每行一段：{"source","lines":[[说话人, 文本],...],"tags":[...]}
them/me：要学的一侧记"她"、另一侧记"他"；both：两侧都学，记"甲"/"乙"（不分谁是谁）。
可选环境变量 CORPUS_MASK=<文件>：每行一个要替换成 <人名> 的词（人工维护；联系人昵称多是普通词，不能自动用）。
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
PLACE = re.compile(r"[一-鿿]{2,8}(大学|学院|中学|小学|医院|公司|小区|广场|市|县|区|镇|村|路|街)(?![一-鿿])")
NAMES: list[str] = []
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
    text = PLACE.sub(lambda m: "<学校>" if m.group(1) in ("大学", "学院", "中学", "小学") else "<地名>", text)
    for name in NAMES:
        if name in text:
            text = text.replace(name, "<人名>")
    return text[:120]


def load_mask(path: str) -> list[str]:
    words = [line.strip() for line in open(path, encoding="utf-8") if line.strip() and not line.startswith("#")]
    return sorted(set(words), key=len, reverse=True)


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
        if target_is_me is None:
            side = "甲" if who == "我" else "乙"
        else:
            side = "她" if (who == "我") == target_is_me else "他"
        if len(chunk) >= 2 and chunk[-1] == [side, text] and chunk[-2] == [side, text]:
            continue  # 连发同一句只留两次
        chunk.append([side, text])
    yield chunk


def windows(chunk):
    for start in range(0, len(chunk), MAX_LINES):
        part = chunk[start:start + MAX_LINES]
        speakers = {s for s, _ in part}
        if len(part) >= MIN_LINES and len(speakers) == 2:
            yield part


def main():
    root, name, side, prefixes = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    if os.getenv("CORPUS_MASK"):
        NAMES.extend(load_mask(os.environ["CORPUS_MASK"]))
    out = os.path.join(os.path.dirname(__file__), "..", "data", "corpus", name + ".jsonl")
    kept = lines = 0
    with open(out, "w", encoding="utf-8") as f:
        for path in sorted(glob.glob(os.path.join(root, "私聊", "*.txt"))):
            title = open(path, encoding="utf-8").readline().strip("# \n")
            if not any(title.startswith(p) for p in prefixes):
                continue
            source = f"s{prefixes.index(next(p for p in prefixes if title.startswith(p)))}"
            for chunk in segments(path, None if side == "both" else side == "me"):
                for part in windows(chunk):
                    joined = " ".join(t for _, t in part)
                    tags = [k for k, pat in TAGS.items() if re.search(pat, joined)]
                    f.write(json.dumps({"source": source, "lines": part, "tags": tags}, ensure_ascii=False) + "\n")
                    kept += 1
                    lines += len(part)
    print(f"{name}: {kept} 段 / {lines} 行 -> {os.path.normpath(out)}")


if __name__ == "__main__":
    main()
