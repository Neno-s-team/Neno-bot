package ai.meanwhile.app.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.unit.dp

/**
 * 界面上用到的全部图标：都是细线、圆头，一个画法。图标越少越好——聊天页只有返回和"+"，其余都在设置页的彩色小方块里。
 * 颜色由用的地方给（Icon 的 tint）。
 */
object Glyph {
    private fun lines(width: Float, height: Float, stroke: Float, vararg paths: String): ImageVector =
        ImageVector.Builder(defaultWidth = width.dp, defaultHeight = height.dp, viewportWidth = width, viewportHeight = height).apply {
            paths.forEach {
                addPath(pathData = addPathNodes(it), stroke = SolidColor(Color.Black), strokeLineWidth = stroke,
                    strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round)
            }
        }.build()

    private fun circle(cx: Float, cy: Float, r: Float) = "M${cx - r} ${cy}a$r $r 0 1 0 ${2 * r} 0a$r $r 0 1 0 ${-2 * r} 0"

    val Back by lazy { lines(12f, 20f, 2.2f, "M10 2 2 10l8 8") }                         // 聊天页：离开
    val BackArrow by lazy { lines(20f, 20f, 2f, "M17 10H3M9 4l-6 6 6 6") }              // 二级页：回上一页
    val Next by lazy { lines(8f, 13f, 2f, "m1.5 1.5 5 5-5 5") }
    val Down by lazy { lines(12f, 8f, 1.8f, "m1.5 1.5 4.5 4.5 4.5-4.5") }
    val Plus by lazy { lines(16f, 16f, 1.8f, "M8 1.5v13M1.5 8h13") }
    val Send by lazy { lines(16f, 16f, 2.2f, "M8 13.5v-11M3.5 7 8 2.5 12.5 7") }
    val Check by lazy { lines(18f, 14f, 2.4f, "m2 7.5 4.5 4.5L16 2") }

    // 设置页每行左边小方块里的
    val Receipt by lazy { lines(20f, 20f, 2f, "M5 2.5h10v15l-2.5-1.6L10 17.5l-2.5-1.6L5 17.5z", "M8 7h4M8 10.5h4") }
    val Key by lazy { lines(20f, 20f, 2f, circle(7f, 10f, 3.5f), "M10.5 10h7M15 10v3M17.5 10v2") }
    val Chat by lazy {
        lines(20f, 20f, 2f, "M4 4.5h12a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5H9l-4 3v-3H4A1.5 1.5 0 0 1 2.5 13V6A1.5 1.5 0 0 1 4 4.5z")
    }
    val Eye by lazy {
        lines(20f, 20f, 2f, "M1.8 10S5 4.5 10 4.5 18.2 10 18.2 10 15 15.5 10 15.5 1.8 10 1.8 10z", circle(10f, 10f, 2.6f))
    }
    val Thought by lazy {
        lines(20f, 20f, 2f, "M6.2 12.6a3.2 3.2 0 0 1-.4-6.4A4.3 4.3 0 0 1 14 5.5a3.6 3.6 0 0 1 .3 7.1z", "M6.6 15.6v.1M4.3 17.5v.1")
    }
    val Bell by lazy {
        lines(20f, 20f, 2f, "M10 2.8a5 5 0 0 0-5 5v3.4L3.5 14h13L15 11.2V7.8a5 5 0 0 0-5-5zM8.2 16.5a2 2 0 0 0 3.6 0")
    }
    val Download by lazy {
        lines(20f, 20f, 2f, "M10 3v9.5M6 8.8l4 4 4-4M3.5 14.5v1.5a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-1.5")
    }
    val Info by lazy { lines(20f, 20f, 2f, circle(10f, 10f, 7.5f), "M10 9v5M10 6.2v.1") }
}
