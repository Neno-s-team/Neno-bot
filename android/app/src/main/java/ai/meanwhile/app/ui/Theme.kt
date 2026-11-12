package ai.meanwhile.app.ui

import android.provider.Settings
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.remember
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.LineBreak
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 颜色。照苹果信息做：纯白底、黑字、她的气泡浅灰、我的气泡苹果蓝；设置页是浅灰底上的白卡片。
 * Material 的颜色角色（见下面 scheme()）之外，聊天气泡、虚线框、设置页的彩色图标这些 Material 没有对应角色的，放在这里。
 */
@Immutable
data class Palette(
    val paper: Color,         // 聊天页和首次打开的底
    val ink: Color,           // 正文
    val ink2: Color,          // 标题下面那两行说明
    val ink3: Color,          // 次要：时间、状态、"已读"、说明小字
    val hair: Color,          // 细边框（输入框、"+"、卡片描边）
    val her: Color,           // 她的气泡
    val me: Color,            // 我的气泡、主按钮、链接、选中
    val onMe: Color,
    val dash: Color,          // 没发出去的话：虚线框
    val hint: Color,          // 输入框里的占位字
    val ground: Color,        // 设置页和填密钥页的底
    val card: Color,          // 上面的卡片
    val sep: Color,           // 卡片里行与行之间的线
    val float: Color,         // 浮着的圆钮、名字胶囊
    val pill: Color,          // 卡片里的小胶囊按钮（"从剪贴板粘贴"）的底
    val switchOff: Color,
    val danger: Color,
    val shadow: Color,        // 浮着的东西下面那点影子
    val dark: Boolean,
) {
    // 设置页每行左边的彩色小方块：两种模式下同一组颜色
    val green = Color(0xFF34C759)
    val blue = Color(0xFF1F8BF5)
    val orange = Color(0xFFFF9500)
    val purple = Color(0xFFAF52DE)
    val indigo = Color(0xFF5856D6)
    val red = Color(0xFFFF3B30)
    val teal = Color(0xFF30B0C7)
    val gray = Color(0xFF8E8E93)
}

private val LightPalette = Palette(
    paper = Color(0xFFFFFFFF), ink = Color(0xFF000000), ink2 = Color(0xFF3C3C43), ink3 = Color(0xFF8E8E93),
    hair = Color(0xFFE3E3E6), her = Color(0xFFE9E9EB), me = Color(0xFF1F8BF5), onMe = Color(0xFFFFFFFF),
    dash = Color(0xFFC4C4C9), hint = Color(0xFFB0B0B5), ground = Color(0xFFF2F2F7), card = Color(0xFFFFFFFF),
    sep = Color(0xFFE6E6EA), float = Color(0xFFFFFFFF), pill = Color(0xFFF2F2F7), switchOff = Color(0xFFE9E9EB),
    danger = Color(0xFFD70015), shadow = Color(0xFF000000), dark = false,
)
private val DarkPalette = Palette(
    paper = Color(0xFF000000), ink = Color(0xFFFFFFFF), ink2 = Color(0xFFC7C7CC), ink3 = Color(0xFF8E8E93),
    hair = Color(0xFF38383A), her = Color(0xFF262629), me = Color(0xFF1F8BF5), onMe = Color(0xFFFFFFFF),
    dash = Color(0xFF5A5A5E), hint = Color(0xFF6C6C70), ground = Color(0xFF000000), card = Color(0xFF1C1C1E),
    sep = Color(0xFF2C2C2E), float = Color(0xFF2C2C2E), pill = Color(0xFF2C2C2E), switchOff = Color(0xFF39393D),
    danger = Color(0xFFFF6961), shadow = Color(0xFF000000), dark = true,
)

private val LocalPalette = staticCompositionLocalOf { LightPalette }

/** 用法：Mw.colors.her、Mw.type.bodyLarge。 */
object Mw {
    val colors: Palette
        @Composable @ReadOnlyComposable get() = LocalPalette.current
    val type: Typography
        @Composable @ReadOnlyComposable get() = MaterialTheme.typography
}

// 系统自带的弹层、开关、菜单用的是 Material 的角色：从同一组颜色映射过去，两边不会各说各的
private fun scheme(p: Palette) = (if (p.dark) darkColorScheme() else lightColorScheme()).copy(
    background = p.paper, onBackground = p.ink, surface = p.card, onSurface = p.ink,
    surfaceVariant = p.her, onSurfaceVariant = p.ink3, primary = p.me, onPrimary = p.onMe,
    primaryContainer = p.pill, onPrimaryContainer = p.me, secondary = p.me, onSecondary = p.onMe,
    outline = p.dash, outlineVariant = p.hair, error = p.danger, onError = Color.White,
    surfaceContainerLowest = p.card, surfaceContainerLow = p.card, surfaceContainer = p.card,
    surfaceContainerHigh = p.card, surfaceContainerHighest = p.pill, surfaceTint = Color.Transparent, scrim = Color(0x66000000),
    // 从底下冒出来的那一小条提示：跟页面反着来，深色字浅色底 / 浅色字深色底
    inverseSurface = if (p.dark) Color(0xFFE9E9EB) else Color(0xFF2C2C2E), inverseOnSurface = if (p.dark) Color.Black else Color.White,
    inversePrimary = p.me,
)

// 字体跟系统走（中文思源黑体，拉丁和数字 Roboto）。正文 17sp；不拉字距、不用衬线；最小 12sp。
private fun style(size: Int, line: Int, weight: FontWeight = FontWeight.Normal) =
    TextStyle(fontSize = size.sp, lineHeight = line.sp, fontWeight = weight, letterSpacing = 0.sp)

private val Type = Typography(
    displayLarge = style(28, 35, FontWeight.Bold), displayMedium = style(28, 35, FontWeight.Bold),
    displaySmall = style(28, 35, FontWeight.Bold),
    headlineLarge = style(28, 35, FontWeight.Bold),
    headlineMedium = style(28, 35, FontWeight.Bold),         // 首次打开的大标题
    headlineSmall = style(26, 32, FontWeight.SemiBold),      // 设置页头部的名字
    titleLarge = style(24, 30, FontWeight.SemiBold),         // 选"她 / 他"卡片上的那个字
    titleMedium = style(17, 22, FontWeight.Medium),          // 列表每行的名称
    titleSmall = style(15, 20, FontWeight.SemiBold),         // 名字胶囊
    bodyLarge = style(17, 23),                               // 气泡、正文
    bodyMedium = style(16, 24),                              // 标题下的说明、输入框
    bodySmall = style(14, 20),                               // 列表每行的灰字说明
    labelLarge = style(17, 22, FontWeight.SemiBold),         // 按钮
    labelMedium = style(13, 19),                             // 脚注
    labelSmall = style(12, 16, FontWeight.Medium),           // 时间、状态、"已读"、"没发"
)

/** 居中的两三行字：各行差不多长，不在最后一行剩一两个字。 */
val TextStyle.balanced: TextStyle get() = copy(lineBreak = LineBreak.Heading)

/** 间距按 4dp 走。 */
object Space {
    val xs = 4.dp
    val s = 8.dp
    val m = 12.dp
    val l = 16.dp
    val xl = 24.dp
    val edge = 14.dp      // 聊天页和设置页的左右边距（照原型）
}

@Composable
fun MeanwhileTheme(content: @Composable () -> Unit) {
    val palette = if (isSystemInDarkTheme()) DarkPalette else LightPalette
    CompositionLocalProvider(LocalPalette provides palette) {
        MaterialTheme(colorScheme = scheme(palette), typography = Type, content = content)
    }
}

/** 系统开了"减少动效"（动画时长调成 0）就不做动效。 */
@Composable
fun reduceMotion(): Boolean {
    val context = LocalContext.current
    return remember {
        Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
    }
}
