"""真实天气（E1）：她的城市此刻的天气和预报，来自 Open-Meteo（免费、无需密钥）。

- 运行时每 20 分钟取一次（runtime.weather_loop，在事务外联网），写到 `street` 的状态里：
  weather（clear/cloudy/rain/snow，世界规则用）、weather_label（小雨/多云…）、temp、feels、wind、forecast。
- 类别变了才记一条 weather.changed 事件；温度静默更新。
- 有新鲜的真实天气时，扰动池里的随机天气让路；断网超过 3 小时退回随机天气。
- 测试 / 快进模拟不联网，天气仍由种子抽。
"""
from __future__ import annotations

from app.storage.world_store import emit, entity, save_entity

URL = "https://api.open-meteo.com/v1/forecast"
FRESH_SECONDS = 3 * 3600

_CODES = {0: "晴", 1: "晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾", 51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
          56: "冻雨", 57: "冻雨", 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪",
          75: "大雪", 77: "雪粒", 80: "阵雨", 81: "阵雨", 82: "暴雨", 85: "阵雪", 86: "阵雪", 95: "雷阵雨",
          96: "雷阵雨", 99: "雷阵雨"}
_PLAIN = {"clear": "晴", "cloudy": "阴", "rain": "下雨", "snow": "下雪"}


def category(code: int) -> str:
    if code in (0, 1):
        return "clear"
    if code in (2, 3, 45, 48):
        return "cloudy"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    return "rain"


def fetch(lat: float, lon: float, timeout: float = 15) -> dict:
    import requests
    response = requests.get(URL, timeout=timeout, params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    response.raise_for_status()
    return response.json()


def parse(raw: dict) -> dict:
    now, daily = raw["current"], raw["daily"]
    code = int(now["weather_code"])

    def day(i: int) -> dict:
        c = int(daily["weather_code"][i])
        return {"label": _CODES.get(c, "多云"), "weather": category(c), "min": round(daily["temperature_2m_min"][i]),
                "max": round(daily["temperature_2m_max"][i]),
                "rain_prob": int(daily["precipitation_probability_max"][i] or 0)}

    return {"weather": category(code), "label": _CODES.get(code, "多云"), "temp": round(now["temperature_2m"], 1),
            "feels": round(now["apparent_temperature"], 1), "wind": round(now["wind_speed_10m"]),
            "today": day(0), "tomorrow": day(1)}


def apply(c, w, report: dict, place: str = "street") -> bool:
    """把一份天气报告落到世界里；类别或说法变了返回 True（并记事件）。"""
    street = entity(c, w["world_id"], place)
    state = street["state"]
    changed = state.get("weather") != report["weather"] or state.get("weather_label") != report["label"]
    before = state.get("weather_label") or _PLAIN.get(state.get("weather"), "")
    state.update(weather=report["weather"], weather_label=report["label"], temp=report["temp"], feels=report["feels"],
                 wind=report["wind"], forecast={"today": report["today"], "tomorrow": report["tomorrow"]},
                 weather_real_at=w["sim_time"])
    save_entity(c, street)
    if changed:
        emit(c, w, f"weather:{int(w['sim_time'])}", "weather.changed",
             {"from": before, "to": report["label"], "weather": report["weather"], "temp": report["temp"],
              "source": "real", "changes": [street]})
    return changed


def is_real(state: dict, now: float) -> bool:
    return bool(state.get("weather_real_at")) and now - state["weather_real_at"] < FRESH_SECONDS


def line(state: dict) -> str:
    """给她的脑子：外面什么天（"小雨，9°C（体感 6°C）；明天多云 8–16°C"）。"""
    if "weather" not in state:
        return ""
    text = state.get("weather_label") or _PLAIN.get(state["weather"], state["weather"])
    if state.get("temp") is not None:
        text += f"，{round(state['temp'])}°C"
        if state.get("feels") is not None and abs(state["feels"] - state["temp"]) >= 3:
            text += f"（体感 {round(state['feels'])}°C）"
    tomorrow = (state.get("forecast") or {}).get("tomorrow")
    if tomorrow:
        text += f"；明天{tomorrow['label']} {tomorrow['min']}–{tomorrow['max']}°C"
    return text


def mood(state: dict) -> float:
    """这个天气对"去外面"的兴致加减：下雨、太冷、太热、大风都不想出去；不冷不热的晴天想出去走走。"""
    score = 0.0
    weather = state.get("weather")
    if weather == "rain":
        score -= 0.5
    feels = state.get("feels", state.get("temp"))
    if feels is not None:
        if feels < 0:
            score -= 0.5
        elif feels < 5:
            score -= 0.3
        elif feels < 10:
            score -= 0.1
        elif feels > 33:
            score -= 0.4
        elif feels > 30:
            score -= 0.2
        elif 15 <= feels <= 26 and weather == "clear":
            score += 0.2
    if state.get("wind", 0) > 35:
        score -= 0.2
    return score
