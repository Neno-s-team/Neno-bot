# Neno 记忆系统（P2）

依据 [mind-plan.md](mind-plan.md) 第 5 节。P2 实现工作记忆、情景记忆（episode）、
语义记忆（belief）与检索；L4 反思与遗忘流程留到 P4。

## 存储

| 表 | 内容 |
| --- | --- |
| `mind_episode` | 她视角的经历：summary、参与者、标签、情绪、重要性、强度、回忆计数、引用 |
| `mind_belief` | 认识：subject + content + confidence + evidence（episode id） + version |
| `mind_episode_fts` | FTS5（`unicode61`，contentless），`rowid` = `mind_episode.id` |
| `mind_belief_fts` | FTS5（contentless），`rowid` = `mind_belief.id` |

写入、更新、删除都同步 FTS；contentless 表更新时必须先用 `'delete'` 命令带旧索引文本。

## 分词 `to_index_text`

- 中文按相邻两字切：`"吃饭了"` → `"吃饭 饭了"`；单字保留。
- 英文与数字按词转小写；结果用空格连接，写入 FTS。
- 查询走同一个函数，词之间 OR 连接，每个词加双引号。

## 检索打分 `retrieval.py`

线索 = `L1.cues` + 当前消息关键词。先 FTS 取前 30 个候选，再打分：

```text
score = w.rel·rel + w.rec·recency + w.imp·importance + w.emo·intensity + w.str·eff_strength
```

| 项 | 定义 |
| --- | --- |
| `rel` | bm25 在候选内做 min-max 归一化，再翻转（越相关越接近 1） |
| `recency` | `2^(-天数 / MIND_RECENCY_HALF_LIFE_DAYS)` |
| `intensity` | `min(1, |valence| + max(arousal, 0))` |
| `eff_strength` | `strength · 2^(-距上次回忆（或创建）天数 / MIND_MEMORY_HALF_LIFE_DAYS)` |

权重来自 `MIND_RETRIEVAL_WEIGHTS`，默认全 1。取前 `MIND_WM_EPISODES` 条。

**回忆增强**：只对真正放进工作记忆的经历执行
`strength = min(1, eff_strength + 0.1)`、`recall_count += 1`、`last_recalled_at = now`。
增强有三个前提：① 查询有 FTS 命中（退化排序的结果不增强）；② 该条经历真正入选工作记忆；
③ 非续跑回放（回放复用 `wm_json`，不重新检索、不再增强）。

查询词构造：`cues` 在前、当前消息在后，与索引同一分词函数，去重后最多 64 个词，
词间 OR 连接。无线索或 FTS 无命中时退化为按 `recency + importance` 排序。
认识（belief）按 FTS 命中 + confidence 排序，取前 `MIND_WM_BELIEFS` 条。

## 重要性 `importance.py`（纯规则）

基础 0.2；与用户有关 +0.2；第一次发生（同 session 下无相似经历）+0.2；
涉及承诺（答应/约好/保证/下次/明天/说好）+0.2；`|valence| + max(arousal, 0) ≥ 0.6` +0.2；
截到 `[0, 1]`。

## 工作记忆 `wm_builder.py`

固定槽位顺序：

1. 当前情境：本地时间（`MIND_TIMEZONE`）、星期、时段词、距上一条用户消息时间差；
   P2 默认打断等级 3，`world=unavailable`。
2. 在意事项：P2 留空（槽位保留）。
3. 相关经历 ≤ `MIND_WM_EPISODES`；每行带 `#<id>` 编号（如 `- #3 [10-17 01:14]…`），
   L2 的 `belief_updates.evidence` 只能引用这里出现过的 `#id` 数字。
4. 相关认识 ≤ `MIND_WM_BELIEFS`。
5. 最近原话 `MIND_RECENT_TURNS` 条。
6. 自我卡：不裁剪；`wm_json` 里保留该槽位，但给 L2 的渲染文本不包含它
   （自我卡已作为 system 第一块传入，避免重复）。

总字数超过 `MIND_WM_MAX_CHARS` 时按槽位优先级裁剪，不按时间截断：

```text
经历条数（最少 2）→ 认识条数（最少 2）→ 最近原话条数（最少 2）→ 逐条截短 summary
```

拼装结果是 `dict` + 渲染文本；`dict` 存进 `mind_cycle.wm_json`，续跑与回放直接复用。

## 记忆提交 `commit.py`（幂等与事务）

一个 conn 事务内完成：`memory_notes` → episode（+FTS）、`belief_updates` → belief（+FTS）、
`state_delta` → `mind_state`（带版本号 UPDATE）、最后 `UPDATE mind_cycle SET memory_committed=1`。

- 事务开头检查 `memory_committed`：已为 1 直接返回（提交成功后崩溃重跑不会重复写入）。
- 情绪写入复用 `state._compute_next` 的衰减 + delta + clamp，版本冲突回滚整个事务
  （episode/belief 一并回滚）并最多重试 2 次；重试耗尽打 `memory_commit_conflict` 日志。
- 认识首插 `confidence = min(模型值, MIND_BELIEF_INIT_MAX)`（默认 0.6）；更新时单次变化 ≤ ±0.3。
