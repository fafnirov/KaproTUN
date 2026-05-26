package pro.kaprovpn.android.vpn

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import libv2ray.CoreCallbackHandler
import libv2ray.CoreController
import libv2ray.Libv2ray
import java.io.File

/**
 * Идиоматичная Kotlin-обёртка вокруг libv2ray ([Libv2ray] + [CoreController]).
 *
 * Архитектура:
 * - **Singleton.** Xray-core должен быть один на процесс (иначе порты,
 *   реквесты статистики и логи перепутаются). Соглашение: используем
 *   только этот object'.
 * - **Состояние.** Хранится в [state] (StateFlow). UI подписывается.
 * - **Логи xray.** Эмитятся в [logs] как [LogLine].
 * - **`startLoop(...)` блокирующий.** Зовётся через [start] suspend-функцию
 *   с переключением на Dispatchers.IO + Mutex'ом для сериализации.
 *
 * Соответствие десктоп-клиенту: эквивалент `core/xray_process.py` (запуск/
 * остановка xray) + часть `core/controller.py` (state-machine). TUN-routing
 * делается в [KaproVpnService], не здесь.
 */
object XrayBridge {

    private const val TAG = "XrayBridge"

    // Имена geoip/geosite файлов в AAR-assets совпадают с теми что Xray ищет
    // в env-dir. Они мержатся в наш app/src/main/assets/ через AGP при сборке.
    private val GEOIP_FILES = listOf("geoip.dat", "geosite.dat")

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state.asStateFlow()

    private val _logs = MutableSharedFlow<LogLine>(replay = 256, extraBufferCapacity = 256)
    val logs: SharedFlow<LogLine> = _logs.asSharedFlow()

    private val _traffic = MutableStateFlow(TrafficSnapshot.ZERO)
    /**
     * Bandwidth-снепшот текущей сессии: ↑↓ totals + текущая скорость.
     * Обновляется только когда UI зовёт [sampleTraffic] (опрос pull-model
     * раз в секунду из HomeScreen). На [start] сбрасывается в ZERO.
     */
    val traffic: StateFlow<TrafficSnapshot> = _traffic.asStateFlow()

    /** Сериализует start/stop — два паралельных start'а сломали бы порт SOCKS5. */
    private val lifecycleMutex = Mutex()

    @Volatile private var controller: CoreController? = null
    @Volatile private var initialized: Boolean = false
    @Volatile private var envDir: File? = null
    @Volatile private var lastSampleAt: Long = 0L

    // -- public API -----------------------------------------------------------

    /** Версия Xray-core. Не требует [init]. */
    fun coreVersion(): String = try {
        Libv2ray.checkVersionX()
    } catch (e: Throwable) {
        Log.e(TAG, "checkVersionX failed", e)
        "unknown (${e.javaClass.simpleName})"
    }

    /**
     * Одноразовая инициализация рантайма. Зовётся из [App.onCreate].
     * Идемпотентна. Делает:
     *   1. Создаёт `<filesDir>/xray/` под рантайм-state.
     *   2. Распаковывает `geoip.dat`/`geosite.dat` из app-assets если их там нет.
     *   3. Зовёт [Libv2ray.initCoreEnv].
     */
    @Synchronized
    fun init(context: Context) {
        if (initialized) return
        val ctx = context.applicationContext
        try {
            val dir = File(ctx.filesDir, "xray").apply { mkdirs() }
            envDir = dir
            extractGeoipAssets(ctx, dir)
            // Второй параметр key — внутренняя соль libv2ray; пустая строка ок.
            Libv2ray.initCoreEnv(dir.absolutePath, "")
            initialized = true
            Log.i(TAG, "initCoreEnv → ${dir.absolutePath}")
        } catch (e: Throwable) {
            Log.e(TAG, "initCoreEnv failed", e)
            // Не бросаем — coreVersion() будет ещё работать. start() явно
            // выкинет ConnectionException если инит сломан.
        }
    }

    /**
     * Запустить xray-core с переданной [config] и [tunFd] из VpnService.
     *
     * [tunFd] — файловый дескриптор TUN-интерфейса, libv2ray читает с него
     * пакеты и пишет ответы обратно (TUN-mode внутри Go-кода, без отдельного
     * tun2socks). Передавать `0` для HTTP-proxy-режима (не сейчас).
     *
     * Блокирует до полного старта или ошибки. Бросает [ConnectionException]
     * с понятным сообщением — UI может показать.
     */
    suspend fun start(config: String, tunFd: Int) = lifecycleMutex.withLock {
        if (!initialized) {
            throw ConnectionException("Xray runtime не инициализирован — restart приложения")
        }
        if (controller?.isRunning == true) {
            throw ConnectionException("Уже подключено — сначала отключись")
        }
        _state.value = State.Starting
        _traffic.value = TrafficSnapshot.ZERO
        lastSampleAt = 0L
        withContext(Dispatchers.IO) {
            try {
                val c = Libv2ray.newCoreController(callbackHandler)
                c.startLoop(config, tunFd)
                controller = c
                _state.value = State.Connected
                Log.i(TAG, "startLoop OK, tunFd=$tunFd")
            } catch (e: Throwable) {
                Log.e(TAG, "startLoop failed", e)
                controller = null
                val reason = e.message ?: e.javaClass.simpleName
                _state.value = State.Failed(reason)
                throw ConnectionException("Xray не стартовал: $reason", e)
            }
        }
    }

    /** Остановить xray. Идемпотентна — на Idle no-op. */
    suspend fun stop() = lifecycleMutex.withLock {
        val c = controller
        if (c == null || !c.isRunning) {
            _state.value = State.Idle
            return@withLock
        }
        _state.value = State.Stopping
        withContext(Dispatchers.IO) {
            try {
                c.stopLoop()
                Log.i(TAG, "stopLoop OK")
            } catch (e: Throwable) {
                Log.w(TAG, "stopLoop failed (ignored)", e)
            }
            controller = null
            _state.value = State.Idle
        }
    }

    /**
     * Per-outbound bandwidth counter из libv2ray. **ВНИМАНИЕ:** в
     * AndroidLibXrayLite каждый вызов atomic-swap'ает счётчик в 0 — то есть
     * возвращает дельту с предыдущего вызова, а не running total. UI код
     * должен сам аккумулировать (см. [sampleTraffic]).
     */
    fun queryStats(tag: String, link: String = "uplink"): Long = try {
        controller?.queryStats(tag, link) ?: 0L
    } catch (_: Throwable) {
        0L
    }

    /**
     * Опросить libv2ray, посчитать speed относительно прошлого вызова,
     * обновить [traffic]. Зовётся UI'ем ~раз в секунду пока сессия активна.
     *
     * `proxy` — outbound-tag из [XrayConfigBuilder] (см. строки
     * `put("tag", "proxy")` в каждом proto-конвертере). Считаем только его
     * — direct/block нам неинтересны, пользователю важна цифра «через VPN».
     */
    fun sampleTraffic() {
        val c = controller
        if (c == null || !c.isRunning) {
            // Не зануляем traffic — пусть UI покажет последний snapshot после
            // disconnect. На следующем start() он сбрасывается в ZERO явно.
            return
        }
        val now = System.currentTimeMillis()
        val deltaUp = queryStats("proxy", "uplink").coerceAtLeast(0L)
        val deltaDown = queryStats("proxy", "downlink").coerceAtLeast(0L)
        val prev = _traffic.value
        val intervalMs = if (lastSampleAt == 0L) 1000L else (now - lastSampleAt).coerceAtLeast(1L)
        val uplinkBps = deltaUp * 1000L / intervalMs
        val downlinkBps = deltaDown * 1000L / intervalMs
        lastSampleAt = now
        _traffic.value = TrafficSnapshot(
            uplinkTotal = prev.uplinkTotal + deltaUp,
            downlinkTotal = prev.downlinkTotal + deltaDown,
            uplinkBps = uplinkBps,
            downlinkBps = downlinkBps,
        )
    }

    /**
     * Замерить TCP-handshake latency для [configJson] (полный Xray-конфиг).
     * Использует статический [Libv2ray.measureOutboundDelay] — не требует
     * активной сессии (стартует мини-pipeline под капотом).
     *
     * @param testUrl что пинговать. `generate_204` — стандарт для
     *   latency-проб: возвращает 204 No Content, маленький и быстрый.
     * @return latency в миллисекундах. Бросает [Exception] при любой
     *   ошибке (timeout, DNS, конфиг невалиден).
     */
    suspend fun measureDelay(
        configJson: String,
        testUrl: String = "https://www.google.com/generate_204",
    ): Long = withContext(Dispatchers.IO) {
        Libv2ray.measureOutboundDelay(configJson, testUrl)
    }

    // -- types ----------------------------------------------------------------

    sealed class State {
        object Idle : State()
        object Starting : State()
        object Connected : State()
        object Stopping : State()
        data class Failed(val reason: String) : State()
    }

    data class LogLine(val severity: Int, val message: String)

    /**
     * Bandwidth-замер сессии. `*Total` — всё что прошло через outbound `proxy`
     * с момента последнего [start]. `*Bps` — байты в секунду за последний
     * интервал между [sampleTraffic]-вызовами (≈1с).
     */
    data class TrafficSnapshot(
        val uplinkTotal: Long,
        val downlinkTotal: Long,
        val uplinkBps: Long,
        val downlinkBps: Long,
    ) {
        companion object {
            val ZERO = TrafficSnapshot(0L, 0L, 0L, 0L)
        }
    }

    class ConnectionException(message: String, cause: Throwable? = null) :
        RuntimeException(message, cause)

    // -- internals ------------------------------------------------------------

    private val callbackHandler = object : CoreCallbackHandler {
        override fun startup(): Long = 0L
        override fun shutdown(): Long = 0L
        override fun onEmitStatus(severity: Long, message: String): Long {
            _logs.tryEmit(LogLine(severity.toInt(), message))
            return 0L
        }
    }

    /** Достаёт geoip/geosite из мержнутых assets в env-папку. Идемпотентно. */
    private fun extractGeoipAssets(context: Context, envDir: File) {
        for (name in GEOIP_FILES) {
            val target = File(envDir, name)
            if (target.exists() && target.length() > 0) {
                Log.d(TAG, "$name уже в env-dir (${target.length()} bytes) — skip")
                continue
            }
            try {
                context.assets.open(name).use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
                Log.i(TAG, "extracted $name (${target.length()} bytes)")
            } catch (e: Throwable) {
                Log.w(TAG, "extract $name failed (xray попробует встроенные defaults)", e)
            }
        }
    }
}
