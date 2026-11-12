"""档位：.env 里一个 MODEL_PRESET 定下各层用什么模型，只要再填对应的密钥。想细调，再把某一层单独写出来覆盖。

没设 MODEL_PRESET 就什么都不动——老配置照旧（各层自己写模型名，都走 OpenRouter）。

档位怎么来的：data/model-compare/run1/report.md 的对比和用户的结论（见 docs/mobile-plan.md 1.2）。
第一反应、世界里的 NPC、上网、睡前回想、摘要、看图，各档都用 DeepSeek V4.1 Flash（便宜，原生看图）。
"""

from __future__ import annotations

import os
from typing import MutableMapping

_DS_FLASH, _DS_PRO = "deepseek:deepseek-flash", "deepseek:deepseek-v4-pro"
_OR_FLASH, _OR_PRO = "openrouter:deepseek/deepseek-v4.1-flash", "openrouter:deepseek/deepseek-v4-pro"
_GEMINI = "openrouter:google/gemini-3.8-flash"

# 档位 → 四类用途各用哪个模型
PRESETS: dict[str, dict[str, str]] = {
    "cn": {"l1": _DS_FLASH, "think": _DS_PRO, "expr": _DS_PRO, "other": _DS_FLASH},            # 国内：一个 DeepSeek 密钥
    "cn-gemini": {"l1": _DS_FLASH, "think": _DS_PRO, "expr": _GEMINI, "other": _DS_FLASH},     # 国内 + Gemini 措辞：两个密钥
    "intl": {"l1": _OR_FLASH, "think": _OR_PRO, "expr": _GEMINI, "other": _OR_FLASH},          # 境外：一个 OpenRouter 密钥
    "high": {"l1": _OR_FLASH, "think": "openrouter:anthropic/claude-opus-5.5",                 # 高配：不在乎钱
             "expr": "openrouter:google/gemini-4-carbon", "other": _OR_FLASH},
}
LABELS = {"cn": "国内（一个 DeepSeek 密钥）", "cn-gemini": "国内 + Gemini 措辞（DeepSeek + OpenRouter）",
          "intl": "境外（一个 OpenRouter 密钥）", "high": "高配（OpenRouter，贵）"}
# 环境变量 → 属于哪类用途
LAYERS: dict[str, str] = {
    "MIND_L1_MODEL": "l1",
    "MIND_THINK_MODEL": "think", "OPENROUTER_CHAT_MODEL": "think",
    "MIND_EXPR_MODEL": "expr",
    "MIND_REFLECT_MODEL": "other", "WORLD_NPC_MODEL": "other", "WORLD_LLM_MODEL": "other",
    "CONTEXT_SUMMARY_MODEL": "other", "IMAGE_ARCHIVE_MODEL": "other", "OPENROUTER_VISION_MODEL": "other",
}


def models(name: str, overrides: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """这个档位下每个环境变量最后是什么模型（overrides 里单独写了的优先）。"""
    preset = PRESETS[name]
    result = {env: preset[layer] for env, layer in LAYERS.items()}
    for env in result:
        if overrides and str(overrides.get(env) or "").strip():
            result[env] = str(overrides[env]).strip()
    return result


def apply(environ: MutableMapping[str, str] | None = None) -> str:
    """按 MODEL_PRESET 把没单独写的那几层填上。返回生效的档位名（没设返回空串；写错了报错）。"""
    environ = os.environ if environ is None else environ
    name = str(environ.get("MODEL_PRESET") or "").strip().lower()
    if not name:
        return ""
    if name not in PRESETS:
        raise ValueError(f"MODEL_PRESET={name} 不认识，可选：{' / '.join(PRESETS)}")
    for env, model in models(name, environ).items():
        environ[env] = model
    return name
