package pro.kaprovpn.android.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.Image
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.CloudDownload
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
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
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.ParseError
import pro.kaprovpn.android.core.ProxyConfig
import pro.kaprovpn.android.core.ShareUrlParser
import pro.kaprovpn.android.core.serverHostPort

/**
 * Servers — список конфигов. Layout:
 * - Hero card сверху (активный сервер) — visually prominent, янтарный border
 * - Section header "Все серверы (N)"
 * - Compact LazyColumn с остальными
 *
 * Если конфигов нет — Onboarding 3-cards (Phase 15 unchanged).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConfigsScreen(
    modifier: Modifier = Modifier,
    onOpenScan: () -> Unit = {},
    prefillShareUrl: String? = null,
    onPrefillConsumed: () -> Unit = {},
) {
    val configs by AppRepository.configs.collectAsState()
    val settings by AppRepository.settings.collectAsState()
    val pings by AppRepository.pings.collectAsState()
    val activeName = settings.activeConfigName
    var showAddDialog by remember { mutableStateOf(false) }
    var addDialogInitialUrl by remember { mutableStateOf("") }
    var showSubDialog by remember { mutableStateOf(false) }
    var showAddMenu by remember { mutableStateOf(false) }
    // Non-null когда открыт ShareConfigDialog для какого-то конфига.
    // Один state, потому что одновременно открыт только один диалог.
    var shareDialogConfig by remember { mutableStateOf<ProxyConfig?>(null) }
    val snackbarHost = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    // ScanQrScreen вернулся с распарсенным share-URL — открываем AddDialog,
    // pre-fill'им и сразу сообщаем AppNav'у что prefill подхвачен (иначе
    // recompose снова откроет диалог).
    LaunchedEffect(prefillShareUrl) {
        if (prefillShareUrl != null) {
            addDialogInitialUrl = prefillShareUrl
            showAddDialog = true
            onPrefillConsumed()
        }
    }

    val activeConfig = configs.find { it.name == activeName }
    val otherConfigs = configs.filterNot { it.name == activeName }

    Scaffold(
        modifier = modifier,
        topBar = {
            CenterAlignedTopAppBar(
                title = {
                    Text(
                        stringResource(R.string.tab_configs),
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.SemiBold,
                    )
                },
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
                    // CloudDownload (subscription import) переехал в FAB-меню
                    // в Phase QR — TopAppBar теперь чище.
                },
            )
        },
        floatingActionButton = {
            Box {
                ExtendedFloatingActionButton(
                    onClick = { showAddMenu = true },
                    icon = {
                        Icon(Icons.Filled.Add,
                            contentDescription = stringResource(R.string.configs_add))
                    },
                    text = { Text(stringResource(R.string.configs_add)) },
                )
                DropdownMenu(
                    expanded = showAddMenu,
                    onDismissRequest = { showAddMenu = false },
                ) {
                    DropdownMenuItem(
                        text = { Text(stringResource(R.string.configs_add_via_url)) },
                        leadingIcon = { Icon(Icons.Filled.Edit, contentDescription = null) },
                        onClick = {
                            showAddMenu = false
                            addDialogInitialUrl = ""
                            showAddDialog = true
                        },
                    )
                    DropdownMenuItem(
                        text = { Text(stringResource(R.string.configs_add_via_qr)) },
                        leadingIcon = { Icon(Icons.Filled.QrCodeScanner, contentDescription = null) },
                        onClick = {
                            showAddMenu = false
                            onOpenScan()
                        },
                    )
                    DropdownMenuItem(
                        text = { Text(stringResource(R.string.configs_import_subscription)) },
                        leadingIcon = { Icon(Icons.Filled.CloudDownload, contentDescription = null) },
                        onClick = {
                            showAddMenu = false
                            showSubDialog = true
                        },
                    )
                }
            }
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
                // Hero — активный конфиг (если есть)
                if (activeConfig != null) {
                    item(key = "hero-${activeConfig.name}") {
                        ActiveServerHero(
                            config = activeConfig,
                            ping = pings[activeConfig.name] ?: AppRepository.PingState.NotMeasured,
                            onShare = { shareDialogConfig = activeConfig },
                            onDelete = { AppRepository.removeConfig(activeConfig.name) },
                        )
                        Spacer(Modifier.size(8.dp))
                    }
                }

                // Section header for others
                if (otherConfigs.isNotEmpty()) {
                    item(key = "others-header") {
                        Text(
                            text = stringResource(R.string.configs_others_header, otherConfigs.size),
                            style = MaterialTheme.typography.titleSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(start = 4.dp, top = 8.dp, bottom = 4.dp),
                        )
                    }
                }

                items(otherConfigs, key = { it.name }) { cfg ->
                    CompactConfigRow(
                        config = cfg,
                        ping = pings[cfg.name] ?: AppRepository.PingState.NotMeasured,
                        onSelect = { AppRepository.setActiveConfig(cfg.name) },
                        onShare = { shareDialogConfig = cfg },
                        onDelete = { AppRepository.removeConfig(cfg.name) },
                    )
                }
            }
        }
    }

    if (showAddDialog) {
        AddConfigDialog(
            initialUrl = addDialogInitialUrl,
            onDismiss = {
                showAddDialog = false
                addDialogInitialUrl = ""
            },
            onSave = { config ->
                AppRepository.addConfig(config)
                showAddDialog = false
                addDialogInitialUrl = ""
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

    shareDialogConfig?.let { cfg ->
        ShareConfigDialog(
            config = cfg,
            onDismiss = { shareDialogConfig = null },
        )
    }
}

/**
 * Hero-card для активного сервера. Янтарный border + bigger padding,
 * имя крупным шрифтом, под ним — chip с протоколом + ping + host:port.
 */
@Composable
private fun ActiveServerHero(
    config: ProxyConfig,
    ping: AppRepository.PingState,
    onShare: () -> Unit,
    onDelete: () -> Unit,
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .border(
                BorderStroke(1.5.dp, MaterialTheme.colorScheme.primary),
                RoundedCornerShape(16.dp),
            )
            .padding(horizontal = 16.dp, vertical = 14.dp),
    ) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(8.dp)
                        .clip(CircleShape)
                        .background(MaterialTheme.colorScheme.primary),
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    text = stringResource(R.string.configs_active_marker),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.weight(1f))
                IconButton(
                    onClick = onShare,
                    modifier = Modifier.size(24.dp),
                ) {
                    Icon(
                        Icons.Filled.Share,
                        contentDescription = stringResource(R.string.configs_share),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.size(18.dp),
                    )
                }
                Spacer(Modifier.width(8.dp))
                IconButton(
                    onClick = onDelete,
                    modifier = Modifier.size(24.dp),
                ) {
                    Icon(
                        Icons.Filled.Delete,
                        contentDescription = stringResource(R.string.configs_delete),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.size(18.dp),
                    )
                }
            }
            Spacer(Modifier.size(6.dp))
            Text(
                text = config.name,
                style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.SemiBold),
                color = MaterialTheme.colorScheme.onSurface,
            )
            Spacer(Modifier.size(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                ProtocolChip(config.protocol.uppercase())
                Spacer(Modifier.width(8.dp))
                Text(
                    text = config.serverHostPort(),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.weight(1f))
                PingBadge(ping)
            }
        }
    }
}

/** Компактная строка для НЕ-активных. Tap → set active, delete-icon справа. */
@Composable
private fun CompactConfigRow(
    config: ProxyConfig,
    ping: AppRepository.PingState,
    onSelect: () -> Unit,
    onShare: () -> Unit,
    onDelete: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable { onSelect() }
            .padding(horizontal = 14.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                config.name,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Medium,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    config.protocol,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    text = config.serverHostPort(),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        PingBadge(ping)
        Spacer(Modifier.width(4.dp))
        IconButton(onClick = onShare, modifier = Modifier.size(32.dp)) {
            Icon(
                Icons.Filled.Share,
                contentDescription = stringResource(R.string.configs_share),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(18.dp),
            )
        }
        IconButton(onClick = onDelete, modifier = Modifier.size(32.dp)) {
            Icon(
                Icons.Filled.Delete,
                contentDescription = stringResource(R.string.configs_delete),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(18.dp),
            )
        }
    }
}

@Composable
private fun ProtocolChip(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.labelSmall,
        fontWeight = FontWeight.Bold,
        color = MaterialTheme.colorScheme.onPrimary,
        modifier = Modifier
            .clip(RoundedCornerShape(6.dp))
            .background(MaterialTheme.colorScheme.primary)
            .padding(horizontal = 6.dp, vertical = 2.dp),
    )
}

@Composable
private fun PingBadge(state: AppRepository.PingState) {
    val (text, color) = when (state) {
        is AppRepository.PingState.Ok -> stringResource(R.string.configs_ping_ms, state.ms) to
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
        Text(text = text, style = MaterialTheme.typography.labelSmall, color = color)
    }
}

@Composable
private fun OnboardingEmptyState(
    modifier: Modifier = Modifier,
    onImportSubscription: () -> Unit,
    onAddShareUrl: () -> Unit,
) {
    val context = LocalContext.current
    // verticalScroll so the last card stays reachable on short screens (and
    // so we can leave bottom padding under the floating "Добавить" button
    // without clipping content). 96dp = ExtendedFAB height + comfortable
    // gap so the FAB never overlaps the third onboarding card.
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 24.dp)
            .padding(top = 16.dp, bottom = 96.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Spacer(Modifier.size(8.dp))
        // Hero illustration — the orange split-route graphic from art/.
        // Mirrors what the Direct-sites / VPN bypass logic actually does, so
        // it doubles as visual shorthand for the feature. Sized to span the
        // full content width; height is intrinsic (PNG is ~3:1).
        Image(
            painter = painterResource(id = R.drawable.hero_split_routing),
            contentDescription = null,
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 4.dp),
        )
        Text(
            stringResource(R.string.configs_empty_title),
            style = MaterialTheme.typography.headlineSmall,
            fontWeight = FontWeight.SemiBold,
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
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable { onClick() }
            .padding(16.dp),
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

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddConfigDialog(
    initialUrl: String = "",
    onDismiss: () -> Unit,
    onSave: (ProxyConfig) -> Unit,
) {
    // `key = initialUrl` чтобы при reopen с другим pre-fill state TextField'а
    // обновлялся. Без этого второй вызов с свежим QR показал бы старое
    // содержимое из памяти.
    var urlInput by remember(initialUrl) { mutableStateOf(initialUrl) }
    var customName by remember(initialUrl) { mutableStateOf("") }
    var error by remember(initialUrl) { mutableStateOf<String?>(null) }
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

private const val LANDING_URL = "https://kaprovpn.pro/"
