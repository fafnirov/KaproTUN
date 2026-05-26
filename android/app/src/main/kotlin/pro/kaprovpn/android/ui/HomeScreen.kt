package pro.kaprovpn.android.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowRight
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.DnsOption
import pro.kaprovpn.android.core.Storage
import pro.kaprovpn.android.core.XrayConfigBuilder
import pro.kaprovpn.android.core.serverHostPort
import pro.kaprovpn.android.vpn.XrayBridge

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    modifier: Modifier = Modifier,
    onConnect: (configJson: String, sessionName: String, dnsOption: DnsOption) -> Unit,
    onDisconnect: () -> Unit,
    onAddFirstConfig: () -> Unit = {},
    onPickConfig: () -> Unit = onAddFirstConfig,  // тап по config-card → Servers tab
) {
    val context = LocalContext.current
    val xrayState by XrayBridge.state.collectAsState()
    val traffic by XrayBridge.traffic.collectAsState()
    val configs by AppRepository.configs.collectAsState()
    val settings by AppRepository.settings.collectAsState()
    val directSites = remember { Storage.loadDefaultSites(context) }

    val activeConfig = remember(configs, settings) {
        settings.activeConfigName?.let { name -> configs.find { it.name == name } }
    }
    val dnsOption = remember(settings) { DnsOption.get(settings.dnsOptionKey) }

    val isConnected = xrayState is XrayBridge.State.Connected
    val isBusy = xrayState is XrayBridge.State.Starting || xrayState is XrayBridge.State.Stopping
    var lastError by remember { mutableStateOf<String?>(null) }

    // Uptime: track момент когда стали Connected и тикаем каждую секунду.
    // На disconnect — ресетим.
    var connectedSince by remember { mutableStateOf(0L) }
    var uptimeText by remember { mutableStateOf("") }

    LaunchedEffect(xrayState) {
        if (xrayState is XrayBridge.State.Connected) {
            if (connectedSince == 0L) connectedSince = System.currentTimeMillis()
            while (true) {
                val elapsed = (System.currentTimeMillis() - connectedSince) / 1000
                uptimeText = formatUptime(elapsed)
                // Pull-семплинг bandwidth: дёргаем queryStats и пушим snapshot
                // в XrayBridge.traffic. Раз в секунду — достаточно для UI
                // (быстрее даёт нервно дрожащие цифры), не нагружает JNI.
                XrayBridge.sampleTraffic()
                delay(1000L)
            }
        } else {
            connectedSince = 0L
            uptimeText = ""
        }
    }

    val connectState = remember(xrayState) {
        when (xrayState) {
            XrayBridge.State.Idle -> ConnectState.Idle
            XrayBridge.State.Starting -> ConnectState.Starting
            XrayBridge.State.Connected -> ConnectState.Connected
            XrayBridge.State.Stopping -> ConnectState.Stopping
            is XrayBridge.State.Failed -> ConnectState.Failed
        }
    }

    Scaffold(
        modifier = modifier,
        topBar = {
            CenterAlignedTopAppBar(
                title = {
                    Text(
                        stringResource(R.string.app_name),
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.SemiBold,
                    )
                },
            )
        },
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .padding(horizontal = 24.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Spacer(Modifier.weight(1f))

            // Главный элемент — большая круглая кнопка
            ConnectButton(
                state = connectState,
                onClick = {
                    if (isConnected) {
                        onDisconnect()
                        return@ConnectButton
                    }
                    if (isBusy) return@ConnectButton
                    val cfg = activeConfig
                    if (cfg == null) {
                        onAddFirstConfig()
                        return@ConnectButton
                    }
                    try {
                        val json = XrayConfigBuilder.buildConfigJson(
                            proxy = cfg,
                            directDomains = directSites,
                            dnsOption = dnsOption,
                        )
                        lastError = null
                        onConnect(json, cfg.name, dnsOption)
                    } catch (e: Throwable) {
                        lastError = context.getString(R.string.home_config_error, e.message ?: "")
                    }
                },
            )

            Spacer(Modifier.size(8.dp))

            // Single-line статус под кнопкой
            Text(
                text = statusLine(xrayState, uptimeText),
                style = MaterialTheme.typography.titleMedium,
                color = when (xrayState) {
                    is XrayBridge.State.Failed -> Color(0xFFEF4444)
                    XrayBridge.State.Connected -> MaterialTheme.colorScheme.primary
                    else -> MaterialTheme.colorScheme.onSurfaceVariant
                },
                textAlign = TextAlign.Center,
            )

            if (xrayState is XrayBridge.State.Connected) {
                Spacer(Modifier.size(8.dp))
                TrafficStats(traffic)
            }

            lastError?.let { err ->
                Text("⚠ $err",
                    color = Color(0xFFEF4444),
                    style = MaterialTheme.typography.bodySmall)
            }

            Spacer(Modifier.weight(1f))

            // Footer info
            Text(
                text = stringResource(R.string.home_direct_sites, directSites.size),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )

            Spacer(Modifier.size(8.dp))

            // Config card внизу — name + protocol-tag + addr + ▶ для picker'а
            if (activeConfig != null) {
                ConfigBottomCard(
                    name = activeConfig.name,
                    protocol = activeConfig.protocol.uppercase(),
                    hostPort = activeConfig.serverHostPort(),
                    onClick = onPickConfig,
                )
            } else {
                OutlinedButton(onClick = onAddFirstConfig) {
                    Text(stringResource(R.string.home_add_server))
                }
            }
        }
    }
}

/**
 * Карточка активного сервера в нижней трети экрана. Аналог
 * десктоп-варианта (см. скрин). Tap открывает Servers tab (picker).
 *
 * Шапка: "🇳🇱 BMV1+ · VLESS XHTTP · Reality · Avito" (имя из ProxyConfig)
 * Низ: [VLESS] tag + 46.17.101.82:30443 + ▶
 */
@Composable
private fun ConfigBottomCard(
    name: String,
    protocol: String,
    hostPort: String,
    onClick: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable { onClick() }
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = name,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.size(4.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                // Tag-chip
                Text(
                    text = protocol,
                    style = MaterialTheme.typography.labelSmall,
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.onPrimary,
                    modifier = Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .background(MaterialTheme.colorScheme.primary)
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                )
                Spacer(Modifier.size(8.dp))
                Text(
                    text = hostPort,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Icon(
            imageVector = Icons.Filled.KeyboardArrowRight,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** Главная строка статуса под кнопкой. */
@Composable
private fun statusLine(s: XrayBridge.State, uptime: String): String = when (s) {
    XrayBridge.State.Idle -> stringResource(R.string.home_status_idle)
    XrayBridge.State.Starting -> stringResource(R.string.home_status_connecting)
    XrayBridge.State.Connected -> stringResource(R.string.home_status_connected, uptime)
    XrayBridge.State.Stopping -> stringResource(R.string.home_status_disconnecting)
    is XrayBridge.State.Failed -> stringResource(R.string.home_status_failed, s.reason)
}

/** Форматирует seconds → "h:mm:ss" или "mm:ss" если меньше часа. */
private fun formatUptime(seconds: Long): String {
    val h = seconds / 3600
    val m = (seconds % 3600) / 60
    val s = seconds % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s)
    else "%d:%02d".format(m, s)
}

/**
 * Две строки `↓ 12.4 MB · 1.2 MB/с` / `↑ 480 KB · 84 KB/с` — total за
 * сессию и текущая скорость для downlink/uplink. Цифры идут из libv2ray
 * через [XrayBridge.sampleTraffic] (опрос раз в секунду).
 */
@Composable
private fun TrafficStats(snapshot: XrayBridge.TrafficSnapshot) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(
            text = stringResource(
                R.string.home_traffic_down,
                formatBytes(snapshot.downlinkTotal),
                formatBytes(snapshot.downlinkBps),
            ),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Spacer(Modifier.size(2.dp))
        Text(
            text = stringResource(
                R.string.home_traffic_up,
                formatBytes(snapshot.uplinkTotal),
                formatBytes(snapshot.uplinkBps),
            ),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/**
 * Decimal SI-форматирование: 534 → "534 B", 12400 → "12.4 KB",
 * 1_240_000 → "1.2 MB", 1_240_000_000 → "1.24 GB".
 *
 * Decimal а не binary потому что у Android в системных индикаторах сети,
 * data usage и Files точно такой же стиль — пользователь привык.
 */
internal fun formatBytes(bytes: Long): String {
    val b = bytes.coerceAtLeast(0L)
    return when {
        b < 1_000L -> "$b B"
        b < 1_000_000L -> "%.1f KB".format(b / 1_000.0)
        b < 1_000_000_000L -> "%.1f MB".format(b / 1_000_000.0)
        else -> "%.2f GB".format(b / 1_000_000_000.0)
    }
}
