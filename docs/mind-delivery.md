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
| `status` | `pending` / `previewed` / `opened` / `queued` / `decided` / `failed` / `cancelled` / `retracted` |

### mind_deferred（待想队列）

回复作业（reason：`reply_now` / `level2_fast` / `quick_reply` / `level2_busy` / `retracted_rebuild` / `quick_reply_then_think`）。不再按 session 合并；新消息点开时 pending 作业合批重想（见"作业合批规则"）。`due_at` 计算见"时间算法"。

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
- `GET /mind/deliveries/due?session_id=&limit=`：原子认领后返回（`pending → claimed`，租约 `MIND_DELIVERY_LEASE_SEC`），按 `(send_at, seq)` 排序。
- `POST /mind/deliveries/{id}/ack {"status":"sent"|"failed"}`：`sent` 才写 assistant 历史（text/sticker 才写）；重复 ack 幂等返回。`failed` 只记录，不重试。
- `POST /mind/retract {"trace_id":...}`：`seen=none` → 直接撤销（不进周期）；`seen=preview|opened` → 记 `retracted=1`，下一轮工作记忆的"当前情境"写一句撤回提示。

## 时间算法

- 看手机时刻：`seed = sha256(session_id:trace_id)` 抽 `u ~ U(0,1)`，目标风险 `H = -ln(1-u)`，沿课表累计 `λ·Δt`；等级 0 的 λ = 0（直接跳过），等级 4 = 最近 `MIND_FOCUS_WINDOW_MIN` 分钟发过消息。未读 n 条 → λ × min(2, 1+0.2(n-1))。最多模拟 24 小时。
  - 专注窗口（等级 4）只看**她在这个 session 里的活动**：`last_self_activity(session_id, received_at)` = 该 session 在收到时刻之前最近一次 `assistant` 消息（异步模式下 ack sent 才写入，代表真的发出去了）或 `mind_perception.seen='opened'` 的 `seen_at`（她点开过），取较晚者。用户消息不算。
  - 抽样叠加：`focus_until = last_self_activity + MIND_FOCUS_WINDOW_MIN`；在 `[received_at, focus_until)` 内课表等级 >= 3 的部分按等级 4 的 λ 累加，窗口之后按课表原始等级；等级 <= 2 的时段永不升级。窗口只按收到消息那一刻已有的活动计算，同一 seed 结果确定。
  - `segment_at(t, session_id)` 只有显式传 session_id 且等级 >=3、`t < last_self_activity + 窗口` 时才升 4；`glance()` 判级与抽样口径一致。
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

- **她在聊天里**：课表等级 ≥2，且她正在想（作业 running）/正在发（text/typing 未发完）/最后活动在 `MIND_IN_CHAT_LINGER_SEC`（60）秒内 → 新消息不抽样，`glance_at = 收到 + MIND_IN_CHAT_GLANCE_SEC`（2s）。
- **等停顿再想**：回复作业 `due_at = min(本批及更新未读的最后收到时刻 + MIND_SETTLE_SEC[等级], 点开 + MIND_SETTLE_MAX_SEC)`，默认等级 4=5s、2/3=10s、上限 30s。
- **想到一半被打断**：作业跑完周期后，若本批之后还有没开始想的新消息且本轮一句未发 → 取消本轮投递、作业 cancelled、trace 改回 opened，记"未送达"经历；新消息已点开则立即合批重决策，未点开则等它 glance。卡住恢复在有即将 glance 的新消息时让路。
- **点开即看到全部未读**：任一条 glance 结果为 opened 时，同 session 已收到、未撤回的 pending/previewed 感知一并置 opened（同一 `seen_at`，各自 mark_read），进同一批决策。
- **pending 合批重想**：新消息被点开时，如果该会话有 `status='pending'` 的回复作业：旧作业置 `cancelled`，其 trace 改回 `opened`，与新消息合成一批重新跑一次 L1，写一个新作业。
- **pending 的深想作业同样参与合批重想**：`quick_reply_then_think` 尚未开始时与新消息点开重叠，也会被取消并重新合批。
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

## 网关接入（P3b 已实现）

- 网关开关：`NENO_MIND_ASYNC=false`（与 core 的 `MIND_ASYNC_DELIVERY` 保持一致）。false 时网关行为与旧同步链路完全一致。
- `NENO_MIND_POLL_SEC=1`：轮询周期。`NENO_MIND_MAX_CONCURRENCY=4`：不同会话并发上限；同一会话严格串行。
- 网关本地 `mind_outbox` 表以 `delivery_id` 为主键幂等：同一项 core 租约过期后重发不会重复 sendMessage，只按本地状态推进或补 ack。状态机 `pending → submitting → sent / failed / uncertain / needs_attention`：text/sticker 在交给 TDLib 前置 `submitting`（attempts+1），拿到 local_id 立刻写账；崩溃恢复时 `submitting` 与 `uncertain` 一样走 `getMessage` 核对，不重发；429 退回 `pending`，暂停结束后正常重发。发送回执通过 observer 双写旧 outbox 与 `mind_outbox`；`uncertain` 超过 `NENO_MIND_UNCERTAIN_MAX_SEC`（默认 600）仍无结论 → `needs_attention`，之后不再处理也不 ack、不重发。
- text：`payload.text` 原样一条消息发送，超 4096 分块。确认语义沿用现有 outbox：Succeeded → ack sent；明确失败 → ack failed；结果不确定 → 本地 `uncertain`，确认前不 ack、不重发。
- mark_read：`payload.trace_ids`（每条 mark_read 的 trace_ids 约定只有一条）在 inbox 映射到 message_id 后调 `viewMessages`（force_read）。映射缺失时兜底取该聊天里 `created_at <= send_at` 的最新一条入站消息并记 warning——不使用当前时刻，避免把看手机之后新到的消息标成已读。
- typing：每 4 秒 `sendChatAction(chatActionTyping)`，持续 `payload.duration` 秒。
- sticker：`payload.name` 查 StickerTable，失败退回图片；名字未知 → ack failed("unknown sticker")。
- 轮询为每个会话一个独立循环（各自 sleep 轮询间隔），执行时用 Semaphore 限制并发；一个会话在长 typing 时不阻塞其他会话。
- 429：该会话暂停 retry_after 秒，不 ack failed；本地记录退回 pending，暂停结束后重新执行。
- 撤回：`updateDeleteMessages`（is_permanent=true 且 from_cache=false）→ 从没提交过（payload 为空）直接取消；已有 trace 调 `/mind/retract`（失败持久化 `inbox.retract_state='retry'`，网关每 10 秒重试，404 视为完成）；generating 或 payload 已生成但无 trace 先标 pending，prepare 拿到 trace 后补撤。同步模式只记日志。
- presence：`NENO_MIND_PRESENCE=true` 时启动 offline；动作前 online；最后一个动作 `NENO_MIND_PRESENCE_LINGER_SEC` 秒后 offline。网关无其他 openChat/viewMessages 调用。
