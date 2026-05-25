package pro.kaprovpn.android.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import pro.kaprovpn.android.core.ParseError
import pro.kaprovpn.android.core.ShareUrlParser
import pro.kaprovpn.android.core.XrayConfigBuilder
import pro.kaprovpn.android.vpn.XrayBridge

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    onConnect: (configJson: String, sessionName: String) -> Unit,
    onDisconnect: () -> Unit,
) {
    val state by XrayBridge.state.collectAsState()
    var urlInput by remember { mutableStateOf("") }
    var lastError by remember { mutableStateOf<String?>(null) }

    val isConnected = state is XrayBridge.State.Connected
    val isBusy = state is XrayBridge.State.Starting || state is XrayBridge.State.Stopping

    Scaffold(
        topBar = { TopAppBar(title = { Text("KaproVPN") }) }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 24.dp, vertical = 16.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text("KaproVPN", style = MaterialTheme.typography.headlineMedium)
            Text(
                "Phase 3: VpnService + TUN. Вставь share-URL — vless:// / vmess:// / trojan:// / ss://",
                style = MaterialTheme.typography.bodyMedium
            )

            OutlinedTextField(
                value = urlInput,
                onValueChange = { urlInput = it; lastError = null },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("share-URL") },
                placeholder = { Text("vless://...") },
                singleLine = false,
                minLines = 2,
                maxLines = 4,
                enabled = !isConnected && !isBusy,
            )

            Text(
                text = "статус: ${stateLabel(state)}",
                style = MaterialTheme.typography.bodySmall,
                color = if (state is XrayBridge.State.Failed) Color(0xFFEF4444) else MaterialTheme.colorScheme.onSurfaceVariant,
            )

            lastError?.let { err ->
                Text("⚠ $err", color = Color(0xFFEF4444), style = MaterialTheme.typography.bodySmall)
            }

            Spacer(Modifier.padding(top = 8.dp))

            if (!isConnected) {
                Button(
                    onClick = {
                        try {
                            val proxy = ShareUrlParser.parse(urlInput.trim())
                            // Phase 3 MVP: пустой direct-list — туннелируем всё.
                            // Phase 4: подтянем default_sites.json и сделаем
                            // полноценный split-routing.
                            val json = XrayConfigBuilder.buildConfigJson(proxy, emptyList())
                            lastError = null
                            onConnect(json, proxy.name)
                        } catch (e: ParseError) {
                            lastError = "Не удалось распарсить URL: ${e.message}"
                        } catch (e: Throwable) {
                            lastError = "Ошибка: ${e.message}"
                        }
                    },
                    enabled = urlInput.isNotBlank() && !isBusy,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary
                    ),
                ) {
                    Text(if (isBusy) "ПОДКЛЮЧЕНИЕ…" else "ВКЛЮЧИТЬ")
                }
            } else {
                Button(
                    onClick = { onDisconnect() },
                    enabled = !isBusy,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error
                    ),
                ) {
                    Text("ОТКЛЮЧИТЬ")
                }
            }

            Spacer(Modifier.padding(top = 16.dp))

            // Diagnostic: версия Xray-core. Полезно для bug-report'ов.
            Text(
                text = "Xray-core: ${remember { XrayBridge.coreVersion() }}",
                style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

private fun stateLabel(s: XrayBridge.State): String = when (s) {
    XrayBridge.State.Idle -> "не подключено"
    XrayBridge.State.Starting -> "подключение…"
    XrayBridge.State.Connected -> "подключено"
    XrayBridge.State.Stopping -> "отключение…"
    is XrayBridge.State.Failed -> "ошибка: ${s.reason}"
}
