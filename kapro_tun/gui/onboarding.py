"""First-launch welcome screen — shown when the user has zero saved
configs and doesn't know what to do yet.

UX goal: from "I just installed KaproTUN, what now?" to "I'm connected"
in under 60 seconds without reading any docs.

Three big buttons covering the three realistic paths a new user is on:

  1. They have a subscription URL from a provider (most common).
     → opens the subscription import dialog directly.

  2. They have a single share-URL (vless://, trojan://, etc.) — maybe
     pasted from a Telegram bot or a friend.
     → opens the inline AddConfigPage.

  3. They have no VPN provider yet and don't know where to get one.
     → opens kaprovpn.pro in the browser — landing page already covers
     "what it does", "where to get configs", links to the GmailVPN
     partner service, and download links for every OS.

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

from ..core.i18n import tr


# Where to send users without a provider — kaprovpn.pro main page is
# already the setup guide (problem / features / how it works / downloads
# / GmailVPN partner link). Was /setup until v1.9.3, which returned 404 —
# never had separate content, never needed.
SETUP_GUIDE_URL = "https://kaprovpn.pro/"


class OnboardingPage(QWidget):
    """Empty-state Welcome page with 3 quick-start actions."""

    subscription_clicked = Signal()
    add_config_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 20)
        outer.setSpacing(14)

        # --- Hero ---
        title = QLabel(tr("onb.welcome_title"))
        title.setObjectName("h1")
        title.setAlignment(Qt.AlignHCenter)
        outer.addWidget(title)

        subtitle = QLabel(tr("onb.welcome_subtitle"))
        subtitle.setObjectName("dim")
        subtitle.setAlignment(Qt.AlignHCenter)
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        outer.addSpacing(8)

        # --- Three big action cards ---
        outer.addWidget(self._make_card(
            emoji="📥",
            title=tr("onb.card_sub_title"),
            subtitle=tr("onb.card_sub_subtitle"),
            button_text=tr("onb.card_sub_button"),
            on_click=self.subscription_clicked.emit,
            primary=True,
        ))

        outer.addWidget(self._make_card(
            emoji="📋",
            title=tr("onb.card_url_title"),
            subtitle=tr("onb.card_url_subtitle"),
            button_text=tr("onb.card_url_button"),
            on_click=self.add_config_clicked.emit,
            primary=False,
        ))

        outer.addWidget(self._make_card(
            emoji="🌐",
            title=tr("onb.card_guide_title"),
            subtitle=tr("onb.card_guide_subtitle"),
            button_text=tr("onb.card_guide_button"),
            on_click=self._on_guide_clicked,
            primary=False,
        ))

        outer.addStretch(1)

        # --- Tiny footer reassurance ---
        footer = QLabel(tr("onb.footer_privacy"))
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
        # v1.13.0: removed the hardcoded inline stylesheet (dark colours
        # baked into the widget) — now styled by the global QSS via
        # objectName, so light theme picks the right colors automatically.

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
        # v1.13.0: was hardcoded color #fafafa (only readable on dark).
        # Now uses objectName so the global QSS rule picks the active
        # palette's TEXT color (dark on light, white on dark).
        t.setObjectName("onboardTitle")
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
