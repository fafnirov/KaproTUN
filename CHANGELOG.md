# KaproVPN — Changelog

User-facing changes per release. Most-recent first.

The GitHub Actions release workflow reads the top entry for the body
of each release on the Releases page — so what you see here is what
users see when they click the release tag.

Android и Desktop версии нумеруются отдельно — это разные кодовые базы,
синхронизировано только содержимое `kapro_vpn/data/default_sites.json`
(один источник правды для split-routing).

---

# Android

## v0.1.0 — первый публичный Android-клиент (26 мая 2026)

Первый Android-релиз KaproVPN. Нативный Kotlin+Compose поверх Xray-core
(через libv2ray) + системного VpnService + hev-socks5-tunnel как
tun2socks-моста. ~30 итераций после Phase 1; этот тег фиксирует то, что
готово к раздаче знакомым и в Telegram.

**Подключение и протоколы:**

- 🌐 VLESS (включая REALITY), VMess, Trojan, Shadowsocks (SIP002 +
  legacy), Hysteria2 — те же share-URL что работают на десктопе.
- 🔌 Полноценный системный VPN через VpnService — не proxy-only режим,
  весь трафик идёт через туннель.
- 🧭 Split-routing — 168 RU-доменов из общего `default_sites.json`
  (банки, госуслуги, маркетплейсы) идут напрямую мимо VPN.
- 📵 Per-app VPN — можно исключить отдельные приложения (банковские
  клиенты блочат VPN-IP, у Telegram свой anti-DPI работает лучше).
- 🔄 Auto-reconnect on app launch + on device boot.

**DNS и приватность:**

- 🛡 4 DNS-опции: System / AdGuard (ad-block, ~10k доменов в blackhole) /
  Cloudflare / Quad9 — каждая со своими DoH-серверами.
- 🚫 DNS-leak hardening — публичные резолверы и UDP/TCP port 53
  forced-direct, `log.access: none`.
- 🔒 `configs.json` шифруется AES-256-GCM через Android Keystore.
  Старые plain-конфиги мигрируются прозрачно.

**Серверы:**

- 📥 Импорт подписки (plain / base64 / URL-safe base64) + автообновление
  через WorkManager раз в 12 часов.
- 📷 **QR-сканер** для добавления share-URL — CameraX + ML Kit bundled
  (без Google Play Services). Включая работу на устройствах без GMS
  (Huawei, прошивки, AOSP).
- 📤 **QR-share + Copy + Send-sheet** — на каждом сервере. Закрывает
  цикл «телефон-в-телефон» с QR-сканером.
- ✏️ **In-place edit** конфига (rename + URL update) с сохранением
  active-флага и ping-кэша.
- 🏓 Per-config latency-ping с colour-coded badge.

**Системная интеграция:**

- 🔔 Foreground notification с live-state и кнопкой «Отключить».
- ⚡ Quick Settings tile — один тап toggle'ит VPN.
- 🛡 Always-on VPN compatible — поднимается с null-intent,
  graceful onRevoke().
- 📊 Live traffic stats на главном экране — ↓↑ totals за сессию +
  текущая скорость, опрос libv2ray `queryStats` раз в секунду.

**Локализация и качество:**

- 🇷🇺🇬🇧 i18n RU/EN — переключается по системной локали.
- ✅ 36 юнит-тестов на парсер, конфиг-билдер и подписки.
- 📦 Release pipeline: R8 + ABI splits (arm64/armv7/x86_64/x86 +
  universal). Каждый ABI-APK ~40-44 МБ — лезет в Telegram (50 МБ лимит).
- 🧱 **16 KB ELF page alignment** во всех нативных .so — приложение
  работает нативно на Pixel 8+ и Android 15+ без compatibility-mode.

**Что НЕ работает:**

- WireGuard не поддерживается (вырезан из десктопа в v1.4.0, не
  переносим — для WG используй официальный клиент).
- iOS — отложено пока нет Apple Developer аккаунта ($99/год, без
  него VPN-extension не подписать).

---

# Desktop (Windows + Python)

## v1.11.0 — Реальная защита от IPv6-leak в TUN-режиме

В v1.10.3 я зафиксил IPv6-leak только в нашем UI-probe (мы перестали
показывать v6 IP вместо v4). Реальный leak оставался — приложения с
AAAA-записями (Google, YouTube, Cloudflare, Facebook — почти весь
крупный веб) шли через провайдера по IPv6 мимо туннеля. На Билайне /
МТС / Ростелекоме (где у юзера обычно публичный IPv6) — это значит
провайдер видел куда вы заходите.

Теперь по-настоящему починили.

**Что добавлено:**

- 🆕 Новый модуль `kapro_vpn/core/ipv6_block.py` — устанавливает одно
  Windows Firewall правило при connect в TUN-режиме:

  ```
  netsh advfirewall firewall add rule
      name="KaproVPN-ipv6-block-global"
      dir=out action=block enable=yes profile=any
      remoteip=2000::/3
  ```

  `2000::/3` — это global unicast IPv6 (всё что routable в публичном
  интернете). Link-local (`fe80::/10`), multicast (`ff00::/8`),
  loopback (`::1`), ULA (`fc00::/7`) — **не трогаются**, потому что
  они link-scope и в интернет не уходят в любом случае. Это значит
  **локальная сеть с IPv6 продолжает работать** — NAS, AirPlay,
  принтеры, всё что общается через `fe80::...` или multicast.

- ⚙️ Toggle в Settings → **«Защита от IPv6 leak»**. Включена по
  умолчанию (это безопасный default). Юзер может выключить если
  его конкретный setup требует IPv6 наружу.

- 🧹 Cleanup при следующем запуске. Если KaproVPN рухнул мид-сессии
  с активным правилом — на следующем старте оно автоматически
  удаляется (как уже делает kill-switch). Юзер не остаётся без IPv6
  интернета на следующем boot'е.

- 🔒 Работает **только в TUN-режиме**. В HTTP-режиме браузеры
  используют системный прокси, который работает для v4 и v6
  одинаково — отдельный leak-path для v6 в HTTP-режиме отсутствует,
  поэтому правило не нужно (документировано в `_maybe_arm_ipv6_block`).

**Smoke test:** 2 новых invariant'а:
1. `ipv6_block.is_supported() / install() / remove() / is_active()` —
   surface-методы не должны raise ни на одной OS (на Linux/macOS они
   silent no-op, на Windows без админа возвращают False — но не raise).
2. Регрессионный guard на константу `_IPV6_GLOBAL_UNICAST` — если
   кто-то поменяет с `2000::/3` на `::/0` или `ipv6` (= всё), мы
   сломаем LAN IPv6. Smoke ловит сразу.

**Что юзер увидит:**

После update в TUN-режиме при connect в логах появится:

```
[*] IPv6-leak protection активирована (блок outbound к 2000::/3)
```

В Settings под Kill-switch'ом — новый чекбокс. Если выключить —
v6-трафик опять пойдёт мимо туннеля, но статус честный (юзер сам
выбрал).

**Что ЭТО НЕ закрывает:**

- HTTP-прокси режим — в нём v6 leak's не было изначально (отдельная
  архитектура).
- macOS / Linux — TUN-режим там работает, но IPv6-block нужен через
  `pfctl` / `ip6tables`. Будущая работа.

---

## v1.10.4 — Probe-endpoints, которые реально работают через AdGuard

v1.10.3 показывал `Ваш IP: 46.17.101.82 · —` (страна em-dash) когда
выбран AdGuard. Причина — AdGuard DNS NXDOMAIN'ит **все три** наших
endpoint'а (ipinfo.io / api.ipify.org / ifconfig.co — каждый
классифицируется как "трекинговый/аналитический"). В итоге либо
все три падают (пустая строка), либо в лучшем случае срабатывает
только ipify (даёт IP без страны).

Я думал использовать `https://1.1.1.1/cdn-cgi/trace` (Cloudflare
endpoint по IP-литералу — AdGuard не может NXDOMAIN'ить адрес).
Но в `_ALWAYS_BYPASS` из controller.py уже стоит `1.1.1.1 → direct`
на OS-уровне (для anti-DNS-leak). Это значит ЛЮБОЙ трафик к 1.1.1.1
идёт мимо туннеля → Cloudflare увидел бы реальный IP пользователя,
а не VPN-сервера. Полный провал замысла.

**Что в этом релизе:**

Новые primary endpoints, которые AdGuard обычно не блокирует:

- **`api.myip.com`** (primary) — IP + country code в одном response.
  Простой JSON-сервис, не относится к "tracking analytics", не в
  AdGuard blocklist'ах по моим проверкам.
- **`httpbin.org/ip`** (fallback) — IP-only. Postman-affiliated
  testing service, очень популярен среди разработчиков, ad-блокеры
  его обычно не трогают.

Старые endpoints (ipinfo / ipify / ifconfig.co) оставлены как
fallback — для юзеров на System/Cloudflare/Quad9 DNS они работают
нормально и дают city как бонус.

Также добавил `myip.com` и `httpbin.org` в xray-config allow-rule
(чтобы если adblock-блокировка появится на routing-уровне xray —
наше правило `outboundTag=proxy` перебило её, как для остальных
probe-доменов).

**После обновления:**

С AdGuard DNS — должно показывать **`Ваш IP: 144.x.x.x · <страна>`**
(IP + страна, через api.myip.com). С System/Cloudflare/Quad9 — то же
плюс город (через ipinfo как раньше).

---

## v1.10.3 — Hotfix v1.10.2: probe IPv4-only + release-body теперь Desktop, а не Android

Юзер запустил v1.10.2 и прислал скриншот. Probe сработал, но показал
**`2a00:1370:8192:1aa2:4c8:f2e4:7479:a856 · Россия · Moscow`** — это
**реальный IPv6 от Билайн-Москва**, не IP VPN-сервера. Получилось что
наш UI **наглядно показал утечку IPv6 пользователю**.

Параллельно — увидел что Release page на гитхабе для v1.10.0 → v1.10.2
показывает Android-описание вместо моих desktop-changelog'ов.

**Что починил в этом релизе:**

🔧 **Probe принудительно использует IPv4.** В TUN-режиме мы туннелируем
только IPv4. Если у юзера IPv6 включён (типичная россиская
конфигурация — Билайн, МТС дают public v6), Python socket
предпочитает AAAA-записи при резолве. ipify/ipinfo возвращают AAAA,
запрос идёт по IPv6 **мимо туннеля** через реального провайдера,
и UI показывает реальный IP. Fix: контекст-менеджер `_force_ipv4()`
monkey-patch'ит `socket.getaddrinfo` на `AF_INET` only на время probe.

📦 **Release body на гитхабе теперь правильный.** В CHANGELOG.md есть
две h1-секции — `# Android` (записи параллельной Android-кодовой базы)
и `# Desktop` (наши Python/Windows). Awk-скрипт в release.yml брал
первую `## ` секцию глобально — это оказался Android v0.1.0. Все
наши desktop-релизы (v1.10.0, v1.10.1, v1.10.2) на гитхабе
показывали Android-описание. Новый awk: ищет первую `## ` **внутри
блока `# Desktop`**. Юзеры теперь видят настоящий changelog desktop-фич
на странице релиза.

✅ **Smoke regression guard:** новая проверка что после
`fetch_public_ip` глобальный `socket.getaddrinfo` восстанавливается
к исходному. Без этого guard'а — если кто-то выпилит `finally`
в `_force_ipv4`, весь app начнёт ходить только по IPv4 навсегда.

**⚠ Важная нота про IPv6 leak в реальном трафике:**

То что мы пофиксили в этом релизе — **только probe**. Сам по себе
IPv6 leak в TUN-режиме остаётся: ваши приложения по-прежнему могут
делать AAAA-запросы и идти через провайдера мимо VPN. Если у вас
IPv6 — провайдер видит куда вы заходите.

**Полный fix этого** — отдельная фича для v1.11.0: при connect в
TUN-режиме отключать IPv6 на всех сетевых адаптерах (через
`netsh interface ipv6 set state ... disable`), при disconnect
восстанавливать. Это и было пунктом #2 в моём roadmap-аудите. Делаем
дальше.

---

## v1.10.2 — Hotfix: AdGuard блокировал наш же IP-probe (ipinfo.io)

Логи v1.10.1 показали виновника:

```
[ip-probe] connection failed: Failed to resolve 'ipinfo.io'
    ([Errno 11004] getaddrinfo failed)
```

**`ipinfo.io` в blocklist'е AdGuard DNS.** AdGuard классифицирует
его как «трекинговый домен» (он действительно gather'ит geolocation
data), и через AdGuard DNS возвращает NXDOMAIN. Наша же фича
блокировки рекламы убила нашу же фичу показа IP. Иронично.

**Что починил:**

- 🔁 **Multi-endpoint fallback в ip_probe.** Если первый endpoint
  (ipinfo.io) не отвечает — пробуем ipify.org → ifconfig.co.
  ipify.org это IP-only сервис без аналитики — AdGuard его не
  блокирует. Если основной endpoint накроется, fallback подхватит.
  Каждый endpoint получает свой timeout (~1.7s в дефолте), чтобы
  один мёртвый не съел весь probe-бюджет.

- 🛡 **Allow-rule в xray-config для probe-доменов** ПЕРЕД
  AdGuard-блоком. Если кто-то ещё заблокирует probe на уровне xray
  routing (не DNS) — наше правило `outboundTag=proxy` для
  `domain:ipinfo.io`, `domain:ipify.org` и др. перебьёт блок
  (rule order matters, first match wins).

- 📊 **Улучшенные логи probe** — теперь по каждому endpoint видно
  что произошло: «ipinfo.io: connection failed (blocked/no-DNS),
  trying next» → «OK via ipify.org: 46.x.x.x».

**Что юзер увидит после обновления:**

Если у него **AdGuard выбран** — probe попытается ipinfo.io,
получит NXDOMAIN, перейдёт на ipify.org, получит IP, UI отрендерит
`Ваш IP: 46.x.x.x` (без страны — ipify это IP-only сервис; для
страны нужен ipinfo который AdGuard блокирует — это known trade-off).

Если **выбран не-AdGuard DNS** — ipinfo.io работает как раньше,
показывается IP + страна + город.

Если **все три endpoint'а упали** — лог покажет
`[ip-probe] all endpoints failed. Last error: ...`, UI просто не
показывает строку.

**Архитектурный урок:** наш собственный adblock — это adversarial
network condition. Любая фича которая ходит в интернет должна это
учитывать (multi-endpoint fallback, exception в routing rules, и т.п.).

---

## v1.10.1 — Hotfix v1.10.0: PySocks в bundled-сборке + диагностические логи

Юзер сразу после релиза v1.10.0 написал: «Никаких изменений не вижу
кроме версии — строки `Ваш IP` нет». Две причины (плюс никакой
диагностики чтобы понять которая из них сработала).

**Что починил:**

- 🔧 **PySocks (`socks` модуль) добавлен в `KaproVPN.spec` hiddenimports.**
  `urllib3` импортирует его динамически только когда фактически
  используется `socks5://` URL — PyInstaller'овский static-analysis это
  не видит, и в .exe модуль не попадает. В HTTP-режиме probe тогда
  падает с `Missing dependencies for SOCKS support` (тихо, потому что
  ip_probe.fetch_public_ip всё catch'ит). В TUN-режиме это не было
  проблемой (там SOCKS не используется), но из-под source-сборки или
  pip-окружения работало, из-под bundled .exe в HTTP — нет.

- 🔍 **Диагностические логи для probe.** Раньше при любом failure
  (timeout, network error, missing PySocks, HTTP 4xx/5xx) — silent
  fallback, UI не показывал ничего, и не было способа понять что
  произошло. Теперь probe пишет одну строку в Settings → «Логи
  Xray-core» с точной причиной:

  - `[ip-probe] starting via SOCKS5 127.0.0.1:2081 (HTTP mode)` — старт
  - `[ip-probe] starting direct (TUN mode — kernel routes through tunnel)` — старт TUN
  - `[ip-probe] OK: 46.x.x.x NL Amsterdam` — успех
  - `[ip-probe] timeout after 5.0s` — таймаут (медленный сервер или ipinfo блокирован)
  - `[ip-probe] connection failed: ...` — сеть не пускает (часто = PySocks отсутствует в .exe)
  - `[ip-probe] HTTP 4xx from ipinfo.io` — ipinfo сам отказал
  - `[ip-probe] ipinfo.io response had no 'ip' field` — malformed response

**Как проверить после обновления:**

1. Подключиться к серверу.
2. Подождать 2-3 секунды.
3. Если строка `Ваш IP` так и не появилась — Settings → «Логи
   Xray-core» → внизу будет одна из строк выше с причиной.
4. Скинуть строку → быстрый фикс в следующем патче.

---

## v1.10.0 — Видимое подтверждение что VPN работает: «Ваш IP: X (страна)»

После аудита фич я честно сказал что в KaproVPN **нет визуального
пруфа** что туннель реально работает. Юзер кликает «Подключить» →
видит янтарную кнопку и «Подключено · 16:34 · TUN» — и идёт
проверять через ipleak.net вручную. У NordVPN, Hiddify, Mullvad
сразу показывают «Connected · IP: X.X.X.X · Country».

В v1.10.0 это есть. Под статус-строкой теперь появляется:

> **Ваш IP:** 1.2.3.4  ·  Нидерланды · Amsterdam

Появляется ~2 секунды после connect (xray-у нужно время поднять
inbounds), исчезает на disconnect.

**Как работает:**

- После успешного connect делается один https-запрос к **ipinfo.io/json**
  через VPN-туннель.
- В HTTP-режиме явно роутится через наш локальный SOCKS5 (127.0.0.1:2081),
  чтобы ipinfo увидел IP **VPN-сервера**, а не локальный.
- В TUN-режиме просто идёт через системный routing (трафик и так
  туннелируется).
- На любую ошибку (timeout, DNS, сеть упала) — silently возвращает None,
  UI просто не показывает строку. Никаких модалок, никаких failure-toast.

**Privacy:**

- Один запрос на connect, никаких user-ID, никаких аналитических хуков.
- Endpoint — ipinfo.io, public, third-party (не наша инфра).
- Названия стран локализованы в RU для ~45 типовых VPN-локаций
  (NL → Нидерланды, DE → Германия и т.д.).
- В Settings → «Показывать публичный IP после подключения» есть toggle
  для тех кто хочет полный zero-phone-home: выключаешь → probe не
  делается вообще.

**Технически:** новый модуль `kapro_vpn/core/ip_probe.py`,
`_IpProbeWorker` QThread в `gui/main_window.py` (5-сек timeout,
запускается через `QTimer.singleShot(2000)` после success-connect),
новое поле `public_ip_label` в HomePage, новый toggle в SettingsPage,
дефолт `public_ip_probe: True` в settings.json. Зависимость —
`requests[socks]` (PySocks для SOCKS5 в HTTP-режиме); в TUN-режиме
PySocks не нужен.

**Smoke test:** 2 новых проверки — что `fetch_public_ip` возвращает
None (не raise) на dead-SOCKS endpoint (защита от того что probe
никогда не сломает GUI worker), и что таблица локализации стран
покрывает топ-VPN-локации (NL/DE/US/GB).

---

## v1.9.3 — Onboarding: ссылка «открой гайд» теперь ведёт на работающую страницу

Маленький, но обидный баг. Onboarding-карточка «Нет провайдера →
открой гайд» вела на `kaprovpn.pro/setup` — этой страницы не
существует (404). При том что главная `kaprovpn.pro/` рабочая и
содержит ровно то что нужно: описание проекта, инструкции, ссылку
на партнёра-провайдера (GmailVPN), скачивания.

Поменял `SETUP_GUIDE_URL` с `/setup` на `/`. Никаких новых
страниц не нужно — главная уже работает как онбординг-гайд.

Файлы: `kapro_vpn/gui/onboarding.py` (одна строка + докстринг).

---

## v1.9.2 — Inline-помощник: «А как с YouTube-рекламой?»

После v1.9.1 юзер написал что баннерная реклама ушла, но
**YouTube-ads** всё равно остались. Я ему ответил что это
фундаментальное ограничение — нативная YouTube-реклама идёт с
тех же доменов что и сам контент (`googlevideo.com`), и любая
DNS/SNI-фильтрация её не достанет. Единственный рабочий способ —
**uBlock Origin** в браузере (он работает на уровне DOM, видит
`<video>` с рекламой и скипает).

Юзер логично спросил: «если оно не блокирует YouTube, то получается
твоя AdGuard-опция меня обманула — я думал что блокирует всё». Это
честный feedback — UI обещает «блокирует рекламу», юзер ожидает
что **всю**.

**Что добавил:**

Прямо под опцией «AdGuard» в Настройках теперь появляется
inline-подсказка:

> 📺 **YouTube-реклама всё равно показывается?**
> Это нативные ad'ы — режутся только браузером. Установи
> [**uBlock Origin**](https://ublockorigin.com/) для Chrome/Firefox/Edge
> — 30 секунд, бесплатно, режет YouTube-ads на 100%
> (поверх нашего AdGuard).

Ссылка кликабельная — открывает официальный сайт
ublockorigin.com где юзер выбирает свой браузер и ставит расширение.
Подсказка показывается **только** при выбранном AdGuard — под
Cloudflare/Quad9/System это был бы лишний шум.

**Почему не сделал техническое решение:**

Альтернатива — встроить в KaproVPN HTTPS-MITM-перехватчик с
подменой root CA. Это бы заблокировало YouTube-ads, но:
- Превращает наш VPN из «честный proxy с открытым кодом» в
  «инструмент имеющий доступ ко ВСЕМУ HTTPS-трафику, включая Сбер
  и личную почту». Юзер должен был бы ОЧЕНЬ нам доверять.
- Противоречит privacy-позиционированию (см. SECURITY.md).
- YouTube постоянно меняет API чтобы такие штуки сломались.

Поэтому выбираем правильный путь: прямо в UI указываем рабочее
решение. Честно, конкретно, в момент когда юзер только что увидел
рекламу через нашу AdGuard.

Файлы: `kapro_vpn/gui/main_window.py` (новый QLabel
`_ublock_helper` под AdGuard опцией, toggle visibility в
`_on_dns_option_changed`).

---

## v1.9.1 — AdGuard теперь реально блокирует рекламу (xray routing-block)

После релиза v1.9.0 юзер протестировал: выбрал AdGuard, подключился к
подписочному серверу — реклама на YouTube и Avito **всё равно есть**.
Заметил что на личном VLESS-сервере (где провайдер настроил серверный
adblock) — рекламы нет, а через подписку — есть. То есть v1.9.0
AdGuard опция работает только частично.

**Почему v1.9.0 было недостаточно:**

DNS-override в нашей v1.9.0 переключал DNS-резолвер xray-core на
AdGuard DoH. Это работает для **внутренних** routing-правил xray
(когда xray сам резолвит домен для `domain:foo.bar` rule). Но для
самих приложений — нет. Браузер делает DNS-resolve САМ — через
системный Windows DNS или, чаще, через **встроенный Browser
Secure-DNS** (Chrome / Edge по умолчанию шлёт DoH-запросы прямо к
Cloudflare 1.1.1.1, игнорируя любой DNS установленный на уровне OS
или VPN-клиента). Поэтому наш AdGuard DoH в xray для приложений
был невидим.

**Что в v1.9.1:**

Когда выбрана опция AdGuard, KaproVPN теперь добавляет **routing-rule
в xray** который дропает в blackhole все домены из bundled-списка
`geosite:category-ads-all` (~10 000 ad/tracker-доменов, поддерживается
v2fly community). Это работает **на уровне CONNECT/SNI** — xray
видит куда коннектится приложение по hostname (даже когда DNS уже
произошёл где-то ещё), и блокирует.

Преимущества подхода:
- 🛑 **Работает независимо от Browser Secure-DNS** — потому что блок
  происходит после DNS-resolve'а, на самой connect-операции.
- 🌐 **Работает независимо от VPN-сервера** — не нужен серверный
  adblock у провайдера. Гарантированно блокирует на любом сервере.
- 📦 **Без новых зависимостей** — `geosite.dat` уже скачивается
  KaproVPN'ом с v1.2.4 для своих routing-нужд. Просто используем
  существующий файл.
- 🎯 **Только на AdGuard** — сохраняет чистое позиционирование 4 опций:
  Cloudflare (быстрый, без фильтра), Quad9 (только malware), System
  (без изменений).

**Honest disclosure:** YouTube-реклама всё равно не блокируется. Это
фундаментальное ограничение — YouTube доставляет рекламу с тех же
доменов что и видео (googlevideo.com). Никакой DNS- или
domain-фильтр не справится. Для YouTube — uBlock Origin в браузере.

Файлы: `kapro_vpn/core/xray_config.py` (новое routing-rule под
`if dns_opt.key == "adguard"`), `dns_options.py` (обновлён hint),
`scripts/smoke_test.py` (проверка что rule добавлен ТОЛЬКО для
AdGuard — регрессионный guard, если кто-то выпилит `if`-guard).

---

## v1.9.0 — Настройка DNS: AdGuard / Cloudflare / Quad9

Юзер написал что на одиночном VLESS-конфиге рекламы нет, а через
подписочный сервер она есть. Прикол в DNS — на личных серверах часто
стоит AdGuard-фильтрация, а в массовых подписках обычно нет. Чтобы не
зависеть от того, какой DNS поставил провайдер VPN — мы теперь сами
можем подменить DNS, **и блокировка рекламы работает на любом сервере**.

В Настройки добавлен новый блок **«DNS-сервер»** с четырьмя вариантами:

- 🔘 **Системный** — ничего не меняем (по умолчанию).
- 🛑 **AdGuard** — режет рекламу и трекеры на уровне DNS. Главная
  причина этой фичи: на подписочном VPN наконец-то нет баннеров.
- ⚡ **Cloudflare 1.1.1.1** — самый быстрый публичный DNS, без
  блокировок. Полезно если провайдер раздаёт медленный/мусорный DNS.
- 🛡 **Quad9 (9.9.9.9)** — швейцарский, блокирует фишинг и
  malware-домены (рекламу не трогает).

Когда выбран не-системный вариант:

- В xray-config добавляется блок `dns` с **DoH** (DNS over HTTPS) —
  провайдер видит только зашифрованный трафик к выбранному резолверу,
  ни сами запросы, ни ответы не наблюдаются.
- IP-шки выбранного сервиса добавляются в «direct»-routing rules —
  если приложение делает свой DoH напрямую к ним (Yandex.Browser,
  Chrome), запрос не идёт через VPN-туннель.
- В TUN-режиме DNS принудительно прописывается на TUN-адаптере и в
  bypass-routes (чтобы DoH-over-443 не делал лишний хоп через
  VPN-сервер).

Применяется при следующем подключении (xray надо рестартнуть чтобы
он перечитал dns-блок). Никаких новых сторонних зависимостей — DoH
через xray-core, который и так уже используется.

Файлы: новый `kapro_vpn/core/dns_options.py` (центральная точка
истины для 4 вариантов), правки в `xray_config.py` (новый параметр
`dns_option`, генерация `dns`-блока), `controller.py` (передача в
build_config + override DNS на TUN-адаптере + добавление в
bypass-routes), `storage.py` (новый дефолт `dns_option: system`),
`gui/main_window.py` SettingsPage (новый блок «DNS-сервер»),
`scripts/smoke_test.py` (4 новых проверки в CI — по одной на опцию).

---

## v1.8.4 — Фикс «Не удалось добавить host-route (Windows rc=5010)»

Юзер прислал репро — после некрасивого отключения (краш xray, kill из
Task Manager, ребут посреди сессии) при следующем подключении вылезает:

> Не удалось добавить host-route для VPN-сервера. (Windows rc=5010)

И всё. Подключиться нельзя пока не сделаешь руками
`route delete <ip-сервера>` в админ-PowerShell. Полная блокировка.

**Что происходит:**

В v1.2.6 я уже чинил похожий баг с `ERROR_ALREADY_EXISTS` (183) —
когда в routing-таблице висит точный дубликат маршрута от прошлой
сессии. Тогда auto-recovery было: native delete по тем же
`(dest, mask, next_hop)` → retry create.

Но `5010` (`ERROR_OBJECT_ALREADY_EXISTS`) — это **другой** вид
конфликта. Возникает когда у мёртвой записи **другой `proto`** или
она указывает на **ifIndex несуществующего адаптера** (типично: TUN
от прошлой сессии умер, адаптер исчез, но `/32` маршрут на сервер
остался дангл). Native `DeleteIpForwardEntry` match'ит по всем
полям сразу — и не находит запись потому что мы не знаем мёртвый
ifIndex.

**Что фикшу:**

- 🛠 **Auto-recovery теперь обрабатывает и 5010**: сначала native
  delete (как раньше), потом shell `route delete <dest>` —
  последний снимает запись по destination, игнорируя
  next_hop/index/proto. Дальше retry create. На горячем пути
  никакого оверхеда (5010 — рекавери, не норма).
- 📝 **Понятный текст ошибки на случай если recovery не справится**:
  раньше юзер видел голое `(Windows rc=5010)`, теперь —
  расшифровка кода + точная команда что сделать руками (если
  rebute не охота).
- 🔧 **Тесная связь с v1.8.0 kill-switch**: kill-switch теперь
  тоже снимается чище после некрасивых дисконнектов, плюс
  bypass-route для сервера переустанавливается без блокировки.

Файлы: `kapro_vpn/core/network_routes.py` (новая константа
`_ERROR_OBJECT_ALREADY_EXISTS = 5010`, обновлён `RouteSession.add_route`),
`kapro_vpn/core/controller.py` (hint для 5010).

Спасибо за репро. Без скриншота не нашёл бы — Windows-only баг,
триггерится только после ungraceful disconnect на машине где
раньше уже подключались.

---

## v1.8.3 — Hotfix: v1.8.2 не зарелизился из-за моего бага в smoke-тесте

Тот самый момент когда «делаю тесты чтобы не сломать релиз» сам же
и сломал релиз. Заслужил.

**Что произошло:** в v1.8.2 я добавил 5 installer-тестов в smoke-pipeline
как guard против регрессии из v1.8.1. Один из них — «Maintenance
Reinstall button starts install flow» — синхронно эмитил сигнал
`reinstall_clicked`, который запускает install-worker. На Windows у
меня локально это «как-то работало», на Linux CI-раннере воркер
пытался реально установить KaproVPN (скачать xray, написать в
`%LOCALAPPDATA%`, добавить запись в HKCU) — процесс сегфолтил.

Smoke → exit non-zero → `build` job не стартует (через
`needs: smoke-test`) → `action-gh-release` не запускается → **ни одного
артефакта в Releases для v1.8.2**. Юзер видит v1.8.1 как «последний».

**Что фикшу здесь:**

- 🔧 **smoke-test reinstall-проверка** теперь стабит
  `operations.install_everything` к no-op, ждёт worker через
  `.wait(2000)` чтобы Qt не ругалась на «QThread destroyed while still
  running» (тоже может крашить процесс), и **усиливает assertion** до
  `currentWidget is InstallingPage` — это ровно тот же shape что v1.8.1
  ловит, поэтому теперь если кто-то выпилит `setCurrentWidget` из
  reinstall-пути, smoke поймает.

**Что от v1.8.2 въезжает в этот релиз** (так как v1.8.2 на гитхабе
не появился — фактически вы получаете оба обновления разом):

- ✅ **Кнопка «Удалить KaproVPN» в Maintenance UI теперь работает**
  (был забыт `setCurrentWidget` после `addWidget` в `_build_uninstall_flow`).
- ✅ **Убран лишний чекбокс «Создать ярлык на Рабочем столе»** в
  Maintenance UI — при reinstall ярлык уже есть, создавать дубликат
  на Desktop'е не нужно.
- ✅ **5 installer-тестов в smoke-pipeline** теперь работают целиком
  (4 проходили в v1.8.2, 5-й крашил процесс — теперь все 5 зелёные).

Lesson learned: тестировать smoke-test pipeline через `QT_QPA_PLATFORM=offscreen
python -m kapro_vpn.scripts.smoke_test` ЛОКАЛЬНО **с проверкой exit-кода**
прежде чем пушить тег. Не «у меня всё импортится — наверно ок».

---

## v1.8.2 — Фиксы v1.8.1: кнопка «Удалить» теперь работает

Юзер протестировал v1.8.1 — нашёл что я пропустил:

- 🔴 **Кнопка «Удалить KaproVPN» в Maintenance UI ничего не делала**.
  Confirm-page добавлялась в stack, но я забыл `setCurrentWidget()`.
  Через `--uninstall` флаг работало случайно (пустой stack
  автоматически выбирает первую widget). Через Maintenance → Удалить
  стек уже содержал MaintenancePage, новая страница пряталась.
- 🚫 **Лишний чекбокс «Создать ярлык на Рабочем столе»** в Maintenance
  UI. При reinstall ярлык уже есть, чекбокс создал бы дубликат на
  Desktop'е. Убран — reinstall всегда сохраняет существующие ярлыки.

Также добавил **5 автотестов установщика в smoke-test pipeline**:

- install mode lands on WelcomePage
- maintenance mode lands on MaintenancePage
- uninstall mode lands on confirm page (с проверкой что кнопка
  «Удалить» там есть)
- **Maintenance → Uninstall actually switches page** (явно ловит
  v1.8.1-регрессию)
- Maintenance → Reinstall builds InstallingPage

Эти проверки гоняются в CI до сборки бинарей — следующий раз
сломаю переход внутри установщика, релиз не опубликуется.

Спасибо за то что протестировал — я должен был сам, был обязан.

---

## v1.8.1 — Нормальный Setup.exe: «Переустановить / Удалить»

Раньше повторный запуск `KaproVPN-Setup.exe` показывал тот же
install-flow что и при первой установке. Юзер думает «я же уже
поставил, а оно опять предлагает» — confusing.

**Теперь** Setup.exe детектит существующую установку и показывает
**Maintenance UI** с двумя выборами:

- 🔄 **Переустановить** — поверх текущей. Конфиги/настройки
  сохраняются. Полезно при обновлении на свежую версию.
- 🗑 **Удалить KaproVPN** — те же шаги что были раньше через
  Программы и компоненты (запись в реестре + ярлыки + бинарь).
  Скачанные в `%LOCALAPPDATA%\KaproVPN\` сервисы (xray/tun2socks)
  останутся — удали руками если нужен полный wipe.

Maintenance-страница показывает обе версии (что установлено vs
что в Setup.exe), чтобы было понятно — это апдейт, downgrade или
re-install той же версии.

Поведение `Программы и компоненты → KaproVPN → Удалить` не
изменилось (там и было правильно — флаг `--uninstall` сразу ведёт
на confirm-страницу).

---

## v1.8.0 — Security & privacy pass

**Что нового — приватность:**

- 🔒 **Конфиги теперь шифруются на диске** (Windows DPAPI). Файл
  `configs.json` больше не читается просто через Блокнот — нужен
  доступ под твоим Windows-аккаунтом. То же что Chrome делает с
  сохранёнными паролями. Старые plaintext-конфиги читаются прозрачно
  при первом запуске и автоматом перешифровываются на следующий save.
- 🚫 **Access-log явно отключён в xray** — ни одна строка про
  «кто куда подключался» теперь не пишется на диск. Раньше путь под
  это был зарезервирован, но не использовался; теперь _в нашем
  xray-config стоит явный `"access": "none"`_, регрессия исключена.
- 🛡 **DNS-leak защита усилена** — xray принудительно роутит запросы
  к публичным DNS-резолверам (Cloudflare/Google/Yandex/Quad9) и любым
  TCP/UDP порту 53 через `direct` outbound. Даже если что-то проскочит
  TUN-уровень — провайдер VPN твои DNS-запросы не увидит.
- 📜 **Auto-rotate для xray.log** при превышении 1 МБ. Старый файл
  становится `xray.log.1`, новый стартует пустым. Без этого лог рос
  бесконечно за месяцы непрерывной работы.

**Что нового — прозрачность:**

- 📋 **SECURITY.md** — публичный документ на гитхабе, расписывает что
  мы собираем (ничего), что лежит на диске, что наш mirror логирует и
  как долго, что не защищено. Адрес для responsible disclosure.
- ⏱ **Mirror access-logs ограничены 7 днями** (на нашем VPS, см.
  `server-setup/nginx-log-rotation.md`). Без аналитики, без шеринга.

**Что под капотом:**

- Новый модуль `kapro_vpn/core/secrets_store.py` — обёртка над
  Windows CryptProtectData/CryptUnprotectData (DPAPI). 5 МБ
  зависимости меньше, чем cryptography pip-пакет, ноль внешних libs.
- Mac/Linux configs пока остаются в plaintext (файловые permissions
  0600 — та же защита что у `~/.ssh/config`). DPAPI-эквивалент для
  них (Keychain / libsecret) — future work.

**Известное:**

- Если у тебя стоял KaproVPN до 1.8.0 и в `%LOCALAPPDATA%\KaproVPN\`
  валяется `xray-access.log` от старых версий — удали его руками
  один раз. Новые версии его никогда не создают.

---

## v1.7.1 — Background subscription auto-refresh

Раз в 12 часов KaproVPN тихо обновляет твою сохранённую подписку,
добавляет новые сервера от провайдера. Никаких toast-спамов: уведомление
только если реально что-то добавилось. Удалений нет — даже если fetch
упадёт (DPI, провайдер в дауне), известные рабочие конфиги остаются
на месте.

Отключается в Settings → `subscription_auto_refresh`.

---

## v1.7.0 — CI smoke-test gate

GitHub Actions теперь гоняет smoke-test ДО сборки бинарей. 4 проверки:
импорты, парсер всех share-URL форматов, генерация xray-конфига.
Если падает — релиз не публикуется. Меньше шансов что юзер скачает
сломанный билд.

---

## v1.6.1 — First-launch onboarding

При первом запуске вместо пустого «Конфиг не выбран» — Welcome-экран
с тремя большими карточками: «есть subscription URL», «есть share-URL»,
«нет провайдера → открой гайд». Понятно что делать дальше без чтения
доков.

---

## v1.6.0 — Auto-reconnect

Если xray умер не по нашей команде — KaproVPN сам перезапускает
соединение до 3 раз с backoff 1с/5с/15с. После 3-х провалов — toast
«нужно вручную». User-initiated disconnect отменяет цепочку.

---

## v1.5.2 — EN localization (MVP)

Translate tray + main connect button + Settings. Toggle в Settings
(Auto / English / Русский). Auto-detect из системной локали при
первом запуске. Глубокие диалоги (subscription import body, error
popups) пока на русском — следующая итерация добавит инкрементально.

---

## v1.5.1 — Tray quick-connect

Топ-3 самых быстрых конфига (по пингу) теперь в самом верху меню
трея. Один клик = переключение + подключение, без открытия главного
окна.

---

## v1.5.0 — Real firewall kill-switch

Если включить kill-switch в Settings и подключиться, KaproVPN ставит
3 firewall-правила через `netsh advfirewall`: блокирует весь outbound
кроме LAN и `xray.exe`. Если xray упадёт — у браузера интернет
тоже отвалится, и юзер сразу заметит вместо тихой утечки реального
IP в открытый интернет.

При закрытии KaproVPN или disconnect'е правила снимаются. Если
crashed без cleanup'а — на следующем старте автоматом убираются
(чтобы юзер не остался без интернета).

---

## v1.4.0 — WireGuard removed

После шести версий попыток (gVisor user-space, system WG service,
portable extract, locale-fixes...) — WG so rough in real RU networks
that we cut losses. Если нужен WG — используй официальный WireGuard
for Windows. KaproVPN остаётся клиентом VLESS / Trojan / VMess / SS /
Hysteria2.

---

## v1.3.x — все WG-эксперименты (см. git tag history)

## v1.2.x — попытка WG через xray + mirror на files.kaprovpn.pro

## v1.1.x — cross-platform (mac/Linux), всё ещё Windows-only TUN

## v1.0.x — кросс-платформенные сборки, нейтральные label'ы

## v0.x — Windows MVP, всё то что есть сейчас минус всё что выше
