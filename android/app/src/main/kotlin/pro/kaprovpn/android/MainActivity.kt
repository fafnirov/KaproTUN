package pro.kaprovpn.android

import android.app.Activity
import android.content.Intent
import android.net.VpnService
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.DnsOption
import pro.kaprovpn.android.ui.theme.KaproVpnTheme
import pro.kaprovpn.android.vpn.KaproVpnService
import pro.kaprovpn.android.vpn.XrayBridge

class MainActivity : ComponentActivity() {

    /**
     * Pending request — то что хотим подключить, как только пользователь
     * нажмёт «Разрешить VPN» в системном диалоге. На Android разрешение
     * выдаётся одно на навсегда (или до удаления приложения / отзыва в
     * Settings), поэтому в типичном случае мы сюда не попадаем — только
     * при первом подключении.
     */
    private data class PendingConnect(
        val configJson: String,
        val sessionName: String,
        val dnsOption: DnsOption,
    )

    private var pending: PendingConnect? = null

    /**
     * Launcher для запроса VPN-разрешения. [VpnService.prepare] возвращает
     * Intent, который мы должны запустить как activity-for-result; пользователь
     * увидит системный диалог "Разрешить KaproVPN устанавливать VPN-соединения?".
     */
    private val vpnPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val p = pending
        pending = null
        if (result.resultCode == Activity.RESULT_OK && p != null) {
            launchService(p)
        }
    }

    /** Колбэк для UI: либо запрос разрешения, либо сразу старт сервиса. */
    private fun connectWith(configJson: String, sessionName: String, dnsOption: DnsOption) {
        val request = PendingConnect(configJson, sessionName, dnsOption)
        val prepareIntent: Intent? = VpnService.prepare(this)
        if (prepareIntent == null) {
            launchService(request)
        } else {
            pending = request
            vpnPermissionLauncher.launch(prepareIntent)
        }
    }

    private fun launchService(p: PendingConnect) {
        KaproVpnService.start(
            context = this,
            configJson = p.configJson,
            sessionName = p.sessionName,
            tunDnsServers = p.dnsOption.plainServers,
            dnsBypassIps = p.dnsOption.bypassIps,
        )
    }

    private fun disconnect() {
        KaproVpnService.stop(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            KaproVpnTheme {
                pro.kaprovpn.android.ui.AppNav(
                    onConnect = ::connectWith,
                    onDisconnect = ::disconnect,
                )
            }
        }
        // Только при холодном старте — savedInstanceState == null значит
        // Activity не пересоздаётся (rotation / back-to-foreground после
        // process kill). Иначе пользователь словил бы reconnect на каждом
        // повороте экрана.
        if (savedInstanceState == null) maybeAutoconnect()
    }

    /**
     * Если у пользователя включено «Автоподключение при запуске» и есть
     * активный конфиг — стартуем туннель сразу. Skip:
     *   - если toggle off,
     *   - если active config не задан (новая установка),
     *   - если VPN уже подключён (race с Always-on или предыдущим запуском).
     *
     * Permission flow — общий [connectWith], system-диалог покажется только
     * при первом autoconnect после установки.
     */
    private fun maybeAutoconnect() {
        val settings = AppRepository.settings.value
        if (!settings.autoconnectOnLaunch) return
        if (XrayBridge.state.value is XrayBridge.State.Connected) return
        val built = AppRepository.buildActiveConfigJson() ?: return
        val (json, name) = built
        connectWith(json, name, AppRepository.dnsOption())
    }
}
