import hashlib


def sample(seed: str, opportunity: str, purpose: str, version: int = 1) -> float:
    raw = "\0".join([seed, opportunity, purpose, str(version)]).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def stable_id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
