"""Record the SHA-256 of the freshly built KaproTUN.exe in the installer's
sources, so the installer built next installs exactly that binary.

Run between the two PyInstaller steps of the Windows release build:

    python -m PyInstaller KaproTUN.spec ...
    python packaging/write_payload_digest.py
    python -m PyInstaller KaproTUN-Setup.spec ...
"""
import hashlib
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
exe = root / "dist" / "KaproTUN.exe"
target = root / "installer" / "payload_digest.py"

if not exe.is_file():
    sys.exit(f"{exe} is not built yet — run PyInstaller on KaproTUN.spec first")

h = hashlib.sha256()
with open(exe, "rb") as f:
    for block in iter(lambda: f.read(1024 * 1024), b""):
        h.update(block)
digest = h.hexdigest()

source = target.read_text(encoding="utf-8")
updated, count = re.subn(r'^MAIN_EXE_SHA256 = "[0-9a-f]*"$',
                         f'MAIN_EXE_SHA256 = "{digest}"', source, flags=re.M)
if count != 1:
    sys.exit(f"{target} has no MAIN_EXE_SHA256 line to fill in")
target.write_text(updated, encoding="utf-8", newline="\n")
print(f"KaproTUN.exe {exe.stat().st_size} bytes, sha256 {digest} -> {target.name}")
