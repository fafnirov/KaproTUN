package pro.kaprovpn.android.core

import android.graphics.Bitmap
import android.graphics.Color
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.WriterException
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel

/**
 * Pure-Kotlin QR-генератор поверх ZXing. Используется в [ui.ShareConfigDialog]
 * чтобы превратить `ProxyConfig.rawUrl` обратно в картинку — пара к
 * [ui.ScanQrScreen], замыкает цикл share/scan.
 *
 * ZXing возвращает [com.google.zxing.common.BitMatrix] — двумерную чёрно-белую
 * матрицу модулей. Мы её сами раскрашиваем в Bitmap, чтобы избежать
 * `zxing-android` зависимости (там legacy support-lib хвосты).
 */
object QrGenerator {

    /**
     * Сгенерировать QR-картинку из произвольной строки.
     *
     * @param text что закодировать. Длинные share-URL (>1 KB, как у некоторых
     *   subscription-ссылок) могут вылезти за версию 40 — тогда ZXing бросит
     *   [WriterException]; мы возвращаем `null`.
     * @param sizePx размер итоговой картинки в пикселях, квадрат. 512–1024 —
     *   разумно для большинства экранов.
     * @param foreground цвет «чёрных» модулей (по умолчанию чёрный).
     * @param background цвет фона (по умолчанию белый — критично для
     *   распознавания, dark-on-dark QR не читается камерами).
     * @param errorCorrection ECC-уровень. M (15%) — баланс читаемость/размер;
     *   H (30%) — для лого посередине, нам не нужен.
     * @param margin "тихая зона" в модулях вокруг QR. <2 — сканеры начинают
     *   путаться с фоновым шумом. ZXing default 4, оставляем.
     */
    fun generate(
        text: String,
        sizePx: Int,
        foreground: Int = Color.BLACK,
        background: Int = Color.WHITE,
        errorCorrection: ErrorCorrectionLevel = ErrorCorrectionLevel.M,
        margin: Int = 2,
    ): Bitmap? {
        if (text.isEmpty()) return null
        val hints = mapOf(
            EncodeHintType.ERROR_CORRECTION to errorCorrection,
            EncodeHintType.MARGIN to margin,
            EncodeHintType.CHARACTER_SET to "UTF-8",
        )
        val matrix = try {
            QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, sizePx, sizePx, hints)
        } catch (e: WriterException) {
            return null
        }
        val width = matrix.width
        val height = matrix.height
        val pixels = IntArray(width * height)
        for (y in 0 until height) {
            val row = y * width
            for (x in 0 until width) {
                pixels[row + x] = if (matrix.get(x, y)) foreground else background
            }
        }
        // ARGB_8888 чуть жирнее чем RGB_565, но мы храним картинку только в
        // памяти на время диалога — не критично, зато безопаснее для всех
        // цветов foreground/background (с alpha).
        return Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).apply {
            setPixels(pixels, 0, width, 0, 0, width, height)
        }
    }
}
