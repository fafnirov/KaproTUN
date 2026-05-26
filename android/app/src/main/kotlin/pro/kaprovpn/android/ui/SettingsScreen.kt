package pro.kaprovpn.android.ui

import android.content.Intent
import android.provider.Settings
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Apps
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.Dns
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Shield
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.DnsOption
import pro.kaprovpn.android.vpn.XrayBridge

/**
 * Settings — card-based layout. Каждая логическая группа в собственной
 * `SectionCard` (icon + title + content). Никаких сложных эффектов:
 * Material 3 surfaces + rounded corners 16dp. Должно летать на любом
 * телефоне от 2018+.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    modifier: Modifier = Modifier,
    onOpenExcludedApps: () -> Unit = {},
) {
    val settings by AppRepository.settings.collectAsState()
    val coreVersion = remember { XrayBridge.coreVersion() }

    Scaffold(
        modifier = modifier,
        topBar = {
            CenterAlignedTopAppBar(
                title = {
                    Text(
                        stringResource(R.string.tab_settings),
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
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            // ── DNS ──
            SectionCard(
                icon = Icons.Filled.Dns,
                title = stringResource(R.string.settings_dns_header),
                hint = stringResource(R.string.settings_dns_hint),
            ) {
                DnsOption.ALL.forEachIndexed { idx, opt ->
                    DnsOptionRow(
                        option = opt,
                        selected = settings.dnsOptionKey == opt.key,
                        onSelect = { AppRepository.setDnsOption(opt.key) },
                    )
                    if (idx < DnsOption.ALL.lastIndex) Spacer(Modifier.size(6.dp))
                }
            }

            // ── Поведение ──
            SectionCard(
                icon = Icons.Filled.Tune,
                title = stringResource(R.string.settings_behavior_header),
            ) {
                ToggleRow(
                    title = stringResource(R.string.settings_autoconnect_title),
                    subtitle = stringResource(R.string.settings_autoconnect_subtitle),
                    checked = settings.autoconnectOnLaunch,
                    onCheckedChange = { AppRepository.setAutoconnect(it) },
                )
                Spacer(Modifier.size(8.dp))
                ToggleRow(
                    title = stringResource(R.string.settings_subrefresh_title),
                    subtitle = stringResource(R.string.settings_subrefresh_subtitle),
                    checked = settings.subscriptionAutorefresh,
                    onCheckedChange = { AppRepository.setSubscriptionAutorefresh(it) },
                )
            }

            // ── Per-app split tunneling ──
            // Card-as-link: shows the current excluded count and opens
            // ExcludedAppsScreen on tap. We don't list the apps inline —
            // browsing apps is its own screen with search + icons.
            SectionCard(
                icon = Icons.Filled.Apps,
                title = stringResource(R.string.settings_excluded_apps_title),
                hint = stringResource(R.string.settings_excluded_apps_hint),
            ) {
                ChevronRow(
                    title = if (settings.excludedPackages.isEmpty())
                        stringResource(R.string.settings_excluded_apps_none)
                    else
                        stringResource(
                            R.string.settings_excluded_apps_count,
                            settings.excludedPackages.size,
                        ),
                    onClick = onOpenExcludedApps,
                )
            }

            // ── Always-on VPN / kill-switch ──
            val context = LocalContext.current
            SectionCard(
                icon = Icons.Filled.Shield,
                title = stringResource(R.string.settings_alwayson_title),
                hint = stringResource(R.string.settings_alwayson_subtitle),
            ) {
                OutlinedButton(
                    onClick = {
                        runCatching {
                            val intent = Intent(Settings.ACTION_VPN_SETTINGS)
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            context.startActivity(intent)
                        }
                    },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text(stringResource(R.string.settings_alwayson_open)) }
            }

            // ── About ──
            SectionCard(
                icon = Icons.Filled.Info,
                title = stringResource(R.string.settings_about_header),
            ) {
                Text(
                    text = stringResource(R.string.settings_xray_version, coreVersion),
                    style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.size(4.dp))
                Text(
                    text = stringResource(R.string.settings_app_version),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Spacer(Modifier.size(8.dp))
        }
    }
}

/**
 * Reusable section card. Заголовок с иконкой слева; ниже — `hint`
 * описание (опционально) и пользовательский content slot.
 */
@Composable
private fun SectionCard(
    icon: ImageVector,
    title: String,
    hint: String? = null,
    content: @Composable () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                imageVector = icon,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.size(20.dp),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                text = title,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurface,
            )
        }
        if (hint != null) {
            Text(
                text = hint,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        content()
    }
}

@Composable
private fun DnsOptionRow(
    option: DnsOption,
    selected: Boolean,
    onSelect: () -> Unit,
) {
    val isRussian = LocalConfiguration.current.locales[0].language == "ru"
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(
                if (selected) MaterialTheme.colorScheme.primaryContainer
                else androidx.compose.ui.graphics.Color.Transparent
            )
            .clickable { onSelect() }
            .padding(horizontal = 8.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = selected, onClick = onSelect)
        Spacer(Modifier.width(4.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                if (isRussian) option.labelRu else option.labelEn,
                style = MaterialTheme.typography.titleSmall,
            )
            Text(
                if (isRussian) option.hintRu else option.hintEn,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun ToggleRow(
    title: String,
    subtitle: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onCheckedChange(!checked) }
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.width(12.dp))
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

/** Row that acts as a link into a nested screen — title on the left, > on
 *  the right. We use this instead of a button for parity with iOS-style
 *  "deep" preferences rows. */
@Composable
private fun ChevronRow(
    title: String,
    onClick: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            title,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.weight(1f),
        )
        Icon(
            imageVector = Icons.Filled.ChevronRight,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
