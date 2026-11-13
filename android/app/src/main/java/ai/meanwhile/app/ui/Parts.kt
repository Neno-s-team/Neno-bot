package ai.meanwhile.app.ui

import ai.meanwhile.app.BackendService
import ai.meanwhile.app.R
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.windowInsetsTopHeight
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.windowInsetsBottomHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/** 称呼。界面上的"她 / 他"跟着第一次打开时选的走。 */
enum class Ta(val key: String, val word: String) {
    She("she", "她"), He("he", "他");

    companion object {
        fun of(key: String?): Ta = entries.firstOrNull { it.key == key } ?: She
        fun saved(context: Context): Ta = of(BackendService.prefs(context).getString("persona", null))
    }
}

/** 名字后面接中文：名字以字母或数字结尾就空一格（"Neno 起过的念头"），中文名不空。 */
fun String.then(rest: String): String = if (lastOrNull()?.let { it.code < 128 && it.isLetterOrDigit() } == true) "$this $rest" else this + rest

/** TA 那边现在几点（按 TA 那座城市的时区，不是手机的）。 */
fun hourThere(context: Context): Int = java.util.Calendar.getInstance(java.util.TimeZone.getTimeZone(
    BackendService.prefs(context).getString("city_tz", null) ?: "Asia/Shanghai")).get(java.util.Calendar.HOUR_OF_DAY)

/** TA 住的城市叫什么（第一次打开时选的那座的显示名）。还不知道就是空的，用到的地方把城市那半句省掉。 */
fun cityLabel(context: Context): String = BackendService.prefs(context).getString("city_label", "") ?: ""

/**
 * TA 家此刻的画面（画面里不出现人），按 TA 那边的时段和天气换。同一个房间、同一个机位的八张：
 * 睡着（灯都关了）；深夜、傍晚、白天各有不下雨和下雨的两张；晴朗的早晨一张（早晨下雨用白天下雨那张）。
 * hour 是 TA 那边几点，sky 是 clear / cloudy / rain / snow（下雪也用下雨那几张），asleep 是 TA 这会儿在睡觉。
 * "他"的家现在只有一张，等角色定了再补。
 */
fun homePicture(ta: Ta, hour: Int, sky: String?, asleep: Boolean = false): Int {
    val wet = sky == "rain" || sky == "snow"
    return when {
        ta == Ta.He -> R.drawable.home_he_rain
        asleep -> R.drawable.home_she_asleep
        hour < 5 || hour >= 20 -> if (wet) R.drawable.home_she_night_rain else R.drawable.home_she_night
        hour >= 17 -> if (wet) R.drawable.home_she_evening_rain else R.drawable.home_she_evening
        wet -> R.drawable.home_she_rain
        hour < 9 -> R.drawable.home_she_morning
        else -> R.drawable.home_she_day
    }
}

/** 圆头像。hour、sky 不给（世界还没建起来时）就只按 TA 那座城市现在几点来挑。画面换的时候慢慢化过去。 */
@Composable
fun HomePicture(ta: Ta, size: Dp, modifier: Modifier = Modifier, hour: Int? = null, sky: String? = null, asleep: Boolean = false) {
    val context = LocalContext.current
    val picture = homePicture(ta, hour ?: remember { hourThere(context) }, sky, asleep)
    Crossfade(picture, animationSpec = tween(if (reduceMotion()) 0 else 600), label = "home", modifier = modifier.size(size).clip(CircleShape)) {
        Image(painterResource(it), contentDescription = "${ta.word}家此刻的样子", contentScale = ContentScale.Crop,
            modifier = Modifier.size(size))
    }
}

/** 浮着的圆钮（返回）。看上去 44dp，能点的范围 48dp。 */
@Composable
fun RoundButton(icon: ImageVector, label: String, onClick: () -> Unit, modifier: Modifier = Modifier, size: Dp = 44.dp) {
    val c = Mw.colors
    Box(modifier.size(48.dp).clip(CircleShape).clickable(role = Role.Button, onClick = onClick)
        .semantics { contentDescription = label }, contentAlignment = Alignment.Center) {
        Box(Modifier.size(size).shadow(4.dp, CircleShape, ambientColor = c.shadow, spotColor = c.shadow)
            .background(c.float, CircleShape), contentAlignment = Alignment.Center) {
            Icon(icon, contentDescription = null, tint = c.ink)
        }
    }
}

/** 主按钮：蓝底白字，固定在页面底部。不能点的时候整个变淡，字写明为什么。 */
@Composable
fun PrimaryButton(text: String, enabled: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    val c = Mw.colors
    // 不能点时底色变淡；字不跟着淡到看不清——它写的是"为什么还不能点"
    Box(modifier.fillMaxWidth().heightIn(min = 52.dp).clip(RoundedCornerShape(14.dp))
        .background(c.me.copy(alpha = if (enabled) 1f else 0.35f)).clickable(enabled = enabled, role = Role.Button, onClick = onClick)
        .padding(horizontal = Space.l, vertical = Space.m), contentAlignment = Alignment.Center) {
        Text(text, style = Mw.type.labelLarge.balanced, color = c.onMe.copy(alpha = if (enabled || !c.dark) 1f else 0.6f),
            textAlign = TextAlign.Center)
    }
}

/**
 * 能上下滚的页面，贴着屏幕最底下：系统导航条那一截是实的底色（内容不从按键底下透出来），上面 12dp 渐渐隐掉。
 * 手势导航的手机上那一截只有一二十 dp 高。
 */
@Composable
fun BoxScope.BottomFade(color: Color) {
    Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth()) {
        Box(Modifier.fillMaxWidth().height(12.dp).background(Brush.verticalGradient(listOf(color.copy(alpha = 0f), color))))
        Box(Modifier.fillMaxWidth().windowInsetsBottomHeight(WindowInsets.navigationBars).background(color))
    }
}

/**
 * 同样的道理，贴着最上面：状态栏加上 below 那么高（给浮着的返回钮留的）是实的底色，再往下 16dp 渐渐隐掉。
 * shown = 页面滚动过了才出现：没滚的时候上面本来就没东西要挡。
 */
@Composable
fun BoxScope.TopFade(color: Color, shown: Boolean, below: Dp) {
    val alpha by animateFloatAsState(if (shown) 1f else 0f, tween(150), label = "topFade")
    Column(Modifier.align(Alignment.TopCenter).fillMaxWidth().graphicsLayer { this.alpha = alpha }) {
        Box(Modifier.fillMaxWidth().windowInsetsTopHeight(WindowInsets.statusBars).background(color))
        Box(Modifier.fillMaxWidth().height(below).background(color))
        Box(Modifier.fillMaxWidth().height(16.dp).background(Brush.verticalGradient(listOf(color, color.copy(alpha = 0f)))))
    }
}

fun openUrl(context: Context, url: String) {
    try {
        context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    } catch (_: Exception) {
        // 手机上没有浏览器：什么都不做，页面上的网址还在
    }
}

/** 重新打开 App。换了密钥、或者后端没起来要再试时用：后端的配置是启动时读的，重来一遍最稳。 */
fun restartApp(context: Context) {
    val launch = context.packageManager.getLaunchIntentForPackage(context.packageName) ?: return
    context.startActivity(Intent.makeRestartActivityTask(launch.component))
    Runtime.getRuntime().exit(0)
}
