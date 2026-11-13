package ai.meanwhile.app.ui

import ai.meanwhile.app.Backend
import ai.meanwhile.app.BackendService
import ai.meanwhile.app.Cost
import ai.meanwhile.app.Hub
import ai.meanwhile.app.Now
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.util.Log
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 设置，照 Telegram 设置页的结构：左上浮着的圆形返回钮；正中大圆头像、名字、一行灰字；没有"设置"大标题。
 * 下面是大圆角卡片，每行左边一个彩色小方块图标，右边两行字。从聊天页点名字胶囊进来。
 */
@Composable
fun SettingsScreen(backend: Backend, ta: Ta, creator: Boolean, onCreator: (Boolean) -> Unit, onThoughts: () -> Unit,
                   onChangeKey: () -> Unit, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val c = Mw.colors
    val name by Hub.name.collectAsState()
    var cost by remember { mutableStateOf<Cost?>(null) }
    var now by remember { mutableStateOf<Now?>(null) }
    var proactive by remember { mutableStateOf(BackendService.proactive(context)) }
    val said = remember { SnackbarHostState() }          // 导出完了说一声：从底下冒出来的一小条
    val scroll = rememberScrollState()
    LaunchedEffect(Unit) {
        cost = try { backend.cost() } catch (_: Exception) { null }
        now = try { backend.now() } catch (_: Exception) { null }
    }
    val overseas = remember { BackendService.prefs(context).getString("preset", "cn") == "intl" }
    val provider = if (overseas) "OpenRouter" else "DeepSeek"
    val save = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/zip")) { target ->
        if (target != null) scope.launch { said.showSnackbar(withContext(Dispatchers.IO) { exportTo(context, target) }) }
    }

    Box(Modifier.fillMaxSize().background(c.ground)) {
        Column(Modifier.fillMaxSize().verticalScroll(scroll).statusBarsPadding().navigationBarsPadding()) {
            Column(Modifier.fillMaxWidth().padding(top = 46.dp, bottom = 22.dp, start = Space.xl, end = Space.xl),
                horizontalAlignment = Alignment.CenterHorizontally) {
                HomePicture(ta, 100.dp, hour = now?.hour, sky = now?.sky, asleep = now?.activity == "睡觉")
                Text(name, style = Mw.type.headlineSmall, color = c.ink, textAlign = TextAlign.Center, modifier = Modifier.padding(top = 14.dp))
                val city = now?.city?.ifBlank { null } ?: cityLabel(context)
                val sub = listOf(if (city.isEmpty()) "" else "住在$city", now?.let { doing(it) } ?: "").filter { it.isNotEmpty() }.joinToString(" · ")
                if (sub.isNotEmpty()) {
                    Text(sub, style = Mw.type.titleSmall.copy(fontWeight = FontWeight.Normal), color = c.ink3, textAlign = TextAlign.Center,
                        modifier = Modifier.padding(top = 5.dp))
                }
            }
            Card {
                Item(Glyph.Receipt, c.green, "今天花了", cost?.takeIf { it.unknownCalls > 0 }?.let { "另有 ${it.unknownCalls} 次算不出花了多少" }
                    ?: "从你自己的 $provider 账户里扣") {
                    Text(cost?.let { "¥%.2f".format(it.cny) } ?: "—", style = Mw.type.titleMedium.copy(fontSize = Mw.type.titleMedium.fontSize * 1.06f,
                        fontWeight = FontWeight.SemiBold), color = c.ink)
                }
            }
            Card {
                Item(Glyph.Key, c.blue, "模型与密钥", "${if (overseas) "海外" else "国内"} · $provider，密钥只存在这台手机上", onClick = onChangeKey) { Chevron() }
                Divider()
                Toggle(Glyph.Chat, c.orange, "会主动找我", "想到你的时候会自己发消息", proactive) { on ->
                    proactive = on
                    BackendService.prefs(context).edit().putBoolean("proactive", on).apply()
                    scope.launch { try { backend.setProactive(on) } catch (_: Exception) { /* 后端没起来：下次启动时带进去 */ } }
                }
                Divider()
                Toggle(Glyph.Eye, c.purple, "造物者模式", "长按${ta.word}的消息，看${ta.word}当时心里想的", creator, onCreator)
                AnimatedVisibility(visible = creator) {
                    Column {
                        Divider()
                        Item(Glyph.Thought, c.indigo, "${ta.word}起过的念头", "发了的和没发的都在", onClick = onThoughts) { Chevron() }
                    }
                }
                Divider()
                Item(Glyph.Bell, c.red, "通知", "${ta.word}的消息怎么提醒你", onClick = {
                    context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName))
                }) { Chevron() }
                Divider()
                Item(Glyph.Download, c.teal, "导出数据", "聊天记录和${ta.word}的世界，存成一个文件备份", onClick = {
                    save.launch("此刻-" + SimpleDateFormat("yyyyMMdd-HHmm", Locale.US).format(Date()) + ".zip")
                }) { Chevron() }
                Divider()
                Item(Glyph.Info, c.gray, "关于 Meanwhile-此刻", "开源 · 版本 ${version(context)}", onClick = { openUrl(context, REPO) }) { Chevron() }
            }
        }
        // 滚上去的卡片不从返回钮和状态栏底下透出来：滚动过了，顶上才出现一条实的底色（返回钮那么高）
        TopFade(c.ground, shown = scroll.value > 0, below = 64.dp)
        BottomFade(c.ground)
        SnackbarHost(said, Modifier.align(Alignment.BottomCenter).navigationBarsPadding())
        RoundButton(Glyph.BackArrow, "返回", onBack, Modifier.statusBarsPadding().padding(start = 12.dp, top = Space.s), size = 48.dp)
    }
}

private const val REPO = "https://github.com/Neno-s-team/Neno"

private fun version(context: Context): String = try {
    context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: ""
} catch (_: Exception) {
    ""
}

/** 把聊天记录和 TA 的世界存成一个 zip，写到用户选的地方。密钥不在里面（它存在系统的设置里，不在数据目录）。返回一句给人看的话。 */
private fun exportTo(context: Context, target: Uri): String {
    val temp = File(context.cacheDir, "export.zip")
    return try {
        val wrong = Python.getInstance().getModule("mobile_main").callAttr("export", context.filesDir.absolutePath, temp.absolutePath).toString()
        if (wrong.isNotEmpty()) return wrong
        context.contentResolver.openOutputStream(target)?.use { out -> temp.inputStream().use { it.copyTo(out) } }
            ?: return "没存成：那个位置写不进去，换个地方再试。"
        "存好了。聊天记录和世界都在里面，不含密钥。"
    } catch (error: Throwable) {
        Log.e(BackendService.TAG, "export failed", error)
        "没存成。看看手机空间够不够，再试一次。"
    } finally {
        temp.delete()
    }
}

@Composable
internal fun Card(content: @Composable () -> Unit) {
    Column(Modifier.padding(start = Space.edge, end = Space.edge, bottom = Space.edge).fillMaxWidth()
        .clip(RoundedCornerShape(22.dp)).background(Mw.colors.card)) { content() }
}

@Composable
private fun Divider() {
    Box(Modifier.padding(start = 66.dp).fillMaxWidth().height(1.dp).background(Mw.colors.sep))
}

@Composable
private fun Chevron() {
    Icon(Glyph.Next, contentDescription = null, tint = Mw.colors.ink3.copy(alpha = 0.6f), modifier = Modifier.size(8.dp, 13.dp))
}

/** 一行：彩色小方块图标 + 名称 + 一行灰字说明 + 右边的东西（数值、箭头、开关）。 */
@Composable
private fun Item(icon: ImageVector, tile: Color, title: String, sub: String, modifier: Modifier = Modifier, onClick: (() -> Unit)? = null,
                 trailing: @Composable () -> Unit) {
    val c = Mw.colors
    Row(modifier.fillMaxWidth().then(if (onClick != null) Modifier.clickable(role = Role.Button, onClick = onClick) else Modifier)
        .heightIn(min = 62.dp).padding(horizontal = Space.l, vertical = 11.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(34.dp).background(tile, RoundedCornerShape(10.dp)), contentAlignment = Alignment.Center) {
            Icon(icon, contentDescription = null, tint = Color.White)
        }
        Column(Modifier.weight(1f).padding(start = 14.dp, end = Space.m)) {
            Text(title, style = Mw.type.titleMedium, color = c.ink)
            Text(sub, style = Mw.type.bodySmall, color = c.ink3, modifier = Modifier.padding(top = 2.dp))
        }
        trailing()
    }
}

/** 带开关的一行：整行都能点。 */
@Composable
private fun Toggle(icon: ImageVector, tile: Color, title: String, sub: String, on: Boolean, onChange: (Boolean) -> Unit) {
    val c = Mw.colors
    Item(icon, tile, title, sub, Modifier.toggleable(value = on, role = Role.Switch, onValueChange = onChange)) {
        // 圆钮里放一块看不见的东西：Material 的开关只有"圆钮里有内容"时，关着也是整颗大小；不然关着会缩成一个小点，像禁用
        Switch(checked = on, onCheckedChange = null, thumbContent = { Spacer(Modifier.size(SwitchDefaults.IconSize)) },
            colors = SwitchDefaults.colors(
            checkedThumbColor = Color.White, checkedTrackColor = c.green, checkedBorderColor = Color.Transparent,
            uncheckedThumbColor = Color.White, uncheckedTrackColor = c.switchOff, uncheckedBorderColor = Color.Transparent))
    }
}
