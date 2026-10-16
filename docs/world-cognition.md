# 独立世界的 LLM 决策

实施日期：2026-09-25。聊天、Telegram、现有聊天人设都没有接入这条链路。

## 分工

- 世界时钟、物品数量、行动条件、行动耗时和实际后果仍由原来的规则引擎负责。
- 每个启用的角色都可以独立决策；Neno 暂停并不停止其他角色的决策。
- 模型生成动机、目标、短计划和世界机会提案，不直接写数据库状态，也不宣告行动成功。
- 规则不是用来替代创造力，而是保证同一个世界前后相容。
- 原有商店营业时间和 NPC 日程仍存在，可作为背景规律；模型不是另起一套相互冲突的物理状态。

## 角色如何行动

1. 空闲角色到达决策时间后，记录一次符合可见性规则的观察。
2. 输入只有该角色的观察、已知相邻地点、自己的目标、最近行动结果与尚未处理的建议。不是整个世界，也不读聊天记录。
3. 后端根据已知物体的能力生成动作选项。当前可执行性通过事务内试算检查，然后完整回滚；不会真正消耗资源或暴露未观察到的物品。
4. 模型选择 action_key 和最多四个 follow_up_keys。第一次动作只允许选择当前可执行项；未来动作还要在执行时重新检查条件。
5. 接受的动作通过原有执行器依次落实。失败或被中断时停止后续计划，把结果留给下一次决策。
6. 目标有数据库分配的固定 ID 和可检查的完成条件。模型只能继续已有编号，不能自己编 UUID。临时活动可以不建立长期目标，但短计划与执行位置仍持久保存。

调用采用 JSON Schema 约束输出；动作候选和可用目标 ID 都进入本次请求的约束。OpenRouter 路由要求支持这些参数，不静默忽略，不自动换成主聊天模型。

官方接口说明：https://openrouter.ai/docs/guides/features/structured-outputs

并非无限动作生成器：当前仍是已有动作能力的组合。当前候选最多 64 个，分堆/购买提供单件选项、倒水提供一个有界量、烹饪提供一组已知食材。自由配方、任意数量与新的互动能力需要继续扩充动作层，而不是让模型随口声称已经实现。

## 世界机会与随机性

- 世界提案器默认每 7200 个模拟秒遇到一次机会，以 0.25 的概率考虑调用。
- 出现与否先由带 seed 的抽样决定；模型负责机会的具体内容，不用 temperature 代替事件发生率。
- 当前支持：可被忽略的角色活动建议，以及已有天气场景中的天气变化。不是凭空造物或强行编出已经发生的社交事件。
- 活动建议保存在目标角色的建议列表，标记 unverified_idea，不写入事实观察；下一次决策可以忽略。已考虑或超过一天的建议不再反复塞进输入。
- 和原有随机事件共享每日总数、区域额度和分类冷却；weather_weight=0 会禁止提案器改变天气。
- director_probability 控制模型提案机会，原 random.probability 仍控制原有规则事件，二者不是同一个参数。要关闭所有随机发展，把每日事件额度设为 0，或分别关闭两类来源。
- 保存抽样、当次输入、模型响应、接受/拒绝原因以及行动 ID。LLM 输出不保证同 seed 再请求得到相同结果，重放应使用已经保存的输出和事件，不重新调用。

## 模型配置

配置按用途命名，不按供应商命名。已在 .env 和 .env.example 增加：

    WORLD_LLM_ENABLED=true
    WORLD_LLM_MODEL=openai/gpt-6-luna
    WORLD_LLM_EXTRA_BODY={"reasoning":{"effort":"none"}}
    WORLD_LLM_URL=
    WORLD_LLM_API_KEY=
    WORLD_LLM_MAX_CALLS_PER_DAY=48
    WORLD_LLM_MIN_CALL_INTERVAL_SECONDS=60
    WORLD_LLM_TIMEOUT_SECONDS=45
    WORLD_LLM_MAX_OUTPUT_TOKENS=1200
    WORLD_LLM_MAX_CONTEXT_CHARS=14000

空 URL/key 复用 OPENROUTER_URL / OPENROUTER_API_KEY。模型名独立，不跟随主聊天或图片归档模型后续变化。接口需要支持 Chat Completions 的 response_format.json_schema；没有静默降级到自由文本。

- 默认 48 次/真实 UTC 日，统计同一个世界数据库内所有世界的请求预留，包括失败和中断。不同数据库各自计数，不是整个账号的全局账单限制。
- 两次请求至少间隔 60 个真实秒。加速模拟时间不会按比例提高付费请求速率；不会为离线期间每个过期时间点补调模型。
- 已接受计划的普通执行步骤不占新增模型调用。
- 角色默认每 900 个模拟秒可重新决策；行动未结束或角色暂停时不调用。明确等待可以更久。
- 连续三次失败暂停新的请求 15 分钟；不立即反复修复请求。中断请求保留为 uncertain/interrupted，不自动重发。
- 14000 字符是 JSON 世界输入上限，另外还有固定技术指令和输出 Schema；不是全部请求 token 上限。48 次是请求数量限制，不等同于固定货币预算。
- 网络请求期间不持有 SQLite 写锁。世界时钟和规则运行器独立，不等待模型响应；模型故障不应停止基础世界。

## 启动和启用

仍使用独立启动入口：

    .\venv\Scripts\python.exe -m app.services.world --db data/world-dev.db --port 8011

不会自动创建世界。创建时可以 running=false，检查好场景后再恢复时钟。所有接口使用 x-admin-token。

对一个已创建的世界发送 POST /world/worlds/dev/cognition：

    {"request_id":"enable-cognition-1","settings":{"enabled":true,"actor_interval_seconds":900,"director_enabled":true,"director_interval_seconds":7200,"director_probability":0.25,"actor_profiles":{}}}

actor_profiles 可以按角色 ID 填入简短兴趣与近期动机，空字典表示没有额外设定，不表示只有 Neno。全体有效角色都有独立决策资格。

再向 POST /world/worlds/dev/control 发送：

    {"request_id":"run-world-1","operation":"resume"}

停止付费决策：把该世界 cognition.settings.enabled 改为 false，或暂停整个世界。关闭决策不回滚已经提交的行动。更改 .env 需要重启独立世界进程；不要为了此功能重启聊天进程。

原聊天服务的 WORLD_ENABLED 仍默认 false。写入 WORLD_LLM_ENABLED=true 并不让聊天进程开始跑世界；每个世界的 cognition.enabled 也默认 false。

## 诊断接口与可视化接点

- GET /world/status：模型配置状态、今日次数、限流原因、最近决策与时钟状态，不返回 key。
- GET /world/worlds/{wid}/cognition：角色计划、当前动作、下次决策时间和未验证建议。
- GET /world/worlds/{wid}/decisions?limit=20：调用记录与接受/拒绝原因。
- 加 include_context=true 查看对应的有界输入与原始响应；这是管理接口，不是角色可访问的全知接口。
- GET /world/worlds/{wid}/events 和 actions/threads records：核对行动的真实结果，而不是把“计划已接受”当成“事情已完成”。

这些接口供后续 2D 场景与时间线前端使用。本次没有新增可视化页面。

## 已验证和边界

    .\venv\Scripts\python.exe scripts/verify_world_cognition.py
    .\venv\Scripts\python.exe scripts/verify_world_backend.py

上面只用临时数据库与确定性模型替身，不调用外部模型。

    .\venv\Scripts\python.exe scripts/smoke_world_cognition.py --live

显式允许一次真实请求，在临时世界执行接受的短计划；独立审计文件写入 data/world-cognition-smoke/，不碰聊天数据库或 TG。

本次真实试运行先发现了自由生成目标编号与错误移动目标的问题；改用动作候选和约束输出后，完成检查桌子、拿书、阅读、归位四步，read_count 从 0 变为 1。共用了三次短的真实请求，后两者是修复验证，不是后台自动重试。

这是决策与执行链路的验证，不代表已经验证了多日生活质量、丰富社会关系或开放世界叙事。当前世界也没有自动启动常驻运行，避免无意持续调用。
