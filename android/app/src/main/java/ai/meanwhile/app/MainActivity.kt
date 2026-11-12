package ai.meanwhile.app

import ai.meanwhile.app.ui.ChatModel
import ai.meanwhile.app.ui.ChatScreen
import ai.meanwhile.app.ui.ChooseScreen
import ai.meanwhile.app.ui.InnerSheet
import ai.meanwhile.app.ui.LoadingScreen
import ai.meanwhile.app.ui.MeanwhileTheme
import ai.meanwhile.app.ui.Onboarding
import ai.meanwhile.app.ui.SettingsScreen
import ai.meanwhile.app.ui.Ta
import ai.meanwhile.app.ui.ThoughtsScreen
import ai.meanwhile.app.ui.reduceMotion
import ai.meanwhile.app.ui.restartApp
import android.Manifest
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.graphics.Color
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import kotlinx.coroutines.delay

/** 界面入口。界面只是又一个"通道"：打开就是和 TA 的对话；设置从名字胶囊进去，造物者模式藏在设置里。 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 内容铺到屏幕最底下，系统导航条透明地盖在上面：不要它自己那条半透明的色带（跟页面底色对不上）
        enableEdgeToEdge(navigationBarStyle = SystemBarStyle.auto(Color.TRANSPARENT, Color.TRANSPARENT))
        if (Build.VERSION.SDK_INT >= 29) window.isNavigationBarContrastEnforced = false
        setContent { MeanwhileTheme { App(onLeave = { moveTaskToBack(true) }) } }
    }

    override fun onResume() {
        super.onResume()
        Hub.chatOpen = true
        Notifier.clear(this)
    }

    override fun onPause() {
        Hub.chatOpen = false
        super.onPause()
    }
}

/** 第一次打开走到哪了：选她或他和城市 → 填密钥 → 搭世界的加载动画 → 聊天。 */
private fun stage(context: Context): String = when {
    !BackendService.chosen(context) -> "choose"
    !BackendService.configured(context) -> "key"
    !BackendService.worldReady(context) -> "loading"
    else -> "chat"
}

@Composable
private fun App(onLeave: () -> Unit) {
    val context = LocalContext.current
    var at by rememberSaveable { mutableStateOf(stage(context)) }
    BackHandler(enabled = at == "key") { at = "choose" }        // 填密钥时按返回：回去重选
    when (at) {
        "choose" -> ChooseScreen(onDone = { at = stage(context) })
        "key" -> Onboarding(onDone = { at = stage(context) })
        "loading" -> LoadingScreen(onDone = { at = "chat" })
        else -> Main(onLeave)
    }
}

private val DEPTH = mapOf("" to 0, "settings" to 1, "thoughts" to 2, "key" to 2)

@Composable
private fun Main(onLeave: () -> Unit) {
    val context = LocalContext.current
    LaunchedEffect(Unit) { BackendService.start(context) }
    val scope = rememberCoroutineScope()
    val backend = remember { Backend(context) }
    val chat = remember { ChatModel(backend, scope) }
    val ta = remember { Ta.saved(context) }
    val still = reduceMotion()
    var creator by rememberSaveable { mutableStateOf(BackendService.prefs(context).getBoolean("creator", false)) }
    chat.creator = creator
    val up by Hub.up.collectAsState()
    val arrivals by Hub.arrivals.collectAsState()
    LaunchedEffect(up, arrivals, creator) { if (up) chat.refresh() }
    LaunchedEffect(up) {                                        // TA 在干嘛、外面什么天：开着的时候每分钟看一眼
        while (up) {
            delay(60_000)
            chat.refresh()
        }
    }

    var page by rememberSaveable { mutableStateOf("") }                  // "" 聊天 / settings / thoughts / key
    var inner by rememberSaveable { mutableStateOf<String?>(null) }      // 正在看哪一轮 TA 想的
    // 你发出第一句话之后再要通知权限，不在一打开就弹
    val askNotify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    LaunchedEffect(chat.messages.size) {
        if (Build.VERSION.SDK_INT >= 33 && chat.messages.any { it.mine } && !BackendService.prefs(context).getBoolean("asked_notify", false)) {
            BackendService.prefs(context).edit().putBoolean("asked_notify", true).apply()
            askNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
    BackHandler(enabled = page.isNotEmpty()) { page = if (page == "settings") "" else "settings" }

    AnimatedContent(targetState = page, label = "page", transitionSpec = {
        // 往里走从右边滑进来，退出来往右边滑出去；开了"减少动效"就直接换
        val forward = DEPTH.getValue(targetState) > DEPTH.getValue(initialState)
        if (still) fadeIn(tween(0)) togetherWith fadeOut(tween(0))
        else (fadeIn(tween(220)) + slideInHorizontally(tween(220)) { if (forward) it / 6 else -it / 6 }) togetherWith
            (fadeOut(tween(160)) + slideOutHorizontally(tween(220)) { if (forward) -it / 6 else it / 6 })
    }) { shown ->
        when (shown) {
            "settings" -> SettingsScreen(backend, ta, creator, onCreator = {
                creator = it
                BackendService.prefs(context).edit().putBoolean("creator", it).apply()
            }, onThoughts = { page = "thoughts" }, onChangeKey = { page = "key" }, onBack = { page = "" })
            "thoughts" -> ThoughtsScreen(backend, ta, onBack = { page = "settings" })
            // 后端的配置是启动时读的：换完密钥重新打开一次，世界和记忆都还在
            "key" -> Onboarding(onDone = { restartApp(context) }, change = true, onBack = { page = "settings" })
            else -> ChatScreen(chat, ta, creator, onSettings = { page = "settings" }, onLeave = onLeave, onInner = { inner = it })
        }
    }
    inner?.let { InnerSheet(backend, ta, it, onClose = { inner = null }) }
}
