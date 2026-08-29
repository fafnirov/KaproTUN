# Контрибьютинг в KaproTUN (desktop)

Спасибо за интерес! Это десктопный клиент: **Python 3.10+ / PySide6** поверх
**sing-box** (нативный TUN), со split-routing: РФ-адреса и игры идут напрямую,
остальное — через туннель.

> Android-клиент живёт в отдельном репозитории. Здесь — только десктоп
> (Windows / macOS / Linux). Общий между ними только
> `kapro_tun/data/default_sites.json` (источник правды для split-routing).

## Быстрый старт (из исходников)

```bash
git clone https://github.com/fafnirov/KaproTUN.git
cd KaproTUN
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m kapro_tun.main
```

Подключение требует прав администратора/root: клиент работает только в
режиме TUN и создаёт виртуальный сетевой адаптер. Движок **sing-box**
(и на Windows драйвер WinTUN) докачивается сам при первом подключении —
с зеркала `kaprovpn.pro/files`, фолбэк GitHub Releases. Версия sing-box
запинена на линии 1.12.x: 1.13 ломает data-plane VLESS на Windows.

## Тесты

Перед каждым PR прогоняй smoke-набор — он покрывает импорт всех модулей,
генерацию конфига sing-box и порядок правил маршрутизации, парсинг подписок
(share-URL и v2ray-json), сторожа подключения, leak-test, сборку GUI и т.д.:

```bash
python -m kapro_tun.scripts.smoke_test
# На headless-машине (CI) без дисплея:
QT_QPA_PLATFORM=offscreen python -m kapro_tun.scripts.smoke_test
```

CI гейтит сборку релиза этим же набором — красный smoke = нет билда.

## Сборка инсталлятора (Windows)

```bash
pyinstaller KaproTUN.spec          # -> dist/KaproTUN.exe
pyinstaller KaproTUN-Setup.spec    # -> dist/KaproTUN-Setup.exe (скачивает KaproTUN.exe из релиза при установке)
```

## Структура

- `kapro_tun/core/` — логика без UI: контроллер подключения и вердикт
  здоровья туннеля, генерация конфига sing-box, маршруты/TUN, подписки,
  сетевая диагностика, leak-test, защита от утечек.
- `kapro_tun/gui/` — PySide6-интерфейс.
- `kapro_tun/scripts/smoke_test.py` — весь smoke-набор.
- `installer/` — брендированный установщик (тоже PySide6).
- `server-setup/` — скрипты зеркала бинарников на VPS.

## Релиз

1. Внести фикс/фичу.
2. Добавить запись в начало секции Desktop в `CHANGELOG.md` —
   **верхняя запись = тело релиза** на странице Releases.
3. Поднять `__version__` в `kapro_tun/__init__.py`.
4. Commit → tag `vX.Y.Z` → push тега. CI соберёт билды на 3 ОС
   (после прохождения smoke) и опубликует релиз.

## Договорённости

- Smoke обязан проходить.
- Не коммить абсолютные пути с именем пользователя ОС.
- Пользовательские строки — через `core/i18n.py`, обе таблицы (RU и EN)
  обязаны иметь одинаковый набор ключей; smoke это проверяет.
- Не обещать в интерфейсе больше, чем функция делает: если у защиты есть
  границы — писать их в подсказке рядом.
- Лицензия проекта — **GPL-3.0**; вклад принимается на этих же условиях.
