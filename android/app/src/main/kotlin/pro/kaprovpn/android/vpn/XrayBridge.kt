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

    /** Сериализует start/stop — два паралельных start'а сломали бы порт SOCKS5. */
    private val lifecycleMutex = Mutex()

    @Volatile private var controller: CoreController? = null
    @Volatile private var initialized: Boolean = false
    @Volatile private var envDir: File? = null

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

    /** Per-outbound bandwidth counter, в байтах за всё время этой сессии. */
    fun queryStats(tag: String, link: String = "uplink"): Long = try {
        controller?.queryStats(tag, link) ?: 0L
    } catch (_: Throwable) {
        0L
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
