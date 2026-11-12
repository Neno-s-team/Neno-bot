# 安卓端

手机里跑完整的后端：Chaquopy 把 Python 和仓库根目录的 `app/` 原样打进 App，前台服务里起 uvicorn（只监听 `127.0.0.1`），
界面是又一个"通道"，和 `channels/terminal.py` 一样走本机 HTTP。规划见 `docs/mobile-plan.md`，验证记录见 `docs/mobile-m0.md`。

现在是 **M1**：给用户用的界面。外观的约定在仓库根目录的 `DESIGN.md`（照苹果信息做聊天页、照 Telegram 做设置页）。

## 界面在哪

都在 `app/src/main/java/ai/meanwhile/app/ui/`，Jetpack Compose：

| 文件 | 是什么 |
|---|---|
| `Theme.kt` | 颜色（浅色 / 深色）、字号、间距。改外观先看这里 |
| `Glyph.kt` | 全部图标（细线，一个画法） |
| `Parts.kt` | 公共部件：圆头像（TA 家此刻的画面）、浮着的圆钮、底部主按钮、称呼"她 / 他" |
| `Choose.kt` | 第一次打开①：选她或他、住哪座城市 |
| `Onboarding.kt` | 第一次打开②：填模型密钥（设置里换密钥也是这一页） |
| `Loading.kt` | 第一次打开③：搭世界的加载动画 |
| `ChatScreen.kt` `ChatParts.kt` `ChatModel.kt` | 聊天页：顶部头像和名字胶囊、气泡、输入栏 |
| `SettingsScreen.kt` | 设置页，从聊天页点名字胶囊进 |
| `Creator.kt` | 造物者模式：她起过的念头、一条消息背后她想的 |

- 第一次打开的顺序：系统启动画面 → 选她或他和城市 → 填密钥 → 搭世界 → 聊天。走到哪一步记在 App 的设置里（`persona`、密钥、`world_ready`）。
- 界面上的城市名、天气、她在干嘛都来自后端（`GET /mind/now`），不写死。能选哪些城市、哪些角色问 `mobile_main.py` 的 `cities()` / `personas()`。
- 没有"她的一天"页。要她一天的素材，电脑上跑 `python scripts/export_day.py`。
- 头像图在 `app/src/main/res/drawable-nodpi/`，现在每人只有"下雨的早晨"一张；按时段和天气换图的入口是 `Parts.kt` 的 `homePicture()`。

## 改完界面怎么看

用模拟器或真机截图，浅色、深色、字号 1.3 倍都看一遍：

```bash
adb exec-out screencap -p > shot.png
adb shell cmd uimode night yes            # 深色；看完改回 no
adb shell settings put system font_scale 1.3   # 大字；看完改回 1.0
```

## 构建

需要：JDK 17 以上；Python 3.12（和 App 里的版本同一个小版本，Windows 上用 `py -3.12` 找）；安卓 SDK（平台 35、构建工具 35.0.0）。

```bash
cd android
# 告诉 Gradle SDK 在哪（装了 Android Studio 的话它会自己写这个文件）
echo "sdk.dir=/path/to/android-sdk" > local.properties
./gradlew assembleDebug
```

产物在 `app/build/outputs/apk/debug/app-debug.apk`。

- 后端代码取自上一级目录（也就是这个仓库）。如果工作区里有没提交的改动不想带进去，
  加 `-PbackendRoot=<一份干净的导出目录>`（`python scripts/export_release.py <目录>` 能导出一份）。
- 第一次构建要下载 Gradle、安卓构建插件、Python 运行时和依赖包，比较久。

## 装到机器上看

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb logcat -s Meanwhile        # 每次启动会打一行 probe：解释器、原生依赖、SQLite 全文检索；后端起不来的报错也在这
```

## 依赖里麻烦的两个

`pydantic-core` 是 Rust 写的，Chaquopy 自己的包库里没有。现在用的是 Flet 包库（`https://pypi.flet.dev`）里同样标签的安卓版：
`pydantic-core 2.46.4`，所以 `pydantic` 固定在和它配套的 `2.13.4`。升级 pydantic 要两个一起看。

`Pillow` 固定在 `11.0.0`：加了 Flet 包库之后，pip 会从那边拿更高版本的 Pillow，而它依赖的 `libtiff.so` 等放在 Chaquopy 找不到的地方，
App 一启动就崩。`11.0.0` 只有 Chaquopy 自己的包库有。**Flet 包库里带额外共享库的包都不能用**，加新的原生依赖前先确认这一点。
