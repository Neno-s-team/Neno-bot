"""提供方：模型名写成"提供方:模型名"，就走那家的地址和密钥；不写的照旧（调用方给什么地址就用什么）。

    deepseek:deepseek-v4-pro              DeepSeek 官方（国内能直连，支付宝/微信充值）
    openrouter:google/gemini-3.8-flash    OpenRouter
    custom:qwen-max                       任何 OpenAI 兼容的地址（CUSTOM_LLM_URL / CUSTOM_LLM_API_KEY）
    google/gemini-3.8-flash               没写提供方：老写法，走调用方传进来的地址和密钥（默认是 OpenRouter）

各层的额外参数是按 OpenRouter 的写法写的（reasoning.effort、json_schema 强制结构、provider.require_parameters）。
adapt() 把它们换成那一家认的写法。DeepSeek 官方（2026-11-12 查的文档）：
- 思考用 reasoning_effort：none / low / high / max；**不传就默认开着思考**。所以没要求思考的请求，这里明确传 none。
- response_format 只有 json_object，不能强制结构：把结构要求写进最后一条用户消息里，靠各层自己的解析和检查兜底。
- system 消息只能是字符串；不认 cache_control、provider 这些键。
- 不返回花费：cost() 按价格表算，工作日高峰时段（UTC 01–04 点、06–10 点）价格翻倍。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

PROVIDERS: dict[str, dict[str, str]] = {
    "openrouter": {"url_env": "OPENROUTER_URL", "key_env": "OPENROUTER_API_KEY",
                   "url": "https://openrouter.ai/api/v1/chat/completions", "label": "OpenRouter"},
    "deepseek": {"url_env": "DEEPSEEK_URL", "key_env": "DEEPSEEK_API_KEY",
                 "url": "https://api.deepseek.com/chat/completions", "label": "DeepSeek"},
    "custom": {"url_env": "CUSTOM_LLM_URL", "key_env": "CUSTOM_LLM_API_KEY", "url": "", "label": "自定义"},
}
# DeepSeek 官方价格，美元 / 百万 token，(命中缓存的输入, 没命中的输入, 输出)，平时价；高峰 ×2。
DEEPSEEK_PRICES = {"deepseek-flash": (0.003, 0.15, 0.6), "deepseek-v4-pro": (0.022, 0.66, 1.98)}
_EFFORT = {"none": "none", "minimal": "low", "low": "low", "medium": "high", "high": "high", "xhigh": "high", "max": "max"}


class ProviderError(RuntimeError):
    pass


def split(model_name: str) -> tuple[str, str]:
    """'deepseek:deepseek-v4-pro' → ('deepseek', 'deepseek-v4-pro')；没写提供方 → ('', 原样)。
    OpenRouter 的模型名自己也带冒号（'x/y:free'），所以只认已知的提供方名。"""
    head, sep, rest = str(model_name or "").partition(":")
    if sep and head.strip().lower() in PROVIDERS and rest.strip():
        return head.strip().lower(), rest.strip()
    return "", str(model_name or "")


def resolve(model_name: str, url: str = "", api_key: str = "") -> tuple[str, str, str, str]:
    """→ (提供方, 地址, 密钥, 模型名)。写了提供方就用那家的；没写就用调用方给的，提供方按地址猜（只用来记账和转换参数）。"""
    name, model = split(model_name)
    if not name:
        host = str(url or "")
        return ("deepseek" if "api.deepseek.com" in host else "openrouter" if "openrouter.ai" in host else ""), url, api_key, model
    spec = PROVIDERS[name]
    url = os.getenv(spec["url_env"], "").strip() or spec["url"]
    key = os.getenv(spec["key_env"], "").strip()
    if not url:
        raise ProviderError(f"{spec['url_env']} 没填（模型 {model_name} 要用它）")
    if not key:
        raise ProviderError(f"{spec['key_env']} 没填（模型 {model_name} 要用它）")
    return name, url, key, model


def needed_keys(models: list[str]) -> list[str]:
    """这些模型名要用到哪些密钥（环境变量名）。没写提供方的算 OpenRouter。"""
    found = []
    for model in models:
        if not model:
            continue
        key = PROVIDERS[split(model)[0] or "openrouter"]["key_env"]
        if key not in found:
            found.append(key)
    return found


def _text(content: Any) -> Any:
    """消息内容里的分块 → 一个字符串（有图片就留着分块，只去掉这家不认的键）。"""
    if not isinstance(content, list):
        return content
    if any(isinstance(part, dict) and part.get("type") not in ("text", None) for part in content):
        return [{k: v for k, v in part.items() if k != "cache_control"} if isinstance(part, dict) else part for part in content]
    return "\n".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)


def adapt(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把按 OpenRouter 写法组好的请求体，换成这一家认的。OpenRouter 和没认出来的地址原样不动。"""
    if provider != "deepseek":
        return payload
    body = {k: v for k, v in payload.items() if k not in ("reasoning", "provider", "session_id", "usage")}
    spec = payload.get("reasoning") if isinstance(payload.get("reasoning"), dict) else None
    if "reasoning_effort" not in body and "thinking" not in body:
        if spec is None or spec.get("enabled") is False:
            body["reasoning_effort"] = "none"            # 官方默认开着思考：没要求就明确关掉
        else:
            body["reasoning_effort"] = _EFFORT.get(str(spec.get("effort") or "high").lower(), "high")
    keep = ("role", "content", "name", "tool_call_id", "tool_calls")      # 别的键（reasoning_details 等）官方接口不认
    messages = [{**{k: v for k, v in m.items() if k in keep}, "content": _text(m.get("content"))}
                for m in payload.get("messages") or []]
    fmt = payload.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        body["response_format"] = {"type": "json_object"}
        schema = (fmt.get("json_schema") or {}).get("schema")
        note = "只输出一个 JSON 对象，不要别的内容。"
        if schema:
            note += "必须符合这个 JSON Schema（键名、类型、必填项都照它来）：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        for message in reversed(messages):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                message["content"] += "\n\n" + note
                break
        else:
            messages.append({"role": "user", "content": note})
    body["messages"] = messages
    return body


def peak(ts: float | None = None) -> bool:
    """DeepSeek 的高峰时段：周一到周五，UTC 01:00–04:00 和 06:00–10:00（节假日不算高峰，这里没扣，会略微高估）。"""
    now = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)


def cost(provider: str, model: str, usage: dict[str, Any], ts: float | None = None) -> float | None:
    """这家不返回花费时按价格表估（美元）。不认识的返回 None。"""
    price = DEEPSEEK_PRICES.get(model) if provider == "deepseek" else None
    if price is None or not usage:
        return None
    total_in = int(usage.get("prompt_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    out = int(usage.get("completion_tokens") or 0)
    amount = (hit * price[0] + max(0, total_in - hit) * price[1] + out * price[2]) / 1e6
    return round(amount * (2 if peak(ts) else 1), 8)
