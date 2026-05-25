package pro.kaprovpn.android.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.util.Base64

/**
 * Генератор JSON-конфига Xray-core со split-routing.
 *
 * Прямой порт `kapro_vpn/core/xray_config.py`. Логика и форма выхода
 * совпадают побитно с десктоп-клиентом — это критично, потому что:
 *   1. Конфиг идёт в тот же бинарь `xray.so` (через libXray),
 *      ошибки маршрутизации сломают split-routing.
 *   2. Подписки и share-URL должны давать одинаковый результат на
 *      десктопе и мобиле — у пользователя один список конфигов.
 *
 * Парсит [ProxyConfig.rawUrl] заново (а не использует sing-box-формата
 * outbound), потому что Xray-специфичные поля (`xhttp`, REALITY `spiderX`,
 * etc.) в sing-box-конфиге не хранятся.
 */
object XrayConfigBuilder {

    const val DEFAULT_LISTEN_HOST = "127.0.0.1"
    const val DEFAULT_LISTEN_PORT = 2080

    // Дублирующиеся stat-API константы из desktop xray_stats.py — статистика
    // нам понадобится для виджета скорости передачи; пока константы для
    // совместимости с шаблоном.
    private const val API_LISTEN_HOST = "127.0.0.1"
    private const val API_LISTEN_PORT = 18090

    /**
     * Собрать полный Xray client-конфиг со split-routing.
     *
     * `proxy` — что куда подключать (parsed share-URL).
     * `directDomains` — список доменов, идущих в обход прокси (см.
     * `default_sites.json` — банки, госуслуги, маркетплейсы).
     */
    fun buildConfig(
        proxy: ProxyConfig,
        directDomains: List<String>,
        listenHost: String = DEFAULT_LISTEN_HOST,
        listenPort: Int = DEFAULT_LISTEN_PORT,
        logLevel: String = "warning",
        logFile: String? = null,
    ): JsonObject {
        val proxyOutbound = proxyToXrayOutbound(proxy)
        val cleaned = directDomains
            .map { it.trim().lowercase() }
            .filter { it.isNotEmpty() }
            .toSortedSet()
        val domainRules = cleaned.map { "domain:$it" }

        val rules = buildJsonArray {
            // API-inbound → API-outbound. Идёт ПЕРВЫМ, иначе stats-запросы
            // улетят в прокси и потеряются.
            add(buildJsonObject {
                put("type", "field")
                put("inboundTag", buildJsonArray { add("api-in") })
                put("outboundTag", "api")
            })
            // Блочим link-local discovery шум. NetBIOS/mDNS/SSDP в туннеле
            // не место — генерирует сотни пакетов/сек на чистой машине.
            add(buildJsonObject {
                put("type", "field")
                put("outboundTag", "block")
                put("port", "137-139,1900,5353")
            })
            add(buildJsonObject {
                put("type", "field")
                put("outboundTag", "block")
                put("ip", buildJsonArray {
                    add("224.0.0.0/4")
                    add("255.255.255.255/32")
                })
            })
            // Приватные подсети — мимо прокси (RFC1918, link-local).
            add(buildJsonObject {
                put("type", "field")
                put("ip", buildJsonArray { add("geoip:private") })
                put("outboundTag", "direct")
            })
            // Direct-домены из списка.
            if (domainRules.isNotEmpty()) {
                add(buildJsonObject {
                    put("type", "field")
                    put("domain", buildJsonArray { domainRules.forEach { add(it) } })
                    put("outboundTag", "direct")
                })
            }
        }

        return buildJsonObject {
            put("log", buildJsonObject {
                put("loglevel", logLevel)
                if (!logFile.isNullOrEmpty()) put("error", logFile)
            })
            put("stats", buildJsonObject {})
            put("policy", buildJsonObject {
                put("system", buildJsonObject {
                    put("statsInboundUplink", true)
                    put("statsInboundDownlink", true)
                    put("statsOutboundUplink", true)
                    put("statsOutboundDownlink", true)
                })
            })
            put("api", buildJsonObject {
                put("tag", "api")
                put("services", buildJsonArray { add("StatsService") })
            })
            put("inbounds", buildJsonArray {
                add(buildJsonObject {
                    put("tag", "http-in")
                    put("listen", listenHost)
                    put("port", listenPort)
                    put("protocol", "http")
                    put("settings", buildJsonObject {
                        put("allowTransparent", false)
                    })
                    put("sniffing", buildJsonObject {
                        put("enabled", true)
                        put("destOverride", buildJsonArray {
                            add("http"); add("tls")
                        })
                        put("routeOnly", false)
                    })
                })
                add(buildJsonObject {
                    put("tag", "socks-in")
                    put("listen", listenHost)
                    put("port", listenPort + 1)
                    put("protocol", "socks")
                    put("settings", buildJsonObject {
                        put("udp", true)
                        put("auth", "noauth")
                    })
                    put("sniffing", buildJsonObject {
                        put("enabled", true)
                        put("destOverride", buildJsonArray {
                            add("http"); add("tls")
                        })
                        put("routeOnly", false)
                    })
                })
                add(buildJsonObject {
                    put("tag", "api-in")
                    put("listen", API_LISTEN_HOST)
                    put("port", API_LISTEN_PORT)
                    put("protocol", "dokodemo-door")
                    put("settings", buildJsonObject {
                        put("address", API_LISTEN_HOST)
                    })
                })
            })
            put("outbounds", buildJsonArray {
                add(proxyOutbound)
                add(buildJsonObject {
                    put("tag", "direct")
                    put("protocol", "freedom")
                })
                add(buildJsonObject {
                    put("tag", "block")
                    put("protocol", "blackhole")
                })
            })
            put("routing", buildJsonObject {
                put("domainStrategy", "IPIfNonMatch")
                put("rules", rules)
            })
        }
    }

    /** Сериализованный pretty-JSON — то, что отдаём libXray на старте. */
    fun buildConfigJson(
        proxy: ProxyConfig,
        directDomains: List<String>,
        listenHost: String = DEFAULT_LISTEN_HOST,
        listenPort: Int = DEFAULT_LISTEN_PORT,
        logLevel: String = "warning",
        logFile: String? = null,
    ): String {
        val cfg = buildConfig(proxy, directDomains, listenHost, listenPort, logLevel, logFile)
        return prettyJson.encodeToString(JsonElement.serializer(), cfg)
    }

    @OptIn(kotlinx.serialization.ExperimentalSerializationApi::class)
    private val prettyJson = Json {
        prettyPrint = true
        prettyPrintIndent = "  "
        encodeDefaults = true
    }

    // =====================================================================
    // --- per-protocol converters ----------------------------------------
    // =====================================================================

    internal fun proxyToXrayOutbound(cfg: ProxyConfig): JsonObject {
        val scheme = cfg.rawUrl.substringBefore("://", "").lowercase()
        return when (scheme) {
            "vless" -> vlessToXray(cfg.rawUrl)
            "vmess" -> vmessToXray(cfg.rawUrl)
            "trojan" -> trojanToXray(cfg.rawUrl)
            "ss" -> ssToXray(cfg.rawUrl)
            "hysteria2", "hy2" -> throw NotImplementedError(
                "Xray-core не поддерживает Hysteria2. Используй v2/hy2-совместимый клиент " +
                "или жди добавления второго движка (sing-box)."
            )
            else -> throw IllegalArgumentException("Unknown protocol scheme: $scheme")
        }
    }

    private fun vlessToXray(url: String): JsonObject {
        val u = ShareUrlParser.splitShareUri(url)
        val qs = parseQuery(u.rawQuery)
        val flow = firstQs(qs, "flow")
        return buildJsonObject {
            put("tag", "proxy")
            put("protocol", "vless")
            put("settings", buildJsonObject {
                put("vnext", buildJsonArray {
                    add(buildJsonObject {
                        put("address", u.host.orEmpty())
                        put("port", u.port)
                        put("users", buildJsonArray {
                            add(buildJsonObject {
                                put("id", u.userInfo.orEmpty())
                                put("encryption", firstQs(qs, "encryption") ?: "none")
                                if (!flow.isNullOrEmpty()) put("flow", flow)
                            })
                        })
                    })
                })
            })
            put("streamSettings", buildStreamSettings(qs, serverFallback = u.host.orEmpty()))
        }
    }

    private fun vmessToXray(url: String): JsonObject {
        val payload = url.substring("vmess://".length)
        val decoded = b64DecodePadded(payload).toString(Charsets.UTF_8)
        val data = Json.parseToJsonElement(decoded).jsonObject

        // Симулируем querystring под общий _buildStreamSettings:
        val tlsStr = data["tls"]?.contentOrNull()?.lowercase()
        val qsLike: Map<String, List<String>> = mapOf(
            "type" to listOf(data["net"]?.contentOrNull() ?: "tcp"),
            "security" to listOf(if (tlsStr == "tls") "tls" else "none"),
            "sni" to listOf(
                data["sni"]?.contentOrNull()
                    ?: data["host"]?.contentOrNull()
                    ?: data["add"]?.contentOrNull()
                    ?: ""
            ),
            "alpn" to listOf(data["alpn"]?.contentOrNull() ?: ""),
            "fp" to listOf(data["fp"]?.contentOrNull() ?: ""),
            "path" to listOf(data["path"]?.contentOrNull() ?: "/"),
            "host" to listOf(data["host"]?.contentOrNull() ?: ""),
            "serviceName" to listOf(data["path"]?.contentOrNull() ?: ""),
        )

        return buildJsonObject {
            put("tag", "proxy")
            put("protocol", "vmess")
            put("settings", buildJsonObject {
                put("vnext", buildJsonArray {
                    add(buildJsonObject {
                        put("address", data["add"]?.contentOrNull().orEmpty())
                        put("port", data["port"]?.contentOrNull()?.toIntOrNull() ?: 0)
                        put("users", buildJsonArray {
                            add(buildJsonObject {
                                put("id", data["id"]?.contentOrNull().orEmpty())
                                put("alterId", data["aid"]?.contentOrNull()?.toIntOrNull() ?: 0)
                                put("security", data["scy"]?.contentOrNull() ?: "auto")
                            })
                        })
                    })
                })
            })
            put("streamSettings", buildStreamSettings(qsLike, serverFallback = data["add"]?.contentOrNull().orEmpty()))
        }
    }

    private fun trojanToXray(url: String): JsonObject {
        val u = ShareUrlParser.splitShareUri(url)
        // Trojan по умолчанию TLS — если security не указана, явно ставим.
        val rawQs = parseQuery(u.rawQuery).toMutableMap()
        if ("security" !in rawQs) rawQs["security"] = listOf("tls")

        return buildJsonObject {
            put("tag", "proxy")
            put("protocol", "trojan")
            put("settings", buildJsonObject {
                put("servers", buildJsonArray {
                    add(buildJsonObject {
                        put("address", u.host.orEmpty())
                        put("port", u.port)
                        put("password", urlDecode(u.userInfo.orEmpty()))
                    })
                })
            })
            put("streamSettings", buildStreamSettings(rawQs, serverFallback = u.host.orEmpty()))
        }
    }

    private fun ssToXray(url: String): JsonObject {
        var after = url.substring("ss://".length)
        if ('#' in after) after = after.substringBefore('#')
        val query = if ('?' in after) after.substringAfter('?').also { after = after.substringBefore('?') } else ""

        var method = ""
        var password = ""
        var host = ""
        var port = 0
        if ('@' in after) {
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
            }
            val portColon = hostport.lastIndexOf(':')
            host = hostport.substring(0, portColon)
            port = hostport.substring(portColon + 1).toInt()
        } else {
            val decoded = b64DecodePadded(after).toString(Charsets.UTF_8)
            val atIdx = decoded.lastIndexOf('@')
            val cred = decoded.substring(0, atIdx)
            val hostport = decoded.substring(atIdx + 1)
            val credColon = cred.indexOf(':')
            method = cred.substring(0, credColon)
            password = cred.substring(credColon + 1)
            val portColon = hostport.lastIndexOf(':')
            host = hostport.substring(0, portColon)
            port = hostport.substring(portColon + 1).toInt()
        }

        val qs = parseQuery(query)
        return buildJsonObject {
            put("tag", "proxy")
            put("protocol", "shadowsocks")
            put("settings", buildJsonObject {
                put("servers", buildJsonArray {
                    add(buildJsonObject {
                        put("address", host)
                        put("port", port)
                        put("method", method)
                        put("password", password)
                    })
                })
            })
            put("streamSettings", buildStreamSettings(qs, serverFallback = host))
        }
    }

    // =====================================================================
    // --- streamSettings (shared) ----------------------------------------
    // =====================================================================

    private val knownNetworks = setOf("tcp", "raw", "ws", "grpc", "h2", "http", "xhttp", "httpupgrade")

    private fun buildStreamSettings(
        qs: Map<String, List<String>>,
        serverFallback: String,
        defaultNetwork: String = "tcp",
    ): JsonObject {
        var network = (firstQs(qs, "type") ?: defaultNetwork).lowercase()
        if (network == "raw") network = "tcp"
        if (network == "http") network = "h2"
        if (network !in knownNetworks) network = "tcp"

        val security = (firstQs(qs, "security") ?: "none").lowercase()
        val sni = firstQs(qs, "sni", "peer") ?: serverFallback
        val alpn = splitCsv(firstQs(qs, "alpn"))
        val fp = firstQs(qs, "fp") ?: ""
        val insecure = truthy(firstQs(qs, "allowInsecure", "insecure") ?: "0")

        return buildJsonObject {
            put("network", network)

            // --- security layer ---
            when (security) {
                "tls" -> {
                    put("security", "tls")
                    put("tlsSettings", buildJsonObject {
                        put("serverName", sni)
                        if (alpn.isNotEmpty()) {
                            put("alpn", buildJsonArray { alpn.forEach { add(it) } })
                        }
                        if (fp.isNotEmpty()) put("fingerprint", fp)
                        if (insecure) put("allowInsecure", true)
                    })
                }
                "reality" -> {
                    put("security", "reality")
                    put("realitySettings", buildJsonObject {
                        put("serverName", sni)
                        put("publicKey", firstQs(qs, "pbk") ?: "")
                        put("shortId", firstQs(qs, "sid") ?: "")
                        put("fingerprint", fp.ifEmpty { "chrome" })
                        firstQs(qs, "spx")?.let { if (it.isNotEmpty()) put("spiderX", it) }
                    })
                }
                else -> put("security", "none")
            }

            // --- transport layer ---
            when (network) {
                "ws" -> put("wsSettings", buildJsonObject {
                    put("path", firstQs(qs, "path") ?: "/")
                    val host = firstQs(qs, "host") ?: serverFallback
                    if (host.isNotEmpty()) {
                        put("headers", buildJsonObject { put("Host", host) })
                    }
                })
                "grpc" -> put("grpcSettings", buildJsonObject {
                    put("serviceName", firstQs(qs, "serviceName", "servicename", "path") ?: "")
                })
                "h2" -> {
                    val hosts = splitCsv(firstQs(qs, "host") ?: serverFallback)
                        .ifEmpty { listOf(serverFallback) }
                    put("httpSettings", buildJsonObject {
                        put("host", buildJsonArray { hosts.forEach { add(it) } })
                        put("path", firstQs(qs, "path") ?: "/")
                    })
                }
                "xhttp" -> put("xhttpSettings", buildJsonObject {
                    put("path", firstQs(qs, "path") ?: "/")
                    put("mode", firstQs(qs, "mode") ?: "auto")
                    firstQs(qs, "host")?.takeIf { it.isNotEmpty() }?.let { put("host", it) }
                })
                "httpupgrade" -> put("httpupgradeSettings", buildJsonObject {
                    put("path", firstQs(qs, "path") ?: "/")
                    put("host", firstQs(qs, "host") ?: serverFallback)
                })
                // plain "tcp" — no extra block
            }
        }
    }

    // =====================================================================
    // --- helpers (small dupes from ShareUrlParser to keep this self-contained)
    // =====================================================================

    private fun parseQuery(raw: String?): Map<String, List<String>> {
        if (raw.isNullOrEmpty()) return emptyMap()
        val result = mutableMapOf<String, MutableList<String>>()
        for (pair in raw.split('&')) {
            if (pair.isEmpty()) continue
            val eq = pair.indexOf('=')
            val key = if (eq < 0) urlDecode(pair) else urlDecode(pair.substring(0, eq))
            val value = if (eq < 0) "" else urlDecode(pair.substring(eq + 1))
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

    private fun splitCsv(value: String?): List<String> {
        if (value.isNullOrEmpty()) return emptyList()
        return value.split(',').map { it.trim() }.filter { it.isNotEmpty() }
    }

    private fun truthy(value: String): Boolean =
        value.lowercase() in setOf("1", "true", "yes")

    private fun urlDecode(s: String): String = try {
        java.net.URLDecoder.decode(s, "UTF-8")
    } catch (_: Exception) {
        s
    }

    private fun b64DecodePadded(s: String): ByteArray {
        val normalized = s.trim().replace('-', '+').replace('_', '/')
        val pad = (-normalized.length).mod(4)
        return Base64.getDecoder().decode(normalized + "=".repeat(pad))
    }
}

/** Безопасно дёрнуть `.jsonPrimitive.content` — для JsonElement, который может
 *  быть null или не-примитивом. */
private fun JsonElement.contentOrNull(): String? = try {
    (this as? kotlinx.serialization.json.JsonPrimitive)?.content
} catch (_: Exception) {
    null
}
