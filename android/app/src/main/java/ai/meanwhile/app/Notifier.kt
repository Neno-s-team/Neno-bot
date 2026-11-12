package ai.meanwhile.app

import ai.meanwhile.app.ui.Ta
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person
import androidx.core.app.RemoteInput
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** 她发来的消息的通知：像真人发来的——标题是她的名字，内容是原话，几条合成一组，能在通知里直接回。 */
object Notifier {
    private const val CHANNEL = "messages"
    private const val ID = 2
    private const val KEY_REPLY = "reply"
    private val lines = ArrayList<Pair<String, Long>>()      // 还没被看到的那几条（原话，时间）

    fun ensureChannel(context: Context) {
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "${Ta.saved(context).word}的消息", NotificationManager.IMPORTANCE_HIGH)
        )
    }

    @Synchronized
    fun message(context: Context, name: String, text: String) {
        lines.add(text to System.currentTimeMillis())
        while (lines.size > 6) lines.removeAt(0)
        val her = Person.Builder().setName(name).build()
        val style = NotificationCompat.MessagingStyle(Person.Builder().setName("我").build())
        for ((line, at) in lines) style.addMessage(line, at, her)
        val open = PendingIntent.getActivity(
            context, 0, Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val reply = PendingIntent.getBroadcast(
            context, 1, Intent(context, ReplyReceiver::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE,
        )
        val action = NotificationCompat.Action.Builder(android.R.drawable.ic_menu_send, "回复", reply)
            .addRemoteInput(RemoteInput.Builder(KEY_REPLY).setLabel("回复$name").build())
            .setAllowGeneratedReplies(false)
            .build()
        val notice = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setStyle(style)
            .setContentIntent(open)
            .addAction(action)
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .build()
        try {
            NotificationManagerCompat.from(context).notify(ID, notice)
        } catch (_: SecurityException) {
            // 用户没给通知权限：消息照样在聊天页里，只是不弹
        }
    }

    /** 聊天页打开了：这些都看到了，通知收掉。 */
    @Synchronized
    fun clear(context: Context) {
        lines.clear()
        NotificationManagerCompat.from(context).cancel(ID)
    }

    fun replyText(intent: Intent): String? = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(KEY_REPLY)?.toString()?.trim()
}

/** 在通知里直接回她。 */
class ReplyReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val text = Notifier.replyText(intent)?.takeIf { it.isNotEmpty() } ?: return
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                Backend(context).say(text)
                Hub.arrivals.value += 1
            } catch (_: Exception) {
                // 没发出去：通知留着，用户打开聊天页能看到没有这一条
            } finally {
                Notifier.clear(context)
                pending.finish()
            }
        }
    }
}
