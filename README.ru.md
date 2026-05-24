# KaproVPN

[![Релиз](https://img.shields.io/github/v/release/fafnirov/KaproVPN?style=flat-square&color=f59e0b&label=latest)](https://github.com/fafnirov/KaproVPN/releases/latest)
[![Лицензия](https://img.shields.io/github/license/fafnirov/KaproVPN?style=flat-square&color=blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://www.python.org/)
[![Сборка](https://img.shields.io/github/actions/workflow/status/fafnirov/KaproVPN/release.yml?style=flat-square&label=build)](https://github.com/fafnirov/KaproVPN/actions/workflows/release.yml)

[English](README.md) · [Русский](README.ru.md)

Десктопный proxy-клиент (Windows) со встроенным **split-routing'ом
российских сайтов**. Построен поверх [Xray-core](https://github.com/XTLS/Xray-core).

---

### ⬇️ Скачать

Готовый **[`KaproVPN.exe`](https://github.com/fafnirov/KaproVPN/releases/latest)**
(~57 МБ, Windows x64, Python не нужен) — скачал → запустил. Для TUN-режима
(туннелировать всё включая Telegram/Steam/игры) — запускай от админа.

---

## Что делает

GUI для proxy/VPN-соединений (Trojan, VLESS с REALITY и XHTTP, VMess,
Shadowsocks) с одной важной фичей: домены из настраиваемого списка —
российские банки, госуслуги, маркетплейсы и т.п. — идут **в обход прокси**,
через ваш реальный IP. Всё остальное маршрутизируется через прокси-сервер.

## Зачем

Когда пользователь в России подключается через иностранный прокси, сервисы
вроде Сбербанка, gosuslugi.ru, Ozon и многие другие отказываются работать —
у них geofence по российским IP. Выключать VPN каждый раз, когда нужно
оплатить счёт, неудобно. Этот инструмент держит прокси включённым для
открытого интернета, а российские сервисы видят ваш реальный адрес.

## Возможности

- Парсит share-URL в стандартных форматах:
  `trojan://`, `vless://` (включая **REALITY** и **XHTTP** транспорт),
  `vmess://`, `ss://`
- Два режима работы:
  - **HTTP-прокси** (по умолчанию, без админа) — работает только для браузеров
  - **TUN** (нужны права админа) — туннелирует все приложения системно,
    включая Telegram, Steam, игры. Использует bundled tun2socks + WinTUN-драйвер
- Автоматически скачивает `xray.exe`, `tun2socks.exe`, `wintun.dll` при
  первом использовании (~30 МБ в сумме)
- Редактируемый список «всегда напрямую» доменов (108 записей по умолчанию —
  банки, госуслуги, маркетплейсы, медиа…)
- GUI на PySide6 с тёмной темой, одно-экранный AmneziaVPN-подобный layout
- Окно с живыми логами Xray-core для отладки

## Требования

- Windows 10 / 11 (x64)
- ~80 МБ свободного места (~57 МБ exe + ~25 МБ для Xray + tun2socks + WinTUN)
- Права админа *только* для TUN-режима (все приложения туннелируются).
  HTTP-прокси-режим работает без админа, туннелирует только браузер.

## Установка и запуск

### Вариант 1 — готовый exe (рекомендую)

Скачай **[`KaproVPN.exe`](https://github.com/fafnirov/KaproVPN/releases/latest)**,
запусти. Всё.

### Вариант 2 — из исходников (для разработки / contributions)

```bash
git clone https://github.com/fafnirov/KaproVPN.git
cd KaproVPN
pip install -r requirements.txt
python run.py
```

Или собрать свой .exe локально:

```bash
pip install -r requirements-build.txt
pyinstaller KaproVPN.spec
# → dist/KaproVPN.exe
```

При первом запуске приложение скачает последний релиз Xray-core в
`%LOCALAPPDATA%\KaproVPN\xray\`. Туда же идут tun2socks + wintun.dll
(если включён TUN-режим).

## Как это работает

1. Вы вставляете share-URL (например, `vless://…`).
2. Приложение разбирает его и генерирует JSON-конфиг Xray-core с
   правилами маршрутизации:
   - домены из «direct»-списка → outbound `freedom` (ваш реальный IP)
   - всё остальное → proxy-outbound (разобранный URL)
3. `xray.exe` запускается как подпроцесс и слушает на `127.0.0.1:2080`
   (HTTP) и `:2081` (SOCKS5).
4. Системный прокси Windows указывается на порт 2080.
5. Любое приложение, которое уважает системный прокси (браузеры, Office,
   большинство десктопных приложений) теперь следует правилам маршрутизации.

## Ограничения

- Пока только Windows (роуты и прокси-код специфичны для Windows; остальное
  кроссплатформенно).
- Hysteria2 пока не поддерживается — Xray-core не умеет этот протокол. В
  планах — добавить sing-box как второй движок специально для hy2.
- Импорт subscription-URL пока не реализован (планируется).
- TUN-режим работает только с IPv4 — IPv6-трафик может проходить мимо туннеля.

## Структура проекта

```
kapro_vpn/
├── core/
│   ├── parser.py          # парсеры share-URL (vless / vmess / trojan / ss / hy2)
│   ├── xray_config.py     # генератор JSON Xray-core со split-routing
│   ├── xray_installer.py  # загрузка Xray-core с GitHub releases
│   ├── xray_process.py    # управление подпроцессом
│   ├── system_proxy.py    # реестр прокси Windows
│   ├── storage.py         # JSON persistence (конфиги / сайты / настройки)
│   ├── controller.py      # оркестрация connect/disconnect
│   └── paths.py           # пути файловой системы
├── gui/
│   ├── main_window.py     # одно-оконное приложение с Home / Settings / Logs
│   ├── widgets.py         # CircleConnectButton, ConfigCard, NavBar
│   ├── config_dialog.py
│   ├── configs_picker.py
│   ├── sites_dialog.py
│   ├── installer_dialog.py
│   └── styles.py          # QSS тёмной темы с янтарным акцентом
├── data/
│   └── default_sites.json # встроенный дефолтный список direct-роутинга
└── main.py                # точка входа QApplication
```

Пользовательские данные (сохранённые конфиги, отредактированный список сайтов,
настройки, логи) живут в `%LOCALAPPDATA%\KaproVPN\`.

## Контрибьюты

PR'ы приветствуются. Направления, где помощь особенно полезна:

- Поддержка Hysteria2 через второй движок (sing-box)
- TUN-режим (чтобы туннелировались игры и любые приложения, а не только те,
  что знают про HTTP-прокси)
- Порт на Linux / macOS
- Импортёр subscription URL (base64-списки)
- Иконка в системном трее
- Latency / health-check пинги для каждого конфига

## Лицензия

[GNU GPL v3](LICENSE). Любая производная работа также должна быть GPL v3 —
это сознательное решение, чтобы проект не мог быть тихо поглощён закрытым
коммерческим продуктом.
