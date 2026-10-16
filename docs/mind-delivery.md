# P3a 投递计划 / 异步消息接口

只在 `MIND_ASYNC_DELIVERY=true` 且 `MIND_ENABLED=true` 时生效；否则 `/chat` 保持同步返回、不建感知记录。

## 数据表

### mind_perception（L0 感知记录）

| 字段 | 说明 |
| --- | --- |
| `trace_id` | 该条用户消息的 trace（唯一） |
| `received_at` / `glance_at` | 收到时刻 / 抽样出的看手机时刻 |
| `seen` | `none` \| `preview` \| `opened` |
| `seen_at` | 点开时刻（mark_read 锚点） |
| `preview_text` | 预览截取的前 `MIND_PREVIEW_CHARS` 字 |
| `l1_preview_json` | 预览时跑的 L1（抢占判断用） |
| `retracted` / `retracted_seen` | 撤回标记与撤回时的 seen |
| `status` | `pending` / `previewed` / `opened` / `decided` / `cancelled` / `retracted` |

### mind_deferred（待想队列）

同一 session 同时只有一条 `pending`；新消息合并进同一条，`due_at` 只能提前不能推后。出队 = 下一个等级 ≥ min_level 的段开始后再按 λ 抽一次。

### mind_delivery（投递计划）

| 字段 | 说明 |
| --- | --- |
| `cycle_id`, `seq` | 幂等键（UNIQUE） |
| `kind` | `mark_read` \| `typing` \| `text` \| `sticker` |
| `payload_json` | typing 带 `duration`；text 带 `text`；sticker 带 `name` |
| `send_at` | 绝对时刻 |
| `status` | `pending` \| `sent` \| `failed` \| `cancelled` |

`status` 完整集合：`pending`（未认领）→ `claimed`（被 `/mind/deliveries/due` 认领，带 `lease_until`）→ `sent` / `failed`（ack 后终态）。`cancelled` 只用于 text/sticker/typing；`mark_read` 不随打断或撤回取消。

## 记忆与发送一致性

周期内的 text/sticker 一条都没发出就被取消（打断/撤回）时，追加一条简短经历：
`想回「前40字」但没发出去（原因：被新消息打断/对方撤回）`，tags 含"未送达"，note_idx=98（UNIQUE 幂等）。已发出的内容以 ack(sent) 写进 messages 为准。

text ack=failed 时，同一周期后续 text/sticker/typing 全部取消（mark_read 保留）并记日志。

## 网关约定（P3b）

- 网关配置：`NENO_MIND_POLL_SEC=1`、`NENO_MIND_MAX_CONCURRENCY=4`；同一会话串行执行。
- mark_read 的计划时间锚 seen_at，实际执行时间以网关 ack 为准，记录在 acked_at。
- 等级 2 先短回、之后出队深想时按当时状态重想；已发出的短回进历史，深想时能看到，不覆盖、不撤回。
- `/mind/deliveries/due` 会在返回前原子认领（`pending → claimed`，租约 `MIND_DELIVERY_LEASE_SEC` 默认 60s）；租约过期后可重新拿到。ack 接受 pending 或 claimed，重复 ack 幂等。

## 接口（`/mind/*`，沿用 `X-Internal-Token` 鉴权）

- `POST /chat`：照常落库 user 消息、建感知记录，返回 `{"status":"accepted","trace_id":...}`；同 trace 重发不重复建记录。
- `GET /mind/deliveries/due?session_id=&limit=`：返回 `send_at ≤ now` 且 pending 的项，按 `(send_at, seq)` 排序。
- `POST /mind/deliveries/{id}/ack {"status":"sent"|"failed"}`：`sent` 才写 assistant 历史（text/sticker 才写）；重复 ack 幂等返回。`failed` 只记录，不重试。
- `POST /mind/retract {"trace_id":...}`：`seen=none` → 直接撤销（不进周期）；`seen=preview|opened` → 记 `retracted=1`，下一轮工作记忆的"当前情境"写一句撤回提示。

## 时间算法

- 看手机时刻：`seed = sha256(session_id:trace_id)` 抽 `u ~ U(0,1)`，目标风险 `H = -ln(1-u)`，沿课表累计 `λ·Δt`；等级 0 的 λ = 0（直接跳过），等级 4 = 最近 `MIND_FOCUS_WINDOW_MIN` 分钟发过消息。未读 n 条 → λ × min(2, 1+0.2(n-1))。最多模拟 24 小时。
- 投递计划：基准 `t0 = 决策完成时刻`；`mark_read` 在 `seen_at`；每条消息前 `typing`（时长 = 字数 / `MIND_TYPING_CPS`，乘 0.8–1.2 抖动，截到 [1,15]）；条与条之间 `MIND_MSG_GAP_SEC`；`hesitate=true` 第一条前先 typing 3 秒、停 `MIND_HESITATE_PAUSE_SEC` 再发。所有随机以 `sha256(cycle_id)` 派生，同一 cycle 重放结果一致。
- 打断规则：新消息到达时，周期里 text/sticker 一条都没发出 → 取消剩余 pending、合并成新周期重想；已发出一条以上 → 让它发完。

## 调度器

### 感知状态机

`mind_perception.status`：`pending → previewed → opened → queued → decided / failed / retracted / cancelled`

- `queued`：决策完成、已绑定 `deferred_id`（回复作业）。
- `decided`：作业执行成功。
- `failed`：作业重试耗尽（`MIND_JOB_MAX_ATTEMPTS`），或 opened 卡住重决策超限。
- `retracted` / `cancelled`：撤回（前者是已读后的撤回，后者是未读直接撤）。

### 作业合批规则

- **pending 合批重想**：新消息被点开时，如果该会话有 `status='pending'` 的回复作业：旧作业置 `cancelled`，其 trace 改回 `opened`，与新消息合成一批重新跑一次 L1，写一个新作业。
- **running 不打扰**：旧作业已经是 `running`（正在生成回复）时，新消息单独决策、单独成作业，不动旧作业。
- `deferred.enqueue` / `enqueue_job` 不再按 reason 合并不同批次；合批只在 `handle_opened` 层完成。

### 投递认领与撤回

- `due_deliveries` 用一条 `UPDATE ... WHERE id IN (SELECT ...) RETURNING *`（SQLite < 3.35 退回 `BEGIN IMMEDIATE` + 条件 UPDATE）完成原子认领。
- `cancel_remaining_deliveries` 只取消 `status='pending'` 的 text/sticker/typing；`claimed` 已交给网关，撤不回。
- `cycle_has_sent_text` 把 `claimed` 状态的 text/sticker 也算作"已发出"。
- ack sent 时如果行已是 cancelled，仍改成 sent 并写 messages（以实际发送为准），记日志 `delivery_sent_after_cancel`。

FastAPI lifespan 里启动（`MIND_ENABLED and MIND_ASYNC_DELIVERY`），每 `MIND_SCHEDULER_TICK_SEC` 秒扫描：

1. `mind_perception` 到期 → L0 → 回复决策；
2. `mind_deferred` 到期 → 出队完整周期（按出队时刻重拼工作记忆）。

租约：`UPDATE ... SET status='running', lease_until=now+LEASE WHERE id=? AND (status='pending' OR (status='running' AND lease_until<now))`，rowcount=1 才处理。启动时把租约过期的 running 退回 pending 重领。同一 session 串行（内存锁）；不同 session 目前也是串行（写进文档）。

## 给 P3b（网关）的对接说明

- 网关按 `GET /mind/deliveries/due` 轮询，收到 `typing` 调用 `sendChatAction`，收到 `mark_read` 调用 `viewMessages`，收到 `text`/`sticker` 真正发送；成功或失败后调 `/ack`。
- 撤回由网关调用 `/mind/retract`；未读消息直接撤销，已读消息进入下一轮工作记忆。
