package pro.kaprovpn.android.vpn

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import pro.kaprovpn.android.MainActivity
import pro.kaprovpn.android.R

/**
 * VPN-сервис, который держит TUN-интерфейс и натравливает на него libv2ray.
 *
 * Жизненный цикл:
 * 1. UI зовёт `startService(ACTION_CONNECT, EXTRA_CONFIG_JSON)`.
 * 2. [onStartCommand] поднимает foreground-notification, строит TUN через
 *    [VpnService.Builder], получает FD, вызывает [XrayBridge.start] с этим FD.
 * 3. libv2ray читает/пишет пакеты с TUN в Go-горутинах. UI наблюдает
 *    [XrayBridge.state].
 * 4. UI зовёт `startService(ACTION_DISCONNECT)` или закрывает сервис.
 *    [onDestroy] → [XrayBridge.stop] → закрываем TUN-FD.
 *
 * Соответствие десктоп-клиенту: эта же логика разложена между
 * `core/controller.py:_connect_tun` (Windows route table) и
 * `core/network_routes.py`. На Android всё проще — система сама делает
 * routing когда VpnService.Builder establish() возвращает FD, нам только
 * нужно сказать какие маршруты включать.
 */
class KaproVpnService : VpnService() {

    companion object {
        private const val TAG = "KaproVpnService"

        const val ACTION_CONNECT = "pro.kaprovpn.android.action.CONNECT"
        const val ACTION_DISCONNECT = "pro.kaprovpn.android.action.DISCONNECT"
        const val EXTRA_CONFIG_JSON = "config_json"
        const val EXTRA_SESSION_NAME = "session_name"

        private const val NOTIFICATION_ID = 0xC001
        private const val NOTIFICATION_CHANNEL = "vpn_status"

        // TUN параметры — повторяют десктоп (controller.py)
        private const val TUN_LOCAL_ADDR = "10.255.0.2"
        private const val TUN_PREFIX = 30
        private const val TUN_MTU = 1500
        private val TUN_DNS = listOf("77.88.8.8", "1.1.1.1") // Yandex, Cloudflare

        fun start(context: Context, configJson: String, sessionName: String) {
            val intent = Intent(context, KaproVpnService::class.java).apply {
                action = ACTION_CONNECT
                putExtra(EXTRA_CONFIG_JSON, configJson)
                putExtra(EXTRA_SESSION_NAME, sessionName)
            }
            context.startService(intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, KaproVpnService::class.java).apply {
                action = ACTION_DISCONNECT
            }
            context.startService(intent)
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var tun: ParcelFileDescriptor? = null
    private var startJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        Log.i(TAG, "onCreate")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CONNECT -> {
                val config = intent.getStringExtra(EXTRA_CONFIG_JSON)
                val name = intent.getStringExtra(EXTRA_SESSION_NAME) ?: "KaproVPN"
                if (config.isNullOrBlank()) {
                    Log.e(TAG, "ACTION_CONNECT без конфига — игнорю")
                    stopSelf()
                    return START_NOT_STICKY
                }
                connect(config, name)
            }
            ACTION_DISCONNECT -> {
                Log.i(TAG, "ACTION_DISCONNECT")
                disconnect()
            }
            else -> {
                Log.w(TAG, "unknown action: ${intent?.action}")
            }
        }
        // NOT_STICKY: если систему убьёт наш процесс — не пересоздавать
        // (туннель всё равно умер бы вместе с FD). User должен явно
        // переподключиться.
        return START_NOT_STICKY
    }

    private fun connect(configJson: String, sessionName: String) {
        // Перепроверка прав на всякий случай — VpnService.prepare() уже должна
        // была быть вызвана в UI, но pre-Android-13 разрешение могло истечь.
        if (prepare(this) != null) {
            Log.e(TAG, "VpnService.prepare() требует UI-разрешение — стопаем")
            stopSelf()
            return
        }

        startForeground(NOTIFICATION_ID, buildNotification(sessionName, connecting = true))

        startJob = scope.launch {
            val pfd: ParcelFileDescriptor = try {
                buildTun(sessionName)
            } catch (e: Throwable) {
                Log.e(TAG, "TUN setup failed", e)
                stopWithError("Не удалось создать TUN: ${e.message}")
                return@launch
            }
            tun = pfd

            // detachFd передаёт владение FD libv2ray — мы НЕ закрываем pfd
            // сами, иначе у Go отвалится поток чтения и xray остановится.
            val tunFd = pfd.detachFd()
            Log.i(TAG, "TUN established fd=$tunFd")

            try {
                XrayBridge.start(configJson, tunFd)
            } catch (e: Throwable) {
                Log.e(TAG, "XrayBridge.start failed", e)
                stopWithError("Xray не стартовал: ${e.message}")
                return@launch
            }

            // Подключено — обновляем notification (убираем "подключается…")
            startForeground(NOTIFICATION_ID, buildNotification(sessionName, connecting = false))
        }
    }

    private fun disconnect() {
        scope.launch {
            try {
                XrayBridge.stop()
            } catch (e: Throwable) {
                Log.w(TAG, "XrayBridge.stop failed (ignored)", e)
            }
            tun?.runCatching { close() }
            tun = null
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    private fun stopWithError(message: String) {
        Log.e(TAG, "stopWithError: $message")
        tun?.runCatching { close() }
        tun = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun buildTun(sessionName: String): ParcelFileDescriptor {
        val builder = Builder()
            .setSession(sessionName)
            .setMtu(TUN_MTU)
            .addAddress(TUN_LOCAL_ADDR, TUN_PREFIX)
            // Phase 3 MVP: туннелируем ВСЁ. Split-routing (direct-list) приедет
            // в Phase 4 — там будем резолвить direct-домены в IP и добавлять
            // `addRoute(ip, 32)` чтобы система не отправляла их через TUN.
            .addRoute("0.0.0.0", 0)

        TUN_DNS.forEach { builder.addDnsServer(it) }

        // Защищаемся от петли: исключаем сами себя из туннеля. Без этого
        // xray-исходящий трафик улетел бы обратно в TUN → бесконечная
        // петля → timeout. addDisallowedApplication работает только для
        // нашего own UID — Xray-сокеты пойдут мимо туннеля естественно.
        try {
            builder.addDisallowedApplication(packageName)
        } catch (e: Throwable) {
            Log.w(TAG, "addDisallowedApplication failed", e)
        }

        return builder.establish()
            ?: throw IllegalStateException("VpnService.Builder.establish() вернул null")
    }

    override fun onDestroy() {
        Log.i(TAG, "onDestroy")
        startJob?.cancel()
        scope.cancel()
        try {
            // Бест-эффорт остановка xray — если ещё работает, выключаем.
            kotlinx.coroutines.runBlocking { XrayBridge.stop() }
        } catch (_: Throwable) {
        }
        tun?.runCatching { close() }
        tun = null
        super.onDestroy()
    }

    // -- notification ---------------------------------------------------------

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(NOTIFICATION_CHANNEL) != null) return
        nm.createNotificationChannel(NotificationChannel(
            NOTIFICATION_CHANNEL,
            "KaproVPN статус",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Уведомление активного VPN-туннеля"
            setShowBadge(false)
        })
    }

    private fun buildNotification(sessionName: String, connecting: Boolean): Notification {
        val openAppIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            this, 1,
            Intent(this, KaproVpnService::class.java).apply { action = ACTION_DISCONNECT },
            PendingIntent.FLAG_IMMUTABLE,
        )

        return NotificationCompat.Builder(this, NOTIFICATION_CHANNEL)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(if (connecting) "KaproVPN — подключение…" else "KaproVPN — подключено")
            .setContentText(sessionName)
            .setOngoing(true)
            .setContentIntent(openAppIntent)
            .addAction(0, "Отключить", stopIntent)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }
}
