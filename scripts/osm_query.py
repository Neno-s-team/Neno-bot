"""OpenStreetMap Overpass 查询（带重试/换镜像），给城市构建脚本用。

用法：python scripts/osm_query.py '<overpass QL>' out.json
数据 © OpenStreetMap contributors，ODbL。
"""
from __future__ import annotations

import json
import sys
import time

import requests

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HEADERS = {"User-Agent": "neno-world/0.1 (personal research; city builder)"}


def query(ql: str, tries: int = 6, timeout: int = 180) -> dict:
    last = None
    for attempt in range(tries):
        url = MIRRORS[attempt % len(MIRRORS)]
        try:
            response = requests.post(url, data={"data": ql}, headers=HEADERS, timeout=timeout)
            if response.status_code == 200:
                return response.json()
            last = f"{url} {response.status_code}"
        except (requests.RequestException, ValueError) as error:
            last = f"{url} {type(error).__name__}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"overpass failed: {last}")


if __name__ == "__main__":
    data = query(sys.argv[1])
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(len(data.get("elements", [])), "elements")
