"""First-launch welcome screen — shown when the user has zero saved
configs and doesn't know what to do yet.

UX goal: from "I just installed KaproVPN, what now?" to "I'm connected"
in under 60 seconds without reading any docs.

Three big buttons covering the three realistic paths a new user is on:

  1. They have a subscription URL from a provider (most common).
     → opens the subscription import dialog directly.

  2. They have a single share-URL (vless://, trojan://, etc.) — maybe
     pasted from a Telegram bot or a friend.
     → opens the inline AddConfigPage.

  3. They have no VPN provider yet and don't know where to get one.
     → opens kaprovpn.pro/setup in the browser, where we list
     vetted providers + setup walkthroughs.

The page only ever shows up when configs.json is empty. The moment the
user adds their first config, MainWindow switches to the Home page and
this widget is never seen again (until they delete every config).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n import current_locale


# Where to send users without a provider — we own this URL, can put
# whatever "how to get started" content makes sense.
SETUP_GUIDE_URL = "https://kaprovpn.pro/setup"


class OnboardingPage(QWidget):
    """Empty-state Welcome page with 3 quick-start actions."""

    subscription_clicked = Signal()
    add_config_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        ru = current_locale() == "ru"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 20)
        outer.setSpacing(14)

        # --- Hero ---
        title = QLabel("Добро пожаловать в KaproVPN" if ru else "Welcome to KaproVPN")
        title.setObjectName("h1")
        title.setAlignment(Qt.AlignHCenter)
        outer.addWidget(title)

        subtitle = QLabel(
            "Чтобы начать — добавь свой первый сервер.\n"
            "Это занимает 10 секунд."
            if ru else
            "To get started — add your first server.\n"
            "Takes about 10 seconds."
        )
        subtitle.setObjectName("dim")
        subtitle.setAlignment(Qt.AlignHCenter)
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        outer.addSpacing(8)

        # --- Three big action cards ---
        outer.addWidget(self._make_card(
            emoji="📥",
            title="У меня есть URL подписки" if ru else "I have a subscription URL",
            subtitle=(
                "Ссылка вида https://provider.com/sub/xxx — обычно её "
                "выдают провайдеры с несколькими серверами разом."
                if ru else
                "A URL like https://provider.com/sub/xxx — typically "
                "issued by providers with multiple servers to import at once."
            ),
            button_text="Импорт по подписке" if ru else "Import subscription",
            on_click=self.subscription_clicked.emit,
            primary=True,
        ))

        outer.addWidget(self._make_card(
            emoji="📋",
            title="У меня есть share-URL одного сервера" if ru else "I have a single share URL",
            subtitle=(
                "Строки вида vless://… / trojan://… / vmess://… / ss://… — "
                "часто рассылают TG-боты VPN-провайдеров."
                if ru else
                "Strings like vless://… / trojan://… / vmess://… / ss://… — "
                "commonly sent by provider bots in Telegram."
            ),
            button_text="Вставить URL" if ru else "Paste a URL",
            on_click=self.add_config_clicked.emit,
            primary=False,
        ))

        outer.addWidget(self._make_card(
            emoji="🌐",
            title="У меня ещё нет VPN-провайдера" if ru else "I don't have a VPN provider yet",
            subtitle=(
                "Откроем нашу страницу с подборкой проверенных провайдеров "
                "и краткой инструкцией."
                if ru else
                "Opens our guide with a curated list of providers and a "
                "short setup walkthrough."
            ),
            button_text="Открыть гайд" if ru else "Open the guide",
            on_click=self._on_guide_clicked,
            primary=False,
        ))

        outer.addStretch(1)

        # --- Tiny footer reassurance ---
        footer = QLabel(
            "Конфиги хранятся локально на твоём компьютере. "
            "Мы не видим что ты добавляешь и куда подключаешься."
            if ru else
            "Configs are stored locally on your machine. "
            "We don't see what you add or where you connect."
        )
        footer.setObjectName("dim")
        footer.setAlignment(Qt.AlignHCenter)
        footer.setWordWrap(True)
        outer.addWidget(footer)

    def _make_card(
        self,
        *,
        emoji: str,
        title: str,
        subtitle: str,
        button_text: str,
        on_click,
        primary: bool,
    ) -> QFrame:
        """A flat card with emoji + title + dim subtitle + action button."""
        card = QFrame()
        card.setObjectName("onboardCard")
        card.setStyleSheet(
            "QFrame#onboardCard {"
            "  background: #18181b;"
            "  border: 1px solid #27272a;"
            "  border-radius: 10px;"
            "}"
        )

        body = QVBoxLayout(card)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(6)

        # Top row: emoji + title side by side
        head = QHBoxLayout()
        head.setSpacing(10)
        emo = QLabel(emoji)
        emo_font = QFont()
        emo_font.setPointSize(20)
        emo.setFont(emo_font)
        head.addWidget(emo, alignment=Qt.AlignTop)

        head_text = QVBoxLayout()
        head_text.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("color: #fafafa; font-weight: 600; font-size: 11pt;")
        t.setWordWrap(True)
        head_text.addWidget(t)
        s = QLabel(subtitle)
        s.setObjectName("dim")
        s.setWordWrap(True)
        head_text.addWidget(s)
        head.addLayout(head_text, stretch=1)
        body.addLayout(head)

        # Action button — right-aligned so the eye still parses
        # title → button as a natural flow.
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        btn = QPushButton(button_text)
        if primary:
            btn.setObjectName("primary")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setMinimumWidth(160)
        btn.clicked.connect(on_click)
        action_row.addWidget(btn)
        body.addLayout(action_row)

        return card

    def _on_guide_clicked(self) -> None:
        QDesktopServices.openUrl(QUrl(SETUP_GUIDE_URL))
