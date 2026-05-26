# KaproVPN — Android-клиент

Нативный Android-клиент KaproVPN на **Kotlin + Jetpack Compose**.
Использует **Xray-core** (через libv2ray.aar) как прокси-движок,
**hev-socks5-tunnel** как tun2socks-мост и системный **VpnService** под TUN.

Десктоп-аналог — в `../kapro_vpn/` (Python + PySide6, Windows). Архитектурно
переносим только `core/`: парсеры share-URL, генератор Xray-конфига,
работа с подписками. Сетевая плоскость (TUN, routing, lifecycle) написана с
нуля под Android — десктопная Windows-специфика (system_proxy,
network_routes, killswitch, autostart) сюда не идёт.

## Статус

✅ **v0.1.0** — первый публичный signed-релиз. Подписанные ABI-APK
доступны в [GitHub Releases](https://github.com/fafnirov/KaproVPN/releases/tag/android-v0.1.0).

## Что умеет

**Подключение и протоколы:**
- VLESS (включая REALITY с pbk/sid/spx), VMess, Trojan, Shadowsocks
  (SIP002 + legacy base64), Hysteria2/HY2 — парсинг + sing-box-style
  outbound. Совместимо с десктоп-клиентом — те же подписки дают тот же
  результат.
- Полноценный TUN-туннель: VpnService → `libhev-socks5-tunnel.so` →
  локальный SOCKS5 → Xray-core → апстрим. Не proxy-only режим — весь
  системный трафик идёт через VPN.
- Split-routing — 168 RU-доменов (банки, госуслуги, маркетплейсы) идут
  напрямую. Источник — общий `../kapro_vpn/data/default_sites.json`,
  синкается build-task'ом в assets на каждой сборке.

**DNS и приватность:**
- 4 опции DNS: System / AdGuard (ad-block через `geosite:category-ads-all`,
  ~10k доменов в blackhole) / Cloudflare / Quad9 — каждая со своим
  DoH-серверами.
- DNS-leak hardening: запросы к публичным резолверам
  (Cloudflare/Google/Quad9/Yandex) и весь UDP/TCP port 53 forced-direct.
- `log.access: none` — никакой истории браузинга на диске.
- `configs.json` шифруется AES-256-GCM через Android Keystore.
  Старые plain-конфиги автоматически мигрируются на следующий save.
- Per-app split tunneling — пакеты-исключения ходят мимо VPN
  (банковские клиенты, Telegram).

**Подписки и серверы:**
- Импорт subscription URL (plain, base64, URL-safe base64), background
  auto-refresh через WorkManager раз в 12 часов.
- Latency-ping для каждого конфига с colour-coded badge
  (<100мс зелёный, <300мс янтарный, >=300мс красный).
- **QR-сканер** — добавление share-URL камерой через CameraX + ML Kit
  bundled barcode (работает без Google Play Services).
- **QR-share + Copy + system Send-sheet** — на каждой карточке сервера.
  Закрывает цикл «телефон-в-телефон» с QR-сканером.
- **In-place edit** конфига (rename + URL update) — пенсил-кнопка в hero и
  compact-row. Сохраняет active reference при переименовании.

**Системная интеграция:**
- Foreground notification с live-state от XrayBridge и кнопкой
  «Отключить».
- Quick Settings tile — добавляется через системный edit-tiles,
  один тап toggle'ит VPN.
- Autoconnect on app launch + on device boot (BOOT_COMPLETED receiver).
- Always-on VPN compatible — поднимается с null-intent path,
  graceful onRevoke().
- Live traffic stats на Home — ↓↑ totals за сессию + текущая скорость,
  pull-семплинг libv2ray `queryStats` раз в секунду.

**Сборка и распространение:**
- R8 minify + shrink, ABI splits (arm64-v8a / armeabi-v7a / x86_64 / x86 +
  universal). Каждый ABI-APK ~40-44 МБ — лезет в Telegram (50 МБ лимит).
- 16 KB ELF page alignment во всех нативных .so — приложение работает
  на Pixel 8+ и Android 15+ без compatibility-mode.
- i18n RU/EN — UI переключается по системной локали.
- 36 юнит-тестов на парсер, конфиг-билдер и subscription import.

## Сборка

### Из Android Studio

1. `File → Open → <repo>/android`.
2. Дождаться Gradle Sync (~1 минута на первом запуске — скачивается
   libv2ray.aar ~55 МБ из 2dust/AndroidLibXrayLite).
3. `Run → Run 'app'`.

### Из командной строки

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
cd android                               # из корня репо
.\gradlew.bat :app:installDebug          # debug-APK на подключённое устройство
.\gradlew.bat :app:testDebugUnitTest     # юнит-тесты
.\gradlew.bat :app:assembleRelease       # release ABI-splits + universal
```

### Release-APK с подписанием

```powershell
# 1. (один раз) сгенерировать keystore
keytool -genkey -v -keystore android\kaprovpn-release.jks `
    -keyalg RSA -keysize 4096 -validity 10000 -alias kaprovpn

# 2. (один раз) скопировать example + заполнить пути / пароли
Copy-Item android\keystore.properties.example android\keystore.properties
# Открой keystore.properties в редакторе и впиши passwords.

# 3. собрать
cd android
.\gradlew.bat :app:assembleRelease
# → android/app/build/outputs/apk/release/app-{abi}-release.apk
```

Без `keystore.properties` release всё равно собирается, но подписан
debug-key'ем. Установится на чистый телефон, апдейт поверх release с
другой подписью не пройдёт — то есть для personal use ок, для раздачи
из Telegram нужен свой keystore.

## Эмулятор

Стандартный AVD x86_64 с API 28+ работает. У тебя уже создан
`Iphone_17_pro_max` — он подходит.

### Изоляция AVD от десктопного KaproVPN

Если на хосте поднят `KaproTun` (десктопный VPN включён), QEMU/slirp при
старте эмулятора подцепляет его DNS (AdGuard 94.140.14.14) и кеширует
на всю сессию. Когда десктопный VPN выключают — эмулятор теряет DNS
и Chrome ловит `DNS_PROBE_FINISHED_NO_INTERNET`, при том что
Android-приложение сам туннель до сервера проводит через WiFi мимо
`kaprotun`.

Запускать эмулятор лучше через скрипт-обёртку:

```powershell
pwsh .\android\run-emulator.ps1                    # AVD по умолчанию
pwsh .\android\run-emulator.ps1 -Avd Pixel_8_API_34
pwsh .\android\run-emulator.ps1 -NoRoutes          # без admin-прав
```

Скрипт:
- останавливает уже запущенный эмулятор (старые `-dns-server` не применятся);
- запускает с `-dns-server 1.1.1.1,8.8.8.8` (slirp больше не наследует host DNS);
- (с admin'ом) пиннит роуты на эти IP через WiFi с метрикой 1 — даже
  если десктопный VPN захочет перехватить DNS-пакеты, они уйдут мимо;
- по выходу из эмулятора убирает добавленные роуты.

## Архитектура

```
android/
├── settings.gradle.kts            корень: список модулей, репозитории
├── build.gradle.kts               плагины (без зависимостей)
├── gradle.properties              JVM-флаги Gradle, AndroidX-флаги
├── gradle/libs.versions.toml      version catalog — все версии в одном месте
├── run-emulator.ps1               запуск AVD с изоляцией от десктопного VPN
└── app/
    ├── build.gradle.kts           зависимости + copy-task для default_sites.json
    │                              + download-task для libv2ray.aar
    └── src/main/
        ├── AndroidManifest.xml    permissions + Activity + VpnService + tile
        ├── jniLibs/<abi>/         libhev-socks5-tunnel.so (из sockstun 7.0)
        ├── kotlin/pro/kaprovpn/android/
        │   ├── App.kt              Application — init XrayBridge + Repository
        │   ├── MainActivity.kt     Compose root + VPN-permission flow
        │   ├── core/               чистый Kotlin, безопасный для юнит-тестов
        │   │   ├── ShareUrlParser  vless/vmess/trojan/ss/hy2 → ProxyConfig
        │   │   ├── XrayConfigBuilder ProxyConfig → JSON для libv2ray
        │   │   ├── AppRepository   StateFlow-холдер конфигов + settings + ping
        │   │   ├── Storage         JSON load/save, encryption через Keystore
        │   │   ├── Subscription    fetch + parse subscription URL
        │   │   ├── DnsOption       4 опции с DoH / bypass-IPs
        │   │   ├── QrGenerator     ZXing — share-URL → QR Bitmap
        │   │   └── SecretsStore    AES-256-GCM через Android Keystore
        │   ├── ui/                 Compose-экраны
        │   │   ├── AppNav          корневой Scaffold + NavigationBar + sub-screens
        │   │   ├── HomeScreen      большая CONNECT-кнопка + live traffic stats
        │   │   ├── ConfigsScreen   список серверов, FAB-меню (URL/QR/sub)
        │   │   ├── ScanQrScreen    CameraX preview + ML Kit barcode scanner
        │   │   ├── ShareConfigDialog QR + Copy + Send-sheet
        │   │   ├── SubscriptionDialog
        │   │   ├── SettingsScreen  DNS / autoconnect / per-app / about
        │   │   ├── ExcludedAppsScreen
        │   │   ├── LogsScreen      live xray-core output
        │   │   └── theme/          tonal palette (амбер на тёмном)
        │   └── vpn/                Android-специфичный сетевой слой
        │       ├── KaproVpnService extends VpnService — TUN setup + foreground
        │       ├── XrayBridge      singleton-обёртка над libv2ray CoreController
        │       ├── HevTunnel       tun2socks-мост через libhev-socks5-tunnel.so
        │       ├── VpnTileService  Quick Settings tile
        │       ├── BootReceiver    autoconnect на boot
        │       └── SubscriptionRefreshWorker WorkManager — refresh 12h
        └── res/
            ├── values{,-en}/strings.xml   RU source-of-truth + EN-перевод
            ├── drawable*/                  hero, tile-states, notification
            ├── mipmap*/                    адаптивная иконка (launcher)
            └── ... colors/themes
```

### Поток данных при ВКЛЮЧИТЬ

```
HomeScreen.onConnect
  └─ XrayConfigBuilder.buildConfigJson(activeConfig, directDomains, dns)
  └─ MainActivity.requestVpnPermission()
       └─ KaproVpnService.onStartCommand
            ├─ Builder.addAddress/addRoute/setMtu → establish() → tunFd
            ├─ XrayBridge.start(json, tunFd=0)       ← xray-core слушает SOCKS5 на 127.0.0.1:2081
            └─ HevTunnel.start(context, tunFd=fd)    ← читает TUN, форвардит в SOCKS5
                                                       вверх ↑ оба бэгграунд-сервиса
```

Ключевая тонкость: `libv2ray.startLoop(config, tunFd)` НЕ читает пакеты
из TUN — этим занимается `libhev-socks5-tunnel.so` (от
[heiher/sockstun](https://github.com/heiher/sockstun)). Без него xray
работает в proxy-режиме, но системный трафик до него не доходит. Это
была баг-ловушка первой версии («Connected, но веб не открывается»).

## Откуда берётся `default_sites.json`

Из `../kapro_vpn/data/default_sites.json` — один источник правды для всех
клиентов. Gradle-task `copyDefaultSitesJson` в `app/build.gradle.kts`
синкает файл в ассеты при каждой сборке. Не правь копию — правь оригинал.

## Откуда берётся libv2ray.aar

С GitHub Releases [2dust/AndroidLibXrayLite v26.5.19](https://github.com/2dust/AndroidLibXrayLite/releases).
Слишком тяжёлый для git (~55 МБ) — gitignore'нится. Task
`downloadLibV2ray` качает на первый build, дальше UP-TO-DATE.

## Откуда берётся libhev-socks5-tunnel.so

Из release-APK [heiher/sockstun 7.0](https://github.com/heiher/sockstun/releases).
Чекинится в `app/src/main/jniLibs/<abi>/` (~300 КБ × 4 ABI). JNI symbols
ожидают package `hev.sockstun` — нельзя перемещать `TProxyService.kt`
без перекомпиляции .so.

## Что НЕ переносится с десктопа

- Windows-специфика: `system_proxy.py`, `network_routes.py`,
  `killswitch.py`, `autostart.py`, `admin.py`.
- Сабпроцессный запуск Xray (`xray_process.py`) — на Android Xray
  линкуется как `.so` через libv2ray.aar.
- tun2socks как отдельный exe — на Android используется
  `hev-socks5-tunnel` через JNI.
- PySide6 GUI — переписан на Compose.

## Версии (см. `gradle/libs.versions.toml`)

| Что | Версия | Почему |
|---|---|---|
| AGP | 8.7.3 | стабильный с Gradle 8.10+ |
| Kotlin | 2.0.21 | встроенный Compose-compiler plugin |
| Compose BOM | 2024.10.01 | согласованный набор Compose-артефактов |
| CameraX | 1.4.0 | минимум для 16 KB page alignment в `libimage_processing_util_jni.so` |
| ML Kit barcode | 17.3.0 bundled | модель в APK, без Google Play Services |
| ZXing | 3.5.3 (core) | QR-генератор для share-диалога |
| hev-socks5-tunnel | sockstun 7.0 | tun2socks bridge |
| libv2ray | v26.5.19 | Xray-core JNI-биндинги |
| minSdk | 24 (Android 7.0) | покрытие ~98%, VpnService стабилен |
| targetSdk | 34 (Android 14) | актуальный без принудительного 16 KB-режима |
| compileSdk | 34 | CameraX 1.4 ещё работает с этим, 1.5+ требует 35 |

## Troubleshooting

**«Connected, но веб не открывается»** — обычно `HevTunnel` не стартовал
(тihо упал на parse YAML). Смотри logcat по тегу `HevTunnel` — там
видно содержимое `hev-tunnel.log` после tee. Частая причина —
несоответствие `tunnel.ipv4` / MTU в YAML и в `VpnService.Builder`.

**«Android App Compatibility — ELF alignment»** на Android 15+ — должна
быть пофикшена bumpм CameraX до 1.4.0. Если всплывает с другими `.so` —
запустить аудит: `pip install --user pyelftools`, распаковать собранный
APK (`unzip -j app-x86_64-debug.apk 'lib/x86_64/*.so' -d out/`), и в
Python пройти `ELFFile(path).iter_segments()` — каждый `PT_LOAD` сегмент
должен иметь `p_align >= 0x4000` (16 KB). Полный one-liner — в коммите
`9da521e`.

**Подключение к серверу есть, traffic stats на Home показывают
нулевые скорости** — проверь что в xray-config есть `stats: {}` и
`policy.system.statsOutboundUplink/Downlink: true`. Без них
`queryStats("proxy", "uplink")` всегда возвращает 0.

**Эмулятор `DNS_PROBE_FINISHED_NO_INTERNET` после выключения
десктопного KaproVPN** — запустить через `run-emulator.ps1` (см.
секцию «Эмулятор» выше).
