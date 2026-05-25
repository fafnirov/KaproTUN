# KaproVPN — Android-клиент

Нативный Android-клиент KaproVPN на **Kotlin + Jetpack Compose**. Использует
**Xray-core** как прокси-движок и **VpnService** для системного TUN.

Десктоп-аналог — в `../kapro_vpn/` (Python + PySide6). Архитектурно мы
переносим только `core/` (парсеры share-URL, генератор Xray-конфига,
подписки) — VPN-плоскость пишется с нуля под Android.

## Статус

🚧 **v0.1.0-dev** — Phase 1 закрыта.

**Готово:**
- Gradle 8.10.2 + AGP 8.7.3 + Kotlin 2.0.21 + Compose BOM 2024.10.01 ✓
- Compose Hello-world на тёмной янтарной теме ✓
- `core.ShareUrlParser` — порт `kapro_vpn/core/parser.py`,
  4 протокола (vless, vmess, trojan, ss) + hysteria2.
  Permissive splitter — корректно ест кириллицу/пробелы во fragment ✓
- `core.XrayConfigBuilder` — порт `kapro_vpn/core/xray_config.py`,
  Xray-JSON со split-routing (geoip:private + domain:rules) ✓
- 22 unit-теста, `./gradlew :app:testDebugUnitTest` зелёные ✓
- `default_sites.json` синкается из `../kapro_vpn/data/` build-task'ом ✓

**Дальше (Phase 2):** интеграция libXray (Go-AAR), JNI-мост, VpnService,
hev-socks5-tunnel для TUN → SOCKS5 mapping. Это RealHard™ часть.

## Требования

- Android Studio Ladybug (2024.2) или новее
- JDK 21 (поставляется с Android Studio как `jbr/`)
- Android SDK Platform 34 + build-tools 34.0.0
- Минимум для запуска приложения: Android 7.0 (API 24)

Эмулятор: подойдёт любой x86_64 образ с API 28+. У тебя уже создан AVD
`Iphone_17_pro_max` — он сработает.

## Открыть и собрать

```powershell
# в Android Studio: File → Open → C:\Users\user\Desktop\russian-vpn\android
# дождаться Gradle Sync (~1 минута на первом запуске — Android Studio скачает зависимости)
# Run → Run 'app'
```

Или из командной строки (когда сгенерирован wrapper):

```powershell
cd C:\Users\user\Desktop\russian-vpn\android
.\gradlew.bat installDebug
```

## Структура

```
android/
├── settings.gradle.kts            корень: список модулей, репозитории
├── build.gradle.kts               плагины (без зависимостей)
├── gradle.properties              JVM-флаги Gradle, AndroidX-флаги
├── gradle/libs.versions.toml      version catalog — все версии в одном месте
└── app/
    ├── build.gradle.kts           зависимости приложения, copy-task для default_sites.json
    └── src/main/
        ├── AndroidManifest.xml    permissions + Activity + (TODO) VpnService
        ├── kotlin/pro/kaprovpn/android/
        │   ├── App.kt              Application
        │   ├── MainActivity.kt     ComponentActivity + Compose root
        │   ├── ui/                 экраны Compose, тема (амбер на тёмном)
        │   ├── core/               TODO: парсеры, конфиг-билдер (порт kapro_vpn/core)
        │   └── vpn/                TODO: VpnService, libXray bridge
        └── res/                    drawable, strings, themes, mipmap (адаптивная иконка)
```

## Откуда берётся `default_sites.json`

Из `../kapro_vpn/data/default_sites.json` — один источник правды для всех
клиентов. Gradle-task `copyDefaultSitesJson` в `app/build.gradle.kts`
синкает файл в ассеты при каждой сборке. Не правь копию — правь оригинал.

## Что НЕ переносится с десктопа

- Windows-специфика: `system_proxy.py`, `network_routes.py`,
  `killswitch.py`, `autostart.py`, `admin.py`
- Сабпроцессный запуск Xray (`xray_process.py`) — на Android Xray
  линкуется как `.so` через libXray-AAR, не отдельный процесс
- tun2socks как отдельный exe — на Android используется `hev-socks5-tunnel`
  через JNI (или встроенный в VpnService TUN-handler в самом libXray)
- PySide6 GUI — переписан на Compose

## Версии (см. `gradle/libs.versions.toml`)

| Что | Версия | Почему |
|---|---|---|
| AGP | 8.7.3 | стабильный с Gradle 8.10+ |
| Kotlin | 2.0.21 | встроенный Compose-compiler plugin |
| Compose BOM | 2024.10.01 | согласованный набор Compose-артефактов |
| minSdk | 24 (Android 7.0) | покрытие ~98%, VpnService стабилен |
| targetSdk | 34 (Android 14) | актуальный на момент скаффолда |
