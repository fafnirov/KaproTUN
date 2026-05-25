package pro.kaprovpn.android

import android.app.Activity
import android.content.Intent
import android.net.VpnService
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import pro.kaprovpn.android.ui.HomeScreen
import pro.kaprovpn.android.ui.theme.KaproVpnTheme
import pro.kaprovpn.android.vpn.KaproVpnService

class MainActivity : ComponentActivity() {

    /**
     * Pending config — то, что хотим подключить, как только пользователь
     * нажмёт «Разрешить VPN» в системном диалоге. На Android разрешение
     * выдаётся одно на навсегда (или до удаления приложения / отзыва в
     * Settings), поэтому в типичном случае мы сюда не попадаем — только
     * при первом подключении.
     */
    private var pendingConfig: Pair<String, String>? = null

    /**
     * Launcher для запроса VPN-разрешения. [VpnService.prepare] возвращает
     * Intent, который мы должны запустить как activity-for-result; пользователь
     * увидит системный диалог "Разрешить KaproVPN устанавливать VPN-соединения?".
     */
    private val vpnPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val pending = pendingConfig
        pendingConfig = null
        if (result.resultCode == Activity.RESULT_OK && pending != null) {
            val (configJson, sessionName) = pending
            KaproVpnService.start(this, configJson, sessionName)
        }
    }

    /** Колбэк для UI: либо запрос разрешения, либо сразу старт сервиса. */
    private fun connectWith(configJson: String, sessionName: String) {
        val prepareIntent: Intent? = VpnService.prepare(this)
        if (prepareIntent == null) {
            KaproVpnService.start(this, configJson, sessionName)
        } else {
            pendingConfig = configJson to sessionName
            vpnPermissionLauncher.launch(prepareIntent)
        }
    }

    private fun disconnect() {
        KaproVpnService.stop(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            KaproVpnTheme {
                HomeScreen(
                    onConnect = ::connectWith,
                    onDisconnect = ::disconnect,
                )
            }
        }
    }
}
