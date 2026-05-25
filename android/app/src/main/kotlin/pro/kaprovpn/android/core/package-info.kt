/**
 * pro.kaprovpn.android.core — порты Python-логики из ../../kapro_vpn/core/.
 *
 * Сюда переедут:
 *   - ProxyConfig (data class)             ← parser.py:ProxyConfig
 *   - ShareUrlParser (vless/vmess/...)     ← parser.py:parse_*
 *   - XrayConfigBuilder                    ← xray_config.py:build_config
 *   - Subscription (импорт подписок)       ← subscription.py
 *   - Storage (DataStore + JSON)           ← storage.py
 *
 * Эти модули НЕ зависят от Android-фреймворка (Context, VpnService и т.п.) —
 * чистая Kotlin/JVM логика, тестируется без эмулятора.
 */
package pro.kaprovpn.android.core
