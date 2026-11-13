package ai.meanwhile.app

import ai.meanwhile.app.ui.Ta
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.security.SecureRandom
import kotlin.concurrent.thread

/** 服务和界面之间共享的一点状态。 */
object Hub {
    val up = MutableStateFlow(false)                 // 后端起来没有
    val error = MutableStateFlow<String?>(null)      // 后端起不来的原因
    val arrivals = MutableStateFlow(0)               // 她来了新消息、或者看了你的消息：+1，界面据此重新取聊天记录
    val typingUntil = MutableStateFlow(0L)           // 她"正在输入"到什么时候（毫秒）
    val name = MutableStateFlow("Neno")              // 她叫什么（取自人设卡）
    @Volatile var chatOpen = false                   // 聊天页是不是正开着：开着就不弹通知
}

/** 她的世界和大脑。前台服务里跑 Python 后端（只监听本机），并且一直取"到点该发的"——界面关了她的消息也照样到。 */
class BackendService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        val ta = Ta.saved(this).word          // 通知里的称呼跟着第一次打开时选的走
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "${ta}的世界", NotificationManager.IMPORTANCE_MIN).apply { setShowBadge(false) }
        )
        Notifier.ensureChannel(this)
        val notice = Notification.Builder(this, CHANNEL)
            .setContentTitle("${ta}的世界在运转")
            .setSmallIcon(R.drawable.ic_stat_bubble)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, notice, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, notice)
        }
        thread(name = "neno-backend") { runBackend() }
        scope.launch { pump() }
    }

    private fun runBackend() {
        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
            val main = Python.getInstance().getModule("mobile_main")
            // 每次启动把诊断写进日志（adb logcat -s Meanwhile）：解释器、原生依赖、SQLite 全文检索
            Log.i(TAG, "probe " + main.callAttr("probe").toString())
            Hub.name.value = main.callAttr("name").toString().ifBlank { "Neno" }
            Hub.error.value = null
            main.callAttr("start", filesDir.absolutePath, settingsJson(this), PORT)
        } catch (error: Throwable) {
            Hub.error.value = error.message ?: error.toString()
            Log.e(TAG, "backend stopped", error)
        } finally {
            Hub.up.value = false
        }
    }

    /** 一直取到点该发的：正在输入 → 告诉界面；一句话 → 回执（回执了才进聊天记录）、界面没开着就弹通知。 */
    private suspend fun pump() {
        val backend = Backend(this)
        while (scope.isActive) {
            try {
                if (!Hub.up.value) Hub.up.value = backend.healthy()
                if (Hub.up.value) {
                    for (item in backend.due()) {
                        when (item.kind) {
                            "typing" -> Hub.typingUntil.value = System.currentTimeMillis() + (item.duration * 1000).toLong() + 800
                            "text", "sticker" -> {
                                Hub.typingUntil.value = 0
                                if (!Hub.chatOpen) Notifier.message(this, Hub.name.value, item.text)
                            }
                        }
                        backend.ack(item.id)
                        if (item.kind != "typing") Hub.arrivals.value += 1
                    }
                }
            } catch (error: Exception) {
                Hub.up.value = false
            }
            delay(1000)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        scope.cancel()
        try {
            if (Python.isStarted()) Python.getInstance().getModule("mobile_main").callAttr("stop")
        } catch (error: Throwable) {
            Log.w(TAG, "stop failed", error)
        }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val TAG = "Meanwhile"
        const val PORT = 8765
        const val CHANNEL = "world"
        const val PREFS = "settings"

        fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        /** 第一次打开走到哪一步了：选过她或他没有、填过密钥没有、世界建起来过没有。 */
        fun chosen(context: Context): Boolean = prefs(context).contains("persona")

        fun configured(context: Context): Boolean =
            !prefs(context).getString("deepseek_key", "").isNullOrEmpty() || !prefs(context).getString("openrouter_key", "").isNullOrEmpty()

        fun worldReady(context: Context): Boolean = prefs(context).getBoolean("world_ready", false)

        /** "会主动找我"：默认开。 */
        fun proactive(context: Context): Boolean = prefs(context).getBoolean("proactive", true)

        fun start(context: Context) = context.startForegroundService(Intent(context, BackendService::class.java))

        /** 界面和后端之间的暗号：第一次用时生成，存在本机。 */
        fun token(context: Context): String {
            val saved = prefs(context).getString("token", null)
            if (saved != null) return saved
            val bytes = ByteArray(24).also { SecureRandom().nextBytes(it) }
            val made = bytes.joinToString("") { "%02x".format(it) }
            prefs(context).edit().putString("token", made).apply()
            return made
        }

        /** 交给 Python 的配置：档位、密钥、暗号、住哪座城、会不会主动找你。别的都用随包带的默认值。 */
        fun settingsJson(context: Context, preset: String? = null, deepseek: String? = null, openrouter: String? = null): String {
            val saved = prefs(context)
            return JSONObject()
                .put("MODEL_PRESET", preset ?: saved.getString("preset", "cn"))
                .put("DEEPSEEK_API_KEY", deepseek ?: saved.getString("deepseek_key", ""))
                .put("OPENROUTER_API_KEY", openrouter ?: saved.getString("openrouter_key", ""))
                .put("NENO_INTERNAL_TOKEN", token(context))
                .put("ADMIN_TOKEN", token(context) + "-admin")
                .put("WORLD_CITY", saved.getString("city", ""))             // 空的就用后端自己的默认城市
                .put("MIND_PROACTIVE", if (proactive(context)) "on" else "off")
                .toString()
        }
    }
}
