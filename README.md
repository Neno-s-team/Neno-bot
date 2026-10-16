# Neno Core

Neno Core 是一个本地优先、面向单机部署的聊天后端，通过 Telegram 普通用户客户端（TDLib）接入。
它只负责把一条消息可靠地变成一条回复：串行、去重、可恢复投递，并记住图片和长历史。

当前仓库包含三部分：

| 模块 | 目录 | 状态 |
| --- | --- | --- |
| **Neno Core** 聊天后端 | `app/` | 运行中 |
| **Telegram Gateway**（TDLib sidecar + Python 网关） | `telegram_sidecar/`、`telegram_gateway/` | 运行中 |
| **独立世界引擎**（规则调度 + 可选 LLM 认知） | `app/services/world/`、`content/world/` | 已实现，**当前冻结**，默认不启用，不接入聊天 |

> 旧架构中的企微/微信/QQ/OpenClaw 桥接、移动端、主动消息、关系/记忆系统、Consciousness / Living World 等模块已全部移除。

## 目录

- [架构](#架构)
- [快速开始](#快速开始)
- [配置](#配置)
- [API](#api)
- [Telegram 接入](#telegram-接入)
- [图片记忆与历史压缩](#图片记忆与历史压缩)
- [世界引擎（冻结）](#世界引擎冻结)
- [运维：备份、计费与恢复](#运维备份计费与恢复)
- [测试与验证](#测试与验证)
- [文档索引](#文档索引)
- [安全约定](#安全约定)

## 架构

```text
Telegram（普通用户账号）
   │
   ▼
neno-tdlib-sidecar.exe  (C++，加载 tdjson.dll，stdin/stdout JSONL)
   │
   ▼
telegram_gateway        (Python：授权、白名单、媒体下载、inbox/outbox、表情包)
   │  HTTP + X-Internal-Token
   ▼
Neno Core  POST /chat   (FastAPI, 127.0.0.1:8000)
   ├─ request_id 幂等（同 ID 同输入直接返回已保存回复）
   ├─ 输入归一化（图片登记为 image asset，纯文本透传）
   ├─ SessionAggregationController：窗口内突发消息合并为一个 turn
   ├─ SessionSubmitController：同 session 严格串行
   └─ run_chat_turn
        ├─ 历史摘要 + 近期历史（按 token 预算）
        ├─ 近 N 轮原图直接送入主模型，更早图片用工具回看
        ├─ 调 LLM provider（OpenAI 兼容 / Anthropic Messages）
        └─ 落库 user / assistant 消息
   后台 worker：图片归档描述、历史滚动压缩、失败退避重试
```

| 路径 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI 应用装配；启动维护 worker 与（可选）世界引擎 |
| `app/config.py` | 环境变量配置 |
| `app/routers/chat.py` | `/chat` 入口：鉴权 → 幂等 → 归一化 → 聚合 → 串行提交 |
| `app/routers/session.py` | 会话历史管理 |
| `app/routers/operations.py` | `/ops/*` 运维 API（投递、后台任务、摘要、图片、用量） |
| `app/routers/system.py` | `GET /`、`GET /health` |
| `app/routers/world.py` | `/world/*` 世界引擎管理 API |
| `app/services/chat_service.py` | 聊天主链（prompt 组装 + turn 执行） |
| `app/services/chat/` | 幂等、图片记忆、LLM 网关、多模态归一化、后台维护 |
| `app/services/usage.py`、`stats_service.py` | 逐调用 token / 费用记账 |
| `app/llm/openrouter_client.py` | LLM provider HTTP 客户端 |
| `app/storage/` | SQLite 存储（聊天、运维、世界） |
| `telegram_sidecar/` | C++ TDLib sidecar（不含任何业务逻辑） |
| `telegram_gateway/` | Telegram 网关：授权、策略、投递、表情包 |
| `prompts/system.txt` | 聊天人设提示词（受 `CHAT_PERSONA_ENABLED` 控制） |
| `content/world/` | 世界目录与街区模板（含 BDDL 分类元数据） |
| `scripts/` | 启动、构建、备份、离线验证脚本 |
| `docs/` | 设计与运维文档 |
| `tests/` | 单元 / 集成测试 |

## 快速开始

环境：Windows + PowerShell 7、Python 3.10+。构建 Telegram sidecar 另需 Visual Studio Build Tools 与 CMake。

```powershell
git clone https://github.com/Neno-s-team/Neno-bot.git
cd Neno-bot
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # 填入真实 key，只放在本机 .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

验证：

```powershell
curl http://127.0.0.1:8000/health
```

常用脚本：

| 脚本 | 用途 |
| --- | --- |
| `scripts/start-neno-backend.ps1` | 后台启动后端（127.0.0.1:8000） |
| `scripts/start-neno-stack.ps1` | 一键隐藏启动后端 + Gateway，日志写入 `logs/startup.log` |
| `scripts/neno-reboot.ps1` | 重启后端（改 `.env` / prompt / 代码后执行） |
| `scripts/start-telegram-gateway.ps1` | 启动 Telegram Gateway |
| `scripts/build-telegram-sidecar.ps1` | 编译 C++ TDLib sidecar |
| `scripts/build-tdlib.ps1` | 从固定 tag 构建 `tdjson.dll` |
| `scripts/start-cloudflared-tunnel.ps1` | 可选：Cloudflare Tunnel 远程访问 |

## 配置

所有配置来自根目录 `.env`，模板见 [`.env.example`](.env.example)。主要变量：

**LLM**

| 变量 | 用途 |
| --- | --- |
| `OPENROUTER_API_KEY` / `OPENROUTER_URL` | provider key 与端点（OpenAI 兼容或 Anthropic Messages） |
| `OPENROUTER_CHAT_MODEL` | 主聊天模型 |
| `OPENROUTER_VISION_MODEL` | 图片理解模型（默认同聊天模型） |
| `OPENROUTER_PROXY` | 可选代理 |
| `LLM_EXTRA_BODY` | 主聊天请求体补充（JSON），如关闭思考 |
| `CHAT_MAX_TOKENS` / `LLM_TIMEOUT_SECONDS` | 回复长度上限（默认 220）/ 超时（默认 60s） |
| `LLM_DAILY_BUDGET_USD` | 可选：按已记录实际支出的 UTC 日软阈值 |

**聊天与历史**

| 变量 | 用途 |
| --- | --- |
| `CHAT_PERSONA_ENABLED` | 是否加载 `prompts/system.txt` 人设（开发阶段可设为 `false`） |
| `HISTORY_TOKEN_LIMIT` | 送入 prompt 的近期历史 token 预算（默认 500） |
| `SESSION_AGGREGATION_WINDOW_SECONDS` | 突发消息聚合窗口（默认 3s） |
| `IMAGE_HISTORY_TURNS` | 带原图的最近轮数（默认 5） |
| `IMAGE_ARCHIVE_MODEL` / `IMAGE_ARCHIVE_EXTRA_BODY` | 旧图归档描述模型及其独立参数 |
| `CONTEXT_SUMMARY_ENABLED` / `CONTEXT_SUMMARY_MODEL` | 历史滚动压缩开关与模型 |

**安全**

| 变量 | 用途 |
| --- | --- |
| `NENO_INTERNAL_TOKEN` | `/chat` 与 Gateway 之间的内部鉴权；未配置时 `/chat` fail-closed |
| `ADMIN_TOKEN` | `/session/*`、`/ops/*`、`/world/*` 鉴权 |
| `CORS_ALLOWED_ORIGINS` | 显式允许的浏览器 Origin，逗号分隔；留空不启用 CORS |
| `NENO_IMAGE_SPOOL_ROOTS` | 受信媒体目录（系统路径分隔符分隔）；`media_path` 必须位于其中 |

**Telegram**

| 变量 | 用途 |
| --- | --- |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Telegram 应用凭据（非 Bot Token） |
| `TELEGRAM_DATABASE_ENCRYPTION_KEY` | TDLib 本地数据库加密 key |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 聊天白名单；为空时 Gateway 不处理任何聊天 |
| `TDLIB_JSON_LIBRARY` / `TDLIB_SIDECAR_BINARY` | `tdjson.dll` 与 sidecar 路径 |
| `NENO_CORE_URL` | Core 地址（默认 `http://127.0.0.1:8000`） |
| `TELEGRAM_MAX_MEDIA_MB` / `TELEGRAM_DOWNLOAD_TIMEOUT_SECONDS` | 媒体下载上限与超时 |

**世界引擎 / 备份**：`WORLD_ENABLED`、`WORLD_RULE_SCHEDULER_ENABLED`、`WORLD_LLM_*`、`NENO_BACKUP_DIRECTORY`、`NENO_BACKUP_RETENTION`，见下文对应章节。

## API

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/` | 无 | 存活检查 |
| `GET` | `/health` | 无 | 健康检查（DB、近 24h 错误数、图片/人设状态） |
| `POST` | `/chat` | `X-Internal-Token` | 发送消息并返回回复；仅供本机 Gateway / 受信调用方 |
| `GET` | `/session/messages` | `X-Admin-Token` | 读取会话历史 |
| `GET` | `/session/list` | `X-Admin-Token` | 会话列表 |
| `POST` | `/session/clear` | `X-Admin-Token` | 清空会话（同时取消队列、缓存与摘要） |
| `POST` | `/session/delete-message` | `X-Admin-Token` | 按 trace 删除一轮 |
| `*` | `/ops/*` | `X-Admin-Token` | 运维 API，见[运维](#运维备份计费与恢复) |
| `*` | `/world/*` | `X-Admin-Token` | 世界引擎 API，见[世界引擎](#世界引擎冻结) |

### POST /chat

请求：

```json
{
  "message": "你好",
  "session_id": "default",
  "request_id": "tg:123456:789",
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

行为约定：

- `request_id` 可选：相同 ID + 相同输入返回已保存的回复，不重新生成；同 ID 换输入返回 `409`。
  执行中的请求持有 15 分钟租约，防止并发重复生成。
- 同一 `session_id` 严格串行；聚合窗口内的突发消息合并为一个 turn、一次回复。
- 单次请求最多 8 个附件 / 8 张图片，单张源文件 ≤ 20 MiB，`media_path` 必须位于受信 spool 目录。
- LLM 失败返回 `500`；user 消息已落库，历史不丢，重试会复用该行。

## Telegram 接入

使用 TDLib **普通用户客户端**，不使用 Bot API。

- `telegram_sidecar`（C++）：运行时加载固定版本的 `tdjson.dll`，通过 stdin/stdout JSONL 代理 TDLib，不含任何业务逻辑，不接收、不打印凭据。
- `telegram_gateway`（Python）：授权状态、update 处理、白名单策略、媒体下载、表情包、调用本地 `/chat`。不直接导入 `app` 内部模块。
- TDLib 数据库与文件目录独立放在 `data/telegram/tdlib/`。

```powershell
# 1. 构建
pwsh -NoProfile -File scripts\build-telegram-sidecar.ps1
pwsh -NoProfile -File scripts\build-tdlib.ps1

# 2. 在 .env 填写 TELEGRAM_API_ID / TELEGRAM_API_HASH /
#    TELEGRAM_DATABASE_ENCRYPTION_KEY / TELEGRAM_ALLOWED_CHAT_IDS

# 3. 首次交互式登录
python -m telegram_gateway --login

# 4. 启动
pwsh -NoProfile -File scripts\start-telegram-gateway.ps1
```

投递可靠性：

- 入站消息先写入 `data/operations.db` 的 inbox；完整回复与发送计划分别持久化到 outbox。
- 已确认发送的部分不重发；明确失败的部分退避重试；状态未知时先查询 TDLib，不直接重发。
- 拿不到 TDLib 临时消息 ID 时标记 `needs_attention`，需人工核实后通过 `/ops/delivery/resolve` 处理。
- 这**不是**端到端 exactly-once：只覆盖已进入 inbox 的输入；首次启用不会回放旧聊天。
- 文本合并发送，超过 Telegram 单条长度才分块；表情包在文字之后发送。实时通话不在当前范围内。

详见 [`telegram_sidecar/README.md`](telegram_sidecar/README.md)。

## 图片记忆与历史压缩

- 图片直接送入主聊天模型；当前轮及前 `IMAGE_HISTORY_TURNS - 1` 轮带原图（聚合消息算一轮）。
- 更早的图片由后台 worker 用 `IMAGE_ARCHIVE_MODEL` 生成客观描述（每批 ≤ 8 张，不带聊天历史）；失败退避重试，连续 10 次失败暂停并暴露诊断。
- 原图存于 `data/media/images/`，SQLite `image_assets` 只保存文件引用、哈希、归属和描述，不存 Base64；发送时按需缩放至长边 ≤ 2048。
- 模型可调用 `view_image(image_id)` 回看原图、`search_images(query, limit, offset)` 检索本会话图片；回看不产生额外消息记录。
- 历史按有限批次滚动压缩，保留来源消息 ID、未完成事项与图片 ID；原聊天记录不删除。人工修正的图片描述不会被后台结果覆盖。

详见 [`docs/image-memory.md`](docs/image-memory.md)、[`docs/backend-operations.md`](docs/backend-operations.md)。

## 世界引擎（冻结）

一个独立于聊天、持续推进的虚构世界：时钟、地点、物品、NPC、事务化行动、观察与真相分离、可重放事件日志，以及可选的 LLM 角色决策（JSON Schema 约束输出，模型只提意图，规则引擎落实后果）。

**当前状态：已实现并完成 48 小时试运行，现已冻结。** 不调用聊天服务、不发 Telegram、不读聊天记录。

- 主进程中 `WORLD_ENABLED` 默认 `false`，不建表、不启动调度器。
- 独立开发服务（只暴露 `/world` 路由，使用独立 SQLite）：

  ```powershell
  .\venv\Scripts\python.exe -m app.services.world --db data/world-dev.db --port 8011
  ```

- 所有 `/world` 路由（包括只读）需要 `X-Admin-Token`；变更请求需要调用方生成的 `request_id`。
- LLM 认知默认关闭（`WORLD_LLM_ENABLED=false`），并有每日调用次数与最小间隔限制。

详见 [`docs/world-engine-plan.md`](docs/world-engine-plan.md)、[`docs/world-backend.md`](docs/world-backend.md)、[`docs/world-cognition.md`](docs/world-cognition.md)、[`docs/world-running-cost.md`](docs/world-running-cost.md)、[`docs/world-trial.md`](docs/world-trial.md)。

## 运维：备份、计费与恢复

### 运维 API（`X-Admin-Token`）

| 方法 / 路径 | 用途 |
| --- | --- |
| `GET /ops/status` | inbox / outbox / 后台任务计数与最近备份状态 |
| `GET /ops/delivery?limit=30` | 投递阶段、尝试次数、TDLib 消息 ID、错误（不含正文） |
| `POST /ops/delivery/retry` | 重试明确失败的投递，复用已保存回复 |
| `POST /ops/delivery/resolve` | 核实状态未知的 TG 消息 |
| `GET /ops/jobs` / `POST /ops/jobs/retry` | 后台任务状态 / 人工恢复失败任务 |
| `GET /ops/context?session_id=...` | 当前摘要、水位线与来源 ID |
| `POST /ops/images/archive` | 修正图片描述或重新识别 |
| `GET /ops/usage?days=1` | 按用途与模型汇总调用次数、token、实际费用 |

### 计费

每次 LLM 调用按用途（`chat` / `image_archive` / `context_summary` / `world`）记录模型、请求 ID、延迟、token 与 provider 返回的 `usage.cost`。缺失费用记为 `NULL`，不按零处理，也不用字符数估算冒充账单。

### 备份与恢复

备份目录由 `NENO_BACKUP_DIRECTORY` 配置，保留最近 `NENO_BACKUP_RETENTION` 份（默认 7）。内容包括源码、脚本、文档、`.env`、SQLite 在线快照、媒体与表情包、TDLib 运行库。

```powershell
.\venv\Scripts\python.exe scripts\backup_backend.py                       # 立即备份
.\venv\Scripts\python.exe scripts\backup_backend.py --verify <快照目录>     # 校验
.\venv\Scripts\python.exe scripts\backup_backend.py --restore <快照目录> --restore-to <新空目录>
.\scripts\install-backend-backup.ps1                                       # 安装每日计划任务
```

恢复只写入全新或空目录，拒绝覆盖当前项目。

## 测试与验证

```powershell
python -m pytest -q
```

离线验证脚本（临时数据库 + 合成数据 + 假 provider，不发真实消息、不产生费用）：

```powershell
.\venv\Scripts\python.exe -X utf8 scripts\verify_backend_operations.py
.\venv\Scripts\python.exe scripts\verify_world_backend.py
.\venv\Scripts\python.exe scripts\verify_world_cognition.py
```

真实链路验证（会调用模型、产生少量费用，不发 Telegram 消息）：

```powershell
.\venv\Scripts\python.exe scripts\verify-image-memory-live.py --live
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`docs/backend-operations.md`](docs/backend-operations.md) | 投递、历史维护、运维 API、计费、备份恢复 |
| [`docs/image-memory.md`](docs/image-memory.md) | 图片上下文与回看工具 |
| [`docs/world-engine-plan.md`](docs/world-engine-plan.md) | 世界引擎总体规划 |
| [`docs/world-backend.md`](docs/world-backend.md) | 世界后端实现与 API |
| [`docs/world-cognition.md`](docs/world-cognition.md) | 世界中的 LLM 决策 |
| [`docs/world-running-cost.md`](docs/world-running-cost.md) | 世界决策与图片归档运行成本 |
| [`docs/world-trial.md`](docs/world-trial.md) | 48 小时试运行记录 |
| [`docs/remote-access-cloudflare-tunnel.md`](docs/remote-access-cloudflare-tunnel.md) | Cloudflare Tunnel 远程访问 |
| [`docs/codegraph-query-rebuild.md`](docs/codegraph-query-rebuild.md) | 代码图谱索引重建 |

## 安全约定

- `.env`、`data/`、`runtime/`、`logs/` 全部 gitignore；真实 key、token、session ID 不进仓库。
- 后端默认只监听 `127.0.0.1`；对外访问请走 Cloudflare Tunnel 白名单路径，`/chat` 不对公网开放。
- 鉴权全部使用常量时间比较；未配置 token 的接口 fail-closed。
- 日志对 session / chat 等标识符做脱敏，并截断超长字段。
