"""Gateway-side sticker table for Neno.

The persona prompt (prompts/system.txt) asks the model to emit textual marks
``[表情包：名字]``.  The gateway owns the real pack, so the gateway:

1. injects the authoritative name table into the core session once, so the
   model picks EXACT names instead of inventing free-form descriptions;
2. resolves marks in replies back to concrete ``.webp`` files (exact name,
   normalized, then fuzzy) for the TDLib send path in stickerkit.

Pack layout (the output of scripts/stickers2webp.ps1):
    <pack_dir>/custom_stickers.json   [{"description": "喵", "fileName": "sticker_xxx.png"}, ...]
    <pack_dir>/sticker_xxx.webp       real WebP bytes (extension may be .png/.webp; stem-matched)

CLI:
    python -m telegram_gateway.sticker_table render
    python -m telegram_gateway.sticker_table resolve "猫猫把脸埋进被子里"
    python -m telegram_gateway.sticker_table export data/stickers/table.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# [表情包：名字] is the documented mark; bare [名字] is also accepted when the
# inner text resolves to a table entry (the model often drops the prefix).
MARK_PATTERN = re.compile(r"\[(表情包[:：]\s*)?([^\]\n]{1,40})\]")
INJECTION_HEADER = (
    "【表情包表｜网关注入】你现在可以发表情包：在回复里单独占一行输出标记 "
    "[表情包：名字]（简写 [名字] 也行），名字必须从下表精确选取，"
    "不要自造、不要写自由描述；表里没有合适的就不发，用文字或emoji代替。表（名字共{n}个）：\n"
)


@dataclass(frozen=True)
class StickerEntry:
    name: str
    file_name: str
    file_path: Path | None
    emoji: str


@dataclass(frozen=True)
class ReplyPart:
    kind: str  # "text" | "sticker"
    text: str = ""
    entry: StickerEntry | None = None


class StickerTable:
    def __init__(self, entries: list[StickerEntry], source: Path):
        self.entries = entries
        self.source = source
        self._by_name = {e.name: e for e in entries}
        self._by_norm: dict[str, StickerEntry] = {}
        for entry in entries:  # first wins; "咬你" vs "咬你~" must not collide silently
            self._by_norm.setdefault(_normalize(entry.name), entry)
        self._by_norm.pop("", None)

    @classmethod
    def load(cls, pack_dir: Path) -> "StickerTable":
        meta_path = pack_dir / "custom_stickers.json"
        if not meta_path.is_file():
            raise FileNotFoundError(f"custom_stickers.json not found in {pack_dir}")
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        files_by_stem = {p.stem: p for p in pack_dir.iterdir() if p.is_file()}
        entries: list[StickerEntry] = []
        seen: set[str] = set()
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not item.get("fileName"):
                continue
            file_name = str(item["fileName"])
            name = str(item.get("description") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            from telegram_gateway.stickerkit import EMOJI_BY_DESCRIPTION, DEFAULT_EMOJI

            entries.append(
                StickerEntry(
                    name=name,
                    file_name=file_name,
                    file_path=files_by_stem.get(Path(file_name).stem),
                    emoji=EMOJI_BY_DESCRIPTION.get(name, DEFAULT_EMOJI),
                )
            )
        return cls(entries, pack_dir)

    @classmethod
    def load_optional(cls, pack_dir: Path) -> "StickerTable | None":
        try:
            return cls.load(pack_dir)
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            return None

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    def resolve(self, query: str) -> StickerEntry | None:
        """Exact -> normalized -> containment -> difflib fuzzy."""
        query = query.strip()
        if not query:
            return None
        if query in self._by_name:
            return self._by_name[query]
        norm = _normalize(query)
        if norm in self._by_norm:
            return self._by_norm[norm]
        containing = [e for e in self.entries if _normalize(e.name) in norm or norm in _normalize(e.name)]
        if len(containing) == 1:
            return containing[0]
        pool = containing or self.entries
        match = difflib.get_close_matches(norm, [_normalize(e.name) for e in pool], n=1, cutoff=0.5)
        if match:
            return self._by_norm.get(match[0])
        return None

    def render_prompt_block(self) -> str:
        names = "、".join(self.names)
        return INJECTION_HEADER.format(n=len(self.names)) + names

    def extract_marks(self, reply: str) -> list[tuple[str, StickerEntry | None]]:
        """Return every (mark_text, resolved_entry) pair found in a reply."""
        result: list[tuple[str, StickerEntry | None]] = []
        for match in MARK_PATTERN.finditer(reply):
            explicit = match.group(1) is not None
            entry = self.resolve(match.group(2))
            if explicit or entry is not None:
                result.append((match.group(2).strip(), entry))
        return result

    def segment(self, reply: str) -> list[ReplyPart]:
        """Split a reply into ordered text/sticker parts for the send pipeline.

        [表情包：名字] marks that do not resolve are dropped (the injected table
        tells the model to skip stickers rather than invent names); bare
        [文本] that does not resolve stays as ordinary text.
        """
        parts: list[ReplyPart] = []
        cursor = 0
        for match in MARK_PATTERN.finditer(reply):
            head = reply[cursor : match.start()].strip()
            if head:
                parts.append(ReplyPart("text", head))
            explicit = match.group(1) is not None
            entry = self.resolve(match.group(2))
            if entry is not None and entry.file_path is not None:
                parts.append(ReplyPart("sticker", entry=entry))
            elif not explicit:
                parts.append(ReplyPart("text", match.group(0)))
            cursor = match.end()
        tail = reply[cursor:].strip()
        if tail:
            parts.append(ReplyPart("text", tail))
        return parts


class StickerTableInjector:
    """Inject the name table into a core session once (or always/never).

    mode: "once" (default) | "always" | "off"
    """

    def __init__(self, table: StickerTable | None, mode: str = "once"):
        self.table = table
        self.mode = mode
        self._seen_sessions: set[str] = set()

    def decorate(self, session_id: str, message: str) -> str:
        if self.table is None or self.mode == "off":
            return message
        if self.mode == "once":
            if session_id in self._seen_sessions:
                return message
            self._seen_sessions.add(session_id)
        return f"{self.table.render_prompt_block()}\n\n{message}"


def _normalize(text: str) -> str:
    return re.sub(r"[\s_\-_（）()【】\[\]，,。.!！?？~～]+", "", text).lower()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="telegram_gateway.sticker_table")
    parser.add_argument("--pack-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("render", help="print the injectable name table")
    resolve = sub.add_parser("resolve", help="resolve one query to a sticker")
    resolve.add_argument("query")
    export = sub.add_parser("export", help="dump the table as JSON")
    export.add_argument("target", type=Path)
    args = parser.parse_args(argv)

    pack_dir = args.pack_dir
    if pack_dir is None:
        import os

        root = Path(__file__).resolve().parents[1]
        pack_dir = Path(os.getenv("NENO_STICKER_PACK_DIR", str(root / "data" / "stickers" / "current")))
    table = StickerTable.load(pack_dir)

    if args.command == "render":
        print(f"# pack: {pack_dir}  entries: {len(table.entries)}")
        print(table.render_prompt_block())
        return 0
    if args.command == "resolve":
        entry = table.resolve(args.query)
        print(json.dumps(entry.__dict__ | {"file_path": str(entry.file_path)} if entry else None,
                         ensure_ascii=False, indent=2, default=str))
        return 0 if entry else 1
    payload = [
        {"name": e.name, "file_name": e.file_name, "file_path": str(e.file_path), "emoji": e.emoji}
        for e in table.entries
    ]
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(payload)} entries -> {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
