"""Check a file against a digest and keep it from changing until it is used.

Why this exists. KaproTUN runs with administrator rights, but the files it
executes — the engine, a downloaded installer — sit in folders that any
program of the same user can write to without those rights. Checking a digest
when the file is downloaded proves what was downloaded, not what is on disk
an hour later; and even a check made right before the start leaves a gap
between "the digest is right" and "the process is running" in which the file
can be swapped. A program without rights that wins that gap gets its code run
with them.

So the check and the use happen on the same open handle: the file is opened
so that nobody else can write, replace, rename or delete it, hashed through
that handle, and only then started — while it is still held.

Holding the file is not enough by itself: a process is started by PATH, and
a path can be made to lead somewhere else. If any folder on the way is a
junction, it can be re-pointed after the check — the held file stays intact
and a different one runs. So the location is pinned as well: the file's real
path (with every junction resolved) is taken from the handle, each folder on
that real path must not itself be a link and is held against removal and
renaming, and the path is confirmed to still lead to the held file. The
caller starts the file by that real path (`HeldFile.real_path`).

(Where a folder cannot be held — another program has it open its own way, or
we may not list it — it is only looked at. That is enough: the system does
not let a folder be renamed or removed while a file below it is open, and
ours is; the hold is a second lock on top of that.)

Windows only, for the holding part: there the sharing mode of an open handle
is enforced by the system. On macOS and Linux an open file can still be
replaced by path, so hold_verified() there is a digest check and nothing more.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

from .net_download import IntegrityError


class HoldError(IntegrityError):
    """The file could not be held or its location pinned. Says nothing about
    its content — so, unlike a digest mismatch, downloading it again is not
    the cure."""


class _LinkOnPath(PermissionError):
    """A folder on the file's real path is a junction or a link."""

_CHUNK = 1024 * 1024
_SHARING_RETRIES = 5
_SHARING_WAIT_S = 0.2

_WINDOWS = sys.platform == "win32"

if _WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _FILE_READ_ATTRIBUTES = 0x00000080           # look, without taking part in sharing
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000     # needed to open a directory
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000   # the junction itself, not its target
    _ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND = 2, 3
    _ERROR_SHARING_VIOLATION = 32
    _NAME_SURROGATE = 0x20000000                 # reparse tags that send the path elsewhere
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _VOLUME_NAME_DOS, _VOLUME_NAME_GUID = 0, 1
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD),
                    ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME),
                    ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD),
                    ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD),
                    ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD),
                    ("nFileIndexLow", wintypes.DWORD)]

    class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD),
                    ("ReparseTag", wintypes.DWORD)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                               wintypes.DWORD, wintypes.DWORD]
    _k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _k32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
    _k32.GetFileInformationByHandle.restype = wintypes.BOOL
    _k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]

    def _create(path: str, share: int, flags: int, access: int = _GENERIC_READ):
        handle = _k32.CreateFileW(path, access, share, None, _OPEN_EXISTING, flags, None)
        if handle is None or handle == _INVALID_HANDLE:
            code = ctypes.get_last_error()
            if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                raise FileNotFoundError(code, "No such file or directory", path)
            raise PermissionError(code, ctypes.FormatError(code).strip(), path)
        return handle

    def _info(handle) -> "_BY_HANDLE_FILE_INFORMATION":
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            code = ctypes.get_last_error()
            raise OSError(code, ctypes.FormatError(code).strip())
        return info

    def _identity(info) -> tuple:
        return (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow)

    def _is_link(handle) -> bool:
        """Is this a junction, a symbolic link or a mounted folder — something
        that sends the path elsewhere? Other reparse points (a cloud-synced
        folder, for one) are the folder itself and are fine."""
        info = _FILE_ATTRIBUTE_TAG_INFO()
        if not _k32.GetFileInformationByHandleEx(handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS,
                                                 ctypes.byref(info), ctypes.sizeof(info)):
            code = ctypes.get_last_error()
            raise OSError(code, ctypes.FormatError(code).strip())
        return bool(info.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
                    and info.ReparseTag & _NAME_SURROGATE)

    def _final_path(handle) -> str:
        """Where the open file really is: every junction and link resolved."""
        size = 1024
        kind = _VOLUME_NAME_DOS
        while True:
            buf = ctypes.create_unicode_buffer(size)
            n = _k32.GetFinalPathNameByHandleW(handle, buf, size, kind)
            if n == 0:
                code = ctypes.get_last_error()
                if kind == _VOLUME_NAME_DOS:
                    kind = _VOLUME_NAME_GUID     # a volume with no drive letter
                    continue
                raise OSError(code, ctypes.FormatError(code).strip())
            if n < size:
                break
            size = n + 1
        path = buf.value
        if kind == _VOLUME_NAME_GUID:
            return path                          # \\?\Volume{...}\... is usable as it is
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        if path.startswith("\\\\?\\"):
            return path[4:]
        return path


class HeldFile:
    """A file that stays what it is, where it is, until close(). Use as a
    context manager, or keep it for as long as the thing started from the
    file must be able to rely on it. Start the file by `real_path`."""

    _file = None    # so close() is safe even when opening failed

    def __init__(self, path: Path):
        self.path = Path(path)
        self.real_path = self.path
        self._dirs: list = []
        if not _WINDOWS:
            self._file = open(self.path, "rb")
            return
        handle = _create(str(self.path), _FILE_SHARE_READ, _FILE_ATTRIBUTE_NORMAL)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except Exception:
            _k32.CloseHandle(handle)
            raise
        self._file = os.fdopen(fd, "rb")    # owns the handle from here on
        try:
            self._pin_location(handle)
        except Exception:
            self.close()
            raise

    def _pin_location(self, handle) -> None:
        """Make the path as unchangeable as the file (see the module note)."""
        real = Path(_final_path(handle))
        # Hold every folder on the real path: with a handle that does not
        # share delete access open on it, a folder can be neither removed nor
        # renamed — so it cannot be swapped for a junction. Each must be a
        # real folder already, not a link.
        look = _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT
        for folder in list(real.parents)[:-1]:      # the drive root cannot be swapped
            try:
                dir_handle = _create(str(folder), _FILE_SHARE_READ | _FILE_SHARE_WRITE, look)
            except PermissionError:
                # Another program has this folder open in a way that excludes
                # our hold (a backup tool, a sync client), or we may pass
                # through it but not list it. Neither may stop the engine
                # from starting, so here we only look (see the module note).
                dir_handle = _create(
                    str(folder), _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                    look, access=_FILE_READ_ATTRIBUTES)
            self._dirs.append(dir_handle)
            if _is_link(dir_handle):
                raise _LinkOnPath(5, "a folder on the path is a junction or link", str(folder))
        # Now that nothing on the way can move: does the real path lead to
        # the very file we hold?
        probe = _create(str(real), _FILE_SHARE_READ, _FILE_ATTRIBUTE_NORMAL)
        try:
            same = _identity(_info(probe)) == _identity(_info(handle))
        finally:
            _k32.CloseHandle(probe)
        if not same:
            raise PermissionError(5, "the path no longer leads to the checked file", str(real))
        self.real_path = real

    def sha256(self) -> str:
        """Digest of the file as it is under this handle."""
        if self._file is None:
            raise ValueError("the file has been released")
        h = hashlib.sha256()
        self._file.seek(0)
        for block in iter(lambda: self._file.read(_CHUNK), b""):
            h.update(block)
        return h.hexdigest()

    def close(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        dirs, self._dirs = getattr(self, "_dirs", []), []
        for handle in dirs:
            try:
                _k32.CloseHandle(handle)
            except Exception:
                pass

    def __enter__(self) -> "HeldFile":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def hold_verified(path, expected_sha256: str) -> HeldFile:
    """Open `path`, held against changes and pinned in place, and check it
    against `expected_sha256`. Returns the held file; the caller starts it by
    `real_path` and closes it when it no longer has to stay put.

    Raises FileNotFoundError if there is no such file, IntegrityError if the
    digest does not match or no usable digest was given ("nothing to compare
    with" is a refusal, never a pass), and HoldError — an IntegrityError too —
    if the file or its location cannot be held.
    """
    want = (expected_sha256 or "").strip().lower().removeprefix("sha256:")
    name = Path(path).name
    if len(want) != 64 or any(c not in "0123456789abcdef" for c in want):
        raise IntegrityError(
            f"Нет контрольной суммы для {name} — файл не будет использован.")
    held = None
    for attempt in range(_SHARING_RETRIES):
        try:
            held = HeldFile(Path(path))
            break
        except FileNotFoundError:
            raise
        except OSError as e:
            busy = _WINDOWS and e.errno == _ERROR_SHARING_VIOLATION
            if busy and attempt < _SHARING_RETRIES - 1:
                # Something has it open for writing this instant — usually a
                # virus scanner looking at a file that was just downloaded.
                # Give it a moment before deciding the file cannot be used.
                time.sleep(_SHARING_WAIT_S)
                continue
            if isinstance(e, _LinkOnPath):
                why = (f"Путь к файлу {name} ведёт через ссылку на другую папку "
                       f"(junction): {e.filename}. Такой файл не будет использован.")
            elif busy:
                why = (f"Файл {name} открыт другой программой на запись и не будет "
                       "использован. Попробуй ещё раз через несколько секунд.")
            else:
                why = (f"Файл {name} не удалось закрепить перед запуском, поэтому он "
                       f"не будет использован ({e.strerror or type(e).__name__}).")
            raise HoldError(why) from e
    try:
        got = held.sha256()
    except Exception:
        held.close()
        raise
    if got != want:
        held.close()
        raise IntegrityError(
            f"Контрольная сумма не совпала ({name}).\n"
            f"Ожидалась: {want}\nПолучена:  {got}\n\n"
            "Файл изменён после загрузки и не будет запущен.")
    return held


def release_all(held: list) -> None:
    """Close every HeldFile in `held` and empty the list."""
    while held:
        held.pop().close()
