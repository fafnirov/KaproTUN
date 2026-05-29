"""In-app auto-update dialog: shows changelog, downloads Setup.exe, runs it silently.

User clicks "Обновить" → we download the matching `KaproVPN-Setup.exe` to
`%TEMP%`, launch it with `--silent`, and `QApplication.quit()`. The
installer's silent mode then waits a beat for our handles to release,
overwrites the install, and launches the new app — so the user clicks
once and ends up running the new version.

If anything fails (network / GitHub down), we surface the error and
fall back to opening the release page in the browser, which is what
the v0.1.0 updater already did.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .. import __version__
from ..core.updater import UpdateInfo


SETUP_FILENAME = "KaproVPN-Setup.exe"
# Our own mirror — reachable from RU/CIS where github.com is frequently
# DNS-blocked / throttled. That block (getaddrinfo failed for github.com)
# is exactly what made auto-update dead-on-arrival for those users.
KAPROVPN_MIRROR_BASE = "https://files.kaprovpn.pro"


def _release_setup_url(version: str) -> str:
    return (
        f"https://github.com/fafnirov/KaproVPN/releases/download/"
        f"v{version}/{SETUP_FILENAME}"
    )


def _mirror_setup_url(version: str) -> str:
    # Flat, version-tagged name — matches the binary-mirror convention and
    # keeps the server-setup sync a simple `mv` into the docroot.
    return f"{KAPROVPN_MIRROR_BASE}/KaproVPN-Setup-v{version}.exe"


def _setup_sources(version: str) -> list[str]:
    """Download sources in priority order: our mirror first (RU-reachable),
    GitHub as the canonical fallback."""
    return [_mirror_setup_url(version), _release_setup_url(version)]


# --- download worker ------------------------------------------------------

class _DownloadWorker(QThread):
    progress = Signal(int, int)   # bytes_done, bytes_total
    finished_ok = Signal(str)     # path to downloaded file
    failed = Signal(str)

    def __init__(self, urls: list[str], dest: Path, parent=None):
        super().__init__(parent)
        self._urls = urls
        self._dest = dest

    def run(self) -> None:
        # Try each source in order (mirror, then GitHub). The first that
        # delivers a sane-sized file wins; we only report failure if ALL
        # sources fail — so a github.com DNS block alone can't kill the
        # update when the mirror is reachable.
        #
        # Bypass system proxy — see core/xray_installer for the rationale:
        # a stale 127.0.0.1 proxy entry would otherwise self-perpetuate
        # the bug (can't auto-update to a fix because the updater fails).
        errors: list[str] = []
        for url in self._urls:
            host = url.split("/")[2] if "//" in url else url
            try:
                downloaded = 0
                with requests.get(url, stream=True, timeout=(15, 30),
                                  proxies={"http": "", "https": ""}) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0))
                    with open(self._dest, "wb") as f:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            self.progress.emit(downloaded, total)
                # Guard: a mirror/CDN serving an HTML error page as 200
                # would otherwise be "downloaded" and then fail to launch.
                if self._dest.stat().st_size < 1024 * 1024:
                    raise RuntimeError(
                        f"файл подозрительно мал ({self._dest.stat().st_size} Б)"
                    )
                self.finished_ok.emit(str(self._dest))
                return
            except Exception as e:
                errors.append(f"{host}: {type(e).__name__}: {e}")
        self.failed.emit(" | ".join(errors) if errors else "download failed")


# --- dialog ---------------------------------------------------------------

class UpdaterDialog(QDialog):
    """One-stop update flow: changelog → click → download → relaunch."""

    def __init__(self, info: UpdateInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self._download_worker: Optional[_DownloadWorker] = None
        self._setup_path: Optional[Path] = None

        self.setWindowTitle("Доступно обновление")
        self.resize(540, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel(f"KaproVPN v{info.version}")
        title.setObjectName("h1")
        layout.addWidget(title)

        sub = QLabel(f"Текущая: v{__version__}")
        sub.setObjectName("muted")
        layout.addWidget(sub)

        notes_label = QLabel("Что нового:")
        notes_label.setObjectName("h2")
        layout.addWidget(notes_label)

        # Render release notes — markdown via QTextBrowser. Limited
        # rendering (no images, no JS) but enough for headings + lists.
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMarkdown(info.notes or "_no release notes_")
        layout.addWidget(self.notes, stretch=1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.later_btn = QPushButton("Отложить")
        self.later_btn.clicked.connect(self.reject)
        self.update_btn = QPushButton(f"Обновить до v{info.version}")
        self.update_btn.setObjectName("primary")
        self.update_btn.clicked.connect(self._start_download)
        btn_row.addWidget(self.later_btn)
        btn_row.addWidget(self.update_btn)
        layout.addLayout(btn_row)

    # --- download flow ----------------------------------------------------

    def _start_download(self) -> None:
        self.update_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.status_label.setVisible(True)
        self.status_label.setText(
            f"Качаю {SETUP_FILENAME} v{self._info.version} "
            f"(зеркало, при сбое — GitHub)…"
        )
        self.progress_bar.setVisible(True)

        # %TEMP%\KaproVPN-Setup-v0.1.X.exe — version in name so multiple
        # downloads don't collide.
        temp_dir = Path(tempfile.gettempdir())
        dest = temp_dir / f"KaproVPN-Setup-v{self._info.version}.exe"

        self._download_worker = _DownloadWorker(
            _setup_sources(self._info.version), dest, parent=self,
        )
        self._download_worker.progress.connect(self._on_progress)
        self._download_worker.finished_ok.connect(self._on_downloaded)
        self._download_worker.failed.connect(self._on_failed)
        self._download_worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = int(done * 100 / total)
            self.progress_bar.setValue(pct)
            mb_done = done // (1024 * 1024)
            mb_total = total // (1024 * 1024)
            self.status_label.setText(
                f"Скачано {mb_done} / {mb_total} МБ ({pct} %)"
            )
        else:
            mb = done // (1024 * 1024)
            self.status_label.setText(f"Скачано {mb} МБ")

    def _on_downloaded(self, path: str) -> None:
        self._setup_path = Path(path)
        self.status_label.setText("Запускаю установку…")
        self.progress_bar.setRange(0, 0)  # indeterminate spinner

        # Launch the installer in silent mode, detached so it survives
        # our QApplication.quit().
        try:
            subprocess.Popen(
                [str(self._setup_path), "--silent"],
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
                close_fds=True,
            )
        except OSError as e:
            self._on_failed(f"Не удалось запустить установщик: {e}")
            return

        # Bow out so the installer can overwrite our exe.
        self.accept()
        QApplication.quit()

    def _on_failed(self, msg: str) -> None:
        self.status_label.setText(
            f"<span style='color:#ef4444'>Ошибка: {msg}</span><br>"
            f"<a href='{self._info.url}' style='color:#f59e0b'>"
            f"Открыть страницу релиза в браузере</a>"
        )
        self.status_label.setTextFormat(Qt.RichText)
        self.status_label.setOpenExternalLinks(True)
        self.progress_bar.setVisible(False)
        self.later_btn.setEnabled(True)
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Попробовать ещё раз")
