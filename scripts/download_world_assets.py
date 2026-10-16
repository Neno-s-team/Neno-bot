"""Cache the pinned BEHAVIOR archive without extracting or accepting its asset EULA."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1] / "data/world-assets"


def run() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    target = ROOT / manifest["filename"]
    part = target.with_suffix(target.suffix + ".part")
    status = ROOT / "download-status.json"
    lock = ROOT / "download.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    started = time.time()

    def report(state: str, **extra) -> None:
        count = target.stat().st_size if target.exists() else part.stat().st_size if part.exists() else 0
        payload = {"state": state, "pid": os.getpid(), "filename": target.name, "bytes": count,
                   "expected_bytes": manifest["bytes"], "updated_at": time.time(),
                   "elapsed_seconds": round(time.time() - started, 1), **extra}
        temporary = status.with_suffix(".work")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(status)

    try:
        if not target.exists():
            last_error = None
            for attempt in range(12):
                offset = part.stat().st_size if part.exists() else 0
                if offset == manifest["bytes"]:
                    break
                if offset > manifest["bytes"]:
                    raise ValueError("Partial file exceeds expected size; preserving it for inspection")
                if shutil.disk_usage(ROOT).free < manifest["bytes"] - offset + 2 * 1024**3:
                    raise OSError("Insufficient free space with a 2 GiB reserve")
                try:
                    headers = {"Range": f"bytes={offset}-"} if offset else {}
                    report("connecting", attempt=attempt + 1)
                    with requests.get(manifest["url"], headers=headers, timeout=(20, 90), stream=True) as response:
                        response.raise_for_status()
                        if offset and (response.status_code != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")):
                            raise ValueError("Server did not honor resume offset; existing partial file preserved")
                        report("downloading", attempt=attempt + 1)
                        last_report = time.monotonic()
                        with part.open("ab" if offset else "wb") as stream:
                            for chunk in response.iter_content(1024 * 1024):
                                if offset + len(chunk) > manifest["bytes"]:
                                    raise ValueError("Response exceeds expected archive size")
                                stream.write(chunk)
                                offset += len(chunk)
                                if time.monotonic() - last_report >= 5:
                                    stream.flush()
                                    report("downloading", attempt=attempt + 1)
                                    last_report = time.monotonic()
                    if part.stat().st_size != manifest["bytes"]:
                        raise OSError("Download ended before the expected byte count")
                    last_error = None
                    break
                except (requests.RequestException, OSError) as error:
                    last_error = type(error).__name__
                    report("retry_wait", error=last_error, attempt=attempt + 1)
                    time.sleep(min(60, 2 ** attempt))
            if last_error:
                raise OSError(f"Download retry limit reached: {last_error}")
        source = target if target.exists() else part
        report("verifying")
        checksum = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
                checksum.update(chunk)
        if source.stat().st_size != manifest["bytes"] or checksum.hexdigest() != manifest["sha256"]:
            raise ValueError("Archive size or SHA256 mismatch; file preserved and not marked complete")
        if source == part:
            part.replace(target)
        report("complete", sha256=checksum.hexdigest(), extracted=False)
    except BaseException as error:
        report("failed", error=str(error)[:300])
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    run()
