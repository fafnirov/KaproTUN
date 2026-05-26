package pro.kaprovpn.android.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.CloudDownload
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.ParseError
import pro.kaprovpn.android.core.ProxyConfig
import pro.kaprovpn.android.core.ShareUrlParser

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConfigsScreen(modifier: Modifier = Modifier) {
    val configs by AppRepository.configs.collectAsState()
    val settings by AppRepository.settings.collectAsState()
    val pings by AppRepository.pings.collectAsState()
    val activeName = settings.activeConfigName
    var showAddDialog by remember { mutableStateOf(false) }
    var showSubDialog by remember { mutableStateOf(false) }
    val snackbarHost = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    Scaffold(
        modifier = modifier,
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.tab_configs)) },
                actions = {
                    IconButton(
                        onClick = { scope.launch { AppRepository.pingAll() } },
                        enabled = configs.isNotEmpty(),
                    ) {
                        Icon(
                            Icons.Filled.Refresh,
                            contentDescription = stringResource(R.string.configs_ping_refresh),
                        )
                    }
                    IconButton(onClick = { showSubDialog = true }) {
                        Icon(
                            Icons.Filled.CloudDownload,
                            contentDescription = stringResource(R.string.configs_import_subscription),
                        )
                    }
                },
            )
        },
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = { showAddDialog = true },
                icon = {
                    Icon(Icons.Filled.Add,
                        contentDescription = stringResource(R.string.configs_add))
                },
                text = { Text(stringResource(R.string.configs_add)) },
            )
        },
        snackbarHost = { SnackbarHost(snackbarHost) },
    ) { innerPadding ->
        if (configs.isEmpty()) {
            OnboardingEmptyState(
                modifier = Modifier.padding(innerPadding),
                onAddShareUrl = { showAddDialog = true },
                onImportSubscription = { showSubDialog = true },
            )
        } else {
            LazyColumn(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(configs, key = { it.name }) { cfg ->
                    ConfigRow(
                        config = cfg,
                        isActive = cfg.name == activeName,
                        ping = pings[cfg.name] ?: AppRepository.PingState.NotMeasured,
                        onSelect = { AppRepository.setActiveConfig(cfg.name) },
                        onDelete = { AppRepository.removeConfig(cfg.name) },
                    )
                }
            }
        }
    }

    if (showAddDialog) {
        AddConfigDialog(
            onDismiss = { showAddDialog = false },
            onSave = { config ->
                AppRepository.addConfig(config)
                showAddDialog = false
            },
        )
    }

    if (showSubDialog) {
        SubscriptionDialog(
            onDismiss = { showSubDialog = false },
            onAdded = { count ->
                showSubDialog = false
                scope.launch {
                    snackbarHost.showSnackbar(
                        context.getString(R.string.configs_import_done, count)
                    )
                }
            },
        )
    }
}

@Composable
private fun ConfigRow(
    config: ProxyConfig,
    isActive: Boolean,
    ping: AppRepository.PingState,
    onSelect: () -> Unit,
    onDelete: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onSelect() },
        colors = CardDefaults.cardColors(
            containerColor = if (isActive)
                MaterialTheme.colorScheme.primaryContainer
            else MaterialTheme.colorScheme.surfaceVariant
        ),
        shape = RoundedCornerShape(12.dp),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(24.dp)
                    .clip(CircleShape)
                    .background(
                        if (isActive) MaterialTheme.colorScheme.primary
                        else Color.Transparent
                    ),
                contentAlignment = Alignment.Center,
            ) {
                if (isActive) Icon(
                    Icons.Filled.Check,
                    contentDescription = stringResource(R.string.configs_active_marker),
                    tint = MaterialTheme.colorScheme.onPrimary,
                    modifier = Modifier.size(16.dp),
                )
            }
            Spacer(Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(config.name, style = MaterialTheme.typography.titleSmall)
                Text(
                    config.protocol,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            PingBadge(ping)
            Spacer(Modifier.width(4.dp))
            IconButton(onClick = onDelete) {
                Icon(
                    Icons.Filled.Delete,
                    contentDescription = stringResource(R.string.configs_delete),
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun PingBadge(state: AppRepository.PingState) {
    val (text, color) = when (state) {
        is AppRepository.PingState.Ok -> stringResource(R.string.configs_ping_ms, state.ms) to
            // Цветовая индикация: <100мс — зелёный, <300мс — янтарный, >300 — красный
            when {
                state.ms < 100 -> MaterialTheme.colorScheme.primary
                state.ms < 300 -> MaterialTheme.colorScheme.secondary
                else -> MaterialTheme.colorScheme.error
            }
        AppRepository.PingState.InProgress ->
            stringResource(R.string.configs_ping_pending) to MaterialTheme.colorScheme.onSurfaceVariant
        AppRepository.PingState.Failed ->
            stringResource(R.string.configs_ping_failed) to MaterialTheme.colorScheme.error
        AppRepository.PingState.NotMeasured ->
            "" to MaterialTheme.colorScheme.onSurfaceVariant
    }
    if (text.isNotEmpty()) {
        Text(
            text = text,
            style = MaterialTheme.typography.labelSmall,
            color = color,
        )
    }
}

/**
 * Onboarding-стиль empty state — 3 пути для нового пользователя:
 * subscription URL, single share-URL, или landing site если провайдера
 * вообще нет. Аналог `kapro_vpn/gui/onboarding.py` с десктопа. Скрывается
 * как только в списке появляется хотя бы один конфиг.
 */
@Composable
private fun OnboardingEmptyState(
    modifier: Modifier = Modifier,
    onImportSubscription: () -> Unit,
    onAddShareUrl: () -> Unit,
) {
    val context = LocalContext.current
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(horizontal = 24.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Spacer(Modifier.size(8.dp))
        Text(
            stringResource(R.string.configs_empty_title),
            style = MaterialTheme.typography.headlineSmall,
        )
        Text(
            stringResource(R.string.configs_empty_hint),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.size(8.dp))

        OnboardingPathCard(
            title = stringResource(R.string.onboarding_path_subscription_title),
            subtitle = stringResource(R.string.onboarding_path_subscription_subtitle),
            onClick = onImportSubscription,
        )
        OnboardingPathCard(
            title = stringResource(R.string.onboarding_path_share_title),
            subtitle = stringResource(R.string.onboarding_path_share_subtitle),
            onClick = onAddShareUrl,
        )
        OnboardingPathCard(
            title = stringResource(R.string.onboarding_path_noprovider_title),
            subtitle = stringResource(R.string.onboarding_path_noprovider_subtitle),
            onClick = {
                runCatching {
                    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(LANDING_URL))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(intent)
                }
            },
        )
    }
}

@Composable
private fun OnboardingPathCard(
    title: String,
    subtitle: String,
    onClick: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onClick() },
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** Landing-страница c инструкциями и partner-провайдерами.
 *  Совпадает с `SETUP_GUIDE_URL` из десктопного `gui/onboarding.py`. */
private const val LANDING_URL = "https://kaprovpn.pro/"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddConfigDialog(
    onDismiss: () -> Unit,
    onSave: (ProxyConfig) -> Unit,
) {
    var urlInput by remember { mutableStateOf("") }
    var customName by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    val context = LocalContext.current

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.add_dialog_title)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(
                    value = urlInput,
                    onValueChange = { urlInput = it; error = null },
                    label = { Text(stringResource(R.string.add_dialog_url_label)) },
                    placeholder = { Text(stringResource(R.string.add_dialog_url_placeholder)) },
                    minLines = 2,
                    maxLines = 5,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = customName,
                    onValueChange = { customName = it },
                    label = { Text(stringResource(R.string.add_dialog_name_label)) },
                    placeholder = { Text(stringResource(R.string.add_dialog_name_placeholder)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = {
            Button(onClick = {
                try {
                    var cfg = ShareUrlParser.parse(urlInput.trim())
                    if (customName.isNotBlank()) {
                        cfg = cfg.copy(name = customName.trim())
                    }
                    onSave(cfg)
                } catch (e: ParseError) {
                    error = context.getString(R.string.add_dialog_parse_error, e.message ?: "")
                } catch (e: Throwable) {
                    error = context.getString(R.string.add_dialog_generic_error, e.message ?: "")
                }
            }) { Text(stringResource(R.string.add_dialog_save)) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.add_dialog_cancel))
            }
        },
    )
}
