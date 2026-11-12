package ai.meanwhile.app.ui

import ai.meanwhile.app.BackendService
import android.content.Context
import android.util.Log
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray

data class City(val name: String, val label: String, val timezone: String)

/** 第一次打开时能选什么：随包带了哪些城市、哪些角色。问随包的后端代码，不在界面里写死。 */
data class Choices(val cities: List<City>, val people: List<Ta>)

fun loadChoices(context: Context): Choices = try {
    if (!Python.isStarted()) Python.start(AndroidPlatform(context.applicationContext))
    val main = Python.getInstance().getModule("mobile_main")
    val cities = JSONArray(main.callAttr("cities").toString())
    val people = JSONArray(main.callAttr("personas").toString())
    Choices((0 until cities.length()).map { cities.getJSONArray(it).let { c -> City(c.getString(0), c.getString(1), c.optString(2, "Asia/Shanghai")) } },
        (0 until people.length()).map { Ta.of(people.getString(it)) })
} catch (error: Throwable) {
    Log.e(BackendService.TAG, "choices failed", error)
    Choices(emptyList(), listOf(Ta.She))        // 问不到：城市用后端自己的默认，那一行不显示
}

// 卡片上两行灰字的身份。占位，以角色卡为准。
private val WHO = mapOf(Ta.She to "在读研\n有课，也有 ddl", Ta.He to "刚上班\n早出晚归")

/** 第一次打开的第一屏：想认识她还是他，TA 住在哪座城市。 */
@Composable
fun ChooseScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    val c = Mw.colors
    var choices by remember { mutableStateOf<Choices?>(null) }
    var ta by rememberSaveable { mutableStateOf(Ta.She) }
    var city by rememberSaveable { mutableStateOf("") }
    LaunchedEffect(Unit) {
        val found = withContext(Dispatchers.IO) { loadChoices(context) }
        if (found.cities.none { it.name == city }) city = found.cities.firstOrNull()?.name ?: ""
        choices = found
    }
    val cities = choices?.cities ?: emptyList()
    Column(Modifier.fillMaxSize().background(c.paper).systemBarsPadding()) {
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState())) {
            Column(Modifier.fillMaxWidth().padding(start = 32.dp, end = 32.dp, top = 42.dp, bottom = 30.dp),
                horizontalAlignment = Alignment.CenterHorizontally) {
                Text("你想认识谁？", style = Mw.type.headlineMedium, color = c.ink, textAlign = TextAlign.Center)
                Text("TA 有自己的日子，\n不秒回，也会自己来找你。", style = Mw.type.bodyMedium.balanced, color = c.ink2,
                    textAlign = TextAlign.Center, modifier = Modifier.padding(top = 10.dp))
            }
            Row(Modifier.padding(horizontal = Space.l).height(IntrinsicSize.Max), horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                for (who in Ta.entries) {
                    PersonCard(who, picked = ta == who, ready = choices?.people?.contains(who) ?: (who == Ta.She),
                        onPick = { ta = who }, modifier = Modifier.weight(1f).fillMaxHeight())
                }
            }
            if (cities.isNotEmpty()) {
                CityRow(cities, city, onPick = { city = it })
                Text("天气、电影、展览，都跟这座城市的现实同步。", style = Mw.type.labelMedium.balanced, color = c.ink3,
                    textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(start = 32.dp, end = 32.dp, top = Space.m))
            }
        }
        PrimaryButton("就是${ta.word}", enabled = choices != null, modifier = Modifier.padding(Space.l), onClick = {
            val home = cities.firstOrNull { it.name == city }
            BackendService.prefs(context).edit().putString("persona", ta.key).putString("city", city)
                .putString("city_label", home?.label ?: "").putString("city_tz", home?.timezone ?: "Asia/Shanghai").apply()
            onDone()
        })
    }
}

@Composable
private fun PersonCard(who: Ta, picked: Boolean, ready: Boolean, onPick: () -> Unit, modifier: Modifier) {
    val c = Mw.colors
    val shape = RoundedCornerShape(22.dp)
    Box(modifier.clip(shape).border(if (picked) 2.dp else 1.5.dp, if (picked) c.me else c.hair, shape)
        .selectable(selected = picked, enabled = ready, role = Role.RadioButton, onClick = onPick)
        .semantics { if (!ready) contentDescription = "${who.word}，还没准备好" }) {
        Column(Modifier.fillMaxWidth().padding(start = Space.m, end = Space.m, top = 22.dp, bottom = 20.dp)
            .alpha(if (ready) 1f else 0.4f), horizontalAlignment = Alignment.CenterHorizontally) {
            HomePicture(who, 112.dp)
            Text(who.word, style = Mw.type.titleLarge, color = c.ink, modifier = Modifier.padding(top = Space.l))
            Text(if (ready) WHO.getValue(who) else "还没准备好", style = Mw.type.bodySmall, color = c.ink3,
                textAlign = TextAlign.Center, modifier = Modifier.padding(top = 6.dp))
        }
        if (ready) {
            Box(Modifier.align(Alignment.TopEnd).padding(Space.m).size(24.dp).clip(CircleShape)
                .then(if (picked) Modifier.background(c.me) else Modifier.border(1.5.dp, c.hair, CircleShape)),
                contentAlignment = Alignment.Center) {
                if (picked) Icon(Glyph.Check, contentDescription = null, tint = c.onMe, modifier = Modifier.size(12.dp, 10.dp))
            }
        }
    }
}

/** "住在 杭州"。能选的城市不止一座才能点；只有一座就只显示。 */
@Composable
private fun CityRow(cities: List<City>, picked: String, onPick: (String) -> Unit) {
    val c = Mw.colors
    val shape = RoundedCornerShape(16.dp)
    var open by remember { mutableStateOf(false) }
    val many = cities.size > 1
    Box(Modifier.padding(start = Space.l, end = Space.l, top = 18.dp)) {
        Row(Modifier.fillMaxWidth().clip(shape).border(1.5.dp, c.hair, shape)
            .then(if (many) Modifier.clickable(onClickLabel = "换一座城市", role = Role.DropdownList) { open = true } else Modifier)
            .heightIn(min = 56.dp).padding(horizontal = 18.dp, vertical = Space.m),
            verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("住在", style = Mw.type.bodyMedium, color = c.ink3, modifier = Modifier.weight(1f))
            Text(cities.firstOrNull { it.name == picked }?.label ?: "", style = Mw.type.titleMedium, color = c.ink)
            if (many) Icon(Glyph.Down, contentDescription = null, tint = c.ink3)
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            for (item in cities) {
                DropdownMenuItem(text = { Text(item.label, style = Mw.type.bodyLarge) }, onClick = { onPick(item.name); open = false })
            }
        }
    }
}
