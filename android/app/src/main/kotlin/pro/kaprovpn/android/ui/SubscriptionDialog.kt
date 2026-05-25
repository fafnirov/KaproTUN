package pro.kaprovpn.android.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.AppRepository
import pro.kaprovpn.android.core.Subscription

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SubscriptionDialog(
    onDismiss: () -> Unit,
    onAdded: (count: Int) -> Unit,
) {
    var url by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var result by remember { mutableStateOf<Subscription.Result?>(null) }
    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    AlertDialog(
        onDismissRequest = { if (!loading) onDismiss() },
        title = { Text(stringResource(R.string.sub_dialog_title)) },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Text(
                    stringResource(R.string.sub_dialog_hint),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it; error = null },
                    label = { Text(stringResource(R.string.sub_dialog_url_label)) },
                    placeholder = { Text(stringResource(R.string.sub_dialog_url_placeholder)) },
                    singleLine = false,
                    minLines = 2,
                    maxLines = 4,
                    enabled = !loading,
                    modifier = Modifier.fillMaxWidth(),
                )
                error?.let {
                    Text("⚠ $it", color = MaterialTheme.colorScheme.error)
                }
                result?.let { r ->
                    ResultSummary(r)
                }
                if (loading) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 8.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        CircularProgressIndicator()
                        Spacer(Modifier.size(8.dp))
                        Text(stringResource(R.string.sub_dialog_loading),
                            style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        },
        confirmButton = {
            val r = result
            if (r != null && r.configs.isNotEmpty()) {
                Button(onClick = {
                    AppRepository.addConfigs(r.configs)
                    onAdded(r.configs.size)
                }) {
                    Text(stringResource(R.string.sub_dialog_add_all, r.configs.size))
                }
            } else {
                Button(
                    onClick = {
                        if (url.isBlank()) {
                            error = context.getString(R.string.sub_dialog_url_required)
                            return@Button
                        }
                        error = null
                        result = null
                        loading = true
                        scope.launch {
                            try {
                                result = Subscription.import(url.trim())
                                if (result?.configs?.isEmpty() == true) {
                                    error = context.getString(R.string.sub_dialog_no_results)
                                }
                            } catch (e: Throwable) {
                                error = context.getString(
                                    R.string.sub_dialog_fetch_error, e.message ?: ""
                                )
                            } finally {
                                loading = false
                            }
                        }
                    },
                    enabled = !loading && url.isNotBlank(),
                ) {
                    Text(
                        if (loading) stringResource(R.string.sub_dialog_fetching)
                        else stringResource(R.string.sub_dialog_fetch)
                    )
                }
            }
        },
        dismissButton = {
            TextButton(
                onClick = onDismiss,
                enabled = !loading,
            ) {
                Text(
                    if (result != null) stringResource(R.string.sub_dialog_close)
                    else stringResource(R.string.sub_dialog_cancel)
                )
            }
        },
    )
}

@Composable
private fun ResultSummary(r: Subscription.Result) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            text = if (r.errors.isEmpty())
                stringResource(R.string.sub_dialog_summary, r.configs.size)
            else
                stringResource(R.string.sub_dialog_summary_with_errors,
                    r.configs.size, r.errors.size),
            style = MaterialTheme.typography.bodyMedium,
            color = if (r.configs.isNotEmpty())
                MaterialTheme.colorScheme.primary
            else MaterialTheme.colorScheme.error,
        )
        if (r.configs.isNotEmpty()) {
            val preview = r.configs.take(5).joinToString("\n") { "• ${it.name}" }
            val rest = if (r.configs.size > 5)
                stringResource(R.string.sub_dialog_more_servers, r.configs.size - 5)
            else null
            Text(
                text = if (rest != null) "$preview\n$rest" else preview,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
