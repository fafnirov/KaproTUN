# KaproTUN — Stable-release checklist (v3.0.x)

The automated smoke suite is the gate for *publishing* a build; this checklist
is the human soak test you run on the resulting build before calling a version
**stable**. The default TUN engine is **sing-box native TUN**; the legacy
Xray + tun2socks engine is a manual fallback.

> Manual steps are Windows-first (the primary platform). Adapt the commands for
> macOS / Linux where noted.

## 0. Automated gates (must be green first)

```powershell
python -m compileall -q kapro_tun installer
python kapro_tun/scripts/smoke_test.py          # run from the repo root
python -m kapro_tun.scripts.smoke_test          # module form (exercises the sys.path shim)
```

All three must exit 0 / print `=== SMOKE TEST PASSED ===`. The suite runs fully
sandboxed — it must **not** touch the real `%LOCALAPPDATA%\KaproTUN\app.log`
(there's a regression check for exactly that).

## 1. Read-only diagnostics helper

At any point during the soak test, snapshot the live state without changing
anything:

```powershell
python -m kapro_tun.scripts.collect_diagnostics
```

It prints the configured engine/mode, which helper processes are running,
whether any runtime config (with secrets) is lingering on disk, and the tail of
`app.log`.

Quick process check on its own (non-destructive):

```powershell
Get-Process sing-box,xray,tun2socks,hysteria -ErrorAction SilentlyContinue |
  Select-Object Name,Id,StartTime
```

## 2. sing-box engine soak (60–120 min)

Settings → **Движок TUN = «Основной: sing-box»**, mode = **TUN**, run as
Administrator. Connect to a representative server, then:

- [ ] **Processes.** `sing-box.exe` is running; **`xray.exe` and
      `tun2socks.exe` are NOT** (a sing-box session uses neither). Verify with
      the process check above or `collect_diagnostics`.
- [ ] **UI status** shows **`TUN · sing-box`**.
- [ ] **`app.log`** has `[connect] mode=TUN engine=sing_box_tun` and — crucially
      — **no `Xray-core упал`** after a healthy connect (that was the v3.0.2
      false-crash bug). Benign per-connection sing-box noise ("forcibly closed",
      "i/o timeout", "connection download closed") must **not** spam the Logs
      page (kept in diagnostics only).
- [ ] **Real traffic.** Browser loads pages; run a Speedtest; **Telegram**
      connects and sends/receives (UDP path); a download sustains for several
      minutes without the tunnel dropping.
- [ ] **Sleep / wake.** Suspend the machine, resume — the tunnel either survives
      or reconnects cleanly to the **same** engine (no fallback to legacy).
- [ ] **Network switch.** Switch Wi-Fi ↔ Ethernet (or toggle Wi-Fi) — the tunnel
      reconnects without leaking and without engine fallback.
- [ ] **10 reconnects in a row.** Disconnect/connect (or let auto-reconnect fire)
      ~10 times. No reconnect storm, no creeping memory/handles, no silent
      switch to `classic_xray_tun2socks` in `app.log`.
- [ ] **DNS leak — protection ON.** With leak protection on, run a DNS-leak test
      (e.g. browserleaks.com/dns) — resolvers should be the tunnel's, not your
      ISP's. Confirm no IPv4 / IPv6 / WebRTC leak.
- [ ] **DNS leak — protection OFF.** Toggle it off, reconnect — sing-box must
      still **start** (the v3.0.1 fix: no `detour to an empty direct outbound`
      crash); DNS now resolves direct (ISP-visible), as intended.
- [ ] **Kill-switch.** Enable it, connect, then kill `sing-box.exe` from Task
      Manager. Traffic must **stop** (no leak to the real IP), not silently fall
      back to the ISP. Reconnect restores it.
- [ ] **Runtime cleanup.** Disconnect cleanly, then check **no** runtime config
      remains on disk (they carry the server UUID/password):
      `collect_diagnostics` should report `sing-box-runtime.json`,
      `xray-runtime.json`, `hysteria-client.yaml` all **absent ✓**.

## 3. Legacy engine spot-check

Settings → **Движок TUN = «Legacy: Xray + tun2socks»**, reconnect.

- [ ] `xray.exe` + `tun2socks.exe` run; `sing-box.exe` does not.
- [ ] UI shows **`TUN · legacy`**.
- [ ] Ad-block checkbox is **enabled** again (it's an Xray feature; it's
      disabled with a "legacy only" note under sing-box).
- [ ] A config sing-box can't reproduce (e.g. **VLESS XHTTP / Reality**) fails on
      sing-box with a clear "switch to legacy" message and **works** on legacy.

## 4. Sign-off

- [ ] CHANGELOG.md top entry describes this version; `__version__` bumped.
- [ ] `git status` clean except intended changes; READMEs not accidentally
      reverted; `_visual_audit/` not staged.
- [ ] Release published with all four assets, marked **Latest**.

If every box is checked, tag it stable.
