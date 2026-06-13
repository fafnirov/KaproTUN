"""Linux: ручная маршрутизация TUN в обход сломанного sing-box auto_route.

На свежих ядрах (проверено: Linux 7.0 / Ubuntu 26.04) sing-box `auto_route`
падает с «add route 0: invalid argument» — его netlink-вызовы несовместимы с
изменениями ядра, и это воспроизводится на ВСЕХ версиях sing-box (1.11–1.13).
При этом само TUN-устройство поднимается, а системная iproute2 маршруты
добавляет нормально. Поэтому на Linux мы запускаем sing-box с
`auto_route=False` и руками реплицируем то, что делал бы auto_route:

  • policy-route: весь трафик → таблица 2022 → `default dev KaproTun`;
  • fwmark-исключение: собственный egress sing-box (он помечен
    `route.default_mark=1`) уходит мимо TUN через основную таблицу — иначе
    соединение к VPN-серверу зациклилось бы обратно в туннель;
  • systemd-resolved: DNS вешается на TUN (`resolvectl dns/default-route`),
    чтобы getaddrinfo резолвил через туннель (sing-box hijack-dns ловит :53);
  • net.ipv4.ip_forward=1 — gvisor UDP-NAT надёжнее с включённым форвардингом.

Всё best-effort и идемпотентно. `teardown()` возвращает систему как было.
Только Linux: на Windows/macOS auto_route работает, модуль не задействуется.
"""
from __future__ import annotations

import subprocess
import sys

TABLE = "2022"
TUN_DEVICE = "KaproTun"
MARK = "0x1"
RULE_MARK_PRIORITY = "8999"   # egress sing-box (по fwmark) — в main, мимо TUN
RULE_TUN_PRIORITY = "9000"    # весь остальной трафик — в таблицу TUN
# Любой адрес в TUN-подсети: systemd-resolved отправит DNS через KaproTun, а
# sing-box hijack-dns перехватит :53 независимо от адреса назначения.
TUN_DNS_TARGET = "10.255.0.1"


def applies() -> bool:
    """True только на Linux — единственная платформа, где auto_route ломается и
    нужен ручной обход."""
    return sys.platform.startswith("linux")


def _run(args: list[str]) -> int:
    """Тихо выполнить команду, вернуть код возврата (-1 при сбое запуска)."""
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=10).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def setup() -> None:
    """Проложить маршруты и настроить DNS ПОСЛЕ того как sing-box поднял TUN.
    Идемпотентно: сначала снимает любые остатки прошлой сессии."""
    if not applies():
        return
    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    teardown()  # очистить возможные остатки до повторной установки
    _run(["ip", "route", "add", "default", "dev", TUN_DEVICE, "table", TABLE])
    _run(["ip", "rule", "add", "fwmark", MARK, "lookup", "main",
          "priority", RULE_MARK_PRIORITY])
    _run(["ip", "rule", "add", "from", "all", "lookup", TABLE,
          "priority", RULE_TUN_PRIORITY])
    # systemd-resolved → DNS через TUN (иначе getaddrinfo минует туннель)
    _run(["resolvectl", "dns", TUN_DEVICE, TUN_DNS_TARGET])
    _run(["resolvectl", "default-route", TUN_DEVICE, "yes"])
    _run(["resolvectl", "flush-caches"])


def teardown() -> None:
    """Снять всё, что поставил setup(). Безопасно вызывать, даже если ничего не
    устанавливалось (каждая команда не падает на отсутствующем правиле)."""
    if not applies():
        return
    _run(["resolvectl", "revert", TUN_DEVICE])
    _run(["ip", "rule", "del", "priority", RULE_TUN_PRIORITY])
    _run(["ip", "rule", "del", "priority", RULE_MARK_PRIORITY])
    _run(["ip", "route", "flush", "table", TABLE])
    _run(["resolvectl", "flush-caches"])
