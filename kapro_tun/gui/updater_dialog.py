"""In-app auto-update dialog: shows changelog, downloads Setup.exe, runs it silently.

User clicks "Обновить" → we download the matching `KaproTUN-Setup.exe` to
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

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QTextBrowser, QVBoxLayout

from .. import __version__
from ..core import app_log
from ..core.i18n import tr
from ..core.updater import UpdateInfo
from . import kit, tokens
from .background import Background
from .merge_prompt import one_line


SETUP_FILENAME = "KaproTUN-Setup.exe"
# Our own mirror — reachable from RU/CIS where github.com is frequently
# DNS-blocked / throttled. That block (getaddrinfo failed for github.com)
# is exactly what made auto-update dead-on-arrival for those users.
KAPROTUN_MIRROR_BASE = "https://kaprovpn.pro/files"


def _release_setup_url(version: str) -> str:
    return (
        f"https://github.com/fafnirov/KaproTUN/releases/download/"
        f"v{version}/{SETUP_FILENAME}"
    )


def _mirror_setup_url(version: str) -> str:
    # Flat, version-tagged name — matches the binary-mirror convention and
    # keeps the server-setup sync a simple `mv` into the docroot.
    return f"{KAPROTUN_MIRROR_BASE}/KaproTUN-Setup-v{version}.exe"


def _setup_sources(version: str) -> list[str]:
    """Download sources in priority order: our mirror first (RU-reachable),
    GitHub as the canonical fallback."""
    return [_mirror_setup_url(version), _release_setup_url(version)]


# --- download worker ------------------------------------------------------

class _NotesBrowser(QTextBrowser):
    """Release notes as formatted text and nothing more: no image is ever
    loaded (a path in the notes could point at a network share), and a link is
    opened only if it is a plain http(s) address."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ktNotes")
        self.setFrameShape(QFrame.NoFrame)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._open_if_web)
        self._theme_links()

    def setMarkdown(self, text: str) -> None:  # noqa: N802 — Qt's name
        super().setMarkdown(text)
        self._theme_links()

    def _theme_links(self) -> None:
        """Give links the theme's accent. Notes loaded as Markdown ignore
        both the style sheet and the widget palette for this, so the colour
        is written into the link fragments themselves."""
        from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
        accent = QColor(tokens.colors().accent_text)
        doc = self.document()
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.charFormat().isAnchor():
                    cursor = QTextCursor(doc)
                    cursor.setPosition(frag.position())
                    cursor.setPosition(frag.position() + frag.length(), QTextCursor.KeepAnchor)
                    fmt = QTextCharFormat()
                    fmt.setForeground(accent)
                    cursor.mergeCharFormat(fmt)
                it += 1
            block = block.next()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.StyleChange:
            self._theme_links()
        super().changeEvent(event)

    def loadResource(self, _kind, _name):  # noqa: N802 — Qt override
        return None

    def _open_if_web(self, url) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from ..core.safe_text import http_url
        safe = http_url(url.toString())
        if safe:
            QDesktopServices.openUrl(QUrl(safe))


class _Cancelled(Exception):
    """Raised out of the progress callback to unwind an in-flight download.

    net_download.download_to_file invokes `progress` on every chunk and removes
    its .part file on ANY exception, so aborting this way stops the stream
    promptly and leaves no partial installer behind."""


class _DownloadWorker(Background):
    """Downloads the installer off the UI thread. A background job rather than
    a QThread: a stalled read can outlast any wait the dialog is willing to
    make, and a QThread still running when its dialog (or the app) goes away
    aborts the process."""

    progress = Signal(int, int)   # bytes_done, bytes_total
    finished_ok = Signal(str)     # path to downloaded file
    failed = Signal(str)

    def _work(self):
        self.run()
        return lambda: None

    def __init__(self, urls: list[str], dest: Path, parent=None,
                 expect_sha256: str = ""):
        super().__init__(parent)
        self._urls = urls
        self._dest = dest
        self._cancelled = False
        self._expect_sha256 = expect_sha256 or ""

    def cancel(self) -> None:
        """Ask the in-flight download to abort (v3.3.7).

        Safe to call from the GUI thread: the flag is only *read* by the
        worker's own progress callback, which raises _Cancelled to unwind the
        stream. Takes effect on the next chunk."""
        self._cancelled = True

    def _on_chunk(self, done: int, total: int) -> None:
        if self._cancelled:
            raise _Cancelled()
        self.progress.emit(done, total)

    def run(self) -> None:
        # Try each source in order (mirror, then GitHub). The first that
        # delivers a sane-sized file wins; we only report failure if ALL
        # sources fail — so a github.com DNS block alone can't kill the
        # update when the mirror is reachable.
        #
        # Bypass system proxy — see core/xray_installer for the rationale:
        # a stale 127.0.0.1 proxy entry would otherwise self-perpetuate
        # the bug (can't auto-update to a fix because the updater fails).
        from ..core import net_download
        errors: list[str] = []
        urls = list(self._urls)
        if not self._expect_sha256:
            # GitHub did not report a digest for the installer, so we have
            # nothing to check the mirror's bytes against. Drop the mirror and
            # use only the canonical source: the release asset is served by the
            # same host, under the same TLS, as the API response that told us
            # this release exists — trusting one and not the other would be
            # theatre. The mirror is the path that needs the hash, and without
            # one it does not get used.
            urls = [u for u in urls if KAPROTUN_MIRROR_BASE not in u]
            app_log.log("[integrity] no digest from GitHub — mirror skipped, "
                        "installer will come from github.com only")
        for url in urls:
            if self._cancelled:
                return
            host = url.split("/")[2] if "//" in url else url
            try:
                # Size-capped atomic download (.part -> replace). Rejects a
                # response that declares, or streams, more than the setup-exe
                # ceiling — a hostile mirror can't fill the disk.
                net_download.download_to_file(
                    url, self._dest, net_download.MAX_SETUP_EXE,
                    progress=self._on_chunk,
                    timeout=(15, 30),
                    expect_sha256=self._expect_sha256 or None,
                )
                # Guard: a mirror/CDN serving an HTML error page as 200
                # would otherwise be "downloaded" and then fail to launch.
                if self._dest.stat().st_size < 1024 * 1024:
                    raise RuntimeError(
                        tr("upd.file_too_small", size=self._dest.stat().st_size)
                    )
                if self._cancelled:
                    return
                self.finished_ok.emit(str(self._dest))
                return
            except _Cancelled:
                # Dismissed mid-download: stay silent. Don't fall through to the
                # next mirror and don't report a failure — the user said "no".
                return
            except Exception as e:
                errors.append(f"{host}: {type(e).__name__}: {e}")
        if self._cancelled:
            return
        self.failed.emit(" | ".join(errors) if errors else "download failed")


# --- dialog ---------------------------------------------------------------

class UpdaterDialog(kit.OverlayDialog):
    """One-stop update flow: what is new → one click → download → relaunch."""

    def __init__(self, info: UpdateInfo, parent=None):
        super().__init__(parent, wide=True)
        self._info = info
        self._download_worker: Optional[_DownloadWorker] = None
        self._setup_path: Optional[Path] = None
        self._cancelled = False

        self.head("update", tr("upd.window_title"),
                  tr("upd.versions", new=one_line(info.version, 30), cur=__version__), tone="accent")

        panel = QFrame()
        panel.setObjectName("ktPanel")
        col = QVBoxLayout(panel)
        col.setContentsMargins(tokens.SP_3, tokens.SP_3, tokens.SP_3, tokens.SP_3)
        col.setSpacing(tokens.SP_2)
        col.addWidget(kit.label(tr("upd.whats_new"), "label"))
        self.notes = _NotesBrowser()
        self.notes.setFixedHeight(190)
        self.notes.setMarkdown(info.notes or "_no release notes_")
        col.addWidget(self.notes)
        # The release page: only if it is an ordinary web address.
        self.release_link = kit.LinkButton(tr("upd.release_page"), info.url)
        self.release_link.setVisible(bool(self.release_link.url()))
        col.addWidget(self.release_link)
        self.add_widget(panel)

        status = QHBoxLayout()
        self.status_label = kit.label("", "textSm", wrap=True)
        status.addWidget(self.status_label, stretch=1)
        self.percent_label = kit.label("", "strong")
        status.addWidget(self.percent_label)
        self._status_box = QFrame()
        self._status_box.setLayout(status)
        status.setContentsMargins(0, 0, 0, 0)
        self._status_box.setVisible(False)
        self.add_widget(self._status_box)
        self.progress_bar = kit.Progress()
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.setVisible(False)
        self.add_widget(self.progress_bar)

        self.add_actions(
            [("later", tr("upd.later_button"), "secondary"),
             ("update", tr("upd.update_button", version=one_line(info.version, 30)), "primary")],
            # "Later" is what Enter means: updating downloads and runs an
            # installer and drops the tunnel — that takes a deliberate press.
            default="later", handlers={"later": self.reject, "update": self._start_download})
        self.later_btn, self.update_btn = self.buttons["later"], self.buttons["update"]

    def _cancel_download(self) -> None:
        """Abort any in-flight download and make the result slots inert.

        Dismissing this dialog MUST mean "don't update". Before v3.3.7 the
        worker kept running after Esc: when it finished, _on_downloaded still
        launched the installer and quit the app — tearing down the user's
        active tunnel and silently reinstalling, against an explicit "no".

        Idempotent and safe when no download is running. The bounded wait
        just gives the stream a moment to unwind; a download that is stuck in
        a read longer than that ends on its own, unheard."""
        self._cancelled = True
        worker = self._download_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(3000)   # abort lands on the next chunk; bounded join

    def reject(self) -> None:
        # Every "no": the Later button, Esc, a click outside the card.
        self._cancel_download()
        super().reject()

    def _set_status(self, text: str, tone: str = "") -> None:
        self._status_box.setVisible(True)
        self.status_label.setText(text)
        self.status_label.setProperty("tone", tone)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _start_download(self) -> None:
        self._cancelled = False
        self.update_btn.setEnabled(False)
        self._set_status(tr("upd.downloading", filename=SETUP_FILENAME,
                            version=one_line(self._info.version, 30)))
        self.percent_label.setText("")
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.setVisible(True)

        temp_dir = Path(tempfile.gettempdir())
        dest = temp_dir / f"KaproTUN-Setup-v{self._info.version}.exe"
        self._download_worker = _DownloadWorker(
            _setup_sources(self._info.version), dest, parent=self,
            expect_sha256=getattr(self._info, "setup_sha256", ""),
        )
        self._download_worker.progress.connect(self._on_progress)
        self._download_worker.finished_ok.connect(self._on_downloaded)
        self._download_worker.failed.connect(self._on_failed)
        self._download_worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if self._cancelled:
            return
        mb = 1024 * 1024
        if total > 0:
            self.progress_bar.set_fraction(done / total)
            self._set_status(tr("upd.progress_of", done=f"{done / mb:.1f}", total=f"{total / mb:.1f}"))
            self.percent_label.setText(f"{int(done * 100 / total)} %")
        else:
            self.progress_bar.set_fraction(None)
            self._set_status(tr("upd.progress_mb", mb=done // mb))
            self.percent_label.setText("")

    def _on_downloaded(self, path: str) -> None:
        if self._cancelled:
            return
        self._setup_path = Path(path)
        self._set_status(tr("upd.launching"))
        self.percent_label.setText("")
        self.progress_bar.set_fraction(None)
        # The installer takes over in silent mode and starts the new version.
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
            self._on_failed(tr("upd.launch_failed", error=e))
            return
        self.accept()
        QApplication.quit()

    def _on_failed(self, msg: str) -> None:
        if self._cancelled:
            return
        # The message quotes hosts and exception texts: one plain line.
        self._set_status(tr("upd.error_prefix", msg=one_line(msg, 300)), tone="error")
        self.percent_label.setText("")
        self.progress_bar.setVisible(False)
        self.later_btn.setEnabled(True)
        self.update_btn.setEnabled(True)
        self.update_btn.setText(tr("upd.retry_button"))
