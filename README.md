# Neno Core

Neno Core 是一个本地优先的最小聊天后端：本地 HTTP 聊天 API、SQLite 消息历史、
同 session 串行/聚合保护、LLM provider 客户端、单句 system prompt、
基础日志/trace，以及低耦合的多模态（图片）输入归一化接口。

非核心的平台桥接（微信/QQ/企微/OpenClaw）、移动端、主动消息、关系/记忆系统、
Living World/Consciousness 等已从运行时和源码中移除。

## 架构

```text
POST /chat
  │
  ├─ 输入归一化（图片附件 → 视觉模型文本投影，纯文本透传）
  │
  ├─ SessionAggregationController（聚合窗口：突发消息合并为一个批次）
  │
  ├─ SessionSubmitController（同 session 严格串行，绝不并发跑 turn）
  │
  └─ run_chat_turn
       ├─ 读取近期历史（SQLite，按 token 预算）
       ├─ 落库 user 消息（metadata 冻结快照）
       ├─ 组装 prompt：system（单句人设）→ 历史 → 当前消息
       ├─ 调 LLM provider（OpenAI 兼容 / Anthropic Messages）
       └─ 落库 assistant 回复
```

| 路径 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI 应用装配 |
| `app/config.py` | 环境变量配置（LLM、历史预算、聚合窗口、ADMIN_TOKEN、system prompt） |
| `app/routers/chat.py` | 聊天入口：归一化 → 聚合 → 串行提交 |
| `app/routers/session.py` | 会话历史管理（admin 鉴权） |
| `app/routers/system.py` | 健康检查（`GET /`、`GET /health`） |
| `app/services/chat_service.py` | 最小聊天主链（prompt 组装 + turn 执行） |
| `app/services/session_aggregation_controller.py` | 同 session 突发消息聚合（内存锁） |
| `app/services/session_submit_controller.py` | 同 session 串行提交（内存锁） |
| `app/services/chat/llm_gateway.py` | LLM 网关（回复生成 / 图片输入解析） |
| `app/services/chat/multimodal_input_service.py` | 多模态输入归一化接口 |
| `app/llm/openrouter_client.py` | LLM provider HTTP 客户端 |
| `app/storage/db.py` | SQLite：messages / chat_stats / debug_events |
| `app/utils/logging_utils.py` | 脱敏日志 + trace_id + debug_events 落库 |
| `prompts/system.txt` | 当前单句 system prompt |
| `tests/` | 核心单元/集成测试 |

## 快速开始

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # 填入真实 key，只放在本机 .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## API

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| `POST` | `/chat` | `X-Internal-Token` | 发送消息，返回回复；仅供本机 Gateway/受信调用方 |
| `GET` | `/` | 无 | 存活检查 |
| `GET` | `/health` | 无 | 健康检查（DB + 近 24h 错误数） |
| `GET` | `/session/messages` | `X-Admin-Token` | 读取会话历史 |
| `GET` | `/session/list` | `X-Admin-Token` | 会话列表 |
| `POST` | `/session/clear` | `X-Admin-Token` | 清空会话 |
| `POST` | `/session/delete-message` | `X-Admin-Token` | 按 trace 删除一轮 |

### POST /chat

请求：

```json
{
  "message": "你好",
  "session_id": "default",
  "attachments": [
    {"kind": "image", "media_path": "C:/path/to.png", "mime_type": "image/png"}
  ]
}
```

响应：

```json
{
  "reply": "……",
  "trace_id": "ab12cd34",
  "session_id": "default",
  "user_message_id": 1,
  "user_message_ids": [1],
  "assistant_message_id": 2,
  "message_type": "text",
  "latency_ms": 812
}
```

说明：

- `attachments` 可选；图片附件会先经视觉模型归一化为文本投影（`message_type: "image"`），
  持久历史只存文本，不存图片 payload。
- `/chat` 要求配置的 `NENO_INTERNAL_TOKEN`，请求头为 `X-Internal-Token`；未配置时接口 fail-closed。
- 单次请求最多 8 个附件；图片附件最多 8 张，且 `media_path` 必须位于受信媒体 spool 目录。
- 同一 `session_id` 的消息严格串行处理；窗口内的突发消息会合并成一个 turn 一次回复。
- LLM 失败返回 500；此时 user 消息已落库，历史不丢。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `OPENROUTER_API_KEY` | LLM provider key |
| `OPENROUTER_URL` | provider 端点（OpenAI 兼容或 Anthropic Messages） |
| `OPENROUTER_CHAT_MODEL` | 聊天模型 |
| `OPENROUTER_VISION_MODEL` | 图片理解模型（默认同聊天模型） |
| `OPENROUTER_PROXY` | 可选代理 |
| `CHAT_MAX_TOKENS` | 回复长度上限（默认 220） |
| `LLM_TIMEOUT_SECONDS` | LLM 请求超时（默认 60） |
| `HISTORY_TOKEN_LIMIT` | 送入 prompt 的历史 token 预算（默认 500） |
| `SESSION_AGGREGATION_WINDOW_SECONDS` | 突发消息聚合窗口（默认 3） |
| `ADMIN_TOKEN` | `/session/*` 鉴权 |
| `NENO_INTERNAL_TOKEN` | `/chat` 与 Telegram Gateway 的内部鉴权 |
| `CORS_ALLOWED_ORIGINS` | 显式允许的浏览器 Origin，逗号分隔；留空表示不启用 CORS |
| `NENO_IMAGE_SPOOL_ROOTS` | Telegram/本地媒体输入的受信目录，按系统路径分隔符分隔 |

## 测试

```powershell
python -m pytest -q
```

## 运维脚本

- `scripts/start-neno-backend.ps1`：后台启动（127.0.0.1:8000）
- `scripts/neno-reboot.ps1`：重启后端（改 `.env` / prompt / 代码后执行）
- `scripts/start-cloudflared-tunnel.ps1`：远程访问隧道（可选）
- `scripts/build-telegram-sidecar.ps1`：编译 C++ TDLib sidecar
- `scripts/build-tdlib.ps1`：从固定 TDLib tag 构建 `tdjson.dll`
- `scripts/start-telegram-gateway.ps1`：启动 TDLib Telegram Gateway（需先配置 `.env`）

## Telegram 普通用户客户端

Telegram 接入使用 TDLib，不使用 Bot API。C++ sidecar 负责加载 `tdjson.dll`
并通过 JSONL stdin/stdout 承载 TDLib；Python `telegram_gateway` 负责授权状态、
update、多媒体下载和调用本地 `/chat`。TDLib 的数据库和文件目录独立放在
`data/telegram/tdlib/`，Telegram 进程不会直接导入 `app` 内部模块。

先构建 sidecar 和 TDLib：

```powershell
pwsh -NoProfile -File scripts\build-telegram-sidecar.ps1
pwsh -NoProfile -File scripts\build-tdlib.ps1
```

再在 `.env` 填写 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、
`TELEGRAM_DATABASE_ENCRYPTION_KEY` 和明确的 `TELEGRAM_ALLOWED_CHAT_IDS`，
先进行一次交互式用户授权：

```powershell
python -m telegram_gateway --login
```

授权成功后启动：

```powershell
pwsh -NoProfile -File scripts\start-telegram-gateway.ps1
```

白名单为空时 Gateway fail-closed，不处理任何聊天。实时通话不在当前实现范围内；
语音消息与实时通话是两条不同链路。
