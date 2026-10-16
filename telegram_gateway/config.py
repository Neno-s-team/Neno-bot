from __future__ import annotations

import os
import base64
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _configured_path(name: str, default: Path, root: Path) -> Path:
    value = Path(os.getenv(name, str(default))).expanduser()
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def _required_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is not configured")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _csv_ints(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            values.add(int(item))
    return frozenset(values)


@dataclass(frozen=True)
class GatewayConfig:
    api_id: int
    api_hash: str
    database_key: str
    tdjson_library: Path
    sidecar_binary: Path
    database_directory: Path
    files_directory: Path
    core_url: str
    internal_token: str
    allowed_chat_ids: frozenset[int]
    max_media_bytes: int
    download_timeout_seconds: float
    sticker_pack_dir: Path
    mind_async: bool
    mind_poll_seconds: float
    mind_max_concurrency: int
    mind_presence: bool

    def tdlib_parameters_request(self) -> dict[str, object]:
        """Build the TDLib request shape; the database key is sent separately."""
        return {
            "@type": "setTdlibParameters",
            "use_test_dc": False,
            "database_directory": str(self.database_directory),
            "files_directory": str(self.files_directory),
            "database_encryption_key": self.database_encryption_key_b64(),
            "use_file_database": True,
            "use_chat_info_database": True,
            "use_message_database": True,
            "use_secret_chats": False,
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "system_language_code": "en",
            "device_model": "Neno TDLib Gateway",
            "system_version": "Windows",
            "application_version": "0.1",
        }

    def database_encryption_key_b64(self) -> str:
        # TDLib's JSON bytes fields use base64. The env value is a local
        # passphrase, so users do not need to hand-encode it.
        return base64.b64encode(self.database_key.encode("utf-8")).decode("ascii")

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "GatewayConfig":
        root = project_root or Path(__file__).resolve().parents[1]
        runtime_root = root / "runtime" / "tdlib"
        data_root = root / "data" / "telegram" / "tdlib"
        tdjson = _configured_path("TDLIB_JSON_LIBRARY", runtime_root / "tdjson.dll", root)
        sidecar = _configured_path(
            "TDLIB_SIDECAR_BINARY",
            root / "telegram_sidecar" / "build" / "Release" / "neno-tdlib-sidecar.exe",
            root,
        )
        core_url = os.getenv("NENO_CORE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
        max_media_mb = int(os.getenv("TELEGRAM_MAX_MEDIA_MB", "200"))
        if max_media_mb <= 0:
            raise RuntimeError("TELEGRAM_MAX_MEDIA_MB must be positive")
        return cls(
            api_id=_required_int("TELEGRAM_API_ID"),
            api_hash=_required("TELEGRAM_API_HASH"),
            database_key=_required("TELEGRAM_DATABASE_ENCRYPTION_KEY"),
            tdjson_library=tdjson,
            sidecar_binary=sidecar,
            database_directory=Path(
                os.getenv("TELEGRAM_DATABASE_DIRECTORY", str(data_root / "database"))
            ).expanduser(),
            files_directory=Path(
                os.getenv("TELEGRAM_FILES_DIRECTORY", str(data_root / "files"))
            ).expanduser(),
            core_url=core_url,
            internal_token=_required("NENO_INTERNAL_TOKEN"),
            allowed_chat_ids=_csv_ints("TELEGRAM_ALLOWED_CHAT_IDS"),
            max_media_bytes=max_media_mb * 1024 * 1024,
            download_timeout_seconds=float(os.getenv("TELEGRAM_DOWNLOAD_TIMEOUT_SECONDS", "180")),
            sticker_pack_dir=_configured_path(
                "NENO_STICKER_PACK_DIR", root / "data" / "stickers" / "current", root
            ),
            mind_async=os.getenv("NENO_MIND_ASYNC", "false").strip().lower() in ("1", "true", "yes", "on"),
            mind_poll_seconds=float(os.getenv("NENO_MIND_POLL_SEC", "1")),
            mind_max_concurrency=int(os.getenv("NENO_MIND_MAX_CONCURRENCY", "4")),
            mind_presence=os.getenv("NENO_MIND_PRESENCE", "false").strip().lower() in ("1", "true", "yes", "on"),
        )
