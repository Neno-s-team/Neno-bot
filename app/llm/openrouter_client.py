import hashlib
import json
import time
from urllib.parse import urlparse

import requests

from app.config import OPENROUTER_PROXY
from app.utils.logging_utils import log_event


def _proxies() -> dict | None:
    if OPENROUTER_PROXY:
        return {"http": OPENROUTER_PROXY, "https": OPENROUTER_PROXY}
    return None


def _is_anthropic_messages_url(url: str) -> bool:
    return urlparse(str(url or "")).path.rstrip("/").endswith("/messages")


def _anthropic_block(block: dict) -> dict:
    if block.get("type") != "image_url":
        return block
    image_url = block.get("image_url")
    image_url = image_url.get("url") if isinstance(image_url, dict) else image_url
    value = str(image_url or "")
    if value.startswith("data:") and ";base64," in value:
        header, encoded = value.split(";base64,", 1)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": header[5:] or "application/octet-stream",
                "data": encoded,
            },
        }
    return {"type": "image", "source": {"type": "url", "url": value}}


def _anthropic_messages(messages: list) -> tuple[str | list[dict] | None, list[dict]]:
    system_parts: list[dict] = []
    converted: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content", "")
        if role == "system":
            if isinstance(content, list):
                system_parts.extend(dict(block) for block in content if isinstance(block, dict))
            else:
                system_parts.append({"type": "text", "text": str(content)})
            continue
        if role == "tool":
            result_content = ([_anthropic_block(block) for block in content]
                              if isinstance(content, list) else str(content))
            result = {"type": "tool_result", "tool_use_id": message["tool_call_id"],
                      "content": result_content, "is_error": bool(message.get("is_error"))}
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list) and any(
                block.get("type") == "tool_result" for block in converted[-1]["content"]
            ):
                converted[-1]["content"].append(result)
            else:
                converted.append({"role": "user", "content": [result]})
            continue
        if role == "assistant" and message.get("_anthropic_content") is not None:
            converted.append({"role": role, "content": message["_anthropic_content"]})
            continue
        if isinstance(content, list):
            content = [_anthropic_block(block) if isinstance(block, dict) else block for block in content]
        if role == "assistant" and message.get("tool_calls"):
            content = content if isinstance(content, list) else ([{"type": "text", "text": content}] if content else [])
            for call in message["tool_calls"]:
                content.append({"type": "tool_use", "id": call["id"],
                                "name": call["function"]["name"],
                                "input": json.loads(call["function"].get("arguments") or "{}")})
        converted.append({"role": role, "content": content})
    system = (system_parts if any("cache_control" in part for part in system_parts)
              else "\n\n".join(part.get("text", "") for part in system_parts).strip() or None)
    return system, converted


def _openai_messages(messages: list) -> list[dict]:
    converted, recalled_images = [], []
    for message in messages:
        if message["role"] != "tool" and recalled_images:
            converted.append({"role": "user", "content": recalled_images})
            recalled_images = []
        item = {key: value for key, value in message.items() if key in {
            "role", "content", "name", "tool_call_id", "tool_calls", "reasoning_details"
        }}
        if item["role"] == "tool" and isinstance(item.get("content"), list):
            # Tool results are text; pixels follow all results without a database turn.
            blocks = item["content"]
            texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
            images = [block for block in blocks if block.get("type") == "image_url"]
            item["content"] = "\n".join(texts)
            if images:
                recalled_images.extend([{"type": "text", "text": "[Image tool result] " + item["content"]}, *images])
        converted.append(item)
    if recalled_images:
        converted.append({"role": "user", "content": recalled_images})
    return converted


def _response_message(data: dict, is_anthropic: bool) -> dict:
    if is_anthropic:
        blocks = data["content"]
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        result = {"role": "assistant", "content": "".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ), "_anthropic_content": blocks}
        calls = [{"id": block["id"], "type": "function", "function": {
            "name": block["name"], "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
        }} for block in blocks if block.get("type") == "tool_use"]
        if calls:
            result["tool_calls"] = calls
    else:
        result = dict(data["choices"][0]["message"])
        result["role"] = "assistant"
        if isinstance(result.get("content"), list):
            result["content"] = "".join(block.get("text", "") for block in result["content"]
                                         if block.get("type") in {"text", "output_text"})
    if not str(result.get("content") or "").strip() and not result.get("tool_calls"):
        raise ValueError("Provider returned neither text nor tool calls")
    return result


def chat_with_openrouter(
    api_key: str,
    url: str,
    model_name: str,
    messages: list,
    timeout: int = 60,
    trace_id: str | None = None,
    extra_body: dict | None = None,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
    session_id: str | None = None,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    return_message: bool = False,
    purpose: str = "chat",
) -> str | dict:
    is_anthropic = _is_anthropic_messages_url(url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if is_anthropic:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        system, anthropic_messages = _anthropic_messages(messages)
        payload: dict = {
            "model": model_name,
            "messages": anthropic_messages,
            "max_tokens": int(max_tokens) if max_tokens is not None else 1024,
        }
        if system:
            payload["system"] = system
        if stop:
            payload["stop_sequences"] = list(stop)
    else:
        payload = {
            "model": model_name,
            "messages": _openai_messages(messages),
        }
        if max_tokens is not None:
            # 回复长度上限：兜住模型偶发跑飞（脑补一长串假对话），顺便省 token。
            payload["max_tokens"] = int(max_tokens)
        if stop:
            # 角色轮次标记（如换行后的 "Assistant"）一出现就停，防止污染入库引发反馈环。
            payload["stop"] = list(stop)
    if extra_body:
        # 厂商特定参数（如 MiMo 关深度思考 thinking={"type":"disabled"} 把 15s 压到 ~1.2s）
        payload.update(extra_body)
    if tools:
        if is_anthropic:
            payload["tools"] = [{"name": item["function"]["name"],
                                  "description": item["function"].get("description", ""),
                                  "input_schema": item["function"]["parameters"]} for item in tools]
            payload["tool_choice"] = {"type": tool_choice}
        else:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
    if session_id and urlparse(str(url or "")).netloc == "openrouter.ai":
        payload["session_id"] = "neno:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

    started = time.perf_counter()
    log_event(
        "openrouter",
        "openrouter_request_start",
        trace_id=trace_id,
        model=model_name,
        message_count=len(messages),
    )

    try:
        from app.services.usage import measured_post
        resp = measured_post(url, headers=headers, json=payload, timeout=timeout, proxies=_proxies(),
                             purpose=purpose, trace_id=trace_id, session_id=session_id)
    except requests.RequestException as e:
        log_event(
            "openrouter",
            "openrouter_request_failed",
            trace_id=trace_id,
            status_code=None,
            error_type=type(e).__name__,
            error_message=str(e),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise RuntimeError("LLM request failed") from e

    if resp.status_code != 200:
        log_event(
            "openrouter",
            "openrouter_request_failed",
            trace_id=trace_id,
            status_code=resp.status_code,
            error_type="HTTPError",
            error_message=resp.text[:200],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise RuntimeError("LLM provider error")

    data = resp.json()

    usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cache_hit_count = details.get("cached_tokens", usage.get("cache_read_input_tokens", 0))
    cache_write_count = details.get("cache_write_tokens", usage.get("cache_creation_input_tokens", 0))
    if cache_hit_count or cache_write_count:
        log_event(
            "openrouter", "openrouter_cache_usage", trace_id=trace_id,
            cache_hit_count=cache_hit_count, cache_write_count=cache_write_count,
        )

    if return_message:
        try:
            result = _response_message(data, is_anthropic)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError("Invalid tool response format from provider") from exc
        log_event("openrouter", "openrouter_request_ok", trace_id=trace_id,
                  tool_call_count=len(result.get("tool_calls") or []),
                  latency_ms=int((time.perf_counter() - started) * 1000))
        return result

    try:
        if is_anthropic:
            blocks = data["content"]
            if isinstance(blocks, str):
                content = blocks
            else:
                content = "".join(
                    str(block.get("text") or "")
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        else:
            content = data["choices"][0]["message"].get("content")
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
                )
            if content is None:
                content = data.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise KeyError("response content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        log_event(
            "openrouter",
            "openrouter_request_failed",
            trace_id=trace_id,
            status_code=resp.status_code,
            error_type=type(exc).__name__,
            error_message=(
                "Invalid response format from OpenRouter "
                f"(message_keys={sorted(((data.get('choices') or [{}])[0].get('message') or {}).keys())})"
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise RuntimeError("Invalid response format from OpenRouter") from exc

    log_event(
        "openrouter",
        "openrouter_request_ok",
        trace_id=trace_id,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return content


def multimodal_chat_with_openrouter(
    api_key: str,
    url: str,
    model_name: str,
    text_prompt: str,
    image_urls: list[str],
    timeout: int = 60,
    trace_id: str | None = None,
) -> str:
    content = [{"type": "text", "text": text_prompt}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": image_url},
        }
        for image_url in image_urls
    )
    messages = [{"role": "user", "content": content}]
    return chat_with_openrouter(
        api_key=api_key,
        url=url,
        model_name=model_name,
        messages=messages,
        timeout=timeout,
        trace_id=trace_id,
    )
