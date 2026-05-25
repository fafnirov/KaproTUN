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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.DnsOption
import pro.kaprovpn.android.vpn.XrayBridge

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(modifier: Modifier = Modifier) {
    val settings by AppRepository.settings.collectAsState()
    val coreVersion = remember { XrayBridge.coreVersion() }

    Scaffold(
        modifier = modifier,
        topBar = { TopAppBar(title = { Text(stringResource(R.string.tab_settings)) }) },
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            // -- DNS-сервер ----------------------------------------------
            SectionHeader(stringResource(R.string.settings_dns_header))
            Text(
                stringResource(R.string.settings_dns_hint),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            DnsOption.ALL.forEach { opt ->
                DnsOptionRow(
                    option = opt,
                    selected = settings.dnsOptionKey == opt.key,
                    onSelect = { AppRepository.setDnsOption(opt.key) },
                )
            }

            HorizontalDivider(modifier = Modifier.padding(vertical = 8.dp))

            // -- Поведение -----------------------------------------------
            SectionHeader(stringResource(R.string.settings_behavior_header))
            SettingsToggleRow(
                title = stringResource(R.string.settings_autoconnect_title),
                subtitle = stringResource(R.string.settings_autoconnect_subtitle),
                checked = settings.autoconnectOnLaunch,
                onCheckedChange = { AppRepository.setAutoconnect(it) },
            )

            HorizontalDivider(modifier = Modifier.padding(vertical = 8.dp))

            // -- О приложении --------------------------------------------
            SectionHeader(stringResource(R.string.settings_about_header))
            Text(
                text = stringResource(R.string.settings_xray_version, coreVersion),
                style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                text = stringResource(R.string.settings_app_version),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            Spacer(Modifier.padding(bottom = 16.dp))
        }
    }
}

@Composable
private fun SectionHeader(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.titleMedium,
        color = MaterialTheme.colorScheme.primary,
    )
}

@Composable
private fun DnsOptionRow(
    option: DnsOption,
    selected: Boolean,
    onSelect: () -> Unit,
) {
    val container = if (selected)
        MaterialTheme.colorScheme.primaryContainer
    else MaterialTheme.colorScheme.surfaceVariant
    val isRussian = LocalConfiguration.current.locales[0].language == "ru"

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(container)
            .clickable { onSelect() }
            .padding(horizontal = 12.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(
            selected = selected,
            onClick = onSelect,
        )
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
private fun SettingsToggleRow(
    title: String,
    subtitle: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onCheckedChange(!checked) }
            .padding(vertical = 8.dp),
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
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}
