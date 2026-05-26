package pro.kaprovpn.android.vpn

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.VpnService
import android.util.Log
import androidx.core.content.ContextCompat
import pro.kaprovpn.android.core.AppRepository

/**
 * Слушает `ACTION_BOOT_COMPLETED` — система отправляет его после
 * разблокировки устройства. Если пользователь включил «Автоподключение
 * при запуске» И задал активный конфиг — стартуем VPN автоматически.
 *
 * Pre-условия:
 * - VpnService-разрешение уже выдано ранее через UI. Без него
 *   [VpnService.prepare] вернёт Intent, который мы не можем показать
 *   из BroadcastReceiver — стопаем тихо.
 * - Активный конфиг существует и валиден. Иначе — стопаем тихо.
 *
 * Always-on VPN: если он включён системно — устройство САМО стартует
 * наш сервис на boot (Phase 10). Наш receiver ничего не сделает потому
 * что [XrayBridge.state] уже будет Connected к моменту нашего invoke'а
 * — но если вдруг race, повторный startForegroundService безопасен:
 * сервис увидит свой текущий state и проигнорирует второй intent.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        // LOCKED_BOOT_COMPLETED приходит ДО разблокировки (для direct-boot
        // aware apps). Мы не direct-boot-aware (нужен Keystore для расшифровки
        // configs.json + filesDir доступен только после unlock). Ловим только
        // обычный BOOT_COMPLETED.
        if (action != Intent.ACTION_BOOT_COMPLETED) {
            Log.w(TAG, "ignoring action: $action")
            return
        }

        // Cold-process: убедимся что AppRepository проинициализирован.
        // Идемпотентно — если App.onCreate уже зашёл, no-op.
        AppRepository.init(context)

        val settings = AppRepository.settings.value
        if (!settings.autoconnectOnLaunch) {
            Log.i(TAG, "autoconnectOnLaunch выключен — skip")
            return
        }
        if (AppRepository.activeConfig() == null) {
            Log.i(TAG, "active config не задан — skip")
            return
        }
        if (VpnService.prepare(context) != null) {
            // Permission ещё не выдан или revoked. Из BroadcastReceiver
            // показать диалог нельзя. Пользователь должен будет открыть
            // приложение вручную.
            Log.w(TAG, "VPN permission не выдан — skip (нужно запустить app вручную)")
            return
        }

        // Сервис подхватит конфиг из AppRepository через null-intent путь
        // (Phase 10 Always-on logic). startForegroundService обязателен
        // на Android 8+, иначе ANR при первом startForeground вызове.
        val svc = Intent(context, KaproVpnService::class.java)
        ContextCompat.startForegroundService(context, svc)
        Log.i(TAG, "started VPN service on boot")
    }

    companion object {
        private const val TAG = "BootReceiver"
    }
}
