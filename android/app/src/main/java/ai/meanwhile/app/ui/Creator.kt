package ai.meanwhile.app.ui

import ai.meanwhile.app.Backend
import ai.meanwhile.app.Hub
import ai.meanwhile.app.Inner
import ai.meanwhile.app.Thought
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp

// 造物者模式的两处：TA 起过的念头（列表），一条消息背后 TA 想的（从底下升起来的一张）。普通用户看不到。

/** TA 起过的念头：发了的和没发的都在。发出去的话是灰气泡，没发的是虚线框——和聊天页一个画法。 */
@Composable
fun ThoughtsScreen(backend: Backend, ta: Ta, onBack: () -> Unit) {
    val name by Hub.name.collectAsState()
    var items by remember { mutableStateOf<List<Thought>?>(null) }
    LaunchedEffect(Unit) { items = try { backend.thoughts() } catch (_: Exception) { emptyList() } }
    val c = Mw.colors
    Column(Modifier.fillMaxSize().background(c.ground).statusBarsPadding()) {
        Box(Modifier.fillMaxWidth().heightIn(min = 64.dp), contentAlignment = Alignment.Center) {
            RoundButton(Glyph.BackArrow, "返回", onBack, Modifier.align(Alignment.CenterStart).padding(start = 12.dp), size = 48.dp)
            Text(name.then("起过的念头"), style = Mw.type.labelLarge, color = c.ink, textAlign = TextAlign.Center,
                modifier = Modifier.padding(horizontal = 72.dp))
        }
        val list = items
        if (list != null && list.isEmpty()) {
            Text("还没有。\n${ta.word}遇到想说的事、惦记着什么的时候，这里会有。", style = Mw.type.bodyMedium.balanced, color = c.ink3,
                textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(horizontal = 40.dp, vertical = 48.dp))
        }
        Box(Modifier.weight(1f)) {
        LazyColumn(contentPadding = WindowInsets.navigationBars.asPaddingValues(), modifier = Modifier.fillMaxSize().padding(top = Space.s)) {
            itemsIndexed(list ?: emptyList(), key = { index, _ -> index }) { _, item ->
                Card {
                    Column(Modifier.padding(horizontal = Space.l, vertical = 14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        val sent = item.status == "sent"
                        Text("${stamp(item.at, withDay = true)} · ${item.word}", style = Mw.type.labelSmall, color = if (sent) c.me else c.ink3)
                        Text(item.text, style = Mw.type.bodyLarge, color = c.ink)
                        for (line in item.said) {
                            if (sent) Bubble(line, mine = false, tail = false, widest = 280.dp) else UnsentBubble(line, widest = 240.dp)
                        }
                        if (item.reason.isNotEmpty()) Text(item.reason, style = Mw.type.labelMedium, color = c.ink3)
                    }
                }
            }
        }
        BottomFade(c.ground)
        }
    }
}

/** 这一条消息背后 TA 想的。长按 TA 的一条消息出来。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InnerSheet(backend: Backend, ta: Ta, cycleId: String, onClose: () -> Unit) {
    var inner by remember { mutableStateOf<Inner?>(null) }
    var missing by remember { mutableStateOf(false) }
    LaunchedEffect(cycleId) {
        try { inner = backend.inner(cycleId) } catch (_: Exception) { missing = true }
    }
    val c = Mw.colors
    // 一次升到内容那么高（不停在半屏）：不然最后一行会被屏幕底边拦腰切断
    ModalBottomSheet(onDismissRequest = onClose, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        containerColor = c.card, contentColor = c.ink) {
        Column(Modifier.verticalScroll(rememberScrollState()).navigationBarsPadding().padding(horizontal = Space.xl)
            .padding(bottom = Space.xl), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            val seen = inner
            if (seen == null) {
                Text(if (missing) "这一条当时怎么想的，没留下记录。" else "……", style = Mw.type.bodyMedium, color = c.ink3)
                return@Column
            }
            Text(if (seen.initiative) "这是${ta.word}自己想说的" else "${ta.word}当时想的", style = Mw.type.labelLarge, color = c.ink)
            Part("第一反应", seen.gut)
            Part("${ta.word}怎么理解的", seen.reads)
            Part("${ta.word}的感受", seen.feels)
            Part("打算说的", seen.sayPoints.joinToString("\n"))
            Part("没打算说的", seen.hidden.joinToString("\n"))
            if (seen.hesitate) Text("发之前犹豫了一下", style = Mw.type.bodyLarge, color = c.ink)
            if (seen.unsent) {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("想好了却没发出去的", style = Mw.type.labelSmall.copy(fontWeight = FontWeight.Medium), color = c.ink3)
                    for (line in seen.final) UnsentBubble(line, widest = 260.dp)
                }
            }
            if (seen.reads.isEmpty() && seen.feels.isEmpty()) {
                Text("这一条是随口回的，没有多想。", style = Mw.type.labelMedium, color = c.ink3)
            }
        }
    }
}

@Composable
private fun Part(label: String, text: String) {
    if (text.isBlank()) return
    val c = Mw.colors
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(label, style = Mw.type.labelSmall, color = c.ink3)
        Text(text, style = Mw.type.bodyLarge, color = c.ink)
    }
}
