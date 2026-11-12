# 移动端 M0：验证记录

主线任务第 2 步（`docs/mobile-plan.md` 2.3）。要回答的问题：**手机里能不能跑完整的后端。** 过不去就退到"手机连电脑"。

工程在 `android/`，怎么构建见 `android/README.md`。

## 要验的 5 条

| # | 要验的 | 结论 | 依据 |
| --- | --- | --- | --- |
| 1 | pydantic-core 在 Chaquopy 上有没有现成的包 | **过了**：官方包库没有，用 Flet 包库里的安卓版，装到机器上能加载、FastAPI 正常收发 | 第 1、3 节 |
| 2 | 打包进去的 SQLite 带不带 FTS5 | **过了**：SQLite 3.53.4，带 FTS5 | 第 3 节 |
| 3 | 前台服务常驻一天，耗多少电、占多少内存 | **内存过了**（92–117MB）；**耗电没测**，要真机放一天 | 第 3 节 |
| 4 | 被杀掉再打开，补算对不对，主动消息会不会丢 | **过了**：被杀前发的消息重开后照样回；进程被杀系统 1 秒后自己拉起服务；世界时间补到现在。主动消息没专门触发 | 第 3 节 |
| 5 | 不用 `uvicorn[standard]` 的原生加速包，纯 Python 的 uvicorn 能不能跑 | **过了** | 第 2、3 节 |

**结论：走 A（手机里跑完整后端）可行，不用退到"手机连电脑"。** 只剩耗电要在真机上放一天看；国产系统杀后台的情况也要真机看（M2 的电池白名单引导）。

## 1. pydantic-core

- Chaquopy 自己的包库（`chaquo.com/pypi-13.1`，共 133 个包）里**没有** pydantic-core；PyPI 上的 2.50.4 也没有安卓版。
  （同是 Rust 写的 `cryptography`、`tokenizers` 在 Chaquopy 包库里有，说明这条路技术上走得通，只是没人给 pydantic-core 做。）
- **Flet 的包库（`pypi.flet.dev`）里有**：`pydantic_core-2.46.4-1-cp312-cp312-android_24_arm64_v8a.whl`，x86_64 和 cp313、cp314 的也有，
  平台标签和 Chaquopy 用的是同一种写法。配套的 pydantic 是 2.13.4（FastAPI 要求 pydantic ≥ 2.9，够）。
- 所以构建里加了 `--extra-index-url https://pypi.flet.dev`，固定 `pydantic==2.13.4`、`pydantic-core==2.46.4`。
- 查了打进安装包的 `_pydantic_core.cpython-312.so`（arm64 和 x86_64）：只依赖 `libpython3.12.so`、`libdl.so`、`libc.so`，
  和 Chaquopy 自己编的 Pillow 依赖的是同一个 `libpython3.12.so`，没有别的特殊依赖。合得上的可能性很大。
- 风险：这个包是别人按他们自己的 Python 构建编的，跟 Chaquopy 的解释器合不合，最终要装上跑了才知道。
  合不上的退路：自己交叉编译（Rust + maturin + NDK），或者手机上不走 FastAPI，界面直接调 Python 函数
  （我们自己的代码只有 5 个文件用到 pydantic）。

## 2. 构建和电脑上的模拟（2026-11-12）

- **安装包构建成功**：`./gradlew assembleDebug` 4 分钟，`app-debug.apk` 61MB（debug 版、带 arm64 和 x86_64 两种架构；
  正式发只带 arm64 会小一半左右）。里面：Python 3.12 解释器、SQLite、我们的后端 0.9MB、第三方依赖约 12MB。
  两种架构下都有 `pydantic_core/_pydantic_core.cpython-312.so`。
- **按手机的启动方式在电脑上跑通**（`data/tmp/mobile_sim.py`）：用 `mobile_main.py` + 构建时拷出来的那份后端，
  在一个线程里用纯 Python 的 uvicorn（`asyncio` + `h11`）起后端，1.6–3.2 秒起来；发一条消息被受理、能查到她此刻在干嘛、能停。
  数据（`bot.db`、`operations.db`）都落在可写的工作目录里。
- **模拟时发现并改掉的**：用量和收发账本（`operations.db`）默认放在代码旁边，手机上那个目录升级时会被系统换掉。
  `mobile_main.prepare` 里用 `NENO_OPERATIONS_DB` 指到数据目录。
- 世界的内容文件是按代码位置找的（`Path(__file__).parents[3] / "content"`），大脑的提示词是按工作目录找的（`prompts/mind/…`）。
  Chaquopy 会把随包的数据文件解到代码旁边，所以前者不用管；后者靠 `mobile_main.prepare` 每次启动把 `prompts/`、`content/` 同步到工作目录。

## 3. 装到安卓上跑（2026-11-12，无窗口的虚拟机：安卓 14、x86_64、限 2GB 内存）

真机没接，用的是 SDK 自带的虚拟机（`data/tmp/m0_boot.sh` 起、`m0_stop.sh` 关并删数据）。设置（档位、密钥）用 `run-as` 预先写进 App 的配置文件，
后端的 8765 端口用 `adb forward` 转到电脑上，从电脑上发消息、看回复（`data/tmp/m0_chat.py`）。

- **启动诊断**（App 每次启动写进日志，`adb logcat -s Meanwhile`）：
  `python 3.12.15 / sqlite 3.53.4 / fts5 true / pydantic 2.13.4 / pydantic_core 2.46.4 / fastapi 0.162.5 / uvicorn 0.54.0 / tzdata / Asia/Shanghai`。
- **第一次没起来，原因是 Pillow**：`--extra-index-url` 让 pip 从 Flet 包库拿了更高版本的 Pillow（12.3.0），它依赖的 `libtiff.so` 放在
  Chaquopy 找不到的地方，一导入就 `dlopen failed: library "libtiff.so" not found`。固定成 `Pillow==11.0.0` 后好了——这个版本只有 Chaquopy 自己的包库有。
  教训：Flet 包库里**带额外共享库的包不能用**，只能用它那些不依赖别的库的（pydantic-core 就是）。以后加原生依赖先看这一条。
- **发一条 → 她回**：发"在干嘛"，她当时在卧室、197 秒后看手机 → 正在输入 → "在写论文" / "怎么了"。模型调用从虚拟机里出去正常。
- **内存**：带界面聊过之后 PSS 117MB（RSS 223MB，Java 堆 7MB）；只有服务在跑时 PSS 92MB。数据目录 3.4MB，解出来的 Python 代码 25MB。
- **强杀再开**：发"你吃饭了吗"后立刻 `am force-stop`，停了 8 分钟。重开后后端 5 秒内起来，她 3 秒后"看到"那条消息，半分钟后回了
  "还没呢" / "刚在弄论文，等会儿点外卖，你吃了没"。世界时间补到了现在。
- **进程被杀**（`kill -9`，模拟系统回收）：系统 1 秒后自己把 `BackendService` 拉起来（`START_STICKY`），6 秒内后端恢复，不用打开界面。
- **测试时碰到的一个现象**：App 一打开就弹"允许通知"的系统对话框；没人点的话，强杀后再启动会被系统当成"已经在最前面"而不重启进程。
  真用户会点掉，不算缺陷，但 M1 应该换个时机再要通知权限（比如她第一次发来消息之前）。
- **没验的**：耗电（虚拟机上没有意义）；国产系统的后台限制；主动消息在手机上真的发出来（逻辑和电脑版是同一份，没专门等它触发）。

## 构建环境（2026-11-12 这台机器上）

JDK 21、Python 3.12.10 是现成的。安卓 SDK（平台 35、构建工具 35.0.0）和 Gradle 8.11.1 装在项目里的 `data/tools/` 下，不入库、不动系统。
版本组合：安卓构建插件 8.7.3、Kotlin 2.0.21、Chaquopy 17.2.0、App 里的 Python 3.12。
虚拟机和它的系统镜像也在 `data/tools/android-sdk/` 下（约 9GB），做 M1 界面时还要用；不用了可以整个删掉。
