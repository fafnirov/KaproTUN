"""Lightweight in-process i18n for KaproVPN.

Dict-based — no .ts/.qm files, no QTranslator, no lupdate tooling.
For an app this size (~50 user-visible strings) that's enough; Qt's
translation system would be 10x the boilerplate for the same payload.

Usage:
    from ..core.i18n import tr, set_locale
    set_locale("en")
    label.setText(tr("home.connect_button"))   # → "CONNECT"

Locale detection at startup:
    - settings.json "language" key wins ("ru" / "en" / "auto")
    - "auto" or missing → QLocale.system() — anything starting "ru"
      means Russian, everything else falls back to English
    - English is the safest default for unknown locales: it's the
      common second language for Russian users, and unambiguous for
      non-Russian first-time users.

Fallback behaviour:
    - If a key is missing in the active locale, fall back to RU
      (the source language — every key exists there by definition).
    - If still missing, return the key itself so the UI shows a
      grep-able marker instead of an empty string.
"""
from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Translation tables. RU is the source of truth: every key MUST exist here.
# EN is best-effort — missing keys fall back to RU at render time.
# ---------------------------------------------------------------------------

_RU: dict[str, str] = {
    # --- App-wide ---
    "app.name": "KaproVPN",
    "app.tagline": "Прокси-клиент со split-routing'ом",

    # --- Connection states ---
    "state.idle": "Не подключено",
    "state.connecting": "Подключение…",
    "state.connected": "Подключено",
    "state.disconnecting": "Отключение…",

    # --- Home page ---
    "home.connect": "ВКЛЮЧИТЬ",
    "home.disconnect": "ОТКЛЮЧИТЬ",
    "home.connecting": "ПОДКЛЮЧЕНИЕ…",
    "home.no_config": "Конфиг не выбран",
    "home.no_config_hint": "Нажми, чтобы добавить конфиг",
    "home.direct_sites_count": "Прямые сайты — {n} доменов идут напрямую",

    # --- Settings page ---
    "settings.title": "Настройки",
    "settings.mode_label": "Режим работы",
    "settings.mode_http": "HTTP-прокси (только браузер)",
    "settings.mode_http_hint":
        "Работает с Chrome/Edge/Firefox. ТГ и игры не туннелируются.",
    "settings.mode_tun": "TUN (все приложения, нужен админ)",
    "settings.mode_tun_hint":
        "Туннелирует все программы системно: ТГ, Steam, игры.",
    "settings.admin_yes": "✓ Запущено с правами администратора",
    "settings.admin_no": "✗ Не от админа — TUN не доступен",
    "settings.autoconnect": "Автоподключение при запуске",
    "settings.autostart": "Запускать вместе с Windows",
    "settings.kill_switch": "Kill-switch (блокировать трафик если VPN упал)",
    "settings.auto_set_proxy": "Автоматически ставить системный прокси",
    "settings.language_label": "Язык / Language",
    "settings.language_auto": "Авто (по системе)",
    "settings.direct_sites_link": "Прямые сайты (всегда напрямую)",
    "settings.about_version": "KaproVPN v{version}",

    # --- Tray ---
    "tray.tooltip_idle": "KaproVPN — не подключено",
    "tray.tooltip_connecting": "KaproVPN — подключение…",
    "tray.tooltip_connected": "KaproVPN — подключено",
    "tray.menu_connect": "Подключить",
    "tray.menu_disconnect": "Отключить",
    "tray.menu_cancel_connect": "Отменить подключение",
    "tray.menu_configs": "Конфиги",
    "tray.menu_no_configs": "(нет конфигов)",
    "tray.menu_show": "Главное окно",
    "tray.menu_quit": "Выход",

    # --- Bottom nav ---
    "nav.home": "Главная",
    "nav.settings": "Настройки",
    "nav.add": "Добавить",
    "nav.logs": "Логи",

    # --- Add-config page ---
    "add.title": "Добавить конфиг",
    "add.back": "← Назад",
    "add.url_label": "Вставь share-URL:",
    "add.name_label": "Имя (как будет отображаться в списке):",
    "add.name_placeholder": "например, NL Server #2",
    "add.save": "Сохранить и подключить",
    "add.sub_hint":
        "Если провайдер выдал ссылку на подписку — все сервера одним кликом",
    "add.sub_button": "📥 Импорт по подписке",

    # --- Configs picker ---
    "picker.title": "Выбор конфига",
    "picker.add": "Добавить",
    "picker.remove": "Удалить",
    "picker.import": "Импорт по подписке",
    "picker.use": "Использовать",
    "picker.cancel": "Отмена",
    "picker.ping_pending": "…",
    "picker.ping_udp": "UDP",
    "picker.ping_unreachable": "недоступен",
    "picker.ping_ms": "{ms} мс",

    # --- Subscription dialog ---
    "sub.title": "Импорт по подписке",
    "sub.close": "Закрыть",
    "sub.add_to_list": "Добавить в список",
    "sub.fetch": "Загрузить и распарсить",

    # --- Common buttons ---
    "btn.ok": "OK",
    "btn.cancel": "Отмена",
    "btn.save": "Сохранить",
    "btn.delete": "Удалить",
    "btn.close": "Закрыть",
    "btn.retry": "Повторить",
}

_EN: dict[str, str] = {
    "app.name": "KaproVPN",
    "app.tagline": "Split-routing proxy client",

    "state.idle": "Disconnected",
    "state.connecting": "Connecting…",
    "state.connected": "Connected",
    "state.disconnecting": "Disconnecting…",

    "home.connect": "CONNECT",
    "home.disconnect": "DISCONNECT",
    "home.connecting": "CONNECTING…",
    "home.no_config": "No config selected",
    "home.no_config_hint": "Tap to add a config",
    "home.direct_sites_count": "Direct sites — {n} domains bypass the VPN",

    "settings.title": "Settings",
    "settings.mode_label": "Connection mode",
    "settings.mode_http": "HTTP proxy (browser only)",
    "settings.mode_http_hint":
        "Works with Chrome/Edge/Firefox. Telegram and games stay direct.",
    "settings.mode_tun": "TUN (all apps, admin required)",
    "settings.mode_tun_hint":
        "Tunnels every app system-wide: Telegram, Steam, games.",
    "settings.admin_yes": "✓ Running as administrator",
    "settings.admin_no": "✗ Not admin — TUN mode unavailable",
    "settings.autoconnect": "Auto-connect on launch",
    "settings.autostart": "Start with Windows",
    "settings.kill_switch": "Kill-switch (block traffic if VPN drops)",
    "settings.auto_set_proxy": "Automatically set system proxy",
    "settings.language_label": "Language / Язык",
    "settings.language_auto": "Auto (system)",
    "settings.direct_sites_link": "Direct sites (always bypass VPN)",
    "settings.about_version": "KaproVPN v{version}",

    "tray.tooltip_idle": "KaproVPN — disconnected",
    "tray.tooltip_connecting": "KaproVPN — connecting…",
    "tray.tooltip_connected": "KaproVPN — connected",
    "tray.menu_connect": "Connect",
    "tray.menu_disconnect": "Disconnect",
    "tray.menu_cancel_connect": "Cancel connection",
    "tray.menu_configs": "Configs",
    "tray.menu_no_configs": "(no configs)",
    "tray.menu_show": "Main window",
    "tray.menu_quit": "Quit",

    "nav.home": "Home",
    "nav.settings": "Settings",
    "nav.add": "Add",
    "nav.logs": "Logs",

    "add.title": "Add config",
    "add.back": "← Back",
    "add.url_label": "Paste a share URL:",
    "add.name_label": "Name (shown in the list):",
    "add.name_placeholder": "e.g. NL Server #2",
    "add.save": "Save and connect",
    "add.sub_hint":
        "If your provider gave you a subscription URL — import all servers at once",
    "add.sub_button": "📥 Import by subscription",

    "picker.title": "Pick a config",
    "picker.add": "Add",
    "picker.remove": "Remove",
    "picker.import": "Import by subscription",
    "picker.use": "Use",
    "picker.cancel": "Cancel",
    "picker.ping_pending": "…",
    "picker.ping_udp": "UDP",
    "picker.ping_unreachable": "unreachable",
    "picker.ping_ms": "{ms} ms",

    "sub.title": "Subscription import",
    "sub.close": "Close",
    "sub.add_to_list": "Add to list",
    "sub.fetch": "Fetch and parse",

    "btn.ok": "OK",
    "btn.cancel": "Cancel",
    "btn.save": "Save",
    "btn.delete": "Delete",
    "btn.close": "Close",
    "btn.retry": "Retry",
}

_LOCALES = {"ru": _RU, "en": _EN}
_DEFAULT = "en"  # safest default for unknown system locales

_current: str = "ru"  # bootstrap value; init_from_settings sets the real one


def set_locale(locale: str) -> None:
    """Switch active locale. Unknown codes silently fall back to default."""
    global _current
    if locale not in _LOCALES:
        locale = _DEFAULT
    _current = locale


def current_locale() -> str:
    return _current


def available_locales() -> list[str]:
    return list(_LOCALES.keys())


def tr(key: str, **kwargs: Any) -> str:
    """Look up `key` in active locale; fall back to RU; finally the key itself.

    `kwargs` get .format()'d into the result so callers can do
    e.g. tr("home.direct_sites_count", n=42).
    """
    table = _LOCALES.get(_current, _RU)
    template = table.get(key)
    if template is None:
        # Fall back to RU (source language) — every key MUST exist there.
        template = _RU.get(key, key)
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template
    return template


def detect_system_locale() -> str:
    """Pick a sensible default based on the system locale.

    Returns "ru" for any *_RU / ru_* / Russian-Cyrillic locale,
    otherwise "en". Called once at startup if settings.language == "auto"
    (or missing).
    """
    try:
        from PySide6.QtCore import QLocale
        name = QLocale.system().name() or ""
    except Exception:
        return _DEFAULT
    return "ru" if name.lower().startswith("ru") else "en"


def init_from_settings(language_setting: Optional[str]) -> None:
    """Apply user's saved language preference at app startup.

    `language_setting` is the raw "language" field from settings.json:
      - "ru" / "en" → use that explicitly
      - "auto" / None → auto-detect from system
    """
    if language_setting in _LOCALES:
        set_locale(language_setting)
    else:
        set_locale(detect_system_locale())
