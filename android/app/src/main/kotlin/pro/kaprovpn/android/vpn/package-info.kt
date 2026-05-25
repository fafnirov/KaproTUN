/**
 * pro.kaprovpn.android.vpn — VPN-плоскость: VpnService, JNI к libXray, TUN-handler.
 *
 * Сюда поедут:
 *   - KaproVpnService              ← наследник android.net.VpnService
 *   - XrayBridge                   ← обёртка над libXray (start/stop/stats)
 *   - Tun2SocksBridge              ← hev-socks5-tunnel mapping TUN → SOCKS
 *   - ConnectionController         ← оркестратор, аналог controller.py
 *
 * В отличие от core/, модули здесь зависят от Android-фреймворка и нативных
 * библиотек (libxray.so, libhev-socks5-tunnel.so) — тестируются на эмуляторе.
 */
package pro.kaprovpn.android.vpn
