# KaproVPN — Changelog

User-facing changes per release. Most-recent first.

The GitHub Actions release workflow reads the top entry for the body
of each release on the Releases page — so what you see here is what
users see when they click the release tag.

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
