# 后端运维与恢复（2026-09-26）

本次只调整聊天投递、历史维护和运维。主聊天仍使用 Opus，图片归档与历史压缩使用 Luna；不启用人设，不把世界信息接入聊天，不改变现有 48 小时世界试运行。

## 消息投递

- Telegram 收到的输入先写入 data/operations.db 的 inbox，生成后的完整回复和发送计划分别保存。文本仍合并发送，仅超过 Telegram 单条长度限制才分块；表情包在文字之后，发送间隔至少 2 秒。
- /chat 支持 request_id。相同 ID 与相同输入返回之前保存的回复，不重新生成、不重复写消息；同 ID 换输入返回 409。
- 生成途中中断会复用已写入的用户行。回复已落库但 HTTP 回包丢失，也能从 trace 恢复。正在执行的请求保留 15 分钟租约，避免并发重复生成。
- outbox 保存逐条发送状态。已确认部分不重发；明确失败的部分退避重试；发送状态未知时查询 TDLib，不直接重发。
- 如果连 TDLib 临时消息 ID 都没拿到，标记该输入为 needs_attention，让后续新输入继续处理。需要人工核实并提交实际消息 ID，不能把网络超时等同于没有发送。
- 这不是端到端 exactly-once 保证：持久队列只覆盖网关已经接收并写盘的输入；未进入 inbox 的历史消息不自动批量补发。首次启用不会导入旧聊天并触发历史回复。

## 图片与历史

- 主聊天不再同步等待历史图片归档。后台 worker 处理独立队列，失败退避重试，连续失败达到 10 次时暂停并暴露诊断。
- 近期图片仍直接进入主模型；旧图保留原文件、图片 ID 和文字描述，仍可通过原有图片工具重新读取。
- 历史压缩按有限批次滚动合并，保留消息来源 ID、未完成事项以及真实图片 ID。原聊天记录不删除。长历史积压可能需要多个后台批次，摘要是有损压缩，不代表逐字保留所有旧细节。
- 图片描述可以人工修正或重新排队；后台旧结果不会覆盖已经确认的人工描述。修正后重新构建历史摘要。
- 删除一轮只取消该轮的投递，并作废该会话的摘要；清空会话会同时取消相关队列、请求缓存和摘要，防止旧回复再次发送。已有备份不会随聊天清空而删除。

## 管理 API

所有 /ops 接口需要 x-admin-token 请求头，值取本机 .env 的 ADMIN_TOKEN。不要把 token 写入公开日志或命令历史。

| 方法 / 路径 | 用途 |
|---|---|
| GET /ops/status | inbox / outbox / 后台任务状态计数与最近备份状态 |
| GET /ops/delivery?limit=30 | 投递阶段、尝试次数、TDLib 消息 ID、错误；不返回消息正文 |
| POST /ops/delivery/retry | body: {"inbox_key":"tg:CHAT_ID:MESSAGE_ID"}；只重试明确失败项，复用已保存回复 |
| POST /ops/delivery/resolve | body: {"outbox_key":"tg:CHAT_ID:MESSAGE_ID:SEQ","message_id":123}；查询并核实指定的 TG 消息 |
| GET /ops/jobs | 后台任务、租约、退避与错误 |
| POST /ops/jobs/retry | body: {"key":"context_summary:SESSION_ID"}；人工恢复失败任务 |
| GET /ops/context?session_id=SESSION_ID | 当前摘要、水位线与来源 ID |
| POST /ops/images/archive | body: {"session_id":"SESSION_ID","image_id":"IMAGE_ID","description":"修正描述"}；省略 description 则重新识别 |
| GET /ops/usage?days=1 | 按用途与模型汇总调用、输入/输出/缓存 token、实际费用和未知费用次数 |

resolve 不会立即假定成功，网关还需验证消息归属、方向及正文。没有已知临时 ID 的媒体，操作员必须先在 TG 核实，再显式附加 confirm_media=true；网关仍核对媒体类型与发送时间。delivery/retry 拒绝状态未知的发送，不提供自动盲目重发路径。

## 计费

- 每次新调用记录用途 chat / image_archive / context_summary / world、模型、provider 请求 ID、延迟、token 与 provider 返回的 usage.cost。
- 缺少费用字段记为 NULL，不能按零费用解释；不使用不可靠的字符换算冒充真实账单，不重复叠加缓存 token。
- 可选 LLM_DAILY_BUDGET_USD 是已记录实际支出的 UTC 日软阈值。未返回费用、正在执行的并发调用及旧进程不在硬限额保证内；本次没有擅自设置金额。
- 正在运行的 48 小时世界进程没有重启，仍使用它启动时的代码。因此本次新增逐调用账本不追补当前试运行的历史费用，不能把账本里的世界空缺解读为免费。新启动的世界进程才使用新记录逻辑。

## 备份与恢复

备份位置由 NENO_BACKUP_DIRECTORY 配置，本机为 E:/NenoBackups；NENO_BACKUP_RETENTION=7 保留最近 7 个日常快照。Windows 任务 Neno-Backend-Backup 每天本地时间 04:20 执行，错过后在系统可用时补跑。

备份包含源代码、运行脚本、文档、.env、聊天/运维/世界试运行 SQLite、媒体与表情包、TDLib 运行库。SQLite 使用在线快照，每个数据库分别一致，不声称跨库同一瞬间的分布式事务快照。先固定数据库，再复制它引用的媒体。每个文件记录 SHA256，备份与恢复都验证。

初次部署另外保留一个 Telegram 已停机时的 cold_snapshot，包括 TDLib 登录数据库，且不参与日常 7 份轮换。日常任务不热拷贝正在运行的 TDLib 登录库；授权更新后需要再次停网关创建新的冷快照。快照含密钥与登录态，仅保存在本机备份盘，NTFS ACL 限当前用户和 SYSTEM；这不等于加密，仍受备份盘自身加密/安全状况影响。

手动操作（PowerShell，项目根目录）：

    .\venv\Scripts\python.exe scripts\backup_backend.py
    .\venv\Scripts\python.exe scripts\backup_backend.py --verify E:\NenoBackups\neno-SNAPSHOT
    .\venv\Scripts\python.exe scripts\backup_backend.py --restore E:\NenoBackups\neno-SNAPSHOT --restore-to D:\Neno-restore-check
    .\scripts\install-backend-backup.ps1

恢复工具只写全新或空目录，拒绝覆盖当前项目。恢复到不同路径时，启用前检查 .env 中的绝对路径及 image_assets.file_path；需要按新根目录迁移。恢复脚本不自动启动服务、不覆盖现有数据库。重装后仍需用 requirements.txt 重建 venv；同路径恢复可沿用保留的媒体路径。

离线验收：

    .\venv\Scripts\python.exe -X utf8 scripts\verify_backend_operations.py

该脚本只用临时数据库、合成数据、假的 TDLib/provider；不发真实消息、不收费，也不触碰试运行世界。检查幂等请求、部分发送恢复、未知状态不重发、后台维护、历史清空、实际费用、管理员认证和备份恢复。
