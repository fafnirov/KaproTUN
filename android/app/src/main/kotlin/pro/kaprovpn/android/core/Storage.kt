package pro.kaprovpn.android.core

import android.content.Context
import android.util.Log
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Минимальный storage-слой для Android-клиента.
 *
 * Сейчас (Phase 4) умеет только грузить bundled-список direct-доменов
 * из ассетов. В Phase 5 сюда приедут:
 *   - сохранение/загрузка списка ProxyConfig (DataStore + JSON)
 *   - сохранение настроек (DnsOption.key, autoconnect и т.п.)
 *   - encrypted-at-rest для конфигов (EncryptedSharedPreferences /
 *     Android Keystore — аналог Windows DPAPI на десктопе)
 *
 * Соответствие десктоп-клиенту: эквивалент `core/storage.py`.
 */
object Storage {

    private const val TAG = "Storage"
    private const val ASSET_DEFAULT_SITES = "default_sites.json"

    /**
     * Грузит дефолтный список direct-сайтов из ассетов. Bundled через
     * Gradle copy-task — один источник правды с десктоп-клиентом
     * (`../kapro_vpn/data/default_sites.json`).
     *
     * Возвращает пустой список если файл не найден или сломан. В UI это
     * приводит к тому что split-routing просто не работает (всё через
     * туннель) — лучше чем краш приложения на старте.
     */
    fun loadDefaultSites(context: Context): List<String> {
        val raw = try {
            context.assets.open(ASSET_DEFAULT_SITES).bufferedReader().use { it.readText() }
        } catch (e: Throwable) {
            Log.e(TAG, "Не удалось открыть assets/$ASSET_DEFAULT_SITES", e)
            return emptyList()
        }
        return try {
            // Формат default_sites.json: {"description": "...", "sites": ["a.ru", "b.ru", ...]}
            val root = Json.parseToJsonElement(raw).jsonObject
            val sites = root["sites"] as? JsonArray ?: return emptyList()
            sites
                .map { it.jsonPrimitive.content.trim().lowercase() }
                .filter { it.isNotEmpty() }
        } catch (e: Throwable) {
            Log.e(TAG, "Не удалось распарсить default_sites.json", e)
            emptyList()
        }
    }
}
