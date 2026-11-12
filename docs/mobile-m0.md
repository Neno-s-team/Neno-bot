# 移动端 M0：验证记录

主线任务第 2 步（`docs/mobile-plan.md` 2.3）。要回答的问题：**手机里能不能跑完整的后端。** 过不去就退到"手机连电脑"。

工程在 `android/`，怎么构建见 `android/README.md`。

## 要验的 5 条

| # | 要验的 | 结论 | 依据 |
| --- | --- | --- | --- |
| 1 | pydantic-core 在 Chaquopy 上有没有现成的包 | **官方包库没有，Flet 的包库有，已打进安装包**；装到机器上能不能跑还没验 | 见第 1 节 |
| 2 | 打包进去的 SQLite 带不带 FTS5 | 待测（要装到机器上） | App 里的"诊断"按钮（`mobile_main.probe`） |
| 3 | 前台服务常驻一天，耗多少电、占多少内存 | 待测（要真机放一天） | |
| 4 | 被杀掉再打开，补算对不对，主动消息会不会丢 | 待测（要装到机器上） | |
| 5 | 不用 `uvicorn[standard]` 的原生加速包，纯 Python 的 uvicorn 能不能跑 | **电脑上模拟通过**；手机上待测 | 见第 2 节 |

**卡在哪**：2、3、4 和 1、5 的后半截都要一台安卓机器。这台电脑没连手机；模拟器要占 3GB 左右内存，
构建完时空闲内存只有 2GB，没有擅自开。等用户接一台开了 USB 调试的手机，或者同意开模拟器。

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

## 构建环境（2026-11-12 这台机器上）

JDK 21、Python 3.12.10 是现成的。安卓 SDK（平台 35、构建工具 35.0.0）和 Gradle 8.11.1 装在项目里的 `data/tools/` 下，不入库、不动系统。
版本组合：安卓构建插件 8.7.3、Kotlin 2.0.21、Chaquopy 17.2.0、App 里的 Python 3.12。
