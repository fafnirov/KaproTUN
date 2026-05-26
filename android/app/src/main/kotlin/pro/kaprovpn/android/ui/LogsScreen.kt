package pro.kaprovpn.android.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.DeleteSweep
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import pro.kaprovpn.android.R
import pro.kaprovpn.android.vpn.XrayBridge

/**
 * Live-просмотр логов xray-core. Подписан на [XrayBridge.logs] SharedFlow
 * (replay=256). Автоскролл вниз при новой строке.
 *
 * Соответствие десктоп-клиенту: эквивалент `gui/main_window.py:LogsPage` —
 * там тоже scrollable log с цветом по severity.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LogsScreen(modifier: Modifier = Modifier) {
    // Локальное состояние для рендеринга. SharedFlow replay буфер
    // даёт нам бэклог при первом collect. Cap локального списка чтобы
    // не съесть всю RAM на долгой сессии.
    val items = remember { mutableStateListOf<XrayBridge.LogLine>() }

    LaunchedEffect(Unit) {
        XrayBridge.logs.collect { line ->
            items.add(line)
            if (items.size > MAX_LINES) {
                items.removeAt(0)
            }
        }
    }

    val listState = rememberLazyListState()
    // Auto-scroll: при добавлении новой строки прыгаем вниз. Если
    // пользователь скроллил вверх — переусердствовать не хочется, но
    // для MVP всегда тянем в конец (как `tail -f`).
    LaunchedEffect(items.size) {
        if (items.isNotEmpty()) {
            listState.animateScrollToItem(items.size - 1)
        }
    }

    Scaffold(
        modifier = modifier,
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.tab_logs)) },
                actions = {
                    IconButton(
                        onClick = { items.clear() },
                        enabled = items.isNotEmpty(),
                    ) {
                        Icon(
                            Icons.Filled.DeleteSweep,
                            contentDescription = stringResource(R.string.logs_clear),
                        )
                    }
                },
            )
        },
    ) { innerPadding ->
        if (items.isEmpty()) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding)
                    .padding(32.dp),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    stringResource(R.string.logs_empty),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding),
                state = listState,
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
            ) {
                items(items) { line -> LogLineRow(line) }
            }
        }
    }
}

@Composable
private fun LogLineRow(line: XrayBridge.LogLine) {
    Text(
        text = line.message,
        style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
        color = severityColor(line.severity),
        modifier = Modifier.padding(vertical = 1.dp),
    )
}

/**
 * Цветовое кодирование. Severity-схема libv2ray (примерно совпадает с
 * Xray-core internal log levels): 0=Debug, 1=Info, 2=Warning, 3+=Error.
 * Точные значения могут варьироваться между релизами libv2ray — выбираем
 * консервативно по нижней границе.
 */
@Composable
private fun severityColor(severity: Int): Color = when {
    severity >= 3 -> Color(0xFFEF4444)                            // Error — красный
    severity == 2 -> MaterialTheme.colorScheme.secondary          // Warning — амбер
    severity == 1 -> MaterialTheme.colorScheme.onSurface           // Info — обычный
    else -> MaterialTheme.colorScheme.onSurfaceVariant             // Debug — приглушённый
}

private const val MAX_LINES = 1000
