// 安卓端：手机里跑完整的后端（Chaquopy 把 Python 打进 App），界面是又一个"通道"。规划见 docs/mobile-plan.md。
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
    id("com.chaquo.python") version "17.2.0" apply false
}
