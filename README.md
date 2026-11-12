# Neno

**一个有自己日子的 AI 伴侣。** 她不是一问一答的聊天机器人：她住在一个按真实时间运转的世界里，在杭州读研，
有课、有 ddl、会饿会困、会刷手机。你发消息，她不一定马上看到；你不找她，她也可能自己来找你。

An AI companion who lives her own day in a simulated world that runs on real time. Docs are in Chinese.

## 她和普通聊天机器人哪里不一样

- **有自己的一天。** 世界引擎一直在走：起床、上课、去图书馆、吃饭、赶论文、睡觉。没有预设剧情，每天都是随机长出来的。
- **不是秒回。** 她什么时候看到消息，取决于她在干嘛：上课、开组会、睡着了就是看不到，下了课才回。
- **像人一样发消息。** 会显示"正在输入"，一句话拆成几条发，有时犹豫一下；你中途又发一条，她会重新想。
- **会主动找你。** 遇到想说的事、惦记着你的事、好久没聊，她会自己发消息；你没回，她大多会追一句，再不回就算了。
- **会记事，也会忘。** 睡前回想当天，重要的记住，不重要的慢慢淡掉；在意的事会一直惦记。
- **世界跟现实同步。** 天气是真的，电影院在映的片子、城里的展览是真的，她会去看。
- **自己上网。** 刷手机的时候真的在看网页，看到有意思的会跟你说。
- **关系会变。** 亲近、心动、隔阂只由你们之间的相处推动，变得很慢，方向不预设。

## 五分钟跑起来

需要：Python 3.10 以上；一个大模型的密钥——国内推荐 [DeepSeek](https://platform.deepseek.com/api_keys)
（直连，支付宝/微信充值，充 10 块钱能用很久），海外可以用 [OpenRouter](https://openrouter.ai/keys)。

```bash
git clone https://github.com/Neno-s-team/Neno.git
cd Neno
```

- **Windows**：双击 `start.bat`（或在终端里运行 `.\start.bat`）
- **macOS / Linux**：`./start.sh`

第一次运行会自动建环境、装依赖，问你用哪家的模型、要一次密钥并当场测一下，然后你就在这个终端里跟她聊天了。
她的数据都在本机的 `data/` 目录里。想从头再来，停掉后删掉 `data/`。

> 她回得有快有慢是正常的：消息要等她看手机时才会被看到，她在路上可能几分钟，在上课、睡觉就更久。
> 终端里发完消息会提示她在干嘛、大概多久后会看到。

## 接到 Telegram（推荐，两分钟）

终端里聊只是为了先试试。接到 Telegram 之后，她才能真正"主动找你"——消息会推到你手机上。

1. 在 Telegram 里找 `@BotFather`，发 `/newbot`，按提示起个名字，拿到一串 token。
2. 打开 `.env`，把 token 填到 `TELEGRAM_BOT_TOKEN=` 后面。在国内还要填 `TELEGRAM_BOT_PROXY`。
3. 重新运行 `start.bat` / `./start.sh`。终端里会显示一个 6 位配对码，在 Telegram 里发给你的机器人。

绑定之后它只理你一个人。终端和 Telegram 用的是同一份记忆，换过去她还记得你。

## 要花多少钱

所有模型调用都走你自己的密钥，不经过任何别人的服务器。`.env` 里一个 `MODEL_PRESET` 决定用哪一档：

| 档位 | 给谁 | 深思 | 措辞 | 要几个密钥 |
| --- | --- | --- | --- | --- |
| `cn`（默认） | 国内用户 | DeepSeek V4 Pro | DeepSeek V4 Pro | 1 个 DeepSeek |
| `cn-gemini` | 能连上 OpenRouter 的 | DeepSeek V4 Pro | Gemini 3.8 Flash | DeepSeek + OpenRouter |
| `intl` | 海外用户 | DeepSeek V4 Pro | Gemini 3.8 Flash | 1 个 OpenRouter |
| `high` | 不在乎钱 | Claude Opus 5.5 | Gemini 4 Carbon | 1 个 OpenRouter |

其余的（第一反应、世界里的小决定、身边的人、上网、睡前回想、看图）各档都用最便宜的 DeepSeek V4.1 Flash。

- **大概花费**（估算，以各家价格页为准）：默认档一条认真想过的回复几分钱人民币，随口一回不到一分钱。
  DeepSeek 工作日高峰时段价格翻倍。她的世界自己运转也会调模型，但都是最便宜的那个，而且每天有上限。
- **想细调**：某一层单独换模型，写成 `提供方:模型名`（`deepseek:` / `openrouter:` / `custom:` 任何 OpenAI 兼容的地址），`.env` 里有例子。

## 把她改成你想要的样子

| 想改什么 | 改哪里 |
| --- | --- |
| 她是谁、什么性格、和你什么关系 | `prompts/mind/self_card.md` |
| 她说话的口气 | `prompts/mind/style_card.md` |
| 让她说话像你熟悉的某个人 | `scripts/corpus_wechat.py` 把你自己的聊天记录做成风格语料（只留在本机） |
| 她一天怎么过、多久刷一次手机 | `content/world/rhythms/life.json` |
| 她会遇到哪些小事 | `content/world/moments/life/` |
| 她会不会主动找你 | `.env` 里的 `MIND_PROACTIVE`（`on` / `shadow` / `off`） |

## 常见问题

- **她怎么不回我？** 多半是在上课、开会或者睡觉。后端日志在 `logs/backend.log`。
- **想看她本来想跟我说什么？** `python -m app.services.mind.proactive` 会列出她起过的念头和结局。
- **能换城市吗？** 现在的世界是按杭州做的（真实地图、天气、电影和展览）。换城市还没有一键的办法。
- **聊天内容安全吗？** 数据都在你自己的电脑上；每次对话的内容会发给你选的模型提供方。

## 进阶

- [用真人号接 Telegram](docs/telegram-user-account.md)：有"已读"、表情包、在线状态，更像真人，但要编译，比较麻烦。
- [她是怎么想的](docs/how-it-works.md)：多层大脑和世界引擎的结构，想改代码先看这个。

## 现在的状态

能用，但还在早期。已经有的：多层大脑、世界引擎、现实同步、上网、主动消息、终端和 Telegram 两种通道。
打算做的：一个能看到"她的一天"的网页、更多聊天平台、发图片。

## 许可和数据来源

代码以 [AGPL-3.0](LICENSE) 开源。城市数据来自 © OpenStreetMap 贡献者（ODbL）；
物品目录来自 Stanford 的 BDDL（MIT 许可，见 `content/world/BDDL-LICENSE.txt`）。
