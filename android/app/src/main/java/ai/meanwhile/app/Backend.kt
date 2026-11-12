package ai.meanwhile.app

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** 一条聊天消息。mine = 你发的；read 只对你发的有意义：TA 真点开看了才是 true。unsent = TA 想好了却没发出去的（造物者模式才有）。 */
data class Message(val id: Long, val mine: Boolean, val text: String, val at: Double, val read: Boolean, val cycleId: String?,
                   val unsent: Boolean = false)

/**
 * TA 此刻在干嘛。seesIn：还没看的消息大概多少秒后会看到（没有没看的就是 null）。
 * city / weather：TA 住的城市叫什么、外面什么天（"杭州""小雨"）；sky 是天气的大类（clear / cloudy / rain / snow），
 * hour 是 TA 那边几点——头像按这两样换图。
 */
data class Now(val activity: String, val place: String, val phone: String, val muted: Boolean, val seesIn: Int?,
               val city: String = "", val weather: String = "", val sky: String = "", val hour: Int? = null)

data class Thought(val at: Double, val text: String, val word: String, val status: String, val reason: String, val said: List<String>)
data class Inner(
    val initiative: Boolean, val gut: String, val reads: String, val feels: String, val sayPoints: List<String>,
    val hidden: List<String>, val hesitate: Boolean, val final: List<String>, val sent: List<String>, val unsent: Boolean,
)
data class Cost(val cny: Double, val calls: Int, val unknownCalls: Int)
data class Delivery(val id: Long, val kind: String, val text: String, val duration: Double, val cycleId: String)

/** 跟本机后端说话。界面就是又一个"通道"：交消息、取到点该发的、回执；其余都是只读的。 */
class Backend(context: Context) {
    private val base = "http://127.0.0.1:${BackendService.PORT}"
    private val token = BackendService.token(context)
    private val session = "me"
    private val sid = URLEncoder.encode(session, "UTF-8")

    private fun call(method: String, path: String, body: JSONObject? = null, readTimeout: Int = 30000): String {
        val connection = URL(base + path).openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = 3000
        connection.readTimeout = readTimeout
        connection.setRequestProperty("X-Internal-Token", token)
        if (body != null) {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.use { it.write(body.toString().toByteArray()) }
        }
        try {
            val code = connection.responseCode
            val text = (if (code in 200..299) connection.inputStream else connection.errorStream)
                ?.bufferedReader()?.use { it.readText() } ?: ""
            if (code !in 200..299) throw IllegalStateException("HTTP $code $text")
            return text
        } finally {
            connection.disconnect()
        }
    }

    private suspend fun get(path: String) = withContext(Dispatchers.IO) { JSONObject(call("GET", path)) }

    suspend fun healthy(): Boolean = withContext(Dispatchers.IO) {
        try { call("GET", "/health"); true } catch (_: Exception) { false }
    }

    suspend fun say(text: String) = withContext(Dispatchers.IO) {
        call("POST", "/chat", JSONObject().put("message", text).put("session_id", session).put("source", "android"), 300000)
    }

    suspend fun due(): List<Delivery> = get("/mind/deliveries/due?session_id=$sid&limit=50").getJSONArray("items").objects().map {
        val payload = it.optJSONObject("payload") ?: JSONObject()
        Delivery(it.getLong("id"), it.getString("kind"),
            payload.optString("text").ifEmpty { payload.optString("name").let { n -> if (n.isEmpty()) "" else "[表情包：$n]" } },
            payload.optDouble("duration", 0.0), it.optString("cycle_id"))
    }

    suspend fun ack(id: Long) = withContext(Dispatchers.IO) {
        call("POST", "/mind/deliveries/$id/ack", JSONObject().put("status", "sent"))
    }

    suspend fun now(): Now = get("/mind/now?session_id=$sid").let {
        Now(it.optString("activity"), it.optString("place"), it.optString("phone"), it.optBoolean("muted"),
            if (it.isNull("sees_in")) null else it.getInt("sees_in"), it.optString("city"), it.optString("weather"),
            it.optString("sky"), if (it.isNull("hour")) null else it.optInt("hour"))
    }

    /** 聊天记录，旧的在前。unsent = true（造物者模式）时把 TA 想好了却没发出去的话也按时间插进来。 */
    suspend fun history(unsent: Boolean = false): List<Message> =
        get("/mind/history?session_id=$sid&limit=200&unsent=$unsent").getJSONArray("items").objects().map {
            Message(it.getLong("id"), it.getString("role") == "user", it.getString("text"), it.optDouble("at", 0.0),
                it.optBoolean("read"), it.optString("cycle_id").takeIf { c -> c.isNotEmpty() && c != "null" },
                unsent = it.getString("role") == "unsent")
        }

    suspend fun thoughts(): List<Thought> = get("/mind/thoughts?session_id=$sid&limit=60").getJSONArray("items").objects().map {
        Thought(it.optDouble("at", 0.0), it.getString("text"), it.getString("word"), it.getString("status"),
            it.optString("reason"), it.getJSONArray("said").strings())
    }

    suspend fun inner(cycleId: String): Inner = get("/mind/cycles/$cycleId").let {
        val thought = it.optJSONObject("thought")
        Inner(it.optBoolean("initiative"), it.optString("gut"), thought?.optString("reads") ?: "", thought?.optString("feels") ?: "",
            it.getJSONArray("say_points").strings(), it.getJSONArray("hidden").strings(), it.optBoolean("hesitate"),
            it.getJSONArray("final").strings(), it.getJSONArray("sent").strings(), it.optBoolean("unsent"))
    }

    /** "会主动找我"的开关：当场生效。下次启动用什么由 App 自己存的设置带进去。 */
    suspend fun setProactive(on: Boolean) = withContext(Dispatchers.IO) {
        call("POST", "/mind/proactive", JSONObject().put("on", on))
    }

    suspend fun cost(): Cost = get("/mind/cost").let { Cost(it.optDouble("cny", 0.0), it.optInt("calls"), it.optInt("unknown_calls")) }
}

private fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }
private fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
