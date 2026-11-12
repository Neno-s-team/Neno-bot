package ai.meanwhile.app

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
import org.json.JSONObject
import java.security.SecureRandom
import kotlin.concurrent.thread

/** 她的世界和大脑。前台服务里起一个线程跑 Python 后端（只监听本机），界面关了也在走。 */
class BackendService : Service() {

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "她的世界", NotificationManager.IMPORTANCE_MIN).apply { setShowBadge(false) }
        )
        val notice = Notification.Builder(this, CHANNEL)
            .setContentTitle("她的世界在运转")
            .setSmallIcon(android.R.drawable.ic_menu_recent_history)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, notice, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, notice)
        }
        thread(name = "neno-backend") { runBackend() }
    }

    private fun runBackend() {
        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
            running = true
            Python.getInstance().getModule("mobile_main")
                .callAttr("start", filesDir.absolutePath, settingsJson(this), PORT)
        } catch (error: Throwable) {
            lastError = error.toString()
            Log.e(TAG, "backend stopped", error)
        } finally {
            running = false
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
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

        @Volatile var running = false
        @Volatile var lastError: String? = null

        fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        /** 界面和后端之间的暗号：第一次用时生成，存在本机。 */
        fun token(context: Context): String {
            val saved = prefs(context).getString("token", null)
            if (saved != null) return saved
            val bytes = ByteArray(24).also { SecureRandom().nextBytes(it) }
            val made = bytes.joinToString("") { "%02x".format(it) }
            prefs(context).edit().putString("token", made).apply()
            return made
        }

        /** 交给 Python 的配置：档位、密钥、暗号。别的都用随包带的默认值。 */
        fun settingsJson(context: Context): String {
            val saved = prefs(context)
            return JSONObject()
                .put("MODEL_PRESET", saved.getString("preset", "cn"))
                .put("DEEPSEEK_API_KEY", saved.getString("deepseek_key", ""))
                .put("OPENROUTER_API_KEY", saved.getString("openrouter_key", ""))
                .put("NENO_INTERNAL_TOKEN", token(context))
                .put("ADMIN_TOKEN", token(context) + "-admin")
                .toString()
        }
    }
}
