package pro.kaprovpn.android.vpn

import android.app.PendingIntent
import android.content.Intent
import android.graphics.drawable.Icon
import android.net.VpnService
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import pro.kaprovpn.android.MainActivity
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository

/**
 * Quick Settings tile: KaproVPN-плитка в системной шторке. Tap = toggle
 * VPN без открытия приложения. Состояние плитки синхронизировано с
 * [XrayBridge.state]: ACTIVE когда подключено, INACTIVE когда нет,
 * UNAVAILABLE когда нет активного конфига (нечего подключать).
 *
 * Чтобы плитка появилась — пользователь должен один раз добавить её
 * через системный UI Quick Settings (edit → drag KaproVPN tile в активный
 * блок). После этого она доступна на любом устройстве с API 24+.
 */
class VpnTileService : TileService() {

    /** Scope только пока плитка слушается (между onStartListening /
     *  onStopListening). На stop отменяем — не хотим утечек. */
    private var scope: CoroutineScope? = null
    private var stateJob: Job? = null

    override fun onStartListening() {
        super.onStartListening()
        // Хороший момент инициализировать репозиторий — TileService может
        // запускаться отдельным процессом до того как App.onCreate отработал.
        AppRepository.init(applicationContext)
        val s = CoroutineScope(SupervisorJob() + Dispatchers.Main)
        scope = s
        stateJob = s.launch {
            XrayBridge.state.collect { updateTile() }
        }
        updateTile()
    }

    override fun onStopListening() {
        stateJob?.cancel()
        scope?.cancel()
        scope = null
        super.onStopListening()
    }

    override fun onClick() {
        super.onClick()
        when (XrayBridge.state.value) {
            XrayBridge.State.Connected -> {
                Log.i(TAG, "tile click — disconnecting")
                KaproVpnService.stop(applicationContext)
            }
            XrayBridge.State.Idle, is XrayBridge.State.Failed -> {
                connectFromTile()
            }
            else -> {
                // Starting / Stopping — игнорим click чтобы не путать lifecycle.
                Log.i(TAG, "tile click during transient state — ignore")
            }
        }
    }

    /**
     * Логика старта из tile. Тонкий момент — `VpnService.prepare()` может
     * вернуть Intent (permission не выдан). Из tile показать system-dialog
     * нельзя напрямую — нужно открыть MainActivity, который у нас
     * registerForActivityResult'ит permission flow.
     */
    private fun connectFromTile() {
        if (AppRepository.activeConfig() == null) {
            Log.w(TAG, "tile click — нет активного конфига, открываю app")
            openMainActivity()
            return
        }
        val prepare = VpnService.prepare(applicationContext)
        if (prepare != null) {
            Log.i(TAG, "tile click — permission нужен, открываю app")
            openMainActivity()
            return
        }
        // Permission уже выдан — стартуем через null-intent path сервиса
        // (Phase 10), он сам подберёт конфиг из AppRepository.
        val svc = Intent(applicationContext, KaproVpnService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            applicationContext.startForegroundService(svc)
        } else {
            applicationContext.startService(svc)
        }
    }

    /** Открыть MainActivity, схлопнув шторку. API-shim вокруг breaking
     *  changes в Android 14 (startActivityAndCollapse). */
    @Suppress("DEPRECATION")
    private fun openMainActivity() {
        val intent = Intent(applicationContext, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14+: startActivityAndCollapse(PendingIntent)
            val pending = PendingIntent.getActivity(
                applicationContext, 0, intent,
                PendingIntent.FLAG_IMMUTABLE,
            )
            startActivityAndCollapse(pending)
        } else {
            // Android 7..13: startActivityAndCollapse(Intent) (depricated в 14)
            startActivityAndCollapse(intent)
        }
    }

    private fun updateTile() {
        val tile = qsTile ?: return
        val state = XrayBridge.state.value
        val activeConfig = AppRepository.activeConfig()

        // Icon follows the connection state: grey-K for idle, yellow-K
        // (animated by Tile.STATE_UNAVAILABLE itself is impossible — we use
        // the connecting drawable as a static "in-progress" hint), orange-K
        // for connected. System still tints based on Tile.state, but the
        // silhouette difference gives users an at-a-glance read.
        val iconRes = when {
            state is XrayBridge.State.Connected -> R.drawable.tile_connected
            state is XrayBridge.State.Starting ||
                state is XrayBridge.State.Stopping -> R.drawable.tile_connecting
            else -> R.drawable.tile_idle
        }
        tile.icon = Icon.createWithResource(this, iconRes)
        tile.label = getString(R.string.app_name)
        tile.state = when {
            activeConfig == null -> Tile.STATE_UNAVAILABLE
            state is XrayBridge.State.Connected -> Tile.STATE_ACTIVE
            state is XrayBridge.State.Starting ||
                state is XrayBridge.State.Stopping -> Tile.STATE_UNAVAILABLE
            else -> Tile.STATE_INACTIVE
        }
        // setSubtitle — API 29+. Покажем имя сервера или statе под label.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            tile.subtitle = when {
                activeConfig == null -> getString(R.string.tile_subtitle_no_config)
                state is XrayBridge.State.Connected -> activeConfig.name
                state is XrayBridge.State.Starting -> getString(R.string.tile_subtitle_starting)
                state is XrayBridge.State.Failed -> getString(R.string.tile_subtitle_failed)
                else -> getString(R.string.tile_subtitle_idle, activeConfig.name)
            }
        }
        tile.updateTile()
    }

    companion object {
        private const val TAG = "VpnTileService"
    }
}
