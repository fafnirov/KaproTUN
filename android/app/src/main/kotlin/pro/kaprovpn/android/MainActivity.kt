package pro.kaprovpn.android

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.core.content.ContextCompat
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

    /** Compose-observable state for our explainer overlay — set true when the
     *  user permanently denied POST_NOTIFICATIONS and the only way out is the
     *  system settings screen. */
    private var showNotificationExplainer by mutableStateOf(false)

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

    /**
     * Android 13+ runtime permission для POST_NOTIFICATIONS. Запрашиваем
     * один раз перед первым connect: без него foreground service нашего
     * VPN'а либо вообще не покажет нотификацию, либо система может его
     * приоритизировать ниже и убить быстрее. Если пользователь откажет —
     * подключаемся всё равно, но запоминаем чтобы показать explainer.
     */
    private val notificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (!granted && shouldShowExplainerAfterDenial()) {
            // "Don't ask again" — система больше не покажет диалог, только
            // юзер вручную может перевключить разрешение. Показываем
            // объяснение с deeplink в настройки.
            showNotificationExplainer = true
            // НЕ продолжаем с connect здесь — пусть пользователь сначала
            // разберётся с разрешением. На dismiss диалога продолжим.
        } else {
            // Granted, или "denied once" (юзер сможет переспросить позже).
            // В обоих случаях продолжаем — нотификация просто будет
            // недоступна и foreground service её не покажет.
            proceedToVpnPermission()
        }
    }

    /** Перед запросом VPN-разрешения сначала просим POST_NOTIFICATIONS
     *  (только на Android 13+; на старых версиях разрешение granted-by-default). */
    private fun connectWith(configJson: String, sessionName: String, dnsOption: DnsOption) {
        pending = PendingConnect(configJson, sessionName, dnsOption)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val already = ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            if (already) {
                proceedToVpnPermission()
            } else {
                notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        } else {
            // Pre-Android-13: разрешение auto-granted из manifest'а.
            proceedToVpnPermission()
        }
    }

    private fun proceedToVpnPermission() {
        val request = pending ?: return
        val prepareIntent: Intent? = VpnService.prepare(this)
        if (prepareIntent == null) {
            pending = null
            launchService(request)
        } else {
            // Pending остаётся для vpnPermissionLauncher callback'а.
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

    /**
     * "Don't ask again" detection: если разрешение denied И система говорит
     * НЕ показывать rationale — значит юзер выбрал «больше не спрашивать».
     * Возможно только когда мы целенаправленно вызывали request (то есть
     * не на холодном старте).
     */
    private fun shouldShowExplainerAfterDenial(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return false
        return !shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS)
    }

    /** Deeplink в системные настройки app info — единственный путь
     *  обратно к permission toggle после "Don't ask again". */
    private fun openAppInfoSettings() {
        val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
            data = Uri.fromParts("package", packageName, null)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        runCatching { startActivity(intent) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            KaproVpnTheme {
                Box(modifier = Modifier.fillMaxSize()) {
                    pro.kaprovpn.android.ui.AppNav(
                        onConnect = ::connectWith,
                        onDisconnect = ::disconnect,
                    )
                    if (showNotificationExplainer) {
                        AlertDialog(
                            onDismissRequest = {
                                showNotificationExplainer = false
                                // Пользователь свайпнул диалог — всё равно
                                // продолжаем connect (нотификации не будет,
                                // но сервис в худшем случае поднимется).
                                proceedToVpnPermission()
                            },
                            title = { Text(stringResource(R.string.notif_perm_title)) },
                            text = { Text(stringResource(R.string.notif_perm_body)) },
                            confirmButton = {
                                TextButton(onClick = {
                                    showNotificationExplainer = false
                                    openAppInfoSettings()
                                    // Pending forget: когда юзер вернётся из
                                    // настроек, ему надо будет тапнуть CONNECT
                                    // снова. Мы переспросим разрешение и
                                    // подключим. Иначе pending висел бы во
                                    // время длительной отлучки в системные
                                    // настройки.
                                    pending = null
                                }) {
                                    Text(stringResource(R.string.notif_perm_open_settings))
                                }
                            },
                            dismissButton = {
                                TextButton(onClick = {
                                    showNotificationExplainer = false
                                    proceedToVpnPermission()
                                }) {
                                    Text(stringResource(R.string.notif_perm_connect_anyway))
                                }
                            },
                        )
                    }
                }
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
