package ai.meanwhile.app.ui

import ai.meanwhile.app.Backend
import ai.meanwhile.app.Message
import ai.meanwhile.app.Now
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/** 聊天页的状态。聊天记录以后端为准（TA 的话回执了才进记录），这里只多存"你刚发出去、后端还没收下"的那几条。 */
class ChatModel(private val backend: Backend, private val scope: CoroutineScope) {
    var messages by mutableStateOf(listOf<Message>())
        private set
    var sending by mutableStateOf(listOf<String>())      // 发出去了、还没进聊天记录的
        private set
    var now by mutableStateOf<Now?>(null)
        private set
    var draft by mutableStateOf("")                      // 输入框里正在写的（放在这里：去设置页转一圈回来还在）
    var failed by mutableStateOf(false)                  // 刚有一条没发出去（原话已经放回输入框，让人再点一次）
        private set
    var creator = false                                  // 造物者模式：聊天记录里带上 TA 想好了却没发出去的话

    fun refresh() {
        scope.launch {
            try {
                messages = backend.history(unsent = creator)
                now = backend.now()
            } catch (_: Exception) {
                // 后端还没起来：等 Hub.up 变了再取
            }
        }
    }

    /** 你连发几条、中途补一句都不用等 TA：每条各自交给后端。 */
    fun send(text: String) {
        sending = sending + text
        failed = false
        scope.launch {
            try {
                backend.say(text)
            } catch (error: Exception) {
                if (draft.isBlank()) draft = text
                failed = true
            } finally {
                sending = sending - text
                refresh()
            }
        }
    }

    fun takeFailed() {
        failed = false
    }
}

/** TA 在干嘛："在家刷手机""在图书馆看文献写论文""在路上，坐 2 号线去湖滨银泰"。 */
fun doing(now: Now): String {
    val what = now.activity.replace("/", "或")
    return when {
        what.isBlank() || what == "空闲" -> if (now.place.isBlank()) "有空" else "在${now.place}"
        what.startsWith("在") -> what
        else -> "在${now.place}$what"
    }
}

/**
 * 名字下面那一行灰字："杭州 小雨 · 在家刷手机"。城市和天气都来自 TA 的世界，不写死。
 * 造物者模式多说一句 TA 大概多久后会看手机——平时不说，朋友之间本来也不知道。
 */
fun statusLine(now: Now?, up: Boolean, ta: Ta, creator: Boolean): String {
    if (!up) return "${ta.word}的世界正在醒来…"
    if (now == null) return ""
    val where = listOf(now.city, now.weather).filter { it.isNotBlank() }.joinToString(" ")
    val parts = mutableListOf(where, doing(now))
    val wait = now.seesIn
    if (creator && wait != null) {
        parts += when {
            wait <= 20 -> "马上看手机"
            wait < 5400 -> "${(wait + 59) / 60} 分钟后看手机"
            else -> "${(wait + 1800) / 3600} 小时后看手机"
        }
    }
    return parts.filter { it.isNotBlank() }.joinToString(" · ")
}

/** 时间分隔："15:20"、"今天 08:15"（withDay：这一天在这一屏里第一次出现）、"昨天 21:05"、"11月9日 08:30"。 */
fun stamp(atSeconds: Double, withDay: Boolean = false, nowMillis: Long = System.currentTimeMillis()): String {
    val at = Calendar.getInstance().apply { timeInMillis = (atSeconds * 1000).toLong() }
    val today = Calendar.getInstance().apply { timeInMillis = nowMillis }
    val time = SimpleDateFormat("HH:mm", Locale.CHINA).format(Date(at.timeInMillis))
    val days = dayNumber(today) - dayNumber(at)
    return when {
        days == 0 -> if (withDay) "今天 $time" else time
        days == 1 -> "昨天 $time"
        else -> SimpleDateFormat("M月d日", Locale.CHINA).format(Date(at.timeInMillis)) + " $time"
    }
}

fun sameDay(a: Double, b: Double): Boolean = dayNumber(Calendar.getInstance().apply { timeInMillis = (a * 1000).toLong() }) ==
    dayNumber(Calendar.getInstance().apply { timeInMillis = (b * 1000).toLong() })

private fun dayNumber(c: Calendar): Int = c.get(Calendar.YEAR) * 366 + c.get(Calendar.DAY_OF_YEAR)
