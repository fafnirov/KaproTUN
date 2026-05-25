package pro.kaprovpn.android.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.net.URLDecoder
import java.util.Base64

/**
 * Парсеры share-URL прокси в sing-box-style outbound JSON.
 *
 * Полный порт `kapro_vpn/core/parser.py` (десктоп-клиент). Поддерживаемые
 * схемы: trojan, vless, vmess, ss (Shadowsocks), hysteria2 / hy2.
 *
 * WireGuard НЕ поддерживается — был выпилен в v1.4.0 десктоп-клиента, не
 * переносим. URL с `[Interface] PrivateKey=` или `wireguard://` отвергаются
 * с явным сообщением.
 *
 * Логика 1-в-1 с Python: каждый парсер возвращает [ProxyConfig] с тем же
 * outbound-словарём, который Python-версия положила бы в sing-box-конфиг.
 * Это важно для совместимости — десктоп и мобила одинаково сериализуют
 * подписки в JSON.
 */
object ShareUrlParser {

    /** Главный диспетчер. По схеме URL выбирает нужный частный парсер. */
    fun parse(url: String): ProxyConfig {
        val text = url.trim()

        // Явный отлуп WireGuard'у — в десктопе тоже бросаем ParseError
        // с подсказкой. Без этого пользователь вставивший WG-конфиг
        // получает невнятную "Unsupported scheme".
        if ("[Interface]" in text && "PrivateKey" in text) {
            throw ParseError(
                "WireGuard в этой версии не поддерживается. " +
                "Для WG-конфигов используй официальный WireGuard-клиент. " +
                "В KaproVPN работают: vless, trojan, vmess, shadowsocks, hysteria2."
            )
        }
        if (text.lowercase().startsWith("wireguard://") || text.lowercase().startsWith("wg://")) {
            throw ParseError("Схема wireguard:// в этой версии не поддерживается.")
        }

        val scheme = text.substringBefore("://", "").lowercase()
        val parser = parsers[scheme]
            ?: throw ParseError(
                "Unsupported scheme '$scheme'. " +
                "Expected one of: ${parsers.keys.joinToString(", ")}"
            )
        return parser(text)
    }

    private val parsers: Map<String, (String) -> ProxyConfig> = mapOf(
        "trojan" to ::parseTrojan,
        "vless" to ::parseVless,
        "vmess" to ::parseVmess,
        "ss" to ::parseShadowsocks,
        "hysteria2" to ::parseHysteria2,
        "hy2" to ::parseHysteria2,
    )

    // ====================================================================
    // --- trojan ---------------------------------------------------------
    // ====================================================================

    internal fun parseTrojan(url: String): ProxyConfig {
        val u = parseUriStrict(url, expectedScheme = "trojan")
        val password = u.userInfo
            ?: throw ParseError("trojan URL needs password@host:port")
        val host = u.host
            ?: throw ParseError("trojan URL needs password@host:port")
        val port = u.port.takeIf { it > 0 }
            ?: throw ParseError("trojan URL needs password@host:port")

        val qs = parseQuery(u.rawQuery)
        val sni = firstQs(qs, "sni", "peer") ?: host
        val alpn = splitAlpn(firstQs(qs, "alpn"))
        val insecure = truthy(firstQs(qs, "allowInsecure", "insecure") ?: "0")
        val utlsFp = firstQs(qs, "fp") ?: ""
        val net = firstQs(qs, "type") ?: "tcp"

        val outbound = buildJsonObject {
            put("type", "trojan")
            put("server", host)
            put("server_port", port)
            put("password", urlDecode(password))
            put("tls", buildTls(sni, insecure = insecure, alpn = alpn, utlsFp = utlsFp))
            buildTransport(net, qs, hostHeaderFallback = host)?.let { put("transport", it) }
        }
        val name = u.fragment?.let { urlDecode(it) }.orEmpty().ifEmpty { "trojan-$host" }
        return ProxyConfig(name = name, protocol = "trojan", rawUrl = url, outbound = outbound)
    }

    // ====================================================================
    // --- vless ----------------------------------------------------------
    // ====================================================================

    internal fun parseVless(url: String): ProxyConfig {
        val u = parseUriStrict(url, expectedScheme = "vless")
        val uuid = u.userInfo
            ?: throw ParseError("vless URL needs uuid@host:port")
        val host = u.host
            ?: throw ParseError("vless URL needs uuid@host:port")
        val port = u.port.takeIf { it > 0 }
            ?: throw ParseError("vless URL needs uuid@host:port")

        val qs = parseQuery(u.rawQuery)
        val security = (firstQs(qs, "security") ?: "none").lowercase()
        val sni = firstQs(qs, "sni", "peer") ?: host
        val alpn = splitAlpn(firstQs(qs, "alpn"))
        val insecure = truthy(firstQs(qs, "allowInsecure", "insecure") ?: "0")
        val utlsFp = firstQs(qs, "fp") ?: ""
        val flow = firstQs(qs, "flow")
        val net = firstQs(qs, "type") ?: "tcp"

        val outbound = buildJsonObject {
            put("type", "vless")
            put("server", host)
            put("server_port", port)
            put("uuid", uuid)
            if (!flow.isNullOrEmpty()) put("flow", flow)

            if (security == "tls" || security == "reality") {
                val realityPbk = if (security == "reality") firstQs(qs, "pbk") ?: "" else ""
                val realitySid = if (security == "reality") firstQs(qs, "sid") ?: "" else ""
                put("tls", buildTls(
                    sni, insecure = insecure, alpn = alpn, utlsFp = utlsFp,
                    realityPbk = realityPbk, realitySid = realitySid,
                ))
            }
            buildTransport(net, qs, hostHeaderFallback = host)?.let { put("transport", it) }
        }
        val name = u.fragment?.let { urlDecode(it) }.orEmpty().ifEmpty { "vless-$host" }
        return ProxyConfig(name = name, protocol = "vless", rawUrl = url, outbound = outbound)
    }

    // ====================================================================
    // --- vmess ----------------------------------------------------------
    // ====================================================================

    internal fun parseVmess(url: String): ProxyConfig {
        if (!url.startsWith("vmess://")) {
            throw ParseError("Not a vmess URL: $url")
        }
        val payload = url.substring("vmess://".length)
        val data: JsonObject = try {
            val decoded = b64DecodePadded(payload).toString(Charsets.UTF_8)
            Json.parseToJsonElement(decoded).jsonObject
        } catch (e: Exception) {
            throw ParseError("vmess payload is not base64 JSON: ${e.message}", e)
        }

        val server = data["add"]?.asString()?.trim().orEmpty()
        val port = data["port"]?.asString()?.toIntOrNull() ?: 0
        val uuid = data["id"]?.asString()?.trim().orEmpty()
        if (server.isEmpty() || port == 0 || uuid.isEmpty()) {
            throw ParseError("vmess JSON missing add/port/id")
        }

        val alterId = data["aid"]?.asString()?.toIntOrNull() ?: 0
        val security = data["scy"]?.asString().orEmpty().ifEmpty { "auto" }
        val net = data["net"]?.asString().orEmpty().ifEmpty { "tcp" }
        val tlsFlag = data["tls"]?.asString()?.lowercase() == "tls"
        val sni = (data["sni"]?.asString().orEmpty()
            .ifEmpty { data["host"]?.asString().orEmpty() }
            .ifEmpty { server })
        val alpn = splitAlpn(data["alpn"]?.asString())
        val utlsFp = data["fp"]?.asString().orEmpty()

        val outbound = buildJsonObject {
            put("type", "vmess")
            put("server", server)
            put("server_port", port)
            put("uuid", uuid)
            put("security", security)
            put("alter_id", alterId)
            if (tlsFlag) put("tls", buildTls(sni, alpn = alpn, utlsFp = utlsFp))

            // Reconstruct a query-string-like map so we can reuse _build_transport
            val qsLike = mapOf(
                "path" to listOf(data["path"]?.asString() ?: "/"),
                "host" to listOf(data["host"]?.asString().orEmpty()),
                "serviceName" to listOf(data["path"]?.asString().orEmpty()),
            )
            buildTransport(net, qsLike, hostHeaderFallback = server)?.let { put("transport", it) }
        }
        val name = data["ps"]?.asString().orEmpty().ifEmpty { "vmess-$server" }
        return ProxyConfig(name = name, protocol = "vmess", rawUrl = url, outbound = outbound)
    }

    // ====================================================================
    // --- shadowsocks ----------------------------------------------------
    // ====================================================================

    internal fun parseShadowsocks(url: String): ProxyConfig {
        if (!url.startsWith("ss://")) {
            throw ParseError("Not an ss URL: $url")
        }
        var after = url.substring("ss://".length)

        // Tail: optional name (#fragment)
        var name = ""
        val hashIdx = after.indexOf('#')
        if (hashIdx >= 0) {
            name = urlDecode(after.substring(hashIdx + 1))
            after = after.substring(0, hashIdx)
        }

        // Tail: optional query
        var query = ""
        val qIdx = after.indexOf('?')
        if (qIdx >= 0) {
            query = after.substring(qIdx + 1)
            after = after.substring(0, qIdx)
        }

        var method = ""
        var password = ""
        var host = ""
        var port = 0

        if ('@' in after) {
            // SIP002: base64-or-plain(method:password) @ host:port
            val atIdx = after.lastIndexOf('@')
            val userinfo = after.substring(0, atIdx)
            val hostport = after.substring(atIdx + 1)
            val decoded = try {
                b64DecodePadded(userinfo).toString(Charsets.UTF_8)
            } catch (_: Exception) {
                urlDecode(userinfo)
            }
            val colonIdx = decoded.indexOf(':')
            if (colonIdx >= 0) {
                method = decoded.substring(0, colonIdx)
                password = decoded.substring(colonIdx + 1)
            } else {
                method = decoded
            }
            val portColon = hostport.lastIndexOf(':')
            if (portColon < 0) throw ParseError("ss URL missing port")
            host = hostport.substring(0, portColon)
            port = hostport.substring(portColon + 1).toIntOrNull()
                ?: throw ParseError("ss URL has non-numeric port")
        } else {
            // Legacy: base64(method:password@host:port)
            val decoded = try {
                b64DecodePadded(after).toString(Charsets.UTF_8)
            } catch (e: Exception) {
                throw ParseError("ss legacy payload is not base64: ${e.message}", e)
            }
            if ('@' !in decoded || ':' !in decoded) {
                throw ParseError("ss legacy URL malformed")
            }
            val atIdx = decoded.lastIndexOf('@')
            val cred = decoded.substring(0, atIdx)
            val hostport = decoded.substring(atIdx + 1)
            val credColon = cred.indexOf(':')
            if (credColon >= 0) {
                method = cred.substring(0, credColon)
                password = cred.substring(credColon + 1)
            }
            val portColon = hostport.lastIndexOf(':')
            if (portColon < 0) throw ParseError("ss URL missing port")
            host = hostport.substring(0, portColon)
            port = hostport.substring(portColon + 1).toIntOrNull()
                ?: throw ParseError("ss URL has non-numeric port")
        }

        if (method.isEmpty() || host.isEmpty() || port == 0) {
            throw ParseError("ss URL missing method/host/port")
        }

        val qs = parseQuery(query)
        val outbound = buildJsonObject {
            put("type", "shadowsocks")
            put("server", host)
            put("server_port", port)
            put("method", method)
            put("password", password)
            firstQs(qs, "plugin")?.let { plugin ->
                if (';' in plugin) {
                    val sep = plugin.indexOf(';')
                    put("plugin", plugin.substring(0, sep))
                    put("plugin_opts", plugin.substring(sep + 1))
                } else {
                    put("plugin", plugin)
                }
            }
        }
        return ProxyConfig(
            name = name.ifEmpty { "ss-$host" },
            protocol = "shadowsocks",
            rawUrl = url,
            outbound = outbound,
        )
    }

    // ====================================================================
    // --- hysteria2 ------------------------------------------------------
    // ====================================================================

    internal fun parseHysteria2(url: String): ProxyConfig {
        val u = parseUriStrict(url, expectedSchemes = setOf("hysteria2", "hy2"))
        val host = u.host
            ?: throw ParseError("hysteria2 URL needs host:port")
        val port = u.port.takeIf { it > 0 }
            ?: throw ParseError("hysteria2 URL needs host:port")

        val qs = parseQuery(u.rawQuery)
        val password = u.userInfo?.let { urlDecode(it) } ?: (firstQs(qs, "auth") ?: "")
        val sni = firstQs(qs, "sni", "peer") ?: host
        val alpn = splitAlpn(firstQs(qs, "alpn") ?: "h3").ifEmpty { listOf("h3") }
        val insecure = truthy(firstQs(qs, "insecure") ?: "0")
        val obfs = firstQs(qs, "obfs")
        val obfsPassword = firstQs(qs, "obfs-password") ?: ""

        val outbound = buildJsonObject {
            put("type", "hysteria2")
            put("server", host)
            put("server_port", port)
            put("password", password)
            put("tls", buildTls(sni, insecure = insecure, alpn = alpn))
            if (!obfs.isNullOrEmpty()) {
                put("obfs", buildJsonObject {
                    put("type", obfs)
                    put("password", obfsPassword)
                })
            }
        }
        val name = u.fragment?.let { urlDecode(it) }.orEmpty().ifEmpty { "hy2-$host" }
        return ProxyConfig(name = name, protocol = "hysteria2", rawUrl = url, outbound = outbound)
    }

    // ====================================================================
    // --- helpers --------------------------------------------------------
    // ====================================================================

    /** Имитация Python urllib.parse.urlparse — permissive, не валидирует
     *  unreserved-символы в fragment/query (java.net.URI это делает строго
     *  и роняется на пользовательских URL с пробелами/кириллицей в # name).
     *
     *  Поля совпадают по именам с java.net.URI, чтобы перенос был механическим. */
    internal data class ShareUri(
        val scheme: String,
        val userInfo: String?,
        val host: String?,
        val port: Int,       // -1 если не указан
        val rawQuery: String?,
        val fragment: String?,
    )

    internal fun splitShareUri(s: String): ShareUri {
        val schemeEnd = s.indexOf("://")
        if (schemeEnd <= 0) throw ParseError("No scheme in URL: $s")
        val scheme = s.substring(0, schemeEnd).lowercase()
        var rest = s.substring(schemeEnd + 3)

        // Fragment first (всё после первого '#')
        var fragment: String? = null
        val hashIdx = rest.indexOf('#')
        if (hashIdx >= 0) {
            fragment = rest.substring(hashIdx + 1)
            rest = rest.substring(0, hashIdx)
        }

        // Query (всё после первого '?')
        var rawQuery: String? = null
        val qIdx = rest.indexOf('?')
        if (qIdx >= 0) {
            rawQuery = rest.substring(qIdx + 1)
            rest = rest.substring(0, qIdx)
        }

        // Path обрезаем (share-URL его не используют — после первого '/' идёт игнор)
        val slashIdx = rest.indexOf('/')
        if (slashIdx >= 0) {
            rest = rest.substring(0, slashIdx)
        }

        // Userinfo (всё до последнего '@')
        var userInfo: String? = null
        val atIdx = rest.lastIndexOf('@')
        if (atIdx >= 0) {
            userInfo = rest.substring(0, atIdx)
            rest = rest.substring(atIdx + 1)
        }

        // Host[:port] — поддержка IPv6 в [квадратных скобках]
        var host: String? = null
        var port = -1
        if (rest.startsWith("[")) {
            val endBracket = rest.indexOf(']')
            if (endBracket < 0) throw ParseError("Unclosed IPv6 bracket: $s")
            host = rest.substring(1, endBracket)
            val tail = rest.substring(endBracket + 1)
            if (tail.startsWith(":")) {
                port = tail.substring(1).toIntOrNull()
                    ?: throw ParseError("Bad port in $s")
            }
        } else if (rest.isNotEmpty()) {
            val portColon = rest.lastIndexOf(':')
            if (portColon >= 0) {
                host = rest.substring(0, portColon)
                port = rest.substring(portColon + 1).toIntOrNull() ?: -1
            } else {
                host = rest
            }
        }

        return ShareUri(
            scheme = scheme,
            userInfo = userInfo,
            host = host?.takeIf { it.isNotEmpty() },
            port = port,
            rawQuery = rawQuery,
            fragment = fragment,
        )
    }

    private fun parseUriStrict(url: String, expectedScheme: String): ShareUri =
        parseUriStrict(url, expectedSchemes = setOf(expectedScheme))

    private fun parseUriStrict(url: String, expectedSchemes: Set<String>): ShareUri {
        val u = splitShareUri(url)
        if (u.scheme !in expectedSchemes) {
            throw ParseError("Not a ${expectedSchemes.first()} URL: $url")
        }
        return u
    }

    /** Парсит querystring в `name -> list of values` (как Python parse_qs). */
    private fun parseQuery(raw: String?): Map<String, List<String>> {
        if (raw.isNullOrEmpty()) return emptyMap()
        val result = mutableMapOf<String, MutableList<String>>()
        for (pair in raw.split('&')) {
            if (pair.isEmpty()) continue
            val eqIdx = pair.indexOf('=')
            val key: String
            val value: String
            if (eqIdx < 0) {
                key = urlDecode(pair)
                value = ""
            } else {
                key = urlDecode(pair.substring(0, eqIdx))
                value = urlDecode(pair.substring(eqIdx + 1))
            }
            result.getOrPut(key) { mutableListOf() }.add(value)
        }
        return result
    }

    private fun firstQs(qs: Map<String, List<String>>, vararg keys: String): String? {
        for (k in keys) {
            val v = qs[k]
            if (!v.isNullOrEmpty()) return v[0]
        }
        return null
    }

    private fun splitAlpn(value: String?): List<String> {
        if (value.isNullOrEmpty()) return emptyList()
        return value.split(',').map { it.trim() }.filter { it.isNotEmpty() }
    }

    private fun truthy(value: String): Boolean =
        value.lowercase() in setOf("1", "true", "yes")

    /** Base64-декодер с padding fix-up + поддержкой URL-safe алфавита. */
    internal fun b64DecodePadded(s: String): ByteArray {
        val normalized = s.trim().replace('-', '+').replace('_', '/')
        val pad = (-normalized.length).mod(4)
        return Base64.getDecoder().decode(normalized + "=".repeat(pad))
    }

    private fun urlDecode(s: String): String = try {
        URLDecoder.decode(s, "UTF-8")
    } catch (_: Exception) {
        s // на повреждённом %-кодировании оставляем как есть, как и Python unquote
    }

    private fun buildTls(
        serverName: String,
        insecure: Boolean = false,
        alpn: List<String> = emptyList(),
        utlsFp: String = "",
        realityPbk: String = "",
        realitySid: String = "",
    ): JsonObject = buildJsonObject {
        put("enabled", true)
        if (serverName.isNotEmpty()) put("server_name", serverName)
        if (insecure) put("insecure", true)
        if (alpn.isNotEmpty()) put("alpn", buildJsonArray { alpn.forEach { add(it) } })
        if (utlsFp.isNotEmpty()) {
            put("utls", buildJsonObject {
                put("enabled", true)
                put("fingerprint", utlsFp)
            })
        }
        if (realityPbk.isNotEmpty()) {
            put("reality", buildJsonObject {
                put("enabled", true)
                put("public_key", realityPbk)
                put("short_id", realitySid)
            })
        }
    }

    /** Транспорт sing-box-стиля. null = plain TCP, ничего не пишем. */
    private fun buildTransport(
        net: String,
        qs: Map<String, List<String>>,
        hostHeaderFallback: String = "",
    ): JsonObject? {
        val type = net.ifEmpty { "tcp" }.lowercase()
        return when (type) {
            "", "tcp", "raw" -> null
            "ws" -> buildJsonObject {
                put("type", "ws")
                put("path", firstQs(qs, "path") ?: "/")
                val host = firstQs(qs, "host") ?: hostHeaderFallback
                if (host.isNotEmpty()) {
                    put("headers", buildJsonObject { put("Host", host) })
                }
            }
            "grpc" -> buildJsonObject {
                put("type", "grpc")
                put("service_name", firstQs(qs, "serviceName", "servicename", "path") ?: "")
            }
            "h2", "http" -> buildJsonObject {
                put("type", "http")
                put("path", firstQs(qs, "path") ?: "/")
                val host = firstQs(qs, "host") ?: hostHeaderFallback
                if (host.isNotEmpty()) {
                    put("host", buildJsonArray {
                        host.split(',').map { it.trim() }.filter { it.isNotEmpty() }
                            .forEach { add(it) }
                    })
                }
            }
            "httpupgrade" -> buildJsonObject {
                put("type", "httpupgrade")
                put("path", firstQs(qs, "path") ?: "/")
                val host = firstQs(qs, "host") ?: hostHeaderFallback
                if (host.isNotEmpty()) put("host", host)
            }
            else -> null
        }
    }

    /** Хелпер: вытащить строку из JsonElement, безопасно. */
    private fun kotlinx.serialization.json.JsonElement.asString(): String? = try {
        jsonPrimitive.content
    } catch (_: Exception) {
        null
    }
}

// Удобство: вызывать как top-level `parse("vless://...")`.
fun parseShareUrl(url: String): ProxyConfig = ShareUrlParser.parse(url)
