"""Encrypt-at-rest for user secrets (configs.json + subscription_url).

Per-platform key handling, one common on-disk model: the config file
always holds a self-describing encrypted blob (magic prefix + payload),
and storage.load decrypts transparently. Three backends:

  • Windows — DPAPI (CryptProtectData / CryptUnprotectData). The key is
    tied to the user account; no key management, no dependencies. This
    is the same primitive Chrome uses for stored passwords. Magic:
    ``KAPROVPN-DPAPI``.

  • macOS / Linux (v1.16.12) — AES-256-GCM. A random 32-byte
    data-encryption key (DEK) is generated once and stored in the OS
    keystore — macOS Keychain (via the ``security`` CLI) or Linux Secret
    Service (via ``secret-tool``). The DEK never touches disk; the file
    holds only ``nonce || ciphertext || tag``. Magic: ``KAPROVPN-AESGCM``.

The threat model we close (all platforms): casual disk-snooping. Someone
picks up your unlocked laptop and opens the config file — they see
gibberish instead of your VLESS UUIDs. You email a config/log by
accident — the file alone is useless without your logged-in session /
keychain. What we do NOT close: malware running as you (the OS will
decrypt for any process you own), or a state actor with your full disk
image AND your account password.

Graceful fallback: where the keystore is unavailable (e.g. a headless
Linux box with no Secret Service daemon, or ``secret-tool`` not
installed) ``is_supported()`` returns False and storage writes plaintext
— exactly the pre-1.16.12 behaviour on those boxes, no regression. File
permissions (0600) remain the only protection there, same as
``~/.ssh/config``.

Migration: load() transparently handles encrypted (either format) and
legacy plaintext. The first save on a capable machine flips the file to
encrypted and it stays that way.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional


# Per-platform magic prefixes. storage.load uses looks_encrypted() to
# decide "decrypt before JSON-parse" vs "parse as legacy plaintext", and
# decrypt() dispatches on which magic it sees. Both are chosen to be
# invalid JSON-leading bytes so a plaintext file is never misread as
# encrypted.
DPAPI_MAGIC = b"KAPROVPN-DPAPI\x00"
AESGCM_MAGIC = b"KAPROVPN-AESGCM\x00"

# Backwards-compat alias — pre-1.16.12 code/tests referred to this name.
ENCRYPTED_MAGIC = DPAPI_MAGIC

# Keystore identifiers for the DEK (macOS Keychain + Linux Secret Service).
_KEYSTORE_SERVICE = "KaproVPN"
_KEYSTORE_ACCOUNT = "config-encryption-key"
_DEK_LEN = 32  # AES-256
_NONCE_LEN = 12  # GCM standard
_GCM_TAG_LEN = 16


def is_supported() -> bool:
    """True if we can encrypt on this machine; caller stores plaintext if not.

    Windows  → always (DPAPI, built in).
    macOS    → cryptography importable + ``security`` CLI present (it always is).
    Linux    → cryptography importable + ``secret-tool`` present.
    Other    → False.
    """
    if sys.platform == "win32":
        return True
    if not _cryptography_available():
        return False
    if sys.platform == "darwin":
        return shutil.which("security") is not None
    # Linux / other POSIX — needs libsecret's secret-tool.
    return shutil.which("secret-tool") is not None


def encrypt(plaintext: bytes) -> bytes:
    """Encrypt under the platform backend. Returns magic-prefixed blob.

    Raises on backend failure (DPAPI API error, keystore unreachable) —
    the caller (storage._write_configs_bytes) decides whether to fall
    back to plaintext.
    """
    if sys.platform == "win32":
        return DPAPI_MAGIC + _dpapi_protect(plaintext)
    key = _get_or_create_dek()  # raises OSError if the keystore won't cooperate
    return AESGCM_MAGIC + _encrypt_with_key(key, plaintext)


def decrypt(data: bytes) -> bytes:
    """Inverse of encrypt(); dispatches on the magic prefix.

    Raises ValueError if the data carries no known magic, or OSError if
    the matching backend can't decrypt it (wrong Windows user, missing
    DEK, tampered ciphertext).
    """
    if data.startswith(AESGCM_MAGIC):
        key = _get_dek()
        if key is None:
            raise OSError("no DEK in keystore — cannot decrypt AES-GCM blob")
        return _decrypt_with_key(key, data[len(AESGCM_MAGIC):])
    if data.startswith(DPAPI_MAGIC):
        if sys.platform != "win32":
            raise OSError("DPAPI blobs can only be decrypted on Windows")
        return _dpapi_unprotect(data[len(DPAPI_MAGIC):])
    raise ValueError("Data is not encrypted (no known magic prefix)")


def looks_encrypted(data: bytes) -> bool:
    """Cheap check for storage.load — recognises either encrypted format."""
    return data.startswith(DPAPI_MAGIC) or data.startswith(AESGCM_MAGIC)


# --------------------------------------------------------------------------
# AES-GCM crypto layer — platform-agnostic and keystore-free, so it can be
# unit-tested with a fixed key on any machine (incl. the Linux CI runner).
# --------------------------------------------------------------------------

def _cryptography_available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except Exception:
        return False


def _encrypt_with_key(key: bytes, plaintext: bytes) -> bytes:
    """``nonce(12) || AES-256-GCM(ciphertext+tag)``. The nonce is random
    per call — GCM security requires a unique nonce per (key, message)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + ct


def _decrypt_with_key(key: bytes, blob: bytes) -> bytes:
    """Inverse of _encrypt_with_key. Raises cryptography's InvalidTag if
    the blob was tampered with (or the key is wrong)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < _NONCE_LEN + _GCM_TAG_LEN:
        raise ValueError("AES-GCM blob too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)


# --------------------------------------------------------------------------
# DEK storage in the OS keystore (macOS Keychain / Linux Secret Service).
# --------------------------------------------------------------------------

def _get_or_create_dek() -> bytes:
    existing = _get_dek()
    if existing is not None:
        return existing
    dek = os.urandom(_DEK_LEN)
    _store_dek(dek)
    # Read it back — if it didn't persist, fail loudly so the caller
    # stores plaintext instead of encrypting under a key we can't
    # retrieve on the next launch (which would silently lose configs).
    if _get_dek() != dek:
        raise OSError("DEK did not persist to the keystore")
    return dek


def _get_dek() -> Optional[bytes]:
    import base64
    raw = _keychain_read() if sys.platform == "darwin" else _secrettool_read()
    if not raw:
        return None
    try:
        dek = base64.b64decode(raw)
    except Exception:
        return None
    return dek if len(dek) == _DEK_LEN else None


def _store_dek(dek: bytes) -> None:
    import base64
    b64 = base64.b64encode(dek).decode("ascii")
    if sys.platform == "darwin":
        _keychain_write(b64)
    else:
        _secrettool_write(b64)


# macOS Keychain via the `security` CLI ------------------------------------

def _keychain_read() -> Optional[str]:
    try:
        res = subprocess.run(
            ["security", "find-generic-password",
             "-s", _KEYSTORE_SERVICE, "-a", _KEYSTORE_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _keychain_write(b64: str) -> None:
    # -U updates the item in place if it already exists.
    try:
        res = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", _KEYSTORE_SERVICE, "-a", _KEYSTORE_ACCOUNT, "-w", b64],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        raise OSError(f"Keychain unavailable: {e}")
    if res.returncode != 0:
        raise OSError(f"Keychain write failed: {res.stderr.strip()}")


# Linux Secret Service via the `secret-tool` CLI --------------------------

def _secrettool_read() -> Optional[str]:
    try:
        res = subprocess.run(
            ["secret-tool", "lookup",
             "service", _KEYSTORE_SERVICE, "account", _KEYSTORE_ACCOUNT],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _secrettool_write(b64: str) -> None:
    try:
        res = subprocess.run(
            ["secret-tool", "store", "--label=KaproVPN config key",
             "service", _KEYSTORE_SERVICE, "account", _KEYSTORE_ACCOUNT],
            input=b64, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        raise OSError(f"secret-tool unavailable: {e}")
    if res.returncode != 0:
        raise OSError(f"secret-tool store failed: {res.stderr.strip()}")


# --------------------------------------------------------------------------
# DPAPI internals (Windows) — unchanged.
# --------------------------------------------------------------------------

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
