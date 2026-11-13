package ai.meanwhile.app.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.RoundRect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Outline
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp

enum class Tail { None, Left, Right }

/**
 * 气泡的形状：圆角 19dp；每组最后一条在靠自己那一侧的下角带一个小尾巴（照苹果信息）。
 * 尾巴伸到气泡外面 7dp：下角不收圆，沿一段圆弧往外甩出一个尖。
 */
class BubbleShape(private val tail: Tail) : Shape {
    override fun createOutline(size: Size, layoutDirection: LayoutDirection, density: Density): Outline {
        val (w, h) = size
        val r = minOf(with(density) { 19.dp.toPx() }, h / 2)
        if (tail == Tail.None) return Outline.Rounded(RoundRect(0f, 0f, w, h, CornerRadius(r)))
        val out = with(density) { 7.dp.toPx() }          // 尾巴伸出去多少
        val arc = with(density) { 10.dp.toPx() }         // 尾巴内侧那段圆弧的半径
        val sweep = 72.54f                               // 圆弧从气泡边上走到尾巴尖转过的角度：acos((10 - 7) / 10)
        val path = Path().apply {
            moveTo(r, 0f)
            lineTo(w - r, 0f)
            arcTo(Rect(w - 2 * r, 0f, w, 2 * r), -90f, 90f, false)
            if (tail == Tail.Right) {
                lineTo(w, h - arc)
                arcTo(Rect(w, h - 2 * arc, w + 2 * arc, h), 180f, -sweep, false)
                lineTo(w + out, h)
                lineTo(r, h)
                arcTo(Rect(0f, h - 2 * r, 2 * r, h), 90f, 90f, false)
            } else {
                lineTo(w, h - r)
                arcTo(Rect(w - 2 * r, h - 2 * r, w, h), 0f, 90f, false)
                lineTo(-out, h)
                arcTo(Rect(-2 * arc, h - 2 * arc, 0f, h), sweep, -sweep, false)
            }
            lineTo(0f, r)
            arcTo(Rect(0f, 0f, 2 * r, 2 * r), 180f, 90f, false)
            close()
        }
        return Outline.Generic(path)
    }
}

/** 一个气泡。你的在右、苹果蓝白字；TA 的在左、浅灰。faint：刚发出去、后端还没收下。长按（造物者模式）看 TA 当时想的。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun Bubble(text: String, mine: Boolean, tail: Boolean, widest: Dp, modifier: Modifier = Modifier, faint: Boolean = false,
           onLongClick: (() -> Unit)? = null) {
    val c = Mw.colors
    Row(modifier.fillMaxWidth(), horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start) {
        Box(Modifier.widthIn(max = widest).alpha(if (faint) 0.55f else 1f)
            .background(if (mine) c.me else c.her, BubbleShape(if (!tail) Tail.None else if (mine) Tail.Right else Tail.Left))
            .then(if (onLongClick == null) Modifier else Modifier.combinedClickable(
                interactionSource = remember { MutableInteractionSource() }, indication = null,
                onLongClickLabel = "看当时想的", onLongClick = onLongClick, onClick = {}))
            .padding(horizontal = 14.dp, vertical = Space.s)) {
            Text(text, style = Mw.type.bodyLarge, color = if (mine) c.onMe else c.ink)
        }
    }
}

/** TA 想好了却没发出去的话：灰色虚线框，旁边写"没发"。只在造物者模式里出现。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun UnsentBubble(text: String, widest: Dp, modifier: Modifier = Modifier, onLongClick: (() -> Unit)? = null) {
    val c = Mw.colors
    Row(modifier.fillMaxWidth().semantics(mergeDescendants = true) {}, verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.s)) {
        Box(Modifier.widthIn(max = widest)
            .drawBehind {
                val line = 1.5.dp.toPx()
                drawRoundRect(c.dash, topLeft = Offset(line / 2, line / 2), size = Size(size.width - line, size.height - line),
                    cornerRadius = CornerRadius(minOf(19.dp.toPx(), size.height / 2)),
                    style = Stroke(line, pathEffect = PathEffect.dashPathEffect(floatArrayOf(5.dp.toPx(), 4.dp.toPx()))))
            }
            .then(if (onLongClick == null) Modifier else Modifier.combinedClickable(
                interactionSource = remember { MutableInteractionSource() }, indication = null,
                onLongClickLabel = "看当时想的", onLongClick = onLongClick, onClick = {}))
            .padding(horizontal = 14.dp, vertical = Space.s)) {
            Text(text, style = Mw.type.bodyLarge, color = c.ink3)
        }
        Text("没发", style = Mw.type.labelSmall.copy(fontWeight = FontWeight.Normal), color = c.ink3)
    }
}

/** TA 正在输入：三个点依次明暗。开了"减少动效"就是三个不动的点。 */
@Composable
fun TypingBubble(modifier: Modifier = Modifier) {
    val c = Mw.colors
    val still = reduceMotion()
    val transition = rememberInfiniteTransition(label = "typing")
    Row(modifier.fillMaxWidth().semantics { contentDescription = "对方正在输入" }) {
        Row(Modifier.background(c.her, BubbleShape(Tail.Left)).heightIn(min = 39.dp).padding(horizontal = 14.dp),
            horizontalArrangement = Arrangement.spacedBy(5.dp), verticalAlignment = Alignment.CenterVertically) {
            repeat(3) { index ->
                val alpha by transition.animateFloat(0.3f, 1f, infiniteRepeatable(
                    tween(600, delayMillis = index * 200), RepeatMode.Reverse), label = "dot$index")
                Box(Modifier.size(8.dp).alpha(if (still) 0.6f else alpha).clip(CircleShape).background(c.ink3))
            }
        }
    }
}

/**
 * 底部只有一个"+"圆钮和一条胶囊输入框。永远能发：你连发几条、中途补一句都不用等 TA。
 * 有字的时候输入框右端出来一个蓝色的发送钮。
 */
@Composable
fun InputBar(text: String, onText: (String) -> Unit, onSend: (String) -> Unit, onPlus: () -> Unit, modifier: Modifier = Modifier) {
    val c = Mw.colors
    Row(modifier.fillMaxWidth().padding(start = 9.dp, end = Space.edge), verticalAlignment = Alignment.Bottom) {
        // 看上去是 38dp 的圆，能点的范围 48dp
        Box(Modifier.size(48.dp).clip(CircleShape).clickable(role = Role.Button, onClick = onPlus)
            .semantics { contentDescription = "发图片" }, contentAlignment = Alignment.Center) {
            Box(Modifier.size(38.dp).background(c.card, CircleShape).border(1.dp, c.hair, CircleShape), contentAlignment = Alignment.Center) {
                Icon(Glyph.Plus, contentDescription = null, tint = c.ink)
            }
        }
        Box(Modifier.weight(1f)) {
            Row(Modifier.fillMaxWidth().padding(start = 5.dp, top = 5.dp, bottom = 5.dp).heightIn(min = 38.dp)
                .border(1.dp, c.hair, RoundedCornerShape(19.dp)), verticalAlignment = Alignment.Bottom) {
                Box(Modifier.weight(1f).padding(start = Space.l, top = 7.dp, bottom = 7.dp), contentAlignment = Alignment.CenterStart) {
                    if (text.isEmpty()) Text("发消息", style = Mw.type.bodyMedium, color = c.hint)
                    BasicTextField(value = text, onValueChange = { onText(it.take(2000)) }, maxLines = 5,
                        textStyle = Mw.type.bodyMedium.copy(color = c.ink), cursorBrush = SolidColor(c.me),
                        modifier = Modifier.fillMaxWidth().semantics { contentDescription = "发消息" })
                }
                Spacer(Modifier.size(38.dp))          // 给发送钮留的位置
            }
            // 发送钮：看上去是输入框右端 30dp 的蓝圆，能点的范围 48dp（上下各比输入框多出 5dp，右边多出 5dp）
            androidx.compose.animation.AnimatedVisibility(visible = text.isNotBlank(),
                modifier = Modifier.align(Alignment.BottomEnd).offset(x = 5.dp),
                enter = scaleIn(tween(140), initialScale = 0.6f) + fadeIn(tween(140)),
                exit = scaleOut(tween(100), targetScale = 0.6f) + fadeOut(tween(100))) {
                Box(Modifier.size(48.dp).clip(CircleShape).clickable(role = Role.Button) { onSend(text.trim()); onText("") }
                    .semantics { contentDescription = "发送" }, contentAlignment = Alignment.Center) {
                    Box(Modifier.size(30.dp).background(c.me, CircleShape), contentAlignment = Alignment.Center) {
                        Icon(Glyph.Send, contentDescription = null, tint = c.onMe)
                    }
                }
            }
        }
    }
}
