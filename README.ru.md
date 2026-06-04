# KaproVPN

[![Релиз](https://img.shields.io/github/v/release/fafnirov/KaproVPN?style=flat-square&color=f59e0b&label=latest)](https://github.com/fafnirov/KaproVPN/releases/latest)
[![Лицензия](https://img.shields.io/github/license/fafnirov/KaproVPN?style=flat-square&color=blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://www.python.org/)
[![Сборка](https://img.shields.io/github/actions/workflow/status/fafnirov/KaproVPN/release.yml?style=flat-square&label=build)](https://github.com/fafnirov/KaproVPN/actions/workflows/release.yml)

[English](README.md) · [Русский](README.ru.md)

Кросс-платформенный proxy-клиент (Windows / macOS / Linux) со
встроенным **split-routing'ом по настраиваемому списку прямых сайтов**.
Построен поверх [Xray-core](https://github.com/XTLS/Xray-core).
Бесплатный и open-source — GPL v3, без платных уровней, без телеметрии.

<p align="center">
  <img src="docs/screenshots/main-window.png" alt="Главное окно KaproVPN — тёмная тема, одно-экранный layout" width="640">
</p>

---

### ⬇️ Скачать

Последний стабильный релиз — выбери файл под свою систему:

| OS | Файл | Заметки |
|----|------|---------|
| **Windows 10 / 11 (x64)** | [`KaproVPN-Setup.exe`](https://github.com/fafnirov/KaproVPN/releases/latest) | Per-user установка, админа не нужно |
| **macOS (Apple Silicon)** | [`KaproVPN-macOS-arm64.dmg`](https://github.com/fafnirov/KaproVPN/releases/latest) | Перетащить в Applications |
| **Linux (x64)** | [`KaproVPN-Linux-x64.AppImage`](https://github.com/fafnirov/KaproVPN/releases/latest) | `chmod +x` и запустить |

TUN-режим (туннелировать все приложения системно — Telegram, Steam, игры)
требует прав администратора/root. HTTP-прокси режим работает без админа
и туннелирует трафик браузера.

#### ⚠️ Windows SmartScreen ругается при первом запуске

Когда запускаешь `KaproVPN-Setup.exe`, Windows Defender SmartScreen
может выдать **«Система Windows защитила ваш компьютер»** и не дать
запустить. Это потому что мы **не платим Microsoft $300/год** за
EV code-signing сертификат — это бесплатный OSS-проект, не коммерческий.
Чтобы продолжить:

1. На окне SmartScreen нажми **«Подробнее»**
2. Нажми **«Выполнить в любом случае»**

Делать это нужно один раз на каждый релиз. На macOS аналогичное
окно **«разработчик не идентифицирован»** — правый клик по `.dmg` →
**Открыть** → **Открыть** (тоже одноразово).

---

## Что делает

GUI для proxy/VPN-соединений (Trojan, VLESS с REALITY и XHTTP, VMess,
Shadowsocks, Hysteria2) с одной важной фичей: домены из настраиваемого
списка идут **в обход прокси**, через ваш реальный IP. Всё остальное
маршрутизируется через прокси-сервер.

## Зачем

Когда вы подключаетесь через иностранный прокси, часть сервисов
отказывается работать — у них geofence по конкретной стране (банки,
госпорталы, маркетплейсы). Выключать VPN каждый раз, когда нужно к ним
обратиться, неудобно. KaproVPN держит прокси включённым для открытого
интернета, а сайты из вашего списка прямого подключения видят ваш
реальный адрес.

## Возможности

- 🔌 **Все основные форматы share-URL** — `vless://` (включая REALITY
  и XHTTP), `trojan://`, `vmess://`, `ss://`, `hysteria2://`
- 📥 **Импорт subscription-URL** — вставляешь один URL, получаешь все
  конфиги от провайдера. Фоновый авто-рефреш раз в 12 часов
  (additive-only — рабочие конфиги никогда не удаляются).
- 🛡 **Реальный firewall kill-switch** — если туннель умрёт, Windows
  Firewall заблокирует весь outbound кроме процесса активного движка:
  `sing-box.exe` для движка sing-box TUN, либо `xray.exe` (+ `hysteria.exe`
  для Hysteria2) для legacy-движка и HTTP-режима. Никаких тихих утечек
  реального IP.
- 🔁 **Auto-reconnect** — прозрачно переподключает до 3 раз с backoff
  если активный движок упал посреди сессии.
- 🔒 **Конфиги шифруются на диске** — Windows DPAPI (тот же механизм
  которым Chrome шифрует сохранённые пароли). Старые plaintext-конфиги
  автоматом перешифровываются при первом запуске.
- 🌐 **Два режима подключения** —
  - **HTTP-прокси** (без админа) — браузер + приложения, которые умеют
    системный прокси, через Xray
  - **TUN** (нужны админ/root) — туннелирует все приложения, включая игры
    и Telegram. Движок по умолчанию — **sing-box native TUN**: один процесс
    владеет TUN-устройством и сам роутит, без локального SOCKS-моста
    (`127.0.0.1:2081`) и без отдельного tun2socks. **Legacy-движок
    Xray + tun2socks** остаётся ручным fallback'ом в Настройках → Движок TUN.
- ✏️ **Редактируемый список «всегда напрямую»** доменов — 108 разумных
  дефолтов (банки, госуслуги, маркетплейсы, медиа).
- 📡 **Tray quick-connect** — топ-3 самых быстрых конфига по пингу в
  меню трея, один клик = переключение.
- 🌍 **EN / RU локализация** — автоопределение из системной локали,
  переключение в Settings.
- 📊 **Живая статистика трафика + пинг на каждый конфиг** в UI.
- 🔄 **In-app auto-update** — проверяет GitHub Releases, скачивает,
  ставит.

## Приватность

Коротко: **мы не собираем ничего.** Никакой аналитики, никакой
телеметрии, никакого удалённого логирования. Конфиги шифруются на
диске на Windows. Access-log Xray явно отключён в нашем конфиге
(никаких per-domain логов на твоём диске). Опциональный mirror
для скачивания `kaprovpn.pro/files` хранит nginx access-логи 7 дней,
потом удаляет; fallback на GitHub доступен всегда.

Полные детали в [SECURITY.md](SECURITY.md), включая адрес для
responsible disclosure.

## Требования

| OS | Минимум |
|----|---------|
| Windows | 10 / 11 (x64) |
| macOS | 12+ (Apple Silicon) |
| Linux | glibc 2.31+ (Ubuntu 20.04+ и эквиваленты) |

Диск: ~95 МБ в сумме (~57 МБ приложение + ~35 МБ для sing-box + Xray +
tun2socks + WinTUN, скачиваются при первом подключении — sing-box для
основного TUN-движка, Xray для HTTP-режима и legacy-движка).

## Установка и запуск

### Вариант 1 — установщик (рекомендую)

Скачай нужный файл под свою OS со страницы
[Releases](https://github.com/fafnirov/KaproVPN/releases/latest)
и запусти.

### Вариант 2 — из исходников (для разработки / contributions)

```bash
git clone https://github.com/fafnirov/KaproVPN.git
cd KaproVPN
pip install -r requirements.txt
python run.py
```

Собрать свой установщик локально:

```bash
pip install -r requirements-build.txt
pyinstaller KaproVPN.spec          # → dist/KaproVPN.exe (портативная, встраивается в установщик)
pyinstaller KaproVPN-Setup.spec    # → dist/KaproVPN-Setup.exe (Windows-установщик)
```

При первом запуске / первом подключении приложение скачает бинарники в
`%LOCALAPPDATA%\KaproVPN\` (Windows) или `~/.local/share/KaproVPN/`
(macOS / Linux): **sing-box** (`sing-box/`, основной TUN-движок),
**Xray-core** (`xray/`, HTTP-режим + legacy-движок) и на Windows
**tun2socks + wintun.dll** (`tun/`, для legacy-движка и общий WinTUN-драйвер).

## Как это работает

1. Вы вставляете share-URL (например, `vless://…`) или subscription URL.
   Приложение разбирает его в proxy-outbound и применяет правила
   split-routing'а:
   - домены из «direct»-списка → напрямую (ваш реальный IP)
   - приватные / LAN / Docker диапазоны → всегда напрямую, мимо туннеля
   - всё остальное → proxy-outbound (разобранный URL)
   - DNS идёт через туннель при включённой защите от утечек, иначе напрямую

Дальше — в зависимости от режима/движка:

- **HTTP-режим** — `xray.exe` слушает на `127.0.0.1:2080` (HTTP) / `:2081`
  (SOCKS5); системный прокси OS указывается на порт 2080.
- **TUN-режим · sing-box (по умолчанию)** — один `sing-box.exe` владеет
  TUN-устройством, сам управляет маршрутами (`auto_route` +
  `auto_detect_interface`), резолвит DNS и дозванивается до прокси. **Нет
  моста `127.0.0.1:2081` и нет tun2socks** — поэтому исчерпание эфемерных
  портов loopback невозможно, а «прямой» трафик уходит через физическую
  сетевую карту (нет петли маршрутов).
- **TUN-режим · legacy (ручной fallback)** — `tun2socks.exe` создаёт
  виртуальный адаптер и форвардит каждый пакет через `127.0.0.1:2081` в
  `xray.exe`, который роутит по правилам. Выбирается в Настройках для
  конфигов, которые sing-box пока не умеет (например, транспорт XHTTP).

Если активный движок умер не по нашей команде — auto-reconnect переподключает
**тем же** движком. При включённом firewall kill-switch трафик остаётся
заблокированным до переподключения или явного дисконнекта — никаких тихих
утечек.

## Структура проекта

```
kapro_vpn/
├── core/
│   ├── parser.py             # парсеры share-URL (vless / vmess / trojan / ss / hy2)
│   ├── xray_config.py        # генератор JSON Xray-core со split-routing + DNS-leak hardening
│   ├── xray_installer.py     # загрузка Xray-core (с mirror-fallback)
│   ├── xray_process.py       # управление xray-подпроцессом + log rotation
│   ├── sing_box_config.py    # генератор JSON sing-box (основной TUN-движок) + гейт транспортов
│   ├── sing_box_installer.py # загрузка sing-box (с mirror-fallback)
│   ├── sing_box_process.py   # sing-box-подпроцесс + классификатор шумных per-connection логов
│   ├── tun2socks_installer.py  # legacy-движок
│   ├── tun2socks_process.py    # legacy-движок
│   ├── network_routes.py     # роуты/DNS для legacy TUN-движка на Windows
│   ├── network_routes_unix.py # эквивалент для macOS/Linux
│   ├── admin.py              # UAC / sudo хелперы
│   ├── system_proxy.py       # OS HTTP-proxy контроллер (3 платформы)
│   ├── storage.py            # JSON-персист, прозрачно через DPAPI на Win
│   ├── secrets_store.py      # обёртка над Windows DPAPI (Chrome-style шифрование на диске)
│   ├── killswitch.py         # правила Windows Firewall для реального kill-switch
│   ├── controller.py         # оркестрация connect/disconnect + auto-reconnect
│   ├── subscription.py       # импорт subscription-URL + 12 ч фоновый refresh
│   ├── i18n.py               # EN/RU translation tables
│   └── paths.py
├── gui/
│   ├── main_window.py
│   ├── tray.py               # системный трей с топ-3 quick-connect
│   ├── onboarding.py         # первый запуск — 3-карточный welcome
│   ├── subscription_dialog.py
│   ├── sites_dialog.py
│   ├── configs_picker.py
│   ├── widgets.py
│   └── styles.py
├── scripts/
│   └── smoke_test.py         # CI-gate — imports + parser + xray-config + installer-flow
├── data/
│   └── default_sites.json
└── main.py

installer/                    # standalone PyInstaller-сборка для KaproVPN-Setup.exe
├── gui.py                    # Welcome / Maintenance (Reinstall+Uninstall) / Installing
├── operations.py             # download + copy + ярлыки + Programs & Features
├── paths.py
└── main.py
```

Пользовательские данные (конфиги, список сайтов, настройки, логи) живут в:
- Windows: `%LOCALAPPDATA%\KaproVPN\`
- macOS: `~/Library/Application Support/KaproVPN/`
- Linux: `~/.local/share/KaproVPN/`

## Контрибьюты

PR'ы приветствуются. Самые полезные направления сейчас:

- **Подписание на macOS** — если у тебя есть платный Apple Developer
  account, патч в GitHub Actions, который добавит codesigning +
  notarytool, позволит маковским юзерам не видеть «разработчик не
  идентифицирован» Gatekeeper-prompt.
- **Android-клиент** — переехал в отдельный репозиторий:
  [fafnirov/KaproVPN-Android](https://github.com/fafnirov/KaproVPN-Android)
  (Kotlin + Compose, v0.1.0 готов). С этим репо синхронизируется только
  `kapro_vpn/data/default_sites.json` — общий список RU-сайтов для
  split-routing.
- **IPv6 в TUN-режиме** — сейчас только IPv4; IPv6-трафик может уходить
  мимо туннеля.
- **Больше языков** — `kapro_vpn/core/i18n.py` основан на dict'ах,
  добавить новый язык — пара часов.
- **Linux Wayland** — работает на X11/XWayland; нативный Wayland
  требует доработки PySide6 platform-plugin.

## Roadmap

- Crash-report opt-in (юзер сам отправляет лог, авто-сбор не делаем)
- Public-IP / индикатор страны после connect (чтобы видеть пруф что
  туннель работает)
- macOS Keychain / Linux libsecret эквивалент DPAPI для конфигов

## Лицензия

[GNU GPL v3](LICENSE). Любая производная работа также должна быть
GPL v3 — это сознательное решение, чтобы проект не мог быть тихо
поглощён закрытым коммерческим продуктом.
