package pro.kaprovpn.android.ui

import androidx.annotation.StringRes
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.DnsOption

/**
 * Корневой контейнер приложения. Скаффолд с NavigationBar в bottomBar,
 * переключающий три экрана: Home / Configs / Settings.
 */
@Composable
fun AppNav(
    onConnect: (configJson: String, sessionName: String, dnsOption: DnsOption) -> Unit,
    onDisconnect: () -> Unit,
) {
    var selectedTab by remember { mutableStateOf<Tab>(Tab.Home) }

    Scaffold(
        bottomBar = {
            NavigationBar {
                Tab.ALL.forEach { tab ->
                    val label = stringResource(tab.labelRes)
                    NavigationBarItem(
                        selected = selectedTab == tab,
                        onClick = { selectedTab = tab },
                        icon = { Icon(tab.icon, contentDescription = label) },
                        label = { Text(label, style = MaterialTheme.typography.labelSmall) },
                    )
                }
            }
        }
    ) { padding ->
        val modifier = Modifier.padding(padding)
        when (selectedTab) {
            Tab.Home -> HomeScreen(
                modifier = modifier,
                onConnect = onConnect,
                onDisconnect = onDisconnect,
                onAddFirstConfig = { selectedTab = Tab.Configs },
            )
            Tab.Configs -> ConfigsScreen(modifier = modifier)
            Tab.Settings -> SettingsScreen(modifier = modifier)
        }
    }
}

/** Три вкладки. labelRes — индирекция через R.string чтобы получить
 *  локализованную строку через stringResource в Composable-контексте. */
sealed class Tab(@StringRes val labelRes: Int, val icon: ImageVector) {
    object Home : Tab(R.string.tab_home, Icons.Filled.Home)
    object Configs : Tab(R.string.tab_configs, Icons.AutoMirrored.Filled.List)
    object Settings : Tab(R.string.tab_settings, Icons.Filled.Settings)

    companion object {
        // lazy — static-init order для nested object'ов не гарантирован.
        // См. Phase 5 polish commit для деталей.
        val ALL: List<Tab> by lazy { listOf(Home, Configs, Settings) }
    }
}
