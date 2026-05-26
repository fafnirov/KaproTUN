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
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.DnsOption

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
        const val EXTRA_DNS_PLAIN_SERVERS = "dns_plain_servers"
        const val EXTRA_DNS_BYPASS_IPS = "dns_bypass_ips"

        private const val NOTIFICATION_ID = 0xC001
        private const val NOTIFICATION_CHANNEL = "vpn_status"

        // TUN параметры — повторяют десктоп (controller.py)
        private const val TUN_LOCAL_ADDR = "10.255.0.2"
        private const val TUN_PREFIX = 30
        private const val TUN_MTU = 1500
        // Безопасный fallback DNS (когда пользователь не выбрал кастомный):
        // Yandex Public + Cloudflare. Yandex первый — лучше для RU-сайтов.
        private val DEFAULT_TUN_DNS = listOf("77.88.8.8", "1.1.1.1")

        fun start(
            context: Context,
            configJson: String,
            sessionName: String,
            tunDnsServers: List<String> = emptyList(),
            dnsBypassIps: List<String> = emptyList(),
        ) {
            val intent = Intent(context, KaproVpnService::class.java).apply {
                action = ACTION_CONNECT
                putExtra(EXTRA_CONFIG_JSON, configJson)
                putExtra(EXTRA_SESSION_NAME, sessionName)
                putStringArrayListExtra(EXTRA_DNS_PLAIN_SERVERS, ArrayList(tunDnsServers))
                putStringArrayListExtra(EXTRA_DNS_BYPASS_IPS, ArrayList(dnsBypassIps))
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
    private var stateObserver: Job? = null
    private var currentSessionName: String = "KaproVPN"

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        // Notification следует state'у Xray live: connect → connecting →
        // connected → (если упало) failed. Без этого notification врал
        // бы что подключено когда xray умер посередине сессии.
        stateObserver = scope.launch {
            XrayBridge.state.collect { state ->
                val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
                // Не вызываем startForeground — только update existing
                // notification. (Изначальный startForeground делается
                // в connect() перед buildTun.)
                if (state !is XrayBridge.State.Idle) {
                    nm.notify(NOTIFICATION_ID, buildNotification(currentSessionName, state))
                }
            }
        }
        Log.i(TAG, "onCreate")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CONNECT -> {
                val config = intent.getStringExtra(EXTRA_CONFIG_JSON)
                val name = intent.getStringExtra(EXTRA_SESSION_NAME) ?: "KaproVPN"
                val tunDns = intent.getStringArrayListExtra(EXTRA_DNS_PLAIN_SERVERS).orEmpty()
                val dnsBypassIps = intent.getStringArrayListExtra(EXTRA_DNS_BYPASS_IPS).orEmpty()
                if (config.isNullOrBlank()) {
                    Log.e(TAG, "ACTION_CONNECT без конфига — игнорю")
                    stopSelf()
                    return START_NOT_STICKY
                }
                connect(config, name, tunDns, dnsBypassIps)
            }
            ACTION_DISCONNECT -> {
                Log.i(TAG, "ACTION_DISCONNECT")
                disconnect()
            }
            else -> {
                // Null intent / no action = system-initiated start. Чаще всего
                // это Always-on VPN: пользователь включил «Always-on VPN» +
                // «Block connections without VPN» в системных настройках, и
                // система сама стартует наш сервис при загрузке устройства
                // или после краша. Восстанавливаем последний активный конфиг
                // из AppRepository.
                Log.i(TAG, "system-initiated start (Always-on?) — рестор active config")
                val built = AppRepository.buildActiveConfigJson()
                if (built == null) {
                    Log.w(TAG, "system-initiated старт, но active config не задан — стопаем")
                    stopSelf()
                    return START_NOT_STICKY
                }
                val (configJson, sessionName) = built
                val dns = AppRepository.dnsOption()
                connect(configJson, sessionName, dns.plainServers, dns.bypassIps)
                // REDELIVER_INTENT: если процесс убьют после старта, система
                // перезапустит с тем же (null) intent → этот же код опять
                // подхватит active config.
                return START_REDELIVER_INTENT
            }
        }
        return START_NOT_STICKY
    }

    /**
     * Вызывается системой когда пользователь отзывает VPN-разрешение
     * через системные настройки (Network → VPN → "Forget" / "Disconnect")
     * или когда другой VPN-клиент захватывает permission на нашем месте.
     *
     * Default-имплементация VpnService просто закрывает TUN-fd, но мы
     * хотим явно остановить xray, иначе он остаётся живым с уже мёртвым
     * TUN'ом и spam'ит ошибками. См. Phase 10.
     */
    override fun onRevoke() {
        Log.i(TAG, "onRevoke — система отозвала VPN permission")
        disconnect()
        super.onRevoke()
    }

    private fun connect(
        configJson: String,
        sessionName: String,
        tunDns: List<String>,
        dnsBypassIps: List<String>,
    ) {
        // Перепроверка прав на всякий случай — VpnService.prepare() уже должна
        // была быть вызвана в UI, но pre-Android-13 разрешение могло истечь.
        if (prepare(this) != null) {
            Log.e(TAG, "VpnService.prepare() требует UI-разрешение — стопаем")
            stopSelf()
            return
        }

        currentSessionName = sessionName
        startForeground(
            NOTIFICATION_ID,
            buildNotification(sessionName, XrayBridge.State.Starting),
        )

        startJob = scope.launch {
            val pfd: ParcelFileDescriptor = try {
                buildTun(sessionName, tunDns, dnsBypassIps)
            } catch (e: Throwable) {
                Log.e(TAG, "TUN setup failed", e)
                stopWithError("Не удалось создать TUN: ${e.message}")
                return@launch
            }
            tun = pfd

            // ВАЖНО — берём `getFd()` НЕ `detachFd()`, потому что
            // hev-socks5-tunnel (и sockstun из которого мы вытащили .so)
            // полагается на то что fd валиден пока pfd живёт. detachFd
            // оставляет fd "ничейным" в Java-плоскости, native lib мог
            // получить его и сразу exit'нуть как «invalid fd».
            // pfd закрываем сами при disconnect — это и закрывает fd.
            val tunFd = pfd.fd
            Log.i(TAG, "TUN established fd=$tunFd")

            // Шаг 1 — стартуем xray-core (SOCKS5 inbound на 127.0.0.1:2081).
            // Передаём tunFd=0 потому что libv2ray всё равно не читает TUN.
            try {
                XrayBridge.start(configJson, 0)
            } catch (e: Throwable) {
                Log.e(TAG, "XrayBridge.start failed", e)
                stopWithError("Xray не стартовал: ${e.message}")
                return@launch
            }

            // Шаг 2 — стартуем tun2socks (hev-socks5-tunnel). Он читает
            // packets из TUN-fd и форвардит их в xray's SOCKS5 inbound
            // на 127.0.0.1:2081. БЕЗ него xray запущен, но никто его
            // SOCKS5 не дёргает — TUN-fd накапливает пакеты впустую.
            try {
                HevTunnel.start(applicationContext, tunFd)
                Log.i(TAG, "HevTunnel.start OK — TUN packets forwarding via SOCKS5")
            } catch (e: Throwable) {
                Log.e(TAG, "HevTunnel.start failed", e)
                stopWithError("tun2socks не стартовал: ${e.message}")
                return@launch
            }
        }
    }

    private fun disconnect() {
        scope.launch {
            // Останавливаем в обратном порядке от start'а: сначала tun2socks
            // (чтобы он перестал читать с TUN и слать в SOCKS5), потом xray
            // (некому больше держать SOCKS-сессии), потом закрываем TUN-fd.
            try {
                HevTunnel.stop()
            } catch (e: Throwable) {
                Log.w(TAG, "HevTunnel.stop failed (ignored)", e)
            }
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
        try { HevTunnel.stop() } catch (_: Throwable) {}
        tun?.runCatching { close() }
        tun = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun buildTun(
        sessionName: String,
        tunDns: List<String>,
        dnsBypassIps: List<String>,
    ): ParcelFileDescriptor {
        val builder = Builder()
            .setSession(sessionName)
            .setMtu(TUN_MTU)
            .addAddress(TUN_LOCAL_ADDR, TUN_PREFIX)
            // Phase 3 MVP: туннелируем ВСЁ. Split-routing (direct-list) приедет
            // в Phase 4 — там будем резолвить direct-домены в IP и добавлять
            // bypass-маршруты для них.
            .addRoute("0.0.0.0", 0)

        // DNS на TUN — либо выбранный пользователем (DnsOption.plainServers),
        // либо безопасный fallback (Yandex + Cloudflare).
        val dnsToSet = tunDns.ifEmpty { DEFAULT_TUN_DNS }
        dnsToSet.forEach { builder.addDnsServer(it) }

        // DNS bypass IPs — добавляем как НЕ-туннелируемые маршруты. Идея:
        // если пользователь выбрал, скажем, Cloudflare DoH, то 1.1.1.1
        // должен идти мимо туннеля (иначе DoH-over-443 от Chrome'а делает
        // ненужный круг через VPN-сервер). Реализация Android — через
        // exclude-routes когда мы туннелируем 0.0.0.0/0.
        //
        // ВНИМАНИЕ: VpnService.Builder.excludeRoute существует только с
        // API 33 (Android 13). На минимуме нашего API 24 такой возможности
        // нет — DNS bypass для < API 33 делается только через xray routing
        // (см. XrayConfigBuilder, там уже всё есть). Эти exclude-routes —
        // дополнительный слой защиты на Android 13+.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            for (ip in dnsBypassIps) {
                try {
                    builder.excludeRoute(android.net.IpPrefix(
                        java.net.InetAddress.getByName(ip), 32
                    ))
                } catch (e: Throwable) {
                    Log.w(TAG, "excludeRoute $ip failed", e)
                }
            }
        }

        // Защищаемся от петли: исключаем сами себя из туннеля. Без этого
        // xray-исходящий трафик улетел бы обратно в TUN → бесконечная
        // петля → timeout. addDisallowedApplication работает только для
        // нашего own UID — Xray-сокеты пойдут мимо туннеля естественно.
        try {
            builder.addDisallowedApplication(packageName)
        } catch (e: Throwable) {
            Log.w(TAG, "addDisallowedApplication failed", e)
        }

        // Per-app split tunneling. Skip our own package (it's already added
        // above) and silently skip uninstalled / unknown packages — happens
        // when the user excluded an app and then uninstalled it before
        // reconnecting. NameNotFoundException would refuse to establish()
        // the whole TUN otherwise.
        val excluded = AppRepository.excludedPackages() - packageName
        var excludedApplied = 0
        for (pkg in excluded) {
            try {
                builder.addDisallowedApplication(pkg)
                excludedApplied++
            } catch (_: android.content.pm.PackageManager.NameNotFoundException) {
                Log.w(TAG, "excluded package not installed, skipping: $pkg")
            } catch (e: Throwable) {
                Log.w(TAG, "addDisallowedApplication($pkg) failed", e)
            }
        }
        if (excludedApplied > 0) {
            Log.i(TAG, "per-app split: $excludedApplied package(s) bypass the VPN")
        }

        return builder.establish()
            ?: throw IllegalStateException("VpnService.Builder.establish() вернул null")
    }

    override fun onDestroy() {
        Log.i(TAG, "onDestroy")
        startJob?.cancel()
        stateObserver?.cancel()
        scope.cancel()
        // Best-effort cleanup. tun2socks first (читал TUN), потом xray.
        try { HevTunnel.stop() } catch (_: Throwable) {}
        try {
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
            getString(R.string.vpn_notification_channel),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.vpn_notification_channel_desc)
            setShowBadge(false)
        })
    }

    private fun buildNotification(sessionName: String, state: XrayBridge.State): Notification {
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

        val titleRes = when (state) {
            XrayBridge.State.Starting -> R.string.vpn_notification_connecting
            XrayBridge.State.Connected -> R.string.vpn_notification_connected
            XrayBridge.State.Stopping -> R.string.vpn_notification_stopping
            is XrayBridge.State.Failed -> R.string.vpn_notification_failed
            XrayBridge.State.Idle -> R.string.vpn_notification_connected  // транзитное, скоро уйдёт
        }
        val text = when (state) {
            is XrayBridge.State.Failed -> state.reason
            else -> sessionName
        }

        return NotificationCompat.Builder(this, NOTIFICATION_CHANNEL)
            // setSmallIcon REQUIRES a single-colour alpha-only drawable
            // (Android tints it from the channel/theme). The K-silhouette in
            // ic_stat_notification is produced from tray_idle-source.png by
            // scripts/generate_android_assets.py — colour stripped, alpha
            // preserved. Using the full @mipmap/ic_launcher here would show
            // as a flat grey square on Android 5+.
            .setSmallIcon(R.drawable.ic_stat_notification)
            .setContentTitle(getString(titleRes))
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(openAppIntent)
            .addAction(0, getString(R.string.vpn_notification_disconnect), stopIntent)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }
}
