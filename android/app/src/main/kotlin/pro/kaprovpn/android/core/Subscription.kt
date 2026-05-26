package pro.kaprovpn.android.core

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.Socket
import java.net.URI
import java.util.Base64
import javax.net.ssl.SSLException

/**
 * Импорт подписок — порт `kapro_vpn/core/subscription.py`.
 *
 * Большинство VPN-провайдеров (dns.army, BMV, AmneziaFree, ...) выдают
 * один URL, который возвращает base64-encoded список share-URL'ов — по
 * одному на строку. Этот модуль скачивает тело, декодирует, парсит
 * каждый share-URL через [ShareUrlParser] и возвращает готовый
 * [SubscriptionResult].
 *
 * Format detection (как в десктопе):
 * - Если тело содержит явные share-URL (vless://, vmess://, ...) →
 *   парсим как plain.
 * - Иначе пробуем base64-decode; если декод даёт scheme'ы — используем.
 *
 * DPI-fallback (десктоп): можно фетчить через локальный xray-tunnel
 * если direct-request заблокирован DPI. На Android пока не нужно —
 * пользователь может сначала включить VPN и потом импортнуть.
 */
object Subscription {

    private val SUPPORTED_SCHEMES = listOf(
        "vless://", "vmess://", "trojan://", "ss://",
        "hysteria2://", "hy2://",
    )

    private const val USER_AGENT =
        "KaproVPN-Android/0.1.0 (Android; +https://github.com/fafnirov/KaproVPN)"

    private const val CONNECT_TIMEOUT_MS = 10_000
    private const val READ_TIMEOUT_MS = 20_000

    /**
     * Результат загрузки и парсинга подписки.
     *
     * @param configs успешно распарсенные ProxyConfig
     * @param errors короткие human-readable строки об ошибках (по одной
     *   на каждый share-URL, который не распарсился)
     * @param rawLines сколько кандидатов мы попытались распарсить —
     *   полезно для UI «5 из 7 распарсилось»
     */
    data class Result(
        val configs: List<ProxyConfig>,
        val errors: List<String>,
        val rawLines: Int,
        /** true если fetch прошёл через локальный xray-туннель (DPI-fallback).
         *  Используется UI чтобы показать «загружено через VPN». */
        val viaProxy: Boolean = false,
    )

    /**
     * Вытаскивает share-URL'ы из тела ответа подписки.
     *
     * Сначала проверяет нет ли в теле уже plain share-URL'ов. Если нет —
     * пробует base64-decode (с автоматическим fix padding'а). Возвращает
     * пустой список если ничего извлечь не удалось.
     */
    fun parseBody(body: String): List<String> {
        val trimmed = body.trim()
        if (trimmed.isEmpty()) return emptyList()

        // Сначала пробуем plain. Если в теле нет ни одного известного
        // scheme — попробуем base64.
        val candidates = mutableListOf(trimmed)
        if (SUPPORTED_SCHEMES.none { sch -> sch in trimmed }) {
            try {
                // Fix padding до длины кратной 4 (как в parser._b64DecodePadded)
                val padded = trimmed + "=".repeat((-trimmed.length).mod(4))
                val normalized = padded.replace('-', '+').replace('_', '/')
                val decoded = Base64.getDecoder().decode(normalized)
                    .toString(Charsets.UTF_8)
                if (SUPPORTED_SCHEMES.any { sch -> sch in decoded }) {
                    // base64 победил → используем его как primary candidate
                    candidates.add(0, decoded)
                }
            } catch (_: Exception) {
                // Не base64 — оставляем только plain
            }
        }

        for (text in candidates) {
            val urls = text.lines()
                .map { it.trim() }
                .filter { line ->
                    line.isNotEmpty() &&
                        !line.startsWith("#") &&
                        SUPPORTED_SCHEMES.any { sch -> line.startsWith(sch) }
                }
            if (urls.isNotEmpty()) return urls
        }
        return emptyList()
    }

    /**
     * Парсит body и собирает результат. Делится с `import` чтобы можно
     * было использовать на manually-pasted body (когда сервер блокирует
     * запросы из приложений — пользователь копирует тело из браузера).
     */
    fun resultFromBody(body: String, viaProxy: Boolean = false): Result {
        val shareUrls = parseBody(body)
        val configs = mutableListOf<ProxyConfig>()
        val errors = mutableListOf<String>()
        for (url in shareUrls) {
            try {
                configs.add(ShareUrlParser.parse(url))
            } catch (e: ParseError) {
                val short = url.take(60) + (if (url.length > 60) "…" else "")
                errors.add("$short — ${e.message}")
            }
        }
        return Result(
            configs = configs,
            errors = errors,
            rawLines = shareUrls.size,
            viaProxy = viaProxy,
        )
    }

    /**
     * Скачать subscription URL + распарсить. Suspend — внутри переключается
     * на [Dispatchers.IO] потому что [HttpURLConnection] блокирующий.
     *
     * @param proxy если задан — fetch идёт через локальный HTTP-proxy
     *   (наш xray http-inbound при активном VPN). null = direct.
     *
     * Бросает [java.io.IOException] на сетевые проблемы (timeout, DNS,
     * 4xx/5xx). UI ловит и показывает.
     */
    suspend fun import(url: String, proxy: Proxy? = null): Result = withContext(Dispatchers.IO) {
        val rawConn = if (proxy != null) {
            URI(url).toURL().openConnection(proxy)
        } else {
            URI(url).toURL().openConnection()
        }
        val conn = (rawConn as HttpURLConnection).apply {
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            requestMethod = "GET"
            instanceFollowRedirects = true
            setRequestProperty("User-Agent", USER_AGENT)
            // Provider'ы иногда возвращают gzip — JVM сама расжмёт если
            // accept-encoding не задан, но явно ставим identity для
            // предсказуемости (избегаем двойного decode).
            setRequestProperty("Accept-Encoding", "identity")
        }
        try {
            val code = conn.responseCode
            if (code !in 200..299) {
                throw java.io.IOException(
                    "HTTP $code от $url: ${conn.responseMessage}"
                )
            }
            val body = conn.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
            resultFromBody(body, viaProxy = proxy != null)
        } finally {
            conn.disconnect()
        }
    }

    /**
     * Импорт с автоматическим DPI-fallback. Pattern с десктопа
     * (`subscription.import_with_dpi_fallback`):
     *
     *   1. Пробуем direct fetch — fast path, не нагружаем туннель зря.
     *   2. Если упало с DPI-сигнатурой (TLS-handshake reset / EOF) И
     *      локальный xray-proxy слушает на [LOCAL_PROXY_HOST]:[LOCAL_PROXY_PORT]
     *      — retry через него. Подписка летит шифрованно через VPN-сервер,
     *      DPI не видит inner request.
     *   3. Если direct упал НЕ из-за DPI — пробрасываем оригинальное
     *      исключение (DNS, 4xx, timeout — не наша проблема).
     *   4. Если direct упал из-за DPI НО туннель не активен — тоже
     *      пробрасываем оригинал чтобы UI показал «подключись сначала».
     */
    suspend fun importWithDpiFallback(
        url: String,
        localProxyHost: String = LOCAL_PROXY_HOST,
        localProxyPort: Int = LOCAL_PROXY_PORT,
    ): Result {
        val directError: Throwable = try {
            return import(url)
        } catch (e: Throwable) { e }

        if (!looksLikeDpiBlock(directError)) {
            throw directError
        }
        if (!probeLocalProxy(localProxyHost, localProxyPort)) {
            // Туннель не активен — fallback некуда. Surface'им оригинальную
            // DPI-ошибку, UI говорит «подключись сначала».
            throw directError
        }
        val proxy = Proxy(Proxy.Type.HTTP, InetSocketAddress(localProxyHost, localProxyPort))
        return import(url, proxy = proxy)
    }

    /** Сигнатура российского DPI: TLS-handshake RST'ится middle of
     *  ClientHello. Наружу всплывает как [SSLException] / SocketException
     *  с фразами "reset" / "EOF" / etc. */
    internal fun looksLikeDpiBlock(err: Throwable): Boolean {
        // Type-based check — точнее чем строки. SSLException + любые wrapping.
        var e: Throwable? = err
        while (e != null) {
            if (e is SSLException) return true
            if (e is java.io.EOFException) return true
            e = e.cause
        }
        // Fallback на substring — wrapping в IOException делает type-check
        // недостаточным.
        val msg = err.message?.lowercase() ?: return false
        return msg.contains("connection reset") ||
            msg.contains("connection aborted") ||
            msg.contains("unexpected_eof") ||
            msg.contains("ssl handshake") ||
            msg.contains("remote host closed")
    }

    /** TCP-probe: что-то слушает [host]:[port]? Короткий timeout (500мс) —
     *  если xray не запущен, не хотим висеть. */
    internal fun probeLocalProxy(host: String, port: Int, timeoutMs: Int = 500): Boolean = try {
        Socket().use { sock ->
            sock.connect(InetSocketAddress(host, port), timeoutMs)
            true
        }
    } catch (_: Throwable) {
        false
    }

    /** Локальный HTTP-inbound xray. Совпадает с
     *  `XrayConfigBuilder.DEFAULT_LISTEN_HOST/PORT`. */
    private const val LOCAL_PROXY_HOST = "127.0.0.1"
    private const val LOCAL_PROXY_PORT = 2080
}
