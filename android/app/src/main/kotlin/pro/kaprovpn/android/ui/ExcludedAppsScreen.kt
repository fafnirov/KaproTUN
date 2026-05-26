package pro.kaprovpn.android.ui

import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Clear
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.core.graphics.drawable.toBitmap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository

/**
 * Экран per-app split-tunneling — список установленных приложений с
 * чекбоксами «Не пускать через VPN». Изменения сохраняются сразу в
 * AppRepository.setExcludedPackages; применяются на следующем connect
 * (VpnService нельзя менять disallowed-list на живом TUN).
 *
 * Список приложений PackageManager отдаёт быстро по метаданным, но иконки
 * (Drawable) — отдельный disk-hit на каждое приложение. Грузим всё в
 * Dispatchers.IO и кешируем в ImageBitmap, чтобы LazyColumn не лагал
 * на scroll'е.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ExcludedAppsScreen(
    modifier: Modifier = Modifier,
    onBack: () -> Unit,
) {
    val context = LocalContext.current
    BackHandler { onBack() }

    var allApps by remember { mutableStateOf<List<AppRow>?>(null) }
    var query by remember { mutableStateOf("") }
    var excluded by remember { mutableStateOf(AppRepository.excludedPackages()) }

    LaunchedEffect(Unit) {
        // Off-main: pm.getInstalledApplications + per-app icon decode.
        val loaded = withContext(Dispatchers.IO) {
            loadApps(context.packageManager, ownPackage = context.packageName)
        }
        allApps = loaded
    }

    val filtered by remember(allApps, query) {
        derivedStateOf {
            val list = allApps ?: return@derivedStateOf emptyList()
            if (query.isBlank()) list
            else {
                val needle = query.trim().lowercase()
                list.filter {
                    it.label.lowercase().contains(needle) ||
                        it.packageName.lowercase().contains(needle)
                }
            }
        }
    }

    Scaffold(
        modifier = modifier,
        topBar = {
            CenterAlignedTopAppBar(
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = stringResource(R.string.back),
                        )
                    }
                },
                title = {
                    Text(
                        stringResource(R.string.excluded_apps_title),
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.SemiBold,
                    )
                },
            )
        },
    ) { inner ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(inner),
        ) {
            // Hint about needing reconnect — sits up top so the user sees it
            // before they start checking boxes and wondering why nothing
            // happens to traffic.
            Text(
                text = stringResource(R.string.excluded_apps_hint),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 8.dp),
            )

            // Search box (filters by app label or package name)
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                placeholder = { Text(stringResource(R.string.excluded_apps_search)) },
                leadingIcon = { Icon(Icons.Filled.Search, null) },
                trailingIcon = {
                    if (query.isNotEmpty()) {
                        IconButton(onClick = { query = "" }) {
                            Icon(Icons.Filled.Clear, contentDescription = stringResource(R.string.excluded_apps_search_clear))
                        }
                    }
                },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 4.dp),
            )

            val list = allApps
            if (list == null) {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            } else if (filtered.isEmpty()) {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text(
                        stringResource(R.string.excluded_apps_empty),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else {
                LazyColumn(
                    contentPadding = PaddingValues(horizontal = 8.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(2.dp),
                ) {
                    items(filtered, key = { it.packageName }) { app ->
                        val isChecked = app.packageName in excluded
                        AppRowItem(
                            app = app,
                            isChecked = isChecked,
                            onToggle = {
                                val next = if (isChecked) excluded - app.packageName
                                           else excluded + app.packageName
                                excluded = next
                                AppRepository.setExcludedPackages(next)
                            },
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun AppRowItem(
    app: AppRow,
    isChecked: Boolean,
    onToggle: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .clickable(onClick = onToggle)
            .background(
                if (isChecked) MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.25f)
                else MaterialTheme.colorScheme.surface
            )
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        // App icon; Bitmap is cached on the AppRow so we don't re-decode on
        // recomposition.
        androidx.compose.foundation.Image(
            bitmap = app.icon,
            contentDescription = null,
            modifier = Modifier
                .size(40.dp)
                .clip(RoundedCornerShape(8.dp)),
        )
        Column(modifier = Modifier.weight(1f)) {
            Text(
                app.label,
                style = MaterialTheme.typography.bodyLarge,
                maxLines = 1,
                fontWeight = FontWeight.Medium,
            )
            Text(
                app.packageName,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
            )
        }
        Checkbox(checked = isChecked, onCheckedChange = { onToggle() })
    }
}

private data class AppRow(
    val packageName: String,
    val label: String,
    val icon: ImageBitmap,
    val isSystem: Boolean,
)

/**
 * Loads installed apps the user could plausibly want to exclude:
 *   - non-system apps always
 *   - system apps that have a launcher intent (so they're "real" apps from
 *     a user's POV — Chrome, Photos, etc. — not internal services)
 * Always skips our own package; it's already addDisallowedApplication'd
 * unconditionally in KaproVpnService.
 *
 * The icon is rasterized once here so LazyColumn scroll stays smooth.
 */
private fun loadApps(pm: PackageManager, ownPackage: String): List<AppRow> {
    val pkgs = pm.getInstalledApplications(0)
    val out = ArrayList<AppRow>(pkgs.size)
    for (info in pkgs) {
        if (info.packageName == ownPackage) continue
        val isSystem = (info.flags and ApplicationInfo.FLAG_SYSTEM) != 0
        if (isSystem) {
            // Hide system apps that are not user-launchable. Catches things
            // like "android", "com.android.providers.media", etc.
            val launchIntent = try {
                pm.getLaunchIntentForPackage(info.packageName)
            } catch (_: Throwable) {
                null
            }
            if (launchIntent == null) continue
        }
        val label = try {
            info.loadLabel(pm).toString().takeIf { it.isNotBlank() }
                ?: info.packageName
        } catch (_: Throwable) {
            info.packageName
        }
        val iconBitmap = try {
            info.loadIcon(pm).toBitmap(width = 96, height = 96).asImageBitmap()
        } catch (_: Throwable) {
            // 1x1 transparent fallback — beats crashing the whole list if
            // one APK has a broken icon resource.
            android.graphics.Bitmap.createBitmap(1, 1, android.graphics.Bitmap.Config.ARGB_8888)
                .asImageBitmap()
        }
        out.add(AppRow(info.packageName, label, iconBitmap, isSystem))
    }
    out.sortWith(compareBy({ it.label.lowercase() }, { it.packageName }))
    return out
}
