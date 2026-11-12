package ai.meanwhile.app

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.Python
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import kotlin.concurrent.thread

/**
 * M0 的验证界面：只为打通"发一条 → 她回"和看诊断，不是给用户的界面（那是 M1 的事）。
 * 界面就是又一个"通道"：交消息、取到点该发的、回执，全走本机 HTTP。
 */
class MainActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var log: TextView
    private lateinit var scroll: ScrollView
    private lateinit var status: TextView
    @Volatile private var alive = false
    private val base = "http://127.0.0.1:${BackendService.PORT}"
    private val session = "me"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (12 * resources.displayMetrics.density).toInt()
        status = TextView(this).apply { text = "正在启动…"; setPadding(pad, pad, pad, pad / 2) }
        log = TextView(this).apply { textSize = 16f; setPadding(pad, pad, pad, pad); setTextIsSelectable(true) }
        scroll = ScrollView(this).apply { addView(log) }
        val input = EditText(this).apply { hint = "跟她说点什么"; maxLines = 3 }
        val send = Button(this).apply { text = "发送" }
        val probe = Button(this).apply { text = "诊断" }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(pad, 0, pad, pad)
            addView(input, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            addView(send)
            addView(probe)
        }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            fitsSystemWindows = true
            addView(status)
            addView(scroll, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
            addView(row)
        })
        send.setOnClickListener {
            val text = input.text.toString().trim()
            if (text.isNotEmpty()) {
                input.setText("")
                line("我：$text")
                thread { say(text) }
            }
        }
        probe.setOnClickListener { thread { runProbe() } }
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        val saved = BackendService.prefs(this)
        if (saved.getString("deepseek_key", "").isNullOrEmpty() && saved.getString("openrouter_key", "").isNullOrEmpty()) {
            askKey()
        } else {
            startBackend()
        }
    }

    /** 第一次打开：选哪家、粘贴密钥。 */
    private fun askKey() {
        val pad = (20 * resources.displayMetrics.density).toInt()
        val deepseek = RadioButton(this).apply { text = "DeepSeek（国内直连）"; id = 1 }
        val openrouter = RadioButton(this).apply { text = "OpenRouter（要能连上海外）"; id = 2 }
        val group = RadioGroup(this).apply { addView(deepseek); addView(openrouter); check(1) }
        val key = EditText(this).apply { hint = "把密钥粘贴到这里"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD }
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad / 2, pad, 0); addView(group); addView(key) }
        AlertDialog.Builder(this).setTitle("她需要一个大模型来想事情").setView(box).setCancelable(false)
            .setPositiveButton("开始") { _, _ ->
                val china = group.checkedRadioButtonId == 1
                BackendService.prefs(this).edit()
                    .putString("preset", if (china) "cn" else "intl")
                    .putString(if (china) "deepseek_key" else "openrouter_key", key.text.toString().trim())
                    .apply()
                startBackend()
            }.show()
    }

    private fun startBackend() {
        startForegroundService(Intent(this, BackendService::class.java))
        if (!alive) {
            alive = true
            thread(name = "neno-pump") { pump() }
        }
    }

    /** 等后端起来，然后一直取到点该发的。 */
    private fun pump() {
        val started = System.currentTimeMillis()
        var up = false
        while (alive) {
            try {
                if (!up) {
                    request("GET", "/health")
                    up = true
                    setStatus("后端起来了，用了 ${(System.currentTimeMillis() - started) / 1000} 秒")
                }
                val items = JSONObject(request("GET", "/mind/deliveries/due?session_id=$session&limit=50")).getJSONArray("items")
                for (i in 0 until items.length()) {
                    val item = items.getJSONObject(i)
                    val payload = item.optJSONObject("payload") ?: JSONObject()
                    when (item.getString("kind")) {
                        "text" -> line("她：${payload.optString("text")}")
                        "sticker" -> line("她：[表情包：${payload.optString("name")}]")
                        "typing" -> setStatus("对方正在输入…")
                    }
                    request("POST", "/mind/deliveries/${item.getLong("id")}/ack", JSONObject().put("status", "sent"))
                }
            } catch (error: Exception) {
                if (!up) setStatus("正在启动…${BackendService.lastError?.let { "\n$it" } ?: ""}")
            }
            Thread.sleep(1000)
        }
    }

    private fun say(text: String) {
        try {
            request("POST", "/chat", JSONObject().put("message", text).put("session_id", session).put("source", "android"))
            val now = JSONObject(request("GET", "/mind/now?session_id=" + URLEncoder.encode(session, "UTF-8")))
            val wait = if (now.isNull("sees_in")) -1 else now.getInt("sees_in")
            setStatus("她在${now.optString("activity")}" + if (wait > 20) "，大概 ${(wait + 59) / 60} 分钟后会看到" else "")
        } catch (error: Exception) {
            line("（没发出去：$error）")
        }
    }

    private fun runProbe() {
        try {
            val report = Python.getInstance().getModule("mobile_main").callAttr("probe").toString()
            val runtime = Runtime.getRuntime()
            line("诊断：$report\n内存（Java 堆）：${(runtime.totalMemory() - runtime.freeMemory()) / 1048576} MB；后端在跑：${BackendService.running}")
        } catch (error: Throwable) {
            line("诊断失败：$error")
        }
    }

    private fun request(method: String, path: String, body: JSONObject? = null): String {
        val connection = URL(base + path).openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = 3000
        connection.readTimeout = 300000
        connection.setRequestProperty("X-Internal-Token", BackendService.token(this))
        if (body != null) {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.use { it.write(body.toString().toByteArray()) }
        }
        try {
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            if (code !in 200..299) throw IllegalStateException("HTTP $code $text")
            return text
        } finally {
            connection.disconnect()
        }
    }

    private fun line(text: String) = ui.post {
        log.append(text + "\n")
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun setStatus(text: String) = ui.post { status.text = text }

    override fun onDestroy() {
        alive = false
        super.onDestroy()
    }
}
