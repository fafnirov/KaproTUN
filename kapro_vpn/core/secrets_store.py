"""Encrypt-at-rest for user secrets (configs.json + subscription_url).

On Windows we use DPAPI (Data Protection API) via CryptProtectData /
CryptUnprotectData. DPAPI ties the encryption key to the current user
account — anyone who can decrypt is either:

  (a) the same logged-in user, or
  (b) an admin who can impersonate that user.

This is the same primitive Chrome uses for stored passwords, Outlook
uses for mail credentials, etc. It's not "military grade" — it's
"casual disk-snooping protection". The threat model we close:

  ✓ Roommate / lab partner picks up your unlocked laptop, opens
    Explorer to %LOCALAPPDATA%\\KaproVPN\\configs.json — sees gibberish
    instead of your VLESS UUIDs.
  ✓ You email logs/configs to a friend by accident — the file alone
    is useless without your Windows account session.
  ✓ Your laptop is stolen but disk is encrypted (BitLocker) — even
    with cold-boot DPAPI keys, attacker needs your Windows password.

What this does NOT close:
  ✗ Malware running as you. DPAPI happily decrypts for any process
    you own. No protection against a running keylogger.
  ✗ State actor with your full disk image AND your Windows password.

Cross-platform: we only encrypt on Windows because that's where 90%+
of our users are and where DPAPI is a one-import-no-deps win. On
macOS/Linux configs stay plaintext (file permissions 0600 are the
only protection — same as ~/.ssh/config). Future improvement: macOS
Keychain via security CLI, Linux libsecret via secret-tool.

Migration: load_configs() transparently handles both encrypted and
plaintext files. Save always writes encrypted on Windows. So an
upgrade from a pre-1.8.0 install reads the old plaintext once, the
next save flips it to encrypted, and from that point on it stays
encrypted unless the user uninstalls/reinstalls.
"""
from __future__ import annotations

import sys
from typing import Optional


# Magic prefix we prepend to encrypted blobs so load_configs() can
# distinguish "this is DPAPI-encrypted, decrypt before JSON-parsing"
# from "this is legacy plaintext JSON, parse directly". Chose a string
# that's invalid JSON-leading so a misclassification is impossible.
ENCRYPTED_MAGIC = b"KAPROVPN-DPAPI\x00"


def is_supported() -> bool:
    """True on Windows (DPAPI available). False elsewhere — caller
    falls back to plaintext storage in that case.
    """
    return sys.platform == "win32"


def encrypt(plaintext: bytes) -> bytes:
    """DPAPI-encrypt under the current user account. Returns the
    encrypted blob with our magic prefix.

    Raises OSError on Windows API failure — caller decides whether to
    fall back to plaintext or surface the error.
    """
    if not is_supported():
        raise RuntimeError("DPAPI is Windows-only")
    blob = _dpapi_protect(plaintext)
    return ENCRYPTED_MAGIC + blob


def decrypt(data: bytes) -> bytes:
    """Inverse of encrypt(). `data` must start with ENCRYPTED_MAGIC.

    Raises ValueError if the data isn't ours / isn't encrypted.
    Raises OSError on DPAPI failure (e.g. trying to decrypt a blob
    encrypted by a different Windows user).
    """
    if not is_supported():
        raise RuntimeError("DPAPI is Windows-only")
    if not data.startswith(ENCRYPTED_MAGIC):
        raise ValueError("Data is not DPAPI-encrypted (missing magic)")
    blob = data[len(ENCRYPTED_MAGIC):]
    return _dpapi_unprotect(blob)


def looks_encrypted(data: bytes) -> bool:
    """Cheap check for use by storage.load — distinguishes new
    encrypted format from legacy plaintext JSON.
    """
    return data.startswith(ENCRYPTED_MAGIC)


# ----------------------------------------------------------------- internals

def _dpapi_protect(plaintext: bytes) -> bytes:
    """Call Win32 CryptProtectData. Standalone so the ctypes setup is
    visible in one place.
    """
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    src = DATA_BLOB(
        cbData=len(plaintext),
        pbData=ctypes.cast(
            ctypes.create_string_buffer(plaintext),
            ctypes.POINTER(ctypes.c_byte),
        ),
    )
    dst = DATA_BLOB()

    # CRYPTPROTECT_UI_FORBIDDEN = 0x1 — never show a UI prompt even if
    # the key requires it. We always want headless behaviour.
    if not crypt32.CryptProtectData(
        ctypes.byref(src), None, None, None, None, 0x1,
        ctypes.byref(dst),
    ):
        raise OSError(
            f"CryptProtectData failed: rc={kernel32.GetLastError()}"
        )

    try:
        return bytes(
            ctypes.string_at(dst.pbData, dst.cbData)
        )
    finally:
        kernel32.LocalFree(dst.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    src = DATA_BLOB(
        cbData=len(blob),
        pbData=ctypes.cast(
            ctypes.create_string_buffer(blob),
            ctypes.POINTER(ctypes.c_byte),
        ),
    )
    dst = DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x1,
        ctypes.byref(dst),
    ):
        raise OSError(
            f"CryptUnprotectData failed: rc={kernel32.GetLastError()} "
            f"(was the blob encrypted by a different user?)"
        )

    try:
        return bytes(
            ctypes.string_at(dst.pbData, dst.cbData)
        )
    finally:
        kernel32.LocalFree(dst.pbData)
