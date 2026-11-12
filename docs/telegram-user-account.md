# 用真人号接 Telegram（进阶）

机器人通道（`TELEGRAM_BOT_TOKEN`）两分钟就能用，但它是个"机器人"：没有已读、没有在线状态、发不了表情包。
想让她在 Telegram 里看起来就是一个普通联系人，可以给她一个**真人号**，通过 TDLib 登录。代价是要编译，而且需要一个单独的手机号注册账号。

目前只在 Windows 上做过。需要：PowerShell 7、Visual Studio Build Tools（C++）、CMake。

## 组成

- `telegram_sidecar/`（C++）：加载固定版本的 `tdjson.dll`，通过标准输入输出转发 TDLib 的 JSON。不含业务逻辑，不接收、不打印凭据。
- `telegram_gateway/`（Python）：登录状态、收消息、白名单、媒体下载、表情包、按后端排好的时间发消息。
- TDLib 的数据库和文件放在 `data/telegram/tdlib/`。

## 步骤

```powershell
# 1. 编译（各要一段时间）
pwsh -NoProfile -File scripts\build-telegram-sidecar.ps1
pwsh -NoProfile -File scripts\build-tdlib.ps1
```

2. 去 <https://my.telegram.org> 用她的号登录，建一个应用，拿到 `api_id` 和 `api_hash`。
3. 在 `.env` 里填：

| 变量 | 说明 |
| --- | --- |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | 上一步拿到的 |
| `TELEGRAM_DATABASE_ENCRYPTION_KEY` | 随便一串长一点的随机字符，用来加密本地数据库 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 允许跟她聊天的 chat id（你自己的）。留空则谁都不理 |
| `TDLIB_JSON_LIBRARY` / `TDLIB_SIDECAR_BINARY` | 编译出来的 `tdjson.dll` 和 sidecar 的路径 |
| `NENO_MIND_ASYNC=true` | 让网关按后端排好的时间一条条发 |
| `MIND_HOME_SESSION=tg:<你的 chat id>` | 告诉后端"家"会话是哪个（世界、反思、主动消息都认它） |

同时把 `TELEGRAM_BOT_TOKEN` 留空——两种 Telegram 通道不要同时开。

```powershell
# 4. 第一次要交互式登录（输入手机号和验证码）
python -m telegram_gateway --login

# 5. 启动：后端 + 网关，无窗口，日志在 logs/
pwsh -NoProfile -File scripts\start-neno-stack.ps1
```

## 投递是怎么保证不乱的

- 收到的消息先落到 `data/operations.db`，再交给后端；后端排好"几点发哪一句"，网关到点执行。
- 每一句发出去都有本地账本（按投递项的 id 去重）：确认发出的不重发；明确失败的退避重试；状态不明时先查 Telegram，不盲目重发。
- 只有确认发出去的话才会进她的聊天记录。
- 你在她还没发出口时又来了消息，没发的那几句会作废，她重新想。

更细的说明在 `telegram_sidecar/README.md`。
