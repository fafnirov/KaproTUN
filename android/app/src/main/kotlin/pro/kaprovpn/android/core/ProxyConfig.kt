package pro.kaprovpn.android.core

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/**
 * Пользовательский конфиг прокси — результат парсинга одной share-URL.
 *
 * Идентично [kapro_vpn.core.parser.ProxyConfig] в десктоп-клиенте:
 * - [name] — что показывать в UI (из fragment URL или сгенерированное)
 * - [protocol] — vless/vmess/trojan/shadowsocks/hysteria2
 * - [rawUrl] — исходный share-URL без изменений (используется генератором
 *   Xray-конфига для повторного парсинга — sing-box outbound не сохраняет
 *   Xray-специфичные поля типа REALITY spiderX, xhttp mode и т.п.)
 * - [outbound] — sing-box-формата JSON-объект outbound (для совместимости
 *   и кросс-движочной поддержки в будущем)
 */
@Serializable
data class ProxyConfig(
    val name: String,
    val protocol: String,
    val rawUrl: String,
    val outbound: JsonObject,
)

/** Бросается парсерами на синтаксические / семантические ошибки в share-URL. */
class ParseError(message: String, cause: Throwable? = null) : IllegalArgumentException(message, cause)
