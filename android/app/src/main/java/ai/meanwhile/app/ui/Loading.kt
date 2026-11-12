package ai.meanwhile.app.ui

import ai.meanwhile.app.Backend
import ai.meanwhile.app.BackendService
import ai.meanwhile.app.Hub
import ai.meanwhile.app.R
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.BiasAlignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.blur
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private val InOut = CubicBezierEasing(0.65f, 0f, 0.35f, 1f)
private val ExpoOut = CubicBezierEasing(0.16f, 1f, 0.3f, 1f)
private const val STEP_MS = 1300L       // 每一步的字至少停这么久，不然一闪而过

/**
 * 填完密钥之后、世界第一次建起来的时候：细线圆环一笔画出，圆里一扇下雨的窗从模糊变清楚，
 * 下面的字跟着真实的步骤换——后端起来之前是"搭地图"，起来之后去问一次天气，问到了就是"准备好了"。
 * 没有转圈，没有进度条；开了"减少动效"直接是最后一帧。
 */
@Composable
fun LoadingScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    val c = Mw.colors
    val still = reduceMotion()
    val city = remember { cityLabel(context) }
    val up by Hub.up.collectAsState()
    val error by Hub.error.collectAsState()
    var step by remember { mutableIntStateOf(0) }
    val ring = remember { Animatable(if (still) 1f else 0f) }
    val reveal = remember { Animatable(if (still) 1f else 0f) }
    val began = remember { System.currentTimeMillis() }

    LaunchedEffect(Unit) {
        BackendService.start(context)
        if (!still) {
            launch { ring.animateTo(1f, tween(1000, easing = InOut)) }
            launch { reveal.animateTo(1f, tween(1800, delayMillis = 600, easing = ExpoOut)) }
        }
    }
    LaunchedEffect(up) {
        if (!up) return@LaunchedEffect
        delay((began + STEP_MS - System.currentTimeMillis()).coerceAtLeast(0))
        step = 1
        val asked = System.currentTimeMillis()
        val backend = Backend(context)
        // 等世界里有了今天的天气（联网取的，一般一两秒）；六秒还没有就不等了，天气晚点自己会到
        while (System.currentTimeMillis() - asked < 6000) {
            val weather = try { backend.now().weather } catch (_: Exception) { "" }
            if (weather.isNotEmpty()) break
            delay(400)
        }
        delay((asked + STEP_MS - System.currentTimeMillis()).coerceAtLeast(0))
        step = 2
        delay(if (still) 400 else 900)
        BackendService.prefs(context).edit().putBoolean("world_ready", true).apply()
        onDone()
    }

    Box(Modifier.fillMaxSize().background(c.paper).systemBarsPadding()) {
        Column(Modifier.fillMaxWidth().align(BiasAlignment(0f, -0.2f)), horizontalAlignment = Alignment.CenterHorizontally) {
            Box(Modifier.size(128.dp), contentAlignment = Alignment.Center) {
                Canvas(Modifier.fillMaxSize()) {
                    val line = 1.5.dp.toPx()
                    drawArc(c.dash, startAngle = -90f, sweepAngle = 360f * ring.value, useCenter = false,
                        topLeft = androidx.compose.ui.geometry.Offset(line / 2, line / 2),
                        size = androidx.compose.ui.geometry.Size(size.width - line, size.height - line),
                        style = Stroke(line, cap = StrokeCap.Round))
                }
                Image(painterResource(R.drawable.city_rain), contentDescription = null, contentScale = ContentScale.Crop,
                    modifier = Modifier.size(116.dp).clip(CircleShape)
                        .graphicsLayer { alpha = reveal.value; scaleX = 1.08f - 0.08f * reveal.value; scaleY = scaleX }
                        .blur((14f * (1f - reveal.value)).dp))
            }
            val failed = error != null && !up
            AnimatedContent(targetState = if (failed) -1 else step, label = "step",
                transitionSpec = {
                    if (still) fadeIn(tween(0)) togetherWith fadeOut(tween(0))
                    else (fadeIn(tween(260)) + slideInVertically(tween(260)) { it / 4 }) togetherWith
                        (fadeOut(tween(260)) + slideOutVertically(tween(260)) { -it / 4 })
                },
                modifier = Modifier.padding(top = 30.dp).fillMaxWidth().semantics { liveRegion = LiveRegionMode.Polite }) { shown ->
                val (text, done) = when (shown) {
                    -1 -> "没搭起来" to true
                    0 -> (if (city.isEmpty()) "正在搭地图…" else "正在搭${city}的地图…") to false
                    1 -> (if (city.isEmpty()) "看看今天的天气…" else "看看${city}今天的天气…") to false
                    else -> "准备好了" to true
                }
                Text(text, style = Mw.type.titleSmall.copy(fontWeight = if (done) FontWeight.Medium else FontWeight.Normal),
                    color = if (done) c.ink else c.ink3, textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 24.dp).padding(horizontal = Space.xl))
            }
            if (failed) {
                Text("手机上的后端没能启动。重新打开一次，多半就好了。", style = Mw.type.labelMedium.balanced, color = c.ink3,
                    textAlign = TextAlign.Center, modifier = Modifier.padding(horizontal = 32.dp, vertical = Space.m))
                PrimaryButton("再试一次", enabled = true, onClick = { restartApp(context) }, modifier = Modifier.padding(horizontal = 48.dp))
            }
        }
        Text("只有第一次要等一会儿", style = Mw.type.labelMedium, color = c.ink3, textAlign = TextAlign.Center,
            modifier = Modifier.align(Alignment.BottomCenter).fillMaxWidth().padding(bottom = 32.dp))
    }
}
