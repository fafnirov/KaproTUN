package pro.kaprovpn.android.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.widget.Toast
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import pro.kaprovpn.android.R
import pro.kaprovpn.android.core.ProxyConfig
import pro.kaprovpn.android.core.QrGenerator

/**
 * Показывает share-URL текущего конфига в виде QR (для скана другим телефоном)
 * + plain text с кнопками Copy / системный Share-sheet.
 *
 * Пара к [ScanQrScreen] — два пользователя могут обменяться конфигом
 * «телефон-в-телефон» без копипасты в мессенджер.
 *
 * QR-картинка кешируется через `remember(config.rawUrl)` — не пересоздаётся
 * на recompose из-за смены theme / orientation. `filterQuality = None`,
 * иначе встроенное сглаживание Image сделает QR-модули размытыми и сканеры
 * хуже их распознают.
 */
@Composable
fun ShareConfigDialog(
    config: ProxyConfig,
    onDismiss: () -> Unit,
) {
    val context = LocalContext.current
    val rawUrl = config.rawUrl
    val qrBitmap = remember(rawUrl) {
        // 720px = золотая середина между разрешением и временем генерации.
        // На стандартном dpi-3 экране это ~240dp, мы рисуем в 280dp — Compose
        // мягко доскейлит без потери читаемости.
        QrGenerator.generate(rawUrl, sizePx = 720)
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Column {
                Text(
                    stringResource(R.string.share_title),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                )
                Text(
                    config.name,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        text = {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                if (qrBitmap != null) {
                    Image(
                        bitmap = qrBitmap.asImageBitmap(),
                        contentDescription = stringResource(R.string.share_qr_content_desc),
                        // Белый фон даёт сканерам надёжный контраст даже на
                        // dark theme — QR-модули остаются чёрными, padding
                        // выступает quiet-zone.
                        modifier = Modifier
                            .size(280.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .background(Color.White)
                            .padding(8.dp),
                        filterQuality = FilterQuality.None,
                    )
                } else {
                    // Генерация QR упала — слишком длинный URL (> ZXing
                    // версия 40) или невалидный текст. Покажем понятный
                    // плейсхолдер вместо пустоты.
                    Text(
                        stringResource(R.string.share_qr_failed),
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
                OutlinedTextField(
                    value = rawUrl,
                    onValueChange = { /* read-only */ },
                    readOnly = true,
                    label = { Text(stringResource(R.string.share_url_label)) },
                    singleLine = false,
                    maxLines = 4,
                    modifier = Modifier.fillMaxWidth(),
                )
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Button(
                        onClick = {
                            copyToClipboard(context, rawUrl, config.name)
                            // Pre-Android-13 нет системного toast от clipboard
                            // → показываем сами. На 13+ дублирование — OK,
                            // системный chip всё равно более заметный.
                            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                                Toast.makeText(
                                    context,
                                    context.getString(R.string.share_copied),
                                    Toast.LENGTH_SHORT,
                                ).show()
                            }
                        },
                        modifier = Modifier.fillMaxWidth(0.5f),
                    ) {
                        Text(stringResource(R.string.share_copy))
                    }
                    Button(
                        onClick = { sendViaIntent(context, rawUrl, config.name) },
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(stringResource(R.string.share_send))
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.share_close))
            }
        },
        // dismissButton оставлен пустым — кнопки Copy/Share уже в теле.
    )
}

private fun copyToClipboard(context: Context, text: String, label: String) {
    val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText(label, text))
}

private fun sendViaIntent(context: Context, text: String, configName: String) {
    val sendIntent = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_TEXT, text)
        putExtra(Intent.EXTRA_SUBJECT, configName)
    }
    val chooser = Intent.createChooser(
        sendIntent,
        context.getString(R.string.share_send_via),
    ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { context.startActivity(chooser) }
}
