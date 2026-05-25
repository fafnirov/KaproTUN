package pro.kaprovpn.android.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Юнит-тесты [ShareUrlParser]. Используют те же канонические share-URL,
 * на которых работает десктоп-клиент. Тесты на чистом JVM — без Android,
 * запускаются `./gradlew test`.
 */
class ShareUrlParserTest {

    // -- VLESS ----------------------------------------------------------------

    @Test
    fun `vless plain TLS`() {
        val url = "vless://uuid-1234@example.com:443?security=tls&type=tcp&sni=example.com#NL%20Server"
        val cfg = ShareUrlParser.parse(url)
        assertEquals("vless", cfg.protocol)
        assertEquals("NL Server", cfg.name)
        assertEquals("example.com", cfg.outbound.str("server"))
        assertEquals(443, cfg.outbound.int("server_port"))
        assertEquals("uuid-1234", cfg.outbound.str("uuid"))
        assertNotNull(cfg.outbound["tls"])
        assertEquals("example.com", (cfg.outbound["tls"] as JsonObject).str("server_name"))
    }

    @Test
    fun `vless with REALITY`() {
        val url = "vless://abc-def@1.2.3.4:443?" +
            "security=reality&pbk=publickey123&sid=shortid&fp=chrome&sni=www.google.com&flow=xtls-rprx-vision&type=tcp" +
            "#RU-Reality"
        val cfg = ShareUrlParser.parse(url)
        assertEquals("vless", cfg.protocol)
        assertEquals("RU-Reality", cfg.name)
        assertEquals("xtls-rprx-vision", cfg.outbound.str("flow"))
        val tls = cfg.outbound["tls"] as JsonObject
        assertEquals("www.google.com", tls.str("server_name"))
        val reality = tls["reality"] as JsonObject
        assertEquals(true, reality["enabled"]?.jsonPrimitive?.content?.toBoolean())
        assertEquals("publickey123", reality.str("public_key"))
        assertEquals("shortid", reality.str("short_id"))
    }

    @Test
    fun `vless with WebSocket transport`() {
        val url = "vless://uuid@host.example:443?security=tls&type=ws&path=%2Fws&host=cdn.example#ws"
        val cfg = ShareUrlParser.parse(url)
        val transport = cfg.outbound["transport"] as JsonObject
        assertEquals("ws", transport.str("type"))
        assertEquals("/ws", transport.str("path"))
        val headers = transport["headers"] as JsonObject
        assertEquals("cdn.example", headers.str("Host"))
    }

    @Test
    fun `vless fragment with cyrillic name`() {
        // Pythoн urlparse permissive — кириллица в #fragment не должна ломать парсер.
        val url = "vless://uuid@host:443?security=tls#Россия-1"
        val cfg = ShareUrlParser.parse(url)
        assertEquals("Россия-1", cfg.name)
    }

    @Test
    fun `vless without fragment uses fallback name`() {
        val cfg = ShareUrlParser.parse("vless://uuid@example.com:443?security=tls")
        assertEquals("vless-example.com", cfg.name)
    }

    // -- VMESS ----------------------------------------------------------------

    @Test
    fun `vmess base64 JSON`() {
        // {"v":"2","ps":"My VMess","add":"vm.example.com","port":"10086","id":"vmess-uuid","aid":"0",
        //  "net":"ws","type":"none","host":"cdn.example.com","path":"/vm","tls":"tls","sni":"sni.example"}
        val json = """{"v":"2","ps":"My VMess","add":"vm.example.com","port":"10086","id":"vmess-uuid","aid":"0","net":"ws","type":"none","host":"cdn.example.com","path":"/vm","tls":"tls","sni":"sni.example"}"""
        val b64 = java.util.Base64.getEncoder().encodeToString(json.toByteArray())
        val cfg = ShareUrlParser.parse("vmess://$b64")
        assertEquals("vmess", cfg.protocol)
        assertEquals("My VMess", cfg.name)
        assertEquals("vm.example.com", cfg.outbound.str("server"))
        assertEquals(10086, cfg.outbound.int("server_port"))
        assertEquals("vmess-uuid", cfg.outbound.str("uuid"))
        assertEquals(0, cfg.outbound.int("alter_id"))
        val tls = cfg.outbound["tls"] as JsonObject
        assertEquals("sni.example", tls.str("server_name"))
        val transport = cfg.outbound["transport"] as JsonObject
        assertEquals("ws", transport.str("type"))
        assertEquals("/vm", transport.str("path"))
    }

    @Test
    fun `vmess URL-safe base64`() {
        val json = """{"v":"2","ps":"X","add":"a.com","port":"443","id":"uuid","aid":"0","net":"tcp"}"""
        val b64 = java.util.Base64.getUrlEncoder().withoutPadding()
            .encodeToString(json.toByteArray())
        val cfg = ShareUrlParser.parse("vmess://$b64")
        assertEquals("X", cfg.name)
        assertEquals("a.com", cfg.outbound.str("server"))
    }

    // -- TROJAN ---------------------------------------------------------------

    @Test
    fun `trojan plain TLS`() {
        val url = "trojan://supersecret@tr.example.com:443?security=tls&sni=tr.example.com#Trojan%20Test"
        val cfg = ShareUrlParser.parse(url)
        assertEquals("trojan", cfg.protocol)
        assertEquals("Trojan Test", cfg.name)
        assertEquals("supersecret", cfg.outbound.str("password"))
        assertEquals("tr.example.com", cfg.outbound.str("server"))
    }

    @Test
    fun `trojan url-encoded password`() {
        val url = "trojan://p%40ss%3Aw0rd@host:443?security=tls#enc"
        val cfg = ShareUrlParser.parse(url)
        assertEquals("p@ss:w0rd", cfg.outbound.str("password"))
    }

    // -- SHADOWSOCKS ----------------------------------------------------------

    @Test
    fun `shadowsocks SIP002 base64 userinfo`() {
        val userinfo = java.util.Base64.getEncoder()
            .encodeToString("aes-256-gcm:supersecret".toByteArray())
        val cfg = ShareUrlParser.parse("ss://$userinfo@ss.example.com:8388#SS%20RU")
        assertEquals("shadowsocks", cfg.protocol)
        assertEquals("SS RU", cfg.name)
        assertEquals("aes-256-gcm", cfg.outbound.str("method"))
        assertEquals("supersecret", cfg.outbound.str("password"))
        assertEquals("ss.example.com", cfg.outbound.str("server"))
        assertEquals(8388, cfg.outbound.int("server_port"))
    }

    @Test
    fun `shadowsocks SIP002 plain userinfo`() {
        // Some clients URL-encode method:password instead of base64
        val cfg = ShareUrlParser.parse("ss://chacha20-ietf-poly1305:pw@1.2.3.4:8388#plain")
        assertEquals("chacha20-ietf-poly1305", cfg.outbound.str("method"))
        assertEquals("pw", cfg.outbound.str("password"))
    }

    @Test
    fun `shadowsocks legacy base64 whole`() {
        val legacy = java.util.Base64.getEncoder()
            .encodeToString("aes-128-gcm:secret@10.0.0.1:8388".toByteArray())
        val cfg = ShareUrlParser.parse("ss://$legacy")
        assertEquals("aes-128-gcm", cfg.outbound.str("method"))
        assertEquals("secret", cfg.outbound.str("password"))
        assertEquals("10.0.0.1", cfg.outbound.str("server"))
        assertEquals(8388, cfg.outbound.int("server_port"))
    }

    @Test
    fun `shadowsocks with plugin`() {
        val userinfo = java.util.Base64.getEncoder()
            .encodeToString("aes-256-gcm:pw".toByteArray())
        val cfg = ShareUrlParser.parse("ss://$userinfo@h:443?plugin=obfs-local;obfs=http#p")
        assertEquals("obfs-local", cfg.outbound.str("plugin"))
        assertEquals("obfs=http", cfg.outbound.str("plugin_opts"))
    }

    // -- HYSTERIA2 ------------------------------------------------------------

    @Test
    fun `hysteria2 with password and obfs`() {
        val url = "hysteria2://mypassword@hy.example.com:36712?sni=hy.example.com&insecure=1" +
            "&obfs=salamander&obfs-password=obfssecret#HY2"
        val cfg = ShareUrlParser.parse(url)
        assertEquals("hysteria2", cfg.protocol)
        assertEquals("HY2", cfg.name)
        assertEquals("mypassword", cfg.outbound.str("password"))
        val obfs = cfg.outbound["obfs"] as JsonObject
        assertEquals("salamander", obfs.str("type"))
        assertEquals("obfssecret", obfs.str("password"))
        val tls = cfg.outbound["tls"] as JsonObject
        assertEquals(true, tls["insecure"]?.jsonPrimitive?.content?.toBoolean())
        // ALPN дефолтится в h3 для hy2
        val alpn = tls["alpn"] as JsonArray
        assertEquals("h3", alpn[0].jsonPrimitive.content)
    }

    @Test
    fun `hy2 alias works`() {
        val cfg = ShareUrlParser.parse("hy2://pw@host:443?sni=host")
        assertEquals("hysteria2", cfg.protocol)
    }

    // -- ERROR CASES ----------------------------------------------------------

    @Test
    fun `unknown scheme throws`() {
        assertThrows(ParseError::class.java) {
            ShareUrlParser.parse("snowflake://something")
        }
    }

    @Test
    fun `wireguard config rejected with helpful message`() {
        val wgConfig = """
            [Interface]
            PrivateKey = abcdef==
            Address = 10.0.0.2/24
        """.trimIndent()
        val ex = assertThrows(ParseError::class.java) {
            ShareUrlParser.parse(wgConfig)
        }
        assertTrue(ex.message?.contains("WireGuard") == true)
    }

    @Test
    fun `wireguard scheme rejected`() {
        assertThrows(ParseError::class.java) { ShareUrlParser.parse("wireguard://blah") }
        assertThrows(ParseError::class.java) { ShareUrlParser.parse("wg://blah") }
    }

    @Test
    fun `vless missing port throws`() {
        assertThrows(ParseError::class.java) {
            ShareUrlParser.parse("vless://uuid@host?security=tls")
        }
    }

    @Test
    fun `trojan missing userinfo throws`() {
        assertThrows(ParseError::class.java) {
            ShareUrlParser.parse("trojan://host:443")
        }
    }

    @Test
    fun `vmess malformed base64 throws`() {
        assertThrows(ParseError::class.java) {
            ShareUrlParser.parse("vmess://not-base64-at-all-!!!")
        }
    }

    // -- splitShareUri internal ---------------------------------------------

    @Test
    fun `splitShareUri handles cyrillic and spaces in fragment`() {
        val u = ShareUrlParser.splitShareUri(
            "vless://uuid@host.ru:443?security=tls#Сервер #2 with spaces"
        )
        assertEquals("vless", u.scheme)
        assertEquals("uuid", u.userInfo)
        assertEquals("host.ru", u.host)
        assertEquals(443, u.port)
        assertEquals("security=tls", u.rawQuery)
        assertEquals("Сервер #2 with spaces", u.fragment)
    }

    @Test
    fun `splitShareUri parses IPv6 host`() {
        val u = ShareUrlParser.splitShareUri("vless://uuid@[2001:db8::1]:443?security=tls")
        assertEquals("2001:db8::1", u.host)
        assertEquals(443, u.port)
    }

    @Test
    fun `b64DecodePadded handles URL-safe alphabet without padding`() {
        // "hello" → standard b64 "aGVsbG8=" → URL-safe no-pad "aGVsbG8"
        val bytes = ShareUrlParser.b64DecodePadded("aGVsbG8")
        assertEquals("hello", String(bytes))
    }
}

// -- helpers --

private fun JsonObject.str(key: String): String? = this[key]?.jsonPrimitive?.content
private fun JsonObject.int(key: String): Int? = this[key]?.jsonPrimitive?.content?.toIntOrNull()
