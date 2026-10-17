# Neno 项目交接（多层大脑 + 世界引擎）

更新：2026-10-17。仓库 `Neno-s-team/Neno-bot`，当前 main = `0fe35b0`，全量 `pytest -q` 214 passed（agent 报告）。
用途：给本地 Claude Code 续接。先读本文件，再按「文档地图」读具体文档。

---

## 0. 工作约定（必须遵守）

- **人设不作参考**：`prompts/system.txt`（77KB）之后会重写，设计和代码都不要依据它；它只是数据。测试期 `CHAT_PERSONA_ENABLED=false`。
- 人设只通过 `prompts/mind/self_card.md` / `style_card.md` 进入大脑，框架与人设解耦。
- 不改 `.env`、不提交凭据、不向真实 TG 发消息（除非用户在做真号实测）、不调用付费模型做无关验证。
- 改动直接推 `main`；每次提交跑全量 `pytest -q`，汇报 commit sha + 测试数量 + 偏差说明。
- **测试必须真断言**：用 monkeypatch 固定随机（如 `perception._seed_float`），断言精确值；`monkeypatch.setattr` 放在测试函数第一行（曾出现放错位置导致测试在功能关闭状态下"通过"）；必要时做"故意改坏 → 测试应失败"的自检。
- **解释必须以代码为准**：之前多次出现"凭感觉解释日志"（如把周六当成工作日洗澡段、编造等级 4 衰减），排查时先读代码/查库再下结论。
- 用户中文交流，偏好简洁直接。

## 0.5 用户的目标与我们的沟通方式

### 用户想要什么（按提出顺序）
- **起点**：给 Neno 设计多层的思考和表达；思考本身也分层（L1 分类、L2 主思考……）。
- **总目标**：把 Neno 做成**拟真人的 AI 框架**。多层思考要拟真，参照人脑分层；另有模拟现实世界的规划。
- **"她现在能不能回"**：用户原本的设计是先看她有没有空、能不能看到消息，再决定回不回 → 落成 L0 三道独立判断（能否感知 / 有没有空 / 想不想回）。
- **世界**：给她一个**真实身份**；现实事件映射成世界事件（例：邮箱）；通过映射能"上网冲浪"；世界要有随机性、自己运转。
  - **不想用剧情限制世界**，但完全不限制又很难做 → 定下原则：**世界没有剧情，只有节律、需求、扰动和后果。现实只读，世界可写。**
  - 世界**不围绕 Neno 转**：NPC 也调用 LLM 推进自己的事。NPC 不走 L1–L4，核心/普通/背景 NPC 都用廉价 LLM 驱动。
  - **关系锁**："我老婆不能被抢"——用户与 Neno 的关系是锁定的世界事实，任何 NPC/事件不得改写。
  - 斯坦福小镇可借鉴（单调但清晰）；世界与现实的对照以后再考虑。
- **上下文/记忆**参照人脑 → 记忆系统分层（工作记忆 / 情景 / 语义等，见 mind-memory.md）。
- **人设之后会改**，用户还在想给她什么人设，现在不要参考。
- **成本敏感**：测试期关掉人设（`CHAT_PERSONA_ENABLED=false`）省钱；一次回复 ≈ L1 1 次 + L2/L3 1 次（fast 跳过）+ E2 1 次（一次生成多句），E3 用代码拆条，不是每句调一次模型。

### 分工与流程
- **2026-10-18 起**：规划、审查、指挥、落代码全部由本地 Claude Code 接手（此前规划/审查由 Notion AI 负责，用户在中间转发提示词）。
- **用户**：拍板关键取舍，做真号实测，贴日志/截图。
- **本地 Claude Code**：规划 → 实施 → 自审 → 跑全量测试 → 直接推 `main`，不建新分支；汇报 commit sha + 测试数 + 偏差。
- 规划阶段用户说过"大脑要爆炸，塞不下太多东西"——规划方要替他**减负**：把复杂设计压成一句话原则和分阶段清单，一次只推进一步。
- 用户原话："你来设计是最好的，因为你懂我"——设计决定可以主动拿主意，但关键取舍（如是否锁关系、是否要剧情）要先问他。

### 沟通习惯
- 中文，简洁直接；先给结论。
- 审查结论格式：**结论 → 必须修 / 应该修 → 回答问题**；必须修的项直接改掉并跑测试，汇报里说明。
- 用户常追问"这合理吗"，也会直接指出错误（例如上课时段的来源、把周六误判成洗澡段）。被指出时先查代码/数据再回答，错了就直接认。
- 实测时用户贴日志/截图，解释必须基于代码路径和数据库记录，不要凭印象。

## 1. 文档地图

| 文档 | 内容 |
| --- | --- |
| `docs/mind-plan.md` | 多层大脑总规划（L0–L4、表达层、状态、记忆、P1–P5）。**第 12 节状态表和开头"尚未实现"已过期**，以本文件为准 |
| `docs/mind-memory.md` | P2 记忆：分词、检索打分、工作记忆裁剪 |
| `docs/mind-delivery.md` | P3 异步投递：表、接口、时间算法、专注窗口、调度器、网关对接 |
| `docs/world-engine-plan.md` | 世界引擎原规划 + 第 14/15 节已实现记录 |
| `docs/world-backend.md` / `docs/world-cognition.md` | 世界后端与世界角色决策的实际接口 |
| `docs/world-simulation-plan.md` | 世界模拟扩展规划（现实映射、冲浪、需求/扰动、NPC、W1–W6） |
| `docs/world-running-cost.md` / `docs/world-trial.md` | 世界决策成本实测、48 小时试运行记录 |

## 2. 多层大脑：结构

```
消息/事件 → L0 感知 → L1 直觉 → L2 深思 + L3 自我 → E1 规划 → E2 措辞 → E3 渲染投递 → 提交记忆
                ↑                                                              ↓
           待想队列 ←──────────── 记忆系统 ←──────────── L4 反思（睡前/空闲）
```

| 层 | 职责 | 实现 | 文件 |
| --- | --- | --- | --- |
| L0 | 能否感知、何时看手机（指数风险抽样，确定性 seed）、打断等级 0–4 | 代码 | `perception.py`、`schedule.py` |
| L1 | 分类/直觉/urgency/cues/depth/reply_will | 小模型 1 次（预览阶段可能多 1 次） | `appraisal.py` |
| L2+L3 | 揣测、记忆、决策；说什么/藏什么/leak/tone/条数 | 主模型 1 次；fast 跳过；一致性检查不过最多重试到 2 次 | `deliberation.py`、`consistency.py` |
| E1 | 由 L3 输出构成，不调模型 | 代码 | `cycle.py` |
| E2 | 写口语，**看不到 L2 推理** | 输出模型 1 次，一次生成多句 | `expression.py` |
| E3 | 格式禁令、拆条、表情包校验、已读/正在输入/发送时间 | 代码 | `expression.py`、`delivery.py` |
| 记忆 | episode/belief、FTS5 检索、工作记忆、提交事务 | 代码 | `memory/` |
| 调度 | 待想队列、作业租约、断点续跑、投递计划 | 代码 | `orchestration.py`、`scheduler.py`、`deferred.py`、`store.py` |

- 认知周期 `cycle_id = sha256(session:trace)`，每层落库 `mind_cycle`，崩溃从最后完成的层续跑，回放不重调模型。
- **回退**：`MIND_L1_MODEL` 未配置或 L1/E2/E3 报错 → 走旧单次调用 `generate_chat_reply(messages)`，这时才会带上 `system.txt`。日志 `mind_l1_model_missing` / `cycle_failed`。
- 记忆只在 L2 有效（非 fast、非降级）且 E3 成功时提交一次。

### 打断等级（P3 暂用课表 `prompts/mind/schedule.json` 代替世界）
| 等级 | 含义 | 课表例子 |
| --- | --- | --- |
| 0 | 不可打断（λ=0） | 睡觉 00:30–08:00；工作日洗澡 18:30–19:30 |
| 1 | 只能瞥一眼（只看预览、不已读；urgency 高可抢占） | 工作日上课 08:00–09:40 / 10:00–11:40 / 14:30–16:10 |
| 2 | 能短回 | 工作日午休、周末自习 10:00–12:00 |
| 3 | 能聊 | 默认空闲 |
| 4 | 专注聊天 | 本 session 里**她**最近 `MIND_FOCUS_WINDOW_MIN`（5）分钟内有活动（ack 过的 assistant 消息或 seen=opened），且课表等级 ≥3；抽样时叠加 |

- `days` 用 isoweekday（1=周一）；时区 `MIND_TIMEZONE=Asia/Shanghai`。

### 关键配置
core：`MIND_ENABLED`、`MIND_ASYNC_DELIVERY`、`MIND_L1_MODEL`、`MIND_DELIVERY_LEASE_SEC=60`、`MIND_JOB_MAX_ATTEMPTS=3`、`MIND_LEASE_SEC=120`、`MIND_OPENED_GRACE_SEC=30`、`MIND_E3_MAX_SINGLE_CHARS=200`、`MIND_FOCUS_WINDOW_MIN=5`、`MIND_L0_LAMBDA_PER_MIN`、`CHAT_PERSONA_ENABLED`。
网关：`NENO_MIND_ASYNC`（须与 core 一致）、`NENO_MIND_POLL_SEC=1`、`NENO_MIND_MAX_CONCURRENCY=4`、`NENO_MIND_PRESENCE=false`、`NENO_MIND_UNCERTAIN_MAX_SEC=600`、`NENO_MIND_PRESENCE_LINGER_SEC=60`。

## 3. 多层大脑：进度

| 阶段 | 内容 | 状态 | 最终 commit |
| --- | --- | --- | --- |
| 规划 | mind-plan / world-simulation-plan | ✅ | `d041209` |
| P1 骨架 | 周期、状态、L1/E1/E2/E3、断点续跑 | ✅ | `6752308` |
| P2 深思与记忆 | L2/L3、一致性、FTS5、工作记忆、提交 | ✅ | `f7e6543` |
| P3a core 感知/延迟 | L0+课表、待想队列（queued 状态机、合批重想）、投递计划、调度器、撤回 | ✅ | `948375d` |
| P3b 网关 | TDLib 已读/正在输入/按计划投递；`mind_outbox` pending→submitting→sent/failed/uncertain/needs_attention；getMessage 核对 | ✅ | `5e06416` |
| P3 收尾 | 429→ChatFloodWait 暂停会话；uncertain 走 getChatHistory 匹配兜底；深想作业 trace 绑定 | ✅ | `6c5e307` |
| P3 修正 | 专注窗口改为按 session 看她的活动、抽样叠加；测试改固定 u | ✅ | `e0cb58c` → `0fe35b0` |
| **P3 真号实测** | 见第 4 节 | 🔄 | — |
| P4 反思 | L4 + 遗忘 + 在意事项 | ⏳ 待规划 | — |
| P5 接入世界 | 世界决策走 L2、主动联系、世界状态替换课表 | ⏳ | — |

## 4. 当前：P3 真号实测

配置：core `MIND_ENABLED=true`、`MIND_ASYNC_DELIVERY=true`、`CHAT_PERSONA_ENABLED=false`；网关 `NENO_MIND_ASYNC=true`、`NENO_MIND_PRESENCE=false`。

| # | 场景 | 预期 | 状态 |
| --- | --- | --- | --- |
| 1 | 忙时来消息 | 上课只预览不已读；下课后已读→正在输入→回复 | 空闲段完整链路已跑通（周六 18:56，5.3 分钟后看，正常） |
| 2 | 用户连发 | 她还没发出任何一条时用户补发 → 取消剩余、合并重想、只回一次；已发出 ≥1 条 → 发完，下一条另起周期 | 10-18 自测通过（假网关打本机 core）；真号待复看 |
| 2' | 专注窗口 | 她回完 5 分钟内再发，多数一二十秒内已读 | 10-18 自测通过：延迟与 seed 推算的等级 4 抽样值逐条一致 |
| 3 | 撤回 | 看到前撤回无反应；看到后撤回，下次回复知道 | 待测 |
| 4 | 表情包 | 名字对得上且能发出 | 待测 |
| 5 | 中途重启网关 | 不重复、不漏发 | 待测 |
| — | 等级 0 | 工作日 18:30–19:30 一直不已读 | 待测 |

实测方式：真号由用户发；我用假网关脚本（`/mind/deliveries/due` 轮询 + 立即 ack）直接打本机 core，会话名 `sim:*`。**每段测试结束后备份并清空该会话的聊天与 mind 数据**（用户要求，避免她带着测试上下文）。

## 5. 已知缺口与遗留（非阻塞）

- `needs_attention` 投递项 core 每 60s 重新认领（日志噪音）。
- 网关长时间离线后过期 text 仍会晚发 → 计划 P4/P5 让 core 对过期投递重规划。
- **疲劳/深夜对情绪与泄露的影响归世界引擎**（2026-10-18 用户定）：时间线带来的状态变化由世界维护（W3 需求与节律），大脑只读世界给的状态；`state.compute_leak` 目前只看 arousal 与气质基线，P5 接入时从世界读疲劳。
- **fast 路径不写记忆**：闲聊不产生 episode，P4 每日反思会缺素材，P4 规划时需要定方案。
- `docs/mind-plan.md` 状态描述过期，需更新。
- 点开后等停顿再想、她在聊天里新消息秒看到、想到一半被打断合批重想：已实现（`bf23261`），P5 后等待秒数改由世界状态决定。
- **待规划：网关能力扩展计划**（2026-10-18 用户提出）：监听用户"正在输入"，她看到对方在打字就继续等；其他网关能力一并放进这个计划。

## 6. P4 规划要点（来自 mind-plan §4–§6，尚未写实施提示词）

- 触发：入睡事件；空闲时重要性累积过阈。
- L4 步骤：当天经历提炼大意并降细节权重；提取/修正 belief；在意事项关闭/衰减/升级；情绪收敛为次日初始状态；生成次日意图（主动联系单独开关、默认关）；自我模型微调（幅度有上限）。
- 遗忘：记忆强度随时间衰减、每次被回忆增强；低于阈值只留大意；原始聊天/世界账本永不删（"世界不忘，她会忘"）。
- 在意事项 `mind_concern`：强度、引用事件、衰减率；被 cues 触发重新升温；进 L1 输入（标题）和工作记忆（2–3 条）。
- 计费用途 `mind_reflect`，主模型约每天 1 次。
- 待决：fast 闲聊如何进入记忆（规则打分写轻量 episode？还是只在 L4 时从聊天记录批量提炼）；L4 失败/中断的断点续跑；P3 课表里的"睡觉"段如何作为入睡触发。
- 对应验收（mind-plan §11）：4 情绪余波、5 遗忘（翻聊天记录属于世界动作，P5）。

## 7. 世界引擎：已有什么

来源：`docs/world-engine-plan.md` §14–§15、`docs/world-backend.md`、`docs/world-cognition.md`。

**已实现（独立后端，默认 `WORLD_ENABLED=false`，未接聊天）**
- 版本化物品目录与导入器：3,484 个 BDDL 分类节点 + 53 个自有生活类型 = 3,537 条；259 条有已映射交互能力，其余 metadata_only。`content/world/`、`scripts/import_world_catalog.py`。
- 持久场景实例（街区模板按 seed 生成）、独立世界时钟与到期任务、NPC 显式日程、共享物品操作、带完成条件的持续事项（"修台灯"式协议）、可追溯随机机会（seed 派生、冷却、额度）、角色观察/认知记录、只读实体历史回放、ADMIN_TOKEN 管理 API。`app/services/world/`、`app/storage/world_store.py`、`app/routers/world.py`。
- 世界角色 LLM 决策：从受限动作选项组合计划，规则引擎结算；`WORLD_LLM_*` 独立配置（openai/gpt-6-luna，reasoning none）；默认每库每天 ≤48 次、间隔 ≥60s。
- 成本：单次世界决策约 $0.00062；48 次/天约 $0.9/月（只是外推）。
- 48 小时试运行：2026-09-26 起，独立库 `D:/Neno/data/world-trials/20260926-001605/world.db`，结果看 `final.json`（**尚未在本线程复盘**）。

**未实现**：聊天上下文注入、开放式 LLM 自主规划、二维可视化控制台、3D。

**核心原则（不能破坏）**：世界事实 / 角色认知 / 对外表达三者分离；意图≠已完成行动，规则结算后同事务提交事件+状态；幂等键、回放不重调模型不重抽签；角色只能看到认知范围内的投影；世界故障不吞聊天。

## 8. 世界模拟扩展规划（world-simulation-plan.md）

原则："世界没有剧情，只有节律、需求、扰动和后果。现实只读，世界可写。"

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| W1 身份种子 | 身份/节律/地点/关系/经济/自传记忆；锚定真实城市与时区；一致性检查 | **人设确定** |
| W2 环境适配器 | weather + calendar（过滤→翻译→标注→限流） | W1 |
| W3 需求与节律 | 饥饿/疲劳/社交能量/钱/ddl/想念累积，越阈触发 L2；与大脑共享 `world_need` | W1 |
| W4 NPC | 一律廉价 LLM 单次调用；核心/次要/背景分级与升级；双人对话单次调用；关系锁（用户与 Neno 关系为锁定事实，规则层过滤追求意图） | W3 |
| W5 扰动与调节器 | 小/中/大三档扰动、状态条件抽样、平静度调节频率 | W3 |
| W6 邮箱与冲浪 | Neno 自己的邮箱、trends、兴趣画像、search；**注入防护先行** | W2 |

新表：`world_reality_inbox`、`world_content_pool`、`world_interest_profile`、`world_npc_tier`、`world_need`、`world_regulator`。
模型变量：`WORLD_NPC_MODEL`、`WORLD_NPC_REFLECT_MODEL`、`WORLD_DIRECTOR_MODEL`、`WORLD_LLM_MAX_CALLS_PER_DAY`。
待定：Neno 具体人设与身份（W1 前）、与现实的对照方式、`WORLD_RIVAL_EVENTS`（默认关）。

## 9. P5：大脑 ↔ 世界接口

| 方向 | 内容 |
| --- | --- |
| 世界 → 大脑 | 观察/认知记录进 L0；需求越阈触发 L2；入睡事件触发 L4 |
| 大脑 → 世界 | L2 `intents` 走现有受限动作选择与规则结算（Neno 不再走 world-cognition 的通用角色决策） |
| 共享 | 需求状态只在世界维护一份 |
| 消息 | 用户消息作为"手机收到消息"的世界事件，经 L0 判断是否感知 |

P5 时：`schedule.segment_at` / `iter_segments` 的数据源从 `schedule.json` 换成世界状态（字段名已按世界结构起）；手机位置/静音来自世界实体；"翻聊天记录"成为消耗世界时间的世界动作；主动联系只产生发送意图，投递复用网关，独立开关默认关。

## 10. 建议的后续顺序

1. 完成 P3 真号实测（第 4 节）。
2. 复盘世界 48h 试运行，评估现有世界后端（BDDL 物品目录等）该保留多少；顺手更新 `docs/mind-plan.md` 过期状态。
3. **两条腿交替推进**（2026-10-18 用户拍板）：人设无关的世界框架（W3 需求与节律、W5 扰动，参数留空待人设填）与 P4 反思交替做，不再干等人设。
4. 人设确定后：W1 身份种子 → 填 W3/W5 参数 → P5 接入（用世界替换课表）→ W2/W4 → W6。
5. 可视化控制台随世界真实数据逐步接通（world-engine-plan §13.5）。

## 11. 踩过的坑（审查时重点看）

- `split_injected_context` 返回 `(注入块, 干净正文)`，曾在调用处写反。
- 专注窗口曾按"任意 session 的用户消息"判定且抽样不叠加 → 等级 4 形同虚设。
- SQLite `strftime('%s', ...)` 返回 TEXT，与 float 比较恒为 false，需要 `CAST(... AS REAL)`；`messages.created_at` 是 UTC `CURRENT_TIMESTAMP`。
- 异步模式下 assistant 消息在 ack sent 后才写入历史；claimed 视为已发出；每个 mark_read 的 trace_ids 只有一条。
- 新消息在 L0 抽样前已落库，任何"最近活动"判断要排除当前消息自身。
