package ai.meanwhile.app.ui

import ai.meanwhile.app.Hub
import ai.meanwhile.app.Message
import android.os.Build
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.BlurEffect
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.TileMode
import androidx.compose.ui.graphics.layer.CompositingStrategy
import androidx.compose.ui.graphics.layer.GraphicsLayer
import androidx.compose.ui.graphics.layer.drawLayer
import androidx.compose.ui.graphics.rememberGraphicsLayer
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

/**
 * 聊天页，照苹果信息做。没有标题栏：左上一个圆形返回钮，正中是圆头像（TA 家此刻的画面）、名字胶囊、一行灰字状态，
 * 消息从这一块下面滑过去、渐渐隐掉。底部只有"+"和胶囊输入框。点头像或名字进设置。
 */
@Composable
fun ChatScreen(model: ChatModel, ta: Ta, creator: Boolean, onSettings: () -> Unit, onLeave: () -> Unit, onInner: (String) -> Unit) {
    val up by Hub.up.collectAsState()
    val name by Hub.name.collectAsState()
    val typingUntil by Hub.typingUntil.collectAsState()
    var typing by remember { mutableStateOf(false) }
    LaunchedEffect(typingUntil) {
        typing = typingUntil > System.currentTimeMillis()
        if (typing) {
            delay(typingUntil - System.currentTimeMillis())
            typing = false
        }
    }
    var note by remember { mutableStateOf<String?>(null) }       // 输入框上面那一行小字：点了"+"、或者刚才那条没发出去
    LaunchedEffect(note) {
        if (note != null) {
            delay(3200)
            note = null
        }
    }
    LaunchedEffect(model.failed) {
        if (model.failed) {
            note = "刚才那句没发出去，再点一次发送"
            model.takeFailed()
        }
    }
    val c = Mw.colors
    var headPx by remember { mutableIntStateOf(0) }
    val head = with(LocalDensity.current) { headPx.toDp() }
    // 消息列表画的时候顺手录一份（连底色一起），顶上那一块拿它来做模糊。模糊要安卓 12 以上。
    val behind = if (Build.VERSION.SDK_INT >= 31) rememberGraphicsLayer() else null
    Column(Modifier.fillMaxSize().background(c.paper).imePadding()) {
        Box(Modifier.weight(1f).fillMaxWidth()) {
            Box(Modifier.fillMaxSize().then(if (behind == null) Modifier else Modifier.drawWithContent {
                behind.record {
                    drawRect(c.paper)
                    this@drawWithContent.drawContent()
                }
                drawLayer(behind)
            })) {
                MessageList(model, ta, typing, creator, head, onInner)
            }
            Header(name, statusLine(model.now, up, ta, creator), ta, model, behind, onSettings, onLeave, Modifier.onSizeChanged { headPx = it.height })
        }
        note?.let {
            Text(it, style = Mw.type.labelSmall, color = c.ink3, textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth().padding(horizontal = Space.xl, vertical = Space.xs))
        }
        InputBar(model.draft, onText = { model.draft = it }, onSend = model::send, onPlus = { note = "现在还不能发图片" },
            modifier = Modifier.navigationBarsPadding().padding(bottom = Space.xs))
    }
}

/**
 * 顶上那一块，消息从它下面滑过：滑到这里的消息是糊的，上面再盖一层由实到透明的底色，最下面三成渐渐隐掉。
 * behind 是消息列表录下来的那一份；没有（安卓 12 以下画不了模糊）就退一步：到状态那行字为止是实的底色，往下渐隐。
 */
@Composable
private fun Header(name: String, status: String, ta: Ta, model: ChatModel, behind: GraphicsLayer?, onSettings: () -> Unit,
                   onLeave: () -> Unit, modifier: Modifier) {
    val c = Mw.colors
    val density = LocalDensity.current
    val fade = with(density) { 34.dp.toPx() }
    val blur = with(density) { 24.dp.toPx() }
    val blurred = rememberGraphicsLayer()
    Box(modifier.fillMaxWidth().drawBehind {
        // solid：状态那行字的下沿。到这里为止模糊是足的、底色盖得厚（字后面不透出清楚的消息），再往下 34dp 一起渐渐隐掉
        val solid = ((size.height - fade) / size.height).coerceIn(0f, 1f)
        if (behind != null) {
            blurred.renderEffect = BlurEffect(blur, blur, TileMode.Clamp)
            blurred.compositingStrategy = CompositingStrategy.Offscreen
            blurred.clip = true
            blurred.record {
                drawLayer(behind)
                drawRect(Brush.verticalGradient(solid to Color.Black, 1f to Color.Transparent), blendMode = BlendMode.DstIn)
            }
            drawLayer(blurred)
            drawRect(Brush.verticalGradient(0f to c.paper.copy(alpha = 0.92f), solid to c.paper.copy(alpha = 0.86f), 1f to c.paper.copy(alpha = 0f)))
        } else {
            drawRect(Brush.verticalGradient(0f to c.paper, solid to c.paper, 1f to c.paper.copy(alpha = 0f)))
        }
    }.statusBarsPadding().padding(bottom = 34.dp)) {
        RoundButton(Glyph.Back, "离开", onLeave, Modifier.padding(start = 12.dp, top = 10.dp))
        Column(Modifier.align(Alignment.TopCenter).padding(top = 6.dp).clip(RoundedCornerShape(24.dp))
            .clickable(onClickLabel = "打开设置", role = Role.Button, onClick = onSettings).padding(horizontal = Space.l),
            horizontalAlignment = Alignment.CenterHorizontally) {
            HomePicture(ta, 66.dp, Modifier.shadow(2.dp, CircleShape, ambientColor = c.shadow, spotColor = c.shadow),
                hour = model.now?.hour, sky = model.now?.sky, asleep = model.now?.activity == "睡觉")
            Row(Modifier.offset(y = (-10).dp).shadow(4.dp, RoundedCornerShape(16.dp), ambientColor = c.shadow, spotColor = c.shadow)
                .background(c.float, RoundedCornerShape(16.dp)).padding(start = 14.dp, end = 12.dp, top = 5.dp, bottom = 5.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text(name, style = Mw.type.titleSmall, color = c.ink, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Icon(Glyph.Next, contentDescription = null, tint = c.ink.copy(alpha = 0.45f), modifier = Modifier.padding(start = 4.dp).size(7.dp, 11.dp))
            }
            if (status.isNotEmpty()) {
                Text(status, style = Mw.type.labelSmall, color = c.ink3, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.offset(y = (-4).dp))
            }
        }
    }
}

// 聊天记录里的一行：时间、气泡、没发的虚线框、"已读"
private sealed interface Line { val key: String }
private data class TimeLine(override val key: String, val text: String) : Line
private data class BubbleLine(override val key: String, val message: Message, val tail: Boolean, val gap: Dp, val faint: Boolean = false) : Line
private data class ReadLine(override val key: String) : Line
private data class TypingLine(override val key: String = "typing") : Line

private const val GROUP_SECONDS = 1800       // 隔了半小时以上：中间放一行时间

private fun lines(messages: List<Message>, sending: List<String>, typing: Boolean): List<Line> {
    val out = mutableListOf<Line>()
    // "已读"只标在你最后一条被 TA 看到的消息下面；没看到的什么都不写
    val lastRead = messages.lastOrNull { it.mine && it.read }
    messages.forEachIndexed { index, message ->
        val before = messages.getOrNull(index - 1)
        val after = messages.getOrNull(index + 1)
        val key = if (message.unsent) "u$index-${message.cycleId}-${message.text.hashCode()}" else "m${message.id}"
        val apart = before == null || message.at - before.at >= GROUP_SECONDS
        if (apart) out += TimeLine("t$key", stamp(message.at, withDay = before == null || !sameDay(before.at, message.at)))
        val sameSide = before != null && !apart && before.mine == message.mine
        val last = after == null || after.mine != message.mine || after.unsent != message.unsent || after.at - message.at >= GROUP_SECONDS
        out += BubbleLine(key, message, tail = last && !message.unsent, gap = if (apart) 0.dp else if (sameSide) 4.dp else 12.dp)
        if (message === lastRead) out += ReadLine("r$key")
    }
    sending.forEachIndexed { index, text ->
        out += BubbleLine("s$index", Message(0, true, text, 0.0, false, null), tail = index == sending.lastIndex, gap = 4.dp, faint = true)
    }
    if (typing) out += TypingLine()
    return out
}

@Composable
private fun MessageList(model: ChatModel, ta: Ta, typing: Boolean, creator: Boolean, head: Dp, onInner: (String) -> Unit) {
    val c = Mw.colors
    val still = reduceMotion()
    val rows = remember(model.messages, model.sending, typing) { lines(model.messages, model.sending, typing).asReversed() }
    if (rows.isEmpty()) {
        EmptyChat(model, ta, head)
        return
    }
    val state = rememberLazyListState()
    LaunchedEffect(rows.size) { state.animateScrollToItem(0) }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val widest = (maxWidth - Space.edge * 2) * 0.72f
        // 新的在最下面：列表倒着排，内容不满一屏时贴着底
        LazyColumn(state = state, reverseLayout = true, modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(start = Space.edge, end = Space.edge, top = head, bottom = Space.s)) {
            items(rows, key = { it.key }) { row ->
                val moving = if (still) Modifier else Modifier.animateItem(fadeInSpec = tween(180), placementSpec = tween(220), fadeOutSpec = null)
                when (row) {
                    is TimeLine -> Text(row.text, style = Mw.type.labelSmall, color = c.ink3, textAlign = TextAlign.Center,
                        modifier = moving.fillMaxWidth().padding(top = 18.dp, bottom = 10.dp))
                    is ReadLine -> Text("已读", style = Mw.type.labelSmall, color = c.ink3, textAlign = TextAlign.End,
                        modifier = moving.fillMaxWidth().padding(top = 3.dp, end = 6.dp))
                    is TypingLine -> TypingBubble(moving.padding(top = 4.dp))
                    is BubbleLine -> {
                        val message = row.message
                        val inner = message.cycleId?.takeIf { creator && !message.mine }?.let { id -> { onInner(id) } }
                        if (message.unsent) UnsentBubble(message.text, widest, moving.padding(top = row.gap.coerceAtLeast(6.dp)), inner)
                        else Bubble(message.text, message.mine, row.tail, widest, moving.padding(top = row.gap), row.faint, inner)
                    }
                }
            }
        }
    }
}

@Composable
private fun EmptyChat(model: ChatModel, ta: Ta, head: Dp) {
    val now = model.now
    val t = ta.word
    Box(Modifier.fillMaxSize().padding(top = head).padding(horizontal = 40.dp), contentAlignment = Alignment.Center) {
        Text(if (now == null) "跟${t}说点什么" else "${t}现在${doing(now)}。\n跟${t}说点什么，${t}看到手机就会回。",
            style = Mw.type.bodyMedium.balanced, color = Mw.colors.ink3, textAlign = TextAlign.Center)
    }
}
