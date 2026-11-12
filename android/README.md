# 安卓端

手机里跑完整的后端：Chaquopy 把 Python 和仓库根目录的 `app/` 原样打进 App，前台服务里起 uvicorn（只监听 `127.0.0.1`），
界面是又一个"通道"，和 `channels/terminal.py` 一样走本机 HTTP。规划见 `docs/mobile-plan.md`，验证记录见 `docs/mobile-m0.md`。

现在是 **M0**：一个只为验证用的界面（发一条、看她回、看诊断），不是给用户的界面。

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

## 依赖里唯一麻烦的

`pydantic-core` 是 Rust 写的，Chaquopy 自己的包库里没有。现在用的是 Flet 包库（`https://pypi.flet.dev`）里同样标签的安卓版：
`pydantic-core 2.46.4`，所以 `pydantic` 固定在和它配套的 `2.13.4`。升级 pydantic 要两个一起看。
