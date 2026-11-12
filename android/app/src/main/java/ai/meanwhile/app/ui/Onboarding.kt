package ai.meanwhile.app.ui

import ai.meanwhile.app.BackendService
import android.content.Context
import android.util.Log
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.OffsetMapping
import androidx.compose.ui.text.input.TransformedText
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withLink
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private data class Provider(val preset: String, val pref: String, val name: String, val title: String, val note: String,
                            val prefix: String, val url: String, val steps: String)

private val PROVIDERS = listOf(
    Provider("cn", "deepseek_key", "DeepSeek", "DeepSeek", "国内直连 · 支付宝、微信能充", "sk-",
        "https://platform.deepseek.com/api_keys", "注册、充 10 元、复制 sk- 开头的那串，回来粘贴。"),
    Provider("intl", "openrouter_key", "OpenRouter", "其他方式", "OpenRouter 等，海外网络", "sk-or-",
        "https://openrouter.ai/keys", "要能连上海外网络。注册、充值、复制 sk-or- 开头的那串，回来粘贴。"),
)

/** 这串像不像这家的密钥。联网之前先看一眼，省得白等一次。像就返回 null，不像返回一句人话。 */
private fun looksWrong(key: String, provider: Provider): String? = when {
    key.any { it.isWhitespace() } -> "这串中间有空格或换行，像是多复制了别的字。重新复制一次。"
    provider.preset == "cn" && key.startsWith("sk-or-") -> "这是 OpenRouter 的密钥。要用它，上面选「其他方式」。"
    !key.startsWith(provider.prefix) || key.length < 20 ->
        "这串不像 ${provider.name} 的密钥：它以 ${provider.prefix} 开头，是一长串字母和数字。重新复制一次。"
    else -> null
}

/**
 * 填密钥。第一次打开时在"选她或他"之后；设置里换模型或密钥时也是这一页（change = true，多一个返回钮）。
 * 选哪家 → 粘贴密钥 → 先看格式、再真发一次最小的请求 → 通了才往下走。
 */
@Composable
fun Onboarding(onDone: () -> Unit, change: Boolean = false, onBack: () -> Unit = {}) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    val c = Mw.colors
    val ta = remember { Ta.saved(context) }
    val saved = remember { BackendService.prefs(context).getString("preset", "cn") }
    var picked by rememberSaveable { mutableStateOf(if (change && saved == "intl") 1 else 0) }
    var key by rememberSaveable { mutableStateOf("") }
    var testing by rememberSaveable { mutableStateOf(false) }
    var problem by rememberSaveable { mutableStateOf<String?>(null) }
    var copied by remember { mutableStateOf(clipboard.hasText()) }
    // 去申请密钥再切回来：剪贴板里有东西，就把"粘贴"那颗按钮点亮
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { copied = clipboard.hasText() }
    val provider = PROVIDERS[picked]

    Box(Modifier.fillMaxSize().background(c.ground).systemBarsPadding().imePadding()) {
        Column(Modifier.fillMaxSize()) {
            Column(Modifier.weight(1f).verticalScroll(rememberScrollState())) {
                Column(Modifier.fillMaxWidth().padding(start = 36.dp, end = 36.dp, top = 26.dp, bottom = 26.dp),
                    horizontalAlignment = Alignment.CenterHorizontally) {
                    HomePicture(ta, 84.dp)
                    Text(if (change) "换一家模型，\n或者换密钥" else headline(ta, cityLabel(context), hourThere(context)), style = Mw.type.headlineMedium.balanced,
                        color = c.ink, textAlign = TextAlign.Center, modifier = Modifier.padding(top = 18.dp))
                    Text(if (change) "换完会重新打开一次。\n${ta.word}的记忆和日子都还在。"
                         else "${ta.word}不秒回，也会自己来找你。\n让${ta.word}能想事情，需要一个模型密钥。",
                        style = Mw.type.bodyMedium.balanced, color = c.ink2, textAlign = TextAlign.Center, modifier = Modifier.padding(top = 10.dp))
                }
                Group {
                    PROVIDERS.forEachIndexed { index, item ->
                        if (index > 0) Line()
                        Row(Modifier.fillMaxWidth().selectable(selected = index == picked, enabled = !testing, role = Role.RadioButton,
                                onClick = { picked = index; problem = null })
                            .heightIn(min = 52.dp).padding(horizontal = Space.l, vertical = Space.s),
                            verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text(item.title, style = Mw.type.bodyLarge, color = c.ink)
                                Text(item.note, style = Mw.type.labelMedium, color = c.ink3)
                            }
                            if (index == picked) Icon(Glyph.Check, contentDescription = null, tint = c.me)
                        }
                    }
                }
                Group(Modifier.padding(top = 20.dp)) {
                    KeyRow(key, provider, copied && key.isEmpty(), enabled = !testing, onChange = { key = it.trim(); problem = null },
                        onPaste = { key = clipboard.getText()?.text?.trim() ?: ""; problem = if (key.isEmpty()) "剪贴板里是空的。先去复制密钥。" else null })
                }
                problem?.let {
                    Text(it, style = Mw.type.labelMedium, color = c.danger, modifier = Modifier.padding(start = 32.dp, end = 32.dp, top = 7.dp))
                }
                Text(howTo(provider, c.me), style = Mw.type.labelMedium.copy(color = c.ink3),
                    modifier = Modifier.padding(start = 32.dp, end = 32.dp, top = 7.dp, bottom = Space.l))
            }
            Column(Modifier.padding(start = Space.l, end = Space.l, top = Space.s, bottom = Space.l),
                horizontalAlignment = Alignment.CenterHorizontally) {
                PrimaryButton(when {
                    testing -> "正在试这串密钥…"
                    key.isEmpty() -> if (change) "粘贴新密钥后换上" else "粘贴密钥后开始"
                    change -> "换成这个"
                    else -> "开始"
                }, enabled = key.isNotEmpty() && !testing, onClick = {
                    problem = looksWrong(key, provider)
                    if (problem == null) {
                        testing = true
                        scope.launch {
                            val wrong = withContext(Dispatchers.IO) { tryKey(context, provider, key) }
                            testing = false
                            if (wrong.isEmpty()) {
                                BackendService.prefs(context).edit().putString("preset", provider.preset).putString(provider.pref, key).apply()
                                onDone()
                            } else {
                                problem = wrong
                            }
                        }
                    }
                })
                Text("密钥只存在这台手机上，聊天内容只发给 ${provider.name}", style = Mw.type.labelMedium.balanced, color = c.ink3,
                    textAlign = TextAlign.Center, modifier = Modifier.padding(top = 10.dp))
            }
        }
        if (change) RoundButton(Glyph.BackArrow, "返回", onBack, Modifier.padding(start = 12.dp, top = Space.s), size = 48.dp)
    }
}

/** "她在杭州，正在过她的早上"。半夜就不说"正在过"了。 */
private fun headline(ta: Ta, city: String, hour: Int): String {
    val where = if (city.isEmpty()) ta.word else "${ta.word}在$city，\n"
    val part = when (hour) {
        in 5..8 -> "早上"
        in 9..11 -> "上午"
        in 12..13 -> "中午"
        in 14..17 -> "下午"
        in 18..22 -> "晚上"
        else -> return where + "这会儿大概睡了"
    }
    return where + "正在过${ta.word}的$part"
}

/** "还没有？去 DeepSeek 申请 › 注册、充 10 元……"：中间那半句点了直接去申请的网页。 */
private fun howTo(provider: Provider, link: androidx.compose.ui.graphics.Color): AnnotatedString = buildAnnotatedString {
    append("还没有？")
    withLink(LinkAnnotation.Url(provider.url, TextLinkStyles(SpanStyle(color = link)))) { append("去 ${provider.name} 申请 ›") }
    append(" " + provider.steps)
}

@Composable
private fun Group(modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    Column(modifier.padding(horizontal = Space.l).fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(Mw.colors.card)) { content() }
}

@Composable
private fun Line() {
    Box(Modifier.padding(start = Space.l).fillMaxWidth().heightIn(min = 1.dp).background(Mw.colors.sep))
}

/** 密钥那一行：能手打，但主要是点右边那颗"从剪贴板粘贴"。填了之后中间一段用圆点盖住。 */
@Composable
private fun KeyRow(key: String, provider: Provider, lit: Boolean, enabled: Boolean, onChange: (String) -> Unit, onPaste: () -> Unit) {
    val c = Mw.colors
    Row(Modifier.fillMaxWidth().heightIn(min = 52.dp).padding(start = Space.l, end = Space.s), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.weight(1f).padding(vertical = Space.m), contentAlignment = Alignment.CenterStart) {
            if (key.isEmpty()) Text("${provider.prefix}…", style = Mw.type.bodyLarge, color = c.hint)
            BasicTextField(value = key, onValueChange = onChange, singleLine = true, enabled = enabled,
                textStyle = Mw.type.bodyLarge.copy(color = c.ink), cursorBrush = SolidColor(c.me), visualTransformation = Masked,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, autoCorrectEnabled = false),
                modifier = Modifier.fillMaxWidth().semantics { contentDescription = "${provider.name} 的密钥" })
        }
        // 看上去是 30dp 高的小胶囊，能点的范围 48dp
        Box(Modifier.heightIn(min = 48.dp).clip(RoundedCornerShape(24.dp))
            .clickable(enabled = enabled, role = Role.Button, onClick = { if (key.isEmpty()) onPaste() else onChange("") })
            .padding(horizontal = Space.s), contentAlignment = Alignment.Center) {
            Text(if (key.isEmpty()) "从剪贴板粘贴" else "清掉", style = Mw.type.bodySmall.copy(fontWeight = androidx.compose.ui.text.font.FontWeight.Medium),
                color = if (lit) c.onMe else c.me,
                modifier = Modifier.clip(RoundedCornerShape(15.dp)).background(if (lit) c.me else c.pill).padding(horizontal = Space.m, vertical = 5.dp))
        }
    }
}

/**
 * 开头六位和最后四位照常显示，中间不管多长都只画八个圆点：一行放得下，能认出是哪一串，录屏时也不会把整串露出去。
 * 光标落在被盖住的那一段时，算它在圆点的末尾。
 */
private object Masked : VisualTransformation {
    private const val HEAD = 6
    private const val TAIL = 4
    private const val DOTS = 8

    override fun filter(text: AnnotatedString): TransformedText {
        val raw = text.text
        if (raw.length <= HEAD + TAIL + 2) return TransformedText(text, OffsetMapping.Identity)
        val hidden = raw.length - HEAD - TAIL
        val shown = raw.take(HEAD) + "•".repeat(DOTS) + raw.takeLast(TAIL)
        return TransformedText(AnnotatedString(shown), object : OffsetMapping {
            override fun originalToTransformed(offset: Int): Int = when {
                offset <= HEAD -> offset
                offset >= HEAD + hidden -> offset - hidden + DOTS
                else -> HEAD + DOTS
            }

            override fun transformedToOriginal(offset: Int): Int = when {
                offset <= HEAD -> offset
                offset >= HEAD + DOTS -> offset - DOTS + hidden
                else -> HEAD + hidden
            }
        })
    }
}

/** 真发一次最小的请求。通了返回空串，否则返回一句人话（原始的报错只进日志）。 */
private fun tryKey(context: Context, provider: Provider, key: String): String = try {
    if (!Python.isStarted()) Python.start(AndroidPlatform(context.applicationContext))
    val settings = BackendService.settingsJson(context, preset = provider.preset,
        deepseek = if (provider.preset == "cn") key else "", openrouter = if (provider.preset == "intl") key else "")
    Python.getInstance().getModule("mobile_main").callAttr("try_keys", settings).toString()
} catch (error: Throwable) {
    Log.e(BackendService.TAG, "key test failed", error)
    "没试成，多半是网络不通。看一下网络，再点一次。"
}
