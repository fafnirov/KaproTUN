"""Installer UI — one frameless window, in the look of the main application.

Screens: welcome → installing → done / error; "already installed" with
Reinstall / Remove; removal: confirm → removing → removed / error. And, apart
from the window, the small always-on-top card shown while the application
updates itself (run_silent).

Everything a screen is made of is here or in kapro_tun.gui (tokens, kit,
boot): the same bar and the same mark as the start-up splash, so that
starting, installing and updating read as one family of windows.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Optional

from PySide6.QtCore import (QEventLoop, QPointF, QRectF, QSize, Qt, QThread, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QPlainTextEdit, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from kapro_tun import __version__
from kapro_tun.gui import boot, icons as app_icons, icons_v2, kit, tokens
from kapro_tun.gui.styles import get_qss
from kapro_tun.gui.titlebar import TitleBar

from . import operations, paths

INSTALL_DIR_TEXT = "%LOCALAPPDATA%\\Programs\\KaproTUN"
DATA_DIR_TEXT = "%LOCALAPPDATA%\\KaproTUN"
GITHUB_URL = "https://github.com/fafnirov/KaproTUN"

INSTALL_STEPS = ("Закрываю запущенный KaproTUN", "Скачиваю KaproTUN.exe",
                 "Создаю ярлыки в меню Пуск и на Рабочем столе", "Регистрирую удаление в Windows")
# Set while run_selftest() draws the screens: nothing may start from them.
_selftest = False


def install_steps(desktop: bool = True) -> tuple:
    """The steps of an install as the list shows them; without the desktop
    shortcut the third one does not promise it."""
    if desktop:
        return INSTALL_STEPS
    return INSTALL_STEPS[:2] + ("Создаю ярлык в меню Пуск",) + INSTALL_STEPS[3:]


REMOVE_STEPS = ("Закрываю KaproTUN", "Удаляю ярлыки",
                "Удаляю запись из «Установка и удаление программ»", "Снимаю автозапуск",
                "Удаляю файлы программы")


def install_stage(percent: int) -> int:
    """Which of INSTALL_STEPS is running at `percent` of install_everything
    (the number of steps already finished)."""
    for index, upto in enumerate((5, 55, 95, 100)):
        if percent < upto:
            return index
    return len(INSTALL_STEPS)


def remove_stage(percent: int) -> int:
    for index, upto in enumerate((10, 30, 50, 80, 100)):
        if percent < upto:
            return index
    return len(REMOVE_STEPS)


def split_volume(text: str) -> tuple[str, str]:
    """"Скачиваю KaproTUN.exe… 24 / 62 МБ" → ("Скачиваю KaproTUN.exe…",
    "24 / 62 МБ"): the step goes above the bar, the volume under it."""
    found = re.search(r"\s*(\d+\s*/\s*\d+\s*МБ)\s*$", text)
    if not found:
        return text, ""
    return text[:found.start()].rstrip(), found.group(1)


# --- worker threads -------------------------------------------------------

class _InstallWorker(QThread):
    progress = Signal(str, int)  # (status_text, percent)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, version: str, create_desktop: bool, parent=None):
        super().__init__(parent)
        self._version = version
        self._desktop = create_desktop

    def run(self) -> None:
        try:
            operations.install_everything(
                version=self._version,
                progress=lambda s, p: self.progress.emit(s, p),
                create_desktop=self._desktop,
            )
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class _UninstallWorker(QThread):
    progress = Signal(str, int)
    finished_ok = Signal()
    failed = Signal(str)

    def run(self) -> None:
        try:
            operations.uninstall_everything(
                progress=lambda s, p: self.progress.emit(s, p),
            )
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# --- pieces the screens are made of ---------------------------------------

def _label(text: str, name: str, *, center: bool = False, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName(name)
    lab.setTextFormat(Qt.PlainText)
    if center:
        lab.setAlignment(Qt.AlignHCenter)
    if wrap:
        lab.setWordWrap(True)
    return lab


class Hero(QWidget):
    """The picture on the welcome screen: one line that forks — part of the
    traffic through the VPN, part of it directly. Drawn, not an image."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(tokens.HERO_H)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        scale = self.width() / 414.0
        p.translate(0, (self.height() - 112 * scale) / 2)
        p.scale(scale, scale)

        def stroke(color: str) -> None:
            p.setPen(QPen(QColor(color), 2, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)

        stroke(c.line_strong)
        p.drawLine(QPointF(14, 56), QPointF(170, 56))
        direct = QPainterPath(QPointF(196, 56))
        direct.cubicTo(QPointF(250, 56), QPointF(262, 88), QPointF(318, 88))
        direct.lineTo(372, 88)
        p.drawPath(direct)
        stroke(c.accent)
        tunnel = QPainterPath(QPointF(196, 56))
        tunnel.cubicTo(QPointF(250, 56), QPointF(262, 24), QPointF(318, 24))
        tunnel.lineTo(372, 24)
        p.drawPath(tunnel)

        def node(x: float, y: float, r: float, color: str) -> None:
            p.setPen(QPen(QColor(color), 2))
            p.setBrush(QColor(c.bg))
            p.drawEllipse(QPointF(x, y), r, r)

        node(14, 56, 5, c.line_hover)
        node(183, 56, 13, c.accent)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(c.accent))
        p.drawEllipse(QPointF(183, 56), 5, 5)
        node(380, 24, 7, c.accent)
        node(380, 88, 7, c.line_hover)

        font = QFont(self.font())
        font.setPixelSize(tokens.FS_XS)
        p.setFont(font)
        p.setPen(QColor(c.text_tertiary))
        # By baseline, as in the drawing; the lowest one two pixels up so that
        # its descenders stay inside the picture.
        metrics = p.fontMetrics()
        for text, right, baseline in (("через VPN", 372, 50), ("напрямую", 372, 108)):
            p.drawText(QPointF(right - metrics.horizontalAdvance(text), baseline), text)
        p.drawText(QPointF(14, 80), "твой ПК")
        p.end()


class _Spinner(QWidget):
    """A quarter-arc that turns: "this step is running"."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(tokens.ICON_SM, tokens.ICON_SM)
        self._angle = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setDuration(900)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_tick)

    def _on_tick(self, value) -> None:
        self._angle = float(value)
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._anim.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._anim.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(tokens.colors().accent_text), 1.75, Qt.SolidLine, Qt.RoundCap))
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.drawArc(rect, int((90 - self._angle) * 16), int(-90 * 16))
        p.end()


_FACT_ICON_TOKEN = {"": "text_tertiary", "done": "success_text", "pending": "text_tertiary",
                    "remove": "danger_text", "keep": "accent_text"}


class Facts(QFrame):
    """A short list in one rounded panel: what will be done, what was done,
    what goes and what stays — or the steps of a process, each one done,
    running or still to come."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktFacts")
        self._col = QVBoxLayout(self)
        self._col.setContentsMargins(0, 0, 0, 0)
        self._col.setSpacing(0)
        self._rows: list[dict] = []

    def add(self, icon: str, text: str, meta: str = "", state: str = "") -> None:
        row = QFrame()
        row.setObjectName("ktFactsRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(tokens.SP_3, tokens.SP_2H, tokens.SP_3, tokens.SP_2H)
        line.setSpacing(tokens.SP_2H)
        slot = QWidget()
        slot.setFixedSize(tokens.ICON_SM, tokens.ICON_SM + tokens.SP_HALF)
        holder = QVBoxLayout(slot)
        holder.setContentsMargins(0, tokens.SP_HALF, 0, 0)
        holder.setSpacing(0)
        glyph = icons_v2.IconLabel(icon, tokens.ICON_SM, _FACT_ICON_TOKEN.get(state, "text_tertiary"))
        spinner = _Spinner()
        spinner.setVisible(False)
        holder.addWidget(glyph)
        holder.addWidget(spinner)
        line.addWidget(slot, 0, Qt.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(tokens.SP_HALF)
        label = _label(text, "ktFactText", wrap=True)
        body.addWidget(label)
        meta_label = kit.ElidedLabel()
        meta_label.setObjectName("ktFactMeta")
        meta_label.setText(meta)
        meta_label.setVisible(bool(meta))
        body.addWidget(meta_label)
        line.addLayout(body, 1)
        self._col.addWidget(row)
        self._rows.append({"row": row, "glyph": glyph, "spinner": spinner, "label": label,
                           "meta": meta_label, "state": state, "icon": icon})
        for i, item in enumerate(self._rows):
            item["row"].setProperty("last", "true" if i == len(self._rows) - 1 else "false")
            item["row"].style().unpolish(item["row"])
            item["row"].style().polish(item["row"])

    def add_step(self, text: str) -> None:
        self.add("circle", text, state="pending")
        self.set_state(len(self._rows) - 1, "pending")

    def set_state(self, index: int, state: str, meta: Optional[str] = None) -> None:
        """A process step: done (a tick), active (a turning arc), pending."""
        item = self._rows[index]
        item["state"] = state
        item["spinner"].setVisible(state == "active")
        item["glyph"].setVisible(state != "active")
        item["glyph"].set_icon({"done": "check-circle", "pending": "circle"}.get(state, item["icon"]),
                               _FACT_ICON_TOKEN.get(state, "text_tertiary"))
        item["label"].setProperty("state", state)
        item["label"].style().unpolish(item["label"])
        item["label"].style().polish(item["label"])
        if meta is not None:
            item["meta"].setText(meta)
            item["meta"].setVisible(bool(meta))

    def set_progress(self, done: int, active_meta: str = "") -> None:
        """The first `done` steps are finished, the next one is running."""
        for i in range(len(self._rows)):
            state = "done" if i < done else ("active" if i == done else "pending")
            self.set_state(i, state, active_meta if state == "active" else "")

    def states(self) -> list[str]:
        return [item["state"] for item in self._rows]

    def texts(self) -> list[str]:
        return [item["label"].text() for item in self._rows]


class ResultMark(QWidget):
    """The small round sign of an outcome: done (the one place green is
    used) or failed."""

    def __init__(self, ok: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._ok = ok
        self.setFixedSize(tokens.RESULT_ICON, tokens.RESULT_ICON)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        soft, ink = (c.success_soft, c.success_text) if self._ok else (c.danger_soft, c.danger_text)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(soft))
        p.drawEllipse(QRectF(self.rect()))
        size = tokens.ICON_LG
        p.drawPixmap(int((self.width() - size) / 2), int((self.height() - size) / 2),
                     icons_v2.pixmap("check" if self._ok else "x", size, ink))
        p.end()


class CheckLine(QAbstractButton):
    """A check box of our own: 18 x 18, amber when ticked, its label next to
    it. (The system one cannot be themed to match.)"""

    def __init__(self, text: str, checked: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        font = QFont(self.font())
        font.setPixelSize(tokens.FS_MD)
        self.setFont(font)

    def sizeHint(self):  # noqa: N802
        width = 18 + tokens.SP_2 + self.fontMetrics().horizontalAdvance(self.text()) + 2
        return QSize(width, 22)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        box = QRectF(0.5, (self.height() - 18) / 2 + 0.5, 17, 17)
        if self.isChecked():
            p.setPen(QPen(QColor(c.accent), 1))
            p.setBrush(QColor(c.accent))
        else:
            p.setPen(QPen(QColor(c.line_hover if self.underMouse() else c.line_strong), 1))
            p.setBrush(QColor(c.input_bg))
        p.drawRoundedRect(box, 4, 4)
        if self.isChecked():
            p.drawPixmap(int(box.x() + 3), int(box.y() + 3),
                         icons_v2.pixmap("check", tokens.ICON_XS, c.on_accent))
        if self.hasFocus():
            p.setPen(QPen(QColor(c.focus), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(-2, -2, 2, 2), 6, 6)
        p.setPen(QColor(c.text))
        p.drawText(QRectF(18 + tokens.SP_2, 0, self.width(), self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, self.text())
        p.end()


class _Head(QWidget):
    """The top of a process or confirmation screen: an icon in a rounded
    chip, the title, one line under it."""

    def __init__(self, icon: str, title: str, sub: str, tone: str = "accent",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.SP_3)
        chip = QFrame()
        chip.setObjectName("ktHeadChip")
        chip.setProperty("tone", tone)
        chip.setFixedSize(36, 36)
        inner = QVBoxLayout(chip)
        inner.setContentsMargins(0, 0, 0, 0)
        token = {"accent": "accent_text", "danger": "danger_text"}.get(tone, "text_secondary")
        inner.addWidget(icons_v2.IconLabel(icon, tokens.ICON_MD, token), 0, Qt.AlignCenter)
        row.addWidget(chip, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(tokens.SP_HALF)
        self.title = _label(title, "ktInstH")
        col.addWidget(self.title)
        self.sub = kit.ElidedLabel()
        self.sub.setObjectName("ktInstSub")
        self.sub.setText(sub)
        col.addWidget(self.sub)
        row.addLayout(col, 1)


def _centered(title: str, sub: str, *, big: bool = False, mark: Optional[bool] = None) -> QWidget:
    """A screen's title block in the middle: optionally the round outcome
    sign, the title, one line under it."""
    box = QWidget()
    col = QVBoxLayout(box)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(tokens.SP_1)
    if mark is not None:
        col.addWidget(ResultMark(mark), 0, Qt.AlignHCenter)
        col.addSpacing(tokens.SP_2)
    col.addWidget(_label(title, "ktInstTitle" if big else "ktInstH", center=True))
    col.addWidget(_label(sub, "ktInstSub", center=True, wrap=True))
    return box


def _page() -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    page.setObjectName("page")
    col = QVBoxLayout(page)
    col.setContentsMargins(tokens.INST_PAD_X, tokens.SP_8, tokens.INST_PAD_X, tokens.SP_6)
    col.setSpacing(tokens.SP_4)
    return page, col


def _foot(col: QVBoxLayout) -> QVBoxLayout:
    """The bottom block of a screen, pushed down; buttons go here."""
    col.addStretch(1)
    foot = QVBoxLayout()
    foot.setSpacing(tokens.SP_3)
    col.addLayout(foot)
    return foot


# --- screens ---------------------------------------------------------------

class WelcomePage(QWidget):
    install_clicked = Signal(bool)  # bool = create desktop shortcut

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.INST_PAD_X, tokens.SP_8, tokens.INST_PAD_X, tokens.SP_6)
        col.setSpacing(tokens.SP_4)
        col.addWidget(Hero())
        col.addWidget(_centered("KaproTUN", f"Установщик · v{__version__}", big=True))
        col.addWidget(_label("VPN-клиент с раздельной маршрутизацией: сайты из твоего списка идут "
                             "напрямую, остальное — через VPN.", "ktInstText", center=True, wrap=True))
        facts = Facts()
        facts.add("user", "Ставится в твой профиль — права администратора не нужны", INSTALL_DIR_TEXT)
        facts.add("download", "Скачает KaproTUN.exe, около 60 МБ", "kaprovpn.pro, при сбое — github.com")
        col.addWidget(facts)
        foot = _foot(col)
        self.desktop_check = CheckLine("Создать ярлык на Рабочем столе", True)
        foot.addWidget(self.desktop_check, 0, Qt.AlignHCenter)
        self.install_btn = kit.Button("Установить", "primary", size="lg")
        self.install_btn.setFocusPolicy(Qt.StrongFocus)
        self.install_btn.clicked.connect(self._on_install)
        foot.addWidget(self.install_btn)
        legal = QLabel(f"<span>GPL v3 · </span><a href='{GITHUB_URL}' style='color:"
                       f"{tokens.colors().accent_text}; text-decoration:none'>"
                       "github.com/fafnirov/KaproTUN</a>")
        legal.setObjectName("ktInstLegal")
        legal.setTextFormat(Qt.RichText)
        legal.setOpenExternalLinks(True)
        legal.setAlignment(Qt.AlignHCenter)
        foot.addWidget(legal)

    def _on_install(self) -> None:
        self.install_clicked.emit(self.desktop_check.isChecked())


class MaintenancePage(QWidget):
    """Shown when Setup.exe runs on a machine where KaproTUN is already
    installed. Two choices: Reinstall (the main one) or Remove (quiet, second
    — nothing is removed by accident)."""
    # No bool arg: at reinstall time the user already has shortcuts from the
    # original install; create_desktop=True would only make a duplicate.
    reinstall_clicked = Signal()
    uninstall_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.INST_PAD_X, tokens.SP_8, tokens.INST_PAD_X, tokens.SP_6)
        col.setSpacing(tokens.SP_4)
        col.addWidget(Hero())
        col.addWidget(_centered("KaproTUN", "Уже установлен на этом компьютере", big=True))

        installed = _detect_installed_version()
        vers = QFrame()
        vers.setObjectName("ktVers")
        row = QHBoxLayout(vers)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        cells = [("Установлена", installed or "—")]
        if installed != __version__:
            cells.append(("Этот установщик", __version__))
        for index, (caption, value) in enumerate(cells):
            cell = QFrame()
            cell.setObjectName("ktVersCell")
            cell.setProperty("second", "true" if index else "false")
            box = QVBoxLayout(cell)
            box.setContentsMargins(tokens.SP_4, tokens.SP_3, tokens.SP_4, tokens.SP_3)
            box.setSpacing(tokens.SP_HALF)
            box.addWidget(_label(caption, "ktVersLabel"))
            box.addWidget(_label(value if value == "—" else f"v{value}", "ktVersValue"))
            row.addWidget(cell, 1)
        col.addWidget(vers)
        col.addWidget(_label("Серверы, подписки и настройки сохранятся и при переустановке, и при "
                             "удалении.", "ktInstCaption", wrap=True))
        foot = _foot(col)
        self.reinstall_btn = kit.Button("Переустановить", "primary", size="lg")
        self.reinstall_btn.setFocusPolicy(Qt.StrongFocus)
        self.reinstall_btn.clicked.connect(self.reinstall_clicked)
        foot.addWidget(self.reinstall_btn)
        self.uninstall_btn = QPushButton("Удалить KaproTUN")
        self.uninstall_btn.setObjectName("ktBtnDangerQuiet")
        self.uninstall_btn.setCursor(Qt.PointingHandCursor)
        self.uninstall_btn.setIcon(icons_v2.icon("trash", tokens.ICON_SM, tokens.colors().danger_text))
        self.uninstall_btn.clicked.connect(self.uninstall_clicked)
        foot.addWidget(self.uninstall_btn, 0, Qt.AlignHCenter)


def _detect_installed_version() -> Optional[str]:
    """The version of the KaproTUN that is installed: what the installer
    wrote down when it registered it in Windows (DisplayVersion). None when
    it is not known — the screen then shows a dash.

    Until v4.2.0 this asked PowerShell for the version stamped into
    KaproTUN.exe. The file carries no such stamp, so the answer was always
    empty — after a PowerShell start on the GUI thread, with the path pasted
    into the command."""
    if not paths.installed_exe_path().is_file():
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, operations.UNINSTALL_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "DisplayVersion")
    except (ImportError, OSError):
        return None
    return str(value).strip().lstrip("vV") or None


class InstallingPage(QWidget):
    """A process under way: what it is, the bar, the step being done, and
    the list of steps with each one done, running or still to come."""

    def __init__(self, title: str = "Устанавливаю KaproTUN",
                 sub: str = f"v{__version__} · {INSTALL_DIR_TEXT}", icon: str = "download",
                 tone: str = "accent", steps: tuple = INSTALL_STEPS, stage=install_stage,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._stage = stage
        self._removing = steps is REMOVE_STEPS
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.INST_PAD_X, tokens.SP_8, tokens.INST_PAD_X, tokens.SP_6)
        col.setSpacing(tokens.SP_4)
        self.head = _Head(icon, title, sub, tone)
        col.addWidget(self.head)

        bar_box = QVBoxLayout()
        bar_box.setSpacing(tokens.SP_2)
        self.stepline = boot.StepLine()
        self.stepline.set_tone("strong")
        bar_box.addWidget(self.stepline)
        self.progress = boot.BootProgress()
        bar_box.addWidget(self.progress)
        self.detail = boot.StepLine()
        self.detail.text.setObjectName("ktStepMono")
        self.detail.setVisible(False)
        bar_box.addWidget(self.detail)
        col.addLayout(bar_box)

        self.steps = Facts()
        for text in steps:
            self.steps.add_step(text)
        col.addWidget(self.steps)
        self.note = _label("Если KaproTUN был подключён, он сначала отключится и вернёт настройки "
                           "сети как были.", "ktInstCaption", wrap=True)
        self.note.setVisible(False)
        col.addWidget(self.note)
        col.addStretch(1)
        self.set_progress("Подготовка…", 0)

    def set_progress(self, text: str, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        step_text, volume = split_volume(text)
        done = self._stage(percent)
        closing = done == 0
        # Closing a running KaproTUN takes as long as it takes: no percent.
        self.progress.set_indeterminate(closing)
        if not closing:
            self.progress.set_value(percent / 100)
        self.stepline.set(step_text, "" if closing else f"{percent} %")
        self.note.setVisible(closing and not self._removing)
        downloading = bool(volume)
        self.detail.setVisible(downloading)
        if downloading:
            host = operations.download_source()
            source = {"kaprovpn.pro": "зеркало kaprovpn.pro"}.get(host, host)
            self.detail.set(volume, source)
        meta = "сверяется с контрольной суммой" if (done == 1 and not self._removing) else ""
        self.steps.set_progress(done, meta)


class DonePage(QWidget):
    """How it ended. Success: what is now where, and "start it". Failure:
    the reason — selectable, copyable — what to try, and a way to try again."""

    close_clicked = Signal(bool)  # bool = launch app now
    retry_clicked = Signal()

    def __init__(self, success: bool = True, error: str = "", *, removed: bool = False,
                 desktop: bool = True, removing: bool = False, leftover: bool = False,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.INST_PAD_X, tokens.SP_8, tokens.INST_PAD_X, tokens.SP_6)
        col.setSpacing(tokens.SP_4)
        self.launch_check: Optional[CheckLine] = None
        self.reason: Optional[QPlainTextEdit] = None
        if success and removed:
            col.addWidget(_centered("KaproTUN удалён", "Программа, ярлыки и запись в Windows удалены",
                                    mark=True))
            facts = Facts()
            facts.add("folder", "Серверы и настройки остались — пригодятся, если поставишь снова",
                      DATA_DIR_TEXT)
            if leftover:
                # Windows lets only an administrator schedule a delete for
                # the next boot; without that the running uninstaller stays.
                facts.add("file", "Остался файл удаления — папку можно стереть вручную",
                          INSTALL_DIR_TEXT)
            else:
                facts.add("clock", "Файл удаления сотрётся после перезагрузки")
            col.addWidget(facts)
            foot = _foot(col)
        elif success:
            col.addWidget(_centered("Готово", f"KaproTUN v{__version__} установлен", mark=True))
            facts = Facts()
            facts.add("app-window", "Программа", INSTALL_DIR_TEXT)
            facts.add("monitor", "Ярлыки в меню Пуск и на Рабочем столе" if desktop
                      else "Ярлык в меню Пуск")
            facts.add("trash", "Удалить можно в «Установка и удаление программ»")
            col.addWidget(facts)
            foot = _foot(col)
            self.launch_check = CheckLine("Запустить KaproTUN", True)
            foot.addWidget(self.launch_check, 0, Qt.AlignHCenter)
        else:
            untouched = "Не удалось скачать" in error or "файл занят" in error
            col.addWidget(_centered("Не удалось удалить" if removing else "Не удалось установить",
                                    "Установленные файлы не изменены" if untouched else "",
                                    mark=False))
            col.addWidget(self._reason_box(error))
            if not removing:
                col.addWidget(_label("Проверь интернет и нажми «Повторить». Если не помогает — скачай "
                                     "KaproTUN.exe вручную со страницы релиза.", "ktInstText", wrap=True))
                self.manual_link = kit.LinkButton("Скачать вручную",
                                                  paths.release_page_url(__version__))
                col.addWidget(self.manual_link, 0, Qt.AlignLeft)
            foot = _foot(col)

        if success:
            self.close_btn = kit.Button("Закрыть", "primary", size="lg")
            self.close_btn.setFocusPolicy(Qt.StrongFocus)
            self.close_btn.clicked.connect(self._on_close)
            foot.addWidget(self.close_btn)
        else:
            pair = QHBoxLayout()
            pair.setSpacing(tokens.SP_2)
            self.close_btn = kit.Button("Закрыть", "secondary", size="lg")
            self.close_btn.clicked.connect(self._on_close)
            pair.addWidget(self.close_btn, 1)
            self.retry_btn = kit.Button("Повторить", "primary", icon="refresh", size="lg")
            self.retry_btn.setFocusPolicy(Qt.StrongFocus)
            self.retry_btn.clicked.connect(self.retry_clicked)
            pair.addWidget(self.retry_btn, 1)
            foot.addLayout(pair)

    def _reason_box(self, error: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.reason = QPlainTextEdit(error.strip())
        self.reason.setObjectName("ktReason")
        self.reason.setReadOnly(True)
        self.reason.setFrameShape(QFrame.NoFrame)
        self.reason.setMaximumHeight(tokens.REASON_H)
        lines = max(2, min(6, error.strip().count("\n") + 1))
        self.reason.setMinimumHeight(min(tokens.REASON_H, lines * 16 + 22))
        row.addWidget(self.reason, 1)
        self.copy_btn = kit.Button("", "ghost", icon="copy", size="sm", tooltip="Скопировать причину",
                                   parent=self.reason)
        self.copy_btn.clicked.connect(self._copy_reason)
        self.copy_btn.move(self.reason.width() - 36, 4)
        self.reason.installEventFilter(self)
        return box

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.reason and event.type() == event.Type.Resize:
            self.copy_btn.move(self.reason.width() - self.copy_btn.width() - 4, 4)
        return super().eventFilter(watched, event)

    def _copy_reason(self) -> None:
        QGuiApplication.clipboard().setText(self.reason.toPlainText())

    def _on_close(self) -> None:
        launch = bool(self.launch_check and self.launch_check.isChecked())
        self.close_clicked.emit(launch)


# --- main window ----------------------------------------------------------

class InstallerWindow(QMainWindow):
    """The whole installer is one frameless window with a stack of screens."""

    def __init__(self, mode: str = "install"):
        """`mode` ∈ {'install', 'maintenance', 'uninstall'}.

        - install: Welcome → Installing → Done.
        - maintenance: Reinstall / Remove choice for users who run Setup.exe
          a second time. Reinstall falls through to the install flow,
          Remove to the uninstall flow.
        - uninstall: confirm + remove (what the Uninstall button of Windows'
          "Apps" list runs, registered with --uninstall).
        """
        super().__init__()
        self._mode = mode
        self._uninstall = (mode == "uninstall")
        self._desktop = True
        self._busy = False
        title = {
            "install": "KaproTUN — Установка",
            "maintenance": "KaproTUN — Установлен",
            "uninstall": "KaproTUN — Удаление",
        }.get(mode, "KaproTUN — Установка")
        self.setWindowTitle(title)
        self.setWindowIcon(app_icons.app_icon())
        self.setFixedSize(tokens.INST_W, tokens.INST_H)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint)

        shell = QWidget()
        shell.setObjectName("appShell")
        self.setCentralWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar()
        self.titlebar.setProperty("inst", "true")
        self.titlebar.title_label.setText(title)
        self.titlebar.btn_min.setVisible(False)  # no minimize for installer
        self.titlebar.close_clicked.connect(self.close)
        root.addWidget(self.titlebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, stretch=1)

        if mode == "uninstall":
            self._build_uninstall_flow()
        elif mode == "maintenance":
            self._build_maintenance_flow()
        else:
            self._build_install_flow()

        self._worker = None  # type: ignore

    def _set_busy(self, busy: bool) -> None:
        """While files are being written or removed the window stays: no
        close button, and Alt+F4 does nothing either (closeEvent)."""
        self._busy = busy
        self.titlebar.btn_close.setEnabled(not busy)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Closing now would end the process under a running worker thread:
        # a half-written install, and Qt aborts on a thread destroyed while
        # running.
        if self._busy:
            event.ignore()
            return
        # The worker reports its result a moment before its thread ends.
        worker = self._worker
        if worker is not None:
            worker.wait(3000)
        super().closeEvent(event)

    def _set_title(self, title: str) -> None:
        self.setWindowTitle(title)
        self.titlebar.title_label.setText(title)

    def _show(self, page: QWidget) -> None:
        self.stack.addWidget(page)
        # addWidget selects a page only when the stack was empty; said
        # explicitly, or the new screen stays hidden behind the old one.
        self.stack.setCurrentWidget(page)

    # --- install flow ------------------------------------------------------

    def _build_install_flow(self) -> None:
        self.welcome = WelcomePage()
        self.welcome.install_clicked.connect(self._start_install)
        self.stack.addWidget(self.welcome)
        self.installing = InstallingPage()
        self.stack.addWidget(self.installing)
        self.stack.setCurrentWidget(self.welcome)
        self.welcome.install_btn.setFocus()

    # --- maintenance flow (Reinstall / Remove choice) ----------------------

    def _build_maintenance_flow(self) -> None:
        self.maintenance = MaintenancePage()
        self.maintenance.reinstall_clicked.connect(self._switch_to_install)
        self.maintenance.uninstall_clicked.connect(self._switch_to_uninstall)
        self.stack.addWidget(self.maintenance)
        self.stack.setCurrentWidget(self.maintenance)

    def _switch_to_install(self) -> None:
        """Maintenance → Reinstall: straight to the install, no second
        consent. create_desktop=False: the shortcut from the original
        install is still there, another one would be a duplicate."""
        self._set_title("KaproTUN — Переустановка")
        self._mode = "install"
        self._uninstall = False
        self._start_install(create_desktop=False)

    def _switch_to_uninstall(self) -> None:
        """Maintenance → Remove: the confirmation of the uninstall flow."""
        self._set_title("KaproTUN — Удаление")
        self._mode = "uninstall"
        self._uninstall = True
        self._build_uninstall_flow()

    def _start_install(self, create_desktop: bool) -> None:
        if _selftest:
            return
        self._desktop = bool(create_desktop)
        self.installing = InstallingPage(steps=install_steps(self._desktop))
        self._show(self.installing)
        self._set_busy(True)  # can't bail mid-install
        self._worker = _InstallWorker(
            version=__version__, create_desktop=create_desktop, parent=self,
        )
        self._worker.progress.connect(self.installing.set_progress)
        self._worker.finished_ok.connect(self._on_install_done)
        self._worker.failed.connect(self._on_install_failed)
        self._worker.start()

    def _on_install_done(self) -> None:
        self._set_busy(False)
        done = DonePage(success=True, desktop=self._desktop)
        done.close_clicked.connect(self._on_finish)
        self._show(done)
        done.close_btn.setFocus()

    def _on_install_failed(self, msg: str) -> None:
        self._set_busy(False)
        done = DonePage(success=False, error=msg)
        done.close_clicked.connect(self._on_finish)
        done.retry_clicked.connect(self._on_retry_install)
        self._show(done)
        done.retry_btn.setFocus()

    def _on_retry_install(self) -> None:
        self._start_install(self._desktop)

    def _on_finish(self, launch: bool) -> None:
        if launch and not self._uninstall and not _selftest and paths.installed_exe_path().is_file():
            try:
                subprocess.Popen(
                    [str(paths.installed_exe_path())],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    close_fds=True,
                )
            except OSError:
                pass        # it is installed; "Закрыть" must still close the window
        self.close()

    # --- uninstall flow ----------------------------------------------------

    def _build_uninstall_flow(self) -> None:
        confirm, col = _page()
        col.addWidget(_Head("trash", "Удалить KaproTUN?",
                            "Если программа запущена, она отключится и закроется.", "danger"))
        col.addWidget(_label("Будет удалено", "ktInstLabel"))
        goes = Facts()
        goes.add("x", "Программа", INSTALL_DIR_TEXT, "remove")
        goes.add("x", "Ярлыки в меню Пуск и на Рабочем столе", state="remove")
        goes.add("x", "Запись в «Установка и удаление программ»", state="remove")
        goes.add("x", "Автозапуск вместе с Windows", state="remove")
        col.addWidget(goes)
        col.addWidget(_label("Останется", "ktInstLabel"))
        stays = Facts()
        stays.add("folder", "Серверы, подписки, настройки, движок sing-box и журналы",
                  DATA_DIR_TEXT, "keep")
        col.addWidget(stays)
        col.addWidget(_label("Чтобы стереть всё, удали эту папку вручную после удаления.",
                             "ktInstCaption", wrap=True))
        foot = _foot(col)
        pair = QHBoxLayout()
        pair.setSpacing(tokens.SP_2)
        cancel_btn = kit.Button("Оставить", "secondary", size="lg")
        cancel_btn.setFocusPolicy(Qt.StrongFocus)
        cancel_btn.clicked.connect(self.close)
        pair.addWidget(cancel_btn, 1)
        uninst_btn = kit.Button("Удалить", "danger", size="lg")
        uninst_btn.clicked.connect(self._start_uninstall)
        pair.addWidget(uninst_btn, 1)
        foot.addLayout(pair)
        self._show(confirm)
        # The safe choice has the focus: Enter must not remove the program.
        cancel_btn.setFocus()

    def _start_uninstall(self) -> None:
        if _selftest:
            return
        self.installing = InstallingPage("Удаляю KaproTUN", INSTALL_DIR_TEXT, "trash", "neutral",
                                         REMOVE_STEPS, remove_stage)
        self._show(self.installing)
        self.installing.set_progress("Закрываю KaproTUN…", 2)
        self._set_busy(True)
        self._worker = _UninstallWorker(parent=self)
        self._worker.progress.connect(self.installing.set_progress)
        self._worker.finished_ok.connect(self._on_uninstall_done)
        self._worker.failed.connect(self._on_uninstall_failed)
        self._worker.start()

    def _on_uninstall_done(self) -> None:
        self._set_busy(False)
        done = DonePage(success=True, removed=True, leftover=operations.uninstaller_left_behind())
        done.close_clicked.connect(self._on_removed_close)
        self._show(done)
        done.close_btn.setFocus()

    def _on_uninstall_failed(self, msg: str) -> None:
        self._set_busy(False)
        done = DonePage(success=False, error=msg, removing=True)
        done.close_clicked.connect(self._on_removed_close)
        done.retry_clicked.connect(self._start_uninstall)
        self._show(done)

    def _on_removed_close(self, _launch: bool) -> None:
        self.close()


def _apply_theme(app) -> None:
    """The installer follows the system's light or dark theme (it runs before
    there are any settings to read a choice from)."""
    app.setStyleSheet(get_qss("auto"))


def run(mode: str = "install", *, uninstall: bool = False) -> int:
    """Launch the installer GUI in the given mode.

    `mode` is one of "install" / "maintenance" / "uninstall" — see
    InstallerWindow docstring. The legacy `uninstall=True` kwarg is
    kept for backwards-compat (was the only flag before v1.8.1); if
    set, it overrides `mode` to "uninstall".
    """
    if uninstall:
        mode = "uninstall"

    app = QApplication(sys.argv)
    app.setApplicationName("KaproTUN Installer")
    app.setOrganizationName("KaproTUN")
    _apply_theme(app)
    app.setWindowIcon(app_icons.app_icon())

    window = InstallerWindow(mode=mode)
    window.show()
    return app.exec()


# --- silent (auto-update) mode --------------------------------------------

class _SilentUpdateWindow(QWidget):
    """The small always-on-top card shown while the application updates
    itself: between "the old version has quit" and "the new one is up" the
    user has nothing else to look at. No title bar, no buttons."""

    PREP_MS = 1500          # the old version is given at least this long to let go of its files
    WAIT_OLD_S = 30         # ... and at most this long, before it is closed for it
    ERROR_SECONDS = 5

    gave_up = Signal()      # the countdown after a failure has run out

    def __init__(self):
        super().__init__()
        self.setWindowTitle("KaproTUN — обновление")
        self.setWindowIcon(app_icons.app_icon())
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("ktSplashWindow")
        self.setFixedSize(tokens.UPD_W, tokens.UPD_H)

        card = boot._Card(self)
        card.setGeometry(self.rect())
        col = QVBoxLayout(card)
        col.setContentsMargins(tokens.SP_5, tokens.SP_5, tokens.SP_5, tokens.SP_4)
        col.setSpacing(tokens.SP_3)
        col.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(tokens.SP_3H)
        row.addWidget(boot.Mark(tokens.MARK_MD), 0, Qt.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(tokens.SP_HALF)
        text.addStretch(1)
        title_row = QHBoxLayout()
        title_row.setSpacing(tokens.SP_2)
        self.title = _label("Обновляю KaproTUN…", "ktBootErrTitle")
        title_row.addWidget(self.title)
        self.done_icon = icons_v2.IconLabel("check-circle", tokens.ICON_SM, "success_text")
        self.done_icon.setVisible(False)
        title_row.addWidget(self.done_icon)
        title_row.addStretch(1)
        text.addLayout(title_row)
        self.stepline = boot.StepLine()
        text.addWidget(self.stepline)
        text.addStretch(1)
        row.addLayout(text, 1)
        col.addLayout(row)
        self.progress = boot.BootProgress()
        col.addWidget(self.progress)
        col.addStretch(1)

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center().x() - self.width() // 2, area.center().y() - self.height() // 2)

        self._left = 0
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._on_countdown)
        self.set_prep()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Nothing here is for the user to close (the taskbar and Alt+F4 can
        # still ask): the card goes away by itself, when the update is over.
        # Closing it would end the process under the worker thread — a
        # half-written program and no version started.
        event.ignore()

    def set_prep(self) -> None:
        self.progress.set_indeterminate(True)
        self.stepline.set("Жду, пока закроется старая версия…")

    def set_progress(self, text: str, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        self.progress.set_indeterminate(False)
        self.progress.set_value(percent / 100)
        self.stepline.set(text, f"{percent} %")

    def set_done(self) -> None:
        self.title.setText("KaproTUN обновлён")
        self.done_icon.setVisible(True)
        self.progress.set_indeterminate(False)
        self.progress.set_value(1.0)
        self.stepline.set(f"v{__version__} · запускаю…")

    def set_error(self, reason: str) -> None:
        """The first line of what went wrong, and a countdown: the card
        closes by itself, there is nothing to press."""
        first = next((line.strip() for line in str(reason).splitlines() if line.strip()), "")
        # Our own messages arrive as "RuntimeError: <sentence>:" — the
        # sentence is the reason, the rest is in the way on one short line.
        if first.startswith("RuntimeError: "):
            first = first[len("RuntimeError: "):]
        first = first.rstrip(":")
        self.title.setText("Не удалось обновить")
        if self.progress.is_indeterminate() or self.progress.value() <= 0:
            # Failed before there was any progress to stop at: a whole red
            # bar, not an empty one that looks like nothing happened.
            self.progress.set_indeterminate(False)
            self.progress.set_value(1.0, animate=False)
        self.progress.set_error(True)
        self.stepline.set_tone("error")
        self._left = self.ERROR_SECONDS
        self._reason = first
        self.stepline.set(first, f"{self._left} с")
        self._countdown.start()

    def _on_countdown(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._countdown.stop()
            self.gave_up.emit()
            return
        self.stepline.set(self._reason, f"{self._left} с")


def _start_when_released(start, *, locked, now, deadline, later) -> None:
    """Call start() once the old version has let go of its file — or at
    `deadline`, whichever comes first.

    The old version disconnects, restores the network settings and joins its
    threads before it exits; that can take well over a few seconds. Starting
    earlier makes the install force-close it in the middle of that — and the
    force-close takes the process tree, which this updater is part of."""
    if locked() and now() < deadline:
        later(lambda: _start_when_released(start, locked=locked, now=now, deadline=deadline,
                                           later=later))
        return
    start()


def _log_update_failure(reason: str) -> None:
    """Keep the whole reason of a failed update: the card shows one line of
    it for five seconds."""
    try:
        from kapro_tun.core import paths as app_paths
        folder = app_paths.app_data_dir() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(folder / "update-error.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp}  update to v{__version__} failed\n{reason}\n\n")
    except Exception:
        pass


def run_silent() -> int:
    """Install without questions — what the in-app updater starts."""
    app = QApplication(sys.argv)
    app.setApplicationName("KaproTUN Updater")
    app.setOrganizationName("KaproTUN")
    # The card refuses to be closed; the process ends when the update does.
    app.setQuitOnLastWindowClosed(False)
    _apply_theme(app)
    app.setWindowIcon(app_icons.app_icon())

    window = _SilentUpdateWindow()
    window.show()

    worker = _InstallWorker(version=__version__, create_desktop=False, parent=window)
    worker.progress.connect(window.set_progress)

    def launch_and_quit() -> None:
        exe = paths.installed_exe_path()
        if exe.is_file():
            try:
                subprocess.Popen(
                    [str(exe)],
                    creationflags=(
                        getattr(subprocess, "DETACHED_PROCESS", 0)
                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    ),
                    close_fds=True,
                )
            except OSError:
                pass
        QApplication.quit()

    def on_done() -> None:
        window.set_done()
        # Long enough to read "обновлён", then the new version takes over.
        QTimer.singleShot(700, launch_and_quit)

    def on_failed(reason: str) -> None:
        _log_update_failure(reason)
        window.set_error(reason)

    worker.finished_ok.connect(on_done)
    worker.failed.connect(on_failed)
    # A failed update must not leave the user without a client: whatever is
    # installed (the old version, untouched — or the new one, if only a later
    # step failed) is started again.
    window.gave_up.connect(launch_and_quit)
    # The application that started us is still shutting down, and Windows has
    # not yet released its file: overwriting it now would fail with a sharing
    # violation. The card is already up and says what it is waiting for.
    exe = paths.installed_exe_path()
    deadline = time.monotonic() + _SilentUpdateWindow.WAIT_OLD_S
    QTimer.singleShot(_SilentUpdateWindow.PREP_MS, lambda: _start_when_released(
        worker.start, locked=lambda: operations._exe_is_locked(exe), now=time.monotonic,
        deadline=deadline, later=lambda again: QTimer.singleShot(300, again)))
    code = app.exec()
    worker.wait(5000)
    return code


# --- self-test (for the build) ------------------------------------------------

def _settle(app) -> None:
    """Let a window that was just shown lay itself out and paint — without
    taking a key press or a click that happens to land on it."""
    for _ in range(20):
        app.processEvents(QEventLoop.ExcludeUserInputEvents)


def run_selftest(folder: str) -> int:
    """Draw every screen of this installer once and save the pictures.

        KaproTUN-Setup.exe --selftest <folder>

    Run by the release workflow on the installer it has just packaged: a
    screen that cannot be built or comes out blank in the packaged form (a
    module or a Qt plugin missing from the bundle) fails the build instead of
    reaching a user. Nothing is installed, removed, downloaded or started —
    the screens are built and filled by hand, and while this runs the
    buttons that would start an install or a removal do nothing.
    The result goes to <folder>/installer-selftest.json; exit code 0 or 1."""
    import json
    from pathlib import Path

    global _selftest
    _selftest = True

    out = Path(folder).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(get_qss("dark"))
    results: list[dict] = []

    def shoot(name: str, widget: QWidget) -> None:
        widget.setAttribute(Qt.WA_ShowWithoutActivating, True)
        widget.show()
        _settle(app)
        image = widget.grab().toImage()
        small = image.scaled(96, 96, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        colours = {small.pixel(x, y) for x in range(96) for y in range(96)}
        saved = image.save(str(out / f"{name}.png"))
        # A screen that drew has text, lines and an accent on it; a blank
        # one is a single colour.
        ok = saved and len(colours) >= 8
        results.append({"stage": name, "status": "ok" if ok else "FAIL",
                        "detail": f"{widget.width()}x{widget.height()}, {len(colours)} colours"})

    def screens() -> None:
        def page_in(window: InstallerWindow, page: QWidget) -> InstallerWindow:
            window._show(page)
            return window

        w = InstallerWindow("install")
        shoot("welcome", w)
        w.installing.set_progress("Скачиваю KaproTUN.exe… 24 / 62 МБ", 22)
        w.stack.setCurrentWidget(w.installing)
        shoot("installing", w)
        shoot("done", page_in(w, DonePage(success=True)))
        shoot("error", page_in(w, DonePage(success=False, error="RuntimeError: self-test")))
        w.hide()
        w = InstallerWindow("maintenance")
        shoot("installed", w)
        w.hide()
        w = InstallerWindow("uninstall")
        shoot("confirm", w)
        shoot("removed", page_in(w, DonePage(success=True, removed=True)))
        w.hide()
        card = _SilentUpdateWindow()
        card.set_progress("Скачиваю KaproTUN.exe… 24 / 62 МБ", 22)
        shoot("update", card)
        card.hide()

    try:
        screens()
    except Exception as e:  # the report must say what broke, not the exit code alone
        import traceback
        results.append({"stage": "screens", "status": "FAIL", "detail": f"{type(e).__name__}: {e}",
                        "trace": traceback.format_exc()[-1500:]})
    finally:
        _selftest = False
    failed = any(item["status"] != "ok" for item in results) or len(results) < 8
    try:
        (out / "installer-selftest.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        failed = True
    for item in results:
        print(f"[{item['status']}] {item['stage']}: {item['detail']}", flush=True)
    return 1 if failed else 0
