"""Sticker pack tooling for the Neno TDLib gateway.

Facts this module encodes (verified against third_party/tdlib-current):

* SENDING a sticker message uses ``inputMessageSticker { sticker: inputSticker,
  emoji: string }`` (td_api.tl:6120) where ``inputSticker`` carries an InputFile,
  an optional thumbnail and width/height (td_api.tl:5844).
* On the send path TDLib calls ``create_sticker(..., StickerFormat::Unknown)``
  (MessageContent.cpp:4954) and then GUESSES the format from the file extension
  (StickersManager.cpp:3386-3393).  Only .tgs/.webp/.webm map to a format;
  anything else falls back to StickerFormat::Webp and is uploaded as
  ``image/webp``.  The server then validates the real bytes and rejects
  non-WebP content, so PNG/JPEG payloads MUST be transcoded first.
* Static regular stickers are capped at 512 KiB
  (get_max_sticker_file_size, StickerFormat.cpp:151-156).
* Sticker SETS use ``newSticker { sticker, format, emojis, ... }``
  (td_api.tl:10131) with an explicit ``stickerFormatWebp``; set membership
  additionally enforces 512x512 static frames.

CLI:
    python -m telegram_gateway.stickerkit scan <zip-or-dir>
    python -m telegram_gateway.stickerkit send --chat-id 123 --file x.png [--emoji Y]
    python -m telegram_gateway.stickerkit send --chat-id 123 --file x.png --as-photo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_STICKER_BYTES = 1 << 19  # 512 KiB, StickerFormat.cpp:156 (webp, regular)

# custom_stickers.json carries Chinese semantic tags; map them to the alt emoji
# TDLib expects for inputMessageSticker.emoji.  Anything unknown gets DEFAULT_EMOJI.
DEFAULT_EMOJI = "\U0001f642"  # 🙂
EMOJI_BY_DESCRIPTION: dict[str, str] = {
    "喵": "\U0001f431",
    "惊": "\U0001f62e",
    "咬你": "\U0001f63e",
    "嘤嘤嘤": "\U0001f62d",
    "抱着": "\U0001f917",
    "哭": "\U0001f622",
    "笑": "\U0001f602",
    "生气": "\U0001f624",
    "害羞": "\U0001f633",
    "无语": "\U0001f610",
    "点赞": "\U0001f44d",
    "爱心": "\u2764\ufe0f",
    "晚安": "\U0001f319",
}


@dataclass(frozen=True)
class ImageInfo:
    fmt: str  # "png" | "jpeg" | "webp" | "gif" | "unknown"
    width: int
    height: int

    @property
    def sendable_as_sticker(self) -> bool:
        return self.fmt == "webp"


@dataclass(frozen=True)
class StickerEntry:
    name: str
    size: int
    image: ImageInfo
    description: str

    @property
    def emoji(self) -> str:
        return EMOJI_BY_DESCRIPTION.get(self.description.strip(), DEFAULT_EMOJI)


def probe_image(data: bytes) -> ImageInfo:
    """Identify the real container format from magic bytes (extensions lie)."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return ImageInfo("png", width, height)
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        width, height = _jpeg_size(data)
        return ImageInfo("jpeg", width, height)
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return ImageInfo("gif", width, height)
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return ImageInfo("webp", width, height)
        if chunk == b"VP8L":
            bits = struct.unpack("<I", data[21:25])[0]
            return ImageInfo("webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if chunk == b"VP8 ":
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return ImageInfo("webp", width, height)
        return ImageInfo("webp", 0, 0)
    return ImageInfo("unknown", 0, 0)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return width, height
        offset += 2 + length
    return 0, 0


def scan_pack(source: Path) -> list[StickerEntry]:
    """Read every sticker from a pack zip (or a plain directory) plus its
    custom_stickers.json description mapping."""
    descriptions: dict[str, str] = {}
    blobs: list[tuple[str, bytes]]
    if source.is_dir():
        meta = source / "custom_stickers.json"
        if meta.is_file():
            descriptions = _parse_descriptions(meta.read_text(encoding="utf-8"))
        blobs = [(p.name, p.read_bytes()) for p in sorted(source.iterdir()) if p.is_file()]
    else:
        with zipfile.ZipFile(source) as zf:
            raw_meta = None
            for info in zf.infolist():
                if Path(info.filename).name == "custom_stickers.json":
                    raw_meta = zf.read(info)
            if raw_meta is not None:
                descriptions = _parse_descriptions(raw_meta.decode("utf-8"))
            # read eagerly: the ZipFile handle must still be open
            blobs = [
                (Path(i.filename).name, zf.read(i))
                for i in zf.infolist()
                if i.filename and not i.is_dir()
            ]

    entries: list[StickerEntry] = []
    for name, data in blobs:
        if name == "custom_stickers.json":
            continue
        entries.append(
            StickerEntry(
                name=name,
                size=len(data),
                image=probe_image(data),
                description=descriptions.get(name) or descriptions.get(Path(name).stem, ""),
            )
        )
    return entries


def _parse_descriptions(text: str) -> dict[str, str]:
    payload = json.loads(text)
    result: dict[str, str] = {}
    for item in payload if isinstance(payload, list) else []:
        if isinstance(item, dict) and item.get("fileName"):
            name = str(item["fileName"])
            description = str(item.get("description") or "")
            result[name] = description
            result[Path(name).stem] = description  # survives .png -> .webp renames
    return result


def build_input_sticker(path: Path, emoji: str) -> dict[str, Any]:
    """Build the td_api.inputSticker JSON for a local static sticker."""
    data = path.read_bytes()
    info = probe_image(data)
    return {
        "@type": "inputSticker",
        "sticker": {"@type": "inputFileLocal", "path": str(path)},
        "thumbnail": None,
        "width": info.width,
        "height": info.height,
    }


def build_send_sticker_request(chat_id: int, path: Path, emoji: str) -> dict[str, Any]:
    if not probe_image(path.read_bytes()).sendable_as_sticker:
        raise ValueError(
            f"{path.name} is not WebP content; TDLib would upload it as image/webp "
            "and the server would reject it. Transcode with scripts/stickers2webp.ps1 "
            "or send with --as-photo."
        )
    return {
        "@type": "sendMessage",
        "chat_id": chat_id,
        "input_message_content": {
            "@type": "inputMessageSticker",
            "sticker": build_input_sticker(path, emoji),
            "emoji": emoji,
        },
    }


def build_send_photo_request(chat_id: int, path: Path) -> dict[str, Any]:
    """Fallback path: any PNG/JPEG/WebP goes out as a photo (bubble, not sticker)."""
    data = path.read_bytes()
    info = probe_image(data)
    return {
        "@type": "sendMessage",
        "chat_id": chat_id,
        "input_message_content": {
            "@type": "inputMessagePhoto",
            "photo": {
                "@type": "inputPhoto",
                "photo": {"@type": "inputFileLocal", "path": str(path)},
                "thumbnail": None,
                "video": None,
                "added_sticker_file_ids": [],
                "width": info.width,
                "height": info.height,
            },
            "caption": {"@type": "formattedText", "text": "", "entities": []},
            "show_caption_above_media": False,
            "self_destruct_type": {"@type": "messageSelfDestructTypeImmediately"},
            "has_spoiler": False,
        },
    }


async def _connect() -> Any:
    from telegram_gateway.config import GatewayConfig
    from telegram_gateway.tdlib_client import TdlibClient

    config = GatewayConfig.from_env()
    client = TdlibClient(config.sidecar_binary, config.tdjson_library)
    await client.start()
    await client.send(config.tdlib_parameters_request())
    for _ in range(20):
        try:
            await client.request({"@type": "getMe"}, timeout=10)
            return client
        except Exception:
            await asyncio.sleep(0.5)
    await client.close()
    raise RuntimeError("TDLib authorization is not ready; run telegram_gateway.login first")


async def send_sticker(chat_id: int, path: Path, emoji: str, as_photo: bool = False) -> Any:
    client = await _connect()
    try:
        request = (
            build_send_photo_request(chat_id, path)
            if as_photo
            else build_send_sticker_request(chat_id, path, emoji)
        )
        return await client.request(request, timeout=60)
    finally:
        await client.close()


def _cmd_scan(source: Path) -> int:
    entries = scan_pack(source)
    by_fmt: dict[str, int] = {}
    for entry in entries:
        by_fmt[entry.image.fmt] = by_fmt.get(entry.image.fmt, 0) + 1
    print(f"entries: {len(entries)}")
    for fmt, count in sorted(by_fmt.items(), key=lambda kv: -kv[1]):
        print(f"  {fmt:8s} x{count}")
    sendable = sum(1 for e in entries if e.image.sendable_as_sticker and e.size <= MAX_STICKER_BYTES)
    print(f"sendable as sticker today: {sendable}")
    print(f"need transcode to webp   : {len(entries) - sendable}")
    print()
    for entry in entries:
        flags = []
        if not entry.image.sendable_as_sticker:
            flags.append("TRANSCODE")
        if entry.size > MAX_STICKER_BYTES:
            flags.append("OVERSIZE")
        print(
            f"{entry.image.fmt:5s} {entry.image.width:4d}x{entry.image.height:<4d} "
            f"{entry.size:8d}B  {entry.emoji}  {entry.description[:12]:12s} "
            f"{','.join(flags) or 'OK':9s} {entry.name}"
        )
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    path = Path(args.file)
    result = asyncio.run(
        send_sticker(args.chat_id, path, args.emoji or DEFAULT_EMOJI, as_photo=args.as_photo)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="telegram_gateway.stickerkit")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="inspect a sticker pack zip or directory")
    scan.add_argument("source", type=Path)

    send = sub.add_parser("send", help="send one sticker (or photo) through the TDLib gateway")
    send.add_argument("--chat-id", type=int, required=True)
    send.add_argument("--file", required=True)
    send.add_argument("--emoji", default=None, help="alt emoji; default 🙂")
    send.add_argument("--as-photo", action="store_true", help="send as photo instead of sticker")

    args = parser.parse_args(argv)
    if args.command == "scan":
        return _cmd_scan(args.source)
    return _cmd_send(args)


if __name__ == "__main__":
    sys.exit(main())
