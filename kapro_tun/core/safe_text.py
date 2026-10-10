"""Text from outside the app, made safe to put into a Qt rich-text label.

Server names, hosts, provider notes, error messages and "support" links all
come from a subscription, a share link or a remote service. Qt labels render
HTML whenever a string looks like HTML, so such text shown as-is is markup the
sender controls: `<img src="//host/share/x.png">` is fetched (on Windows a UNC
path is a network path), and `<a href="file://...">` is one click from running
something.

Two rules cover it:
  * a label that only shows external text is set to Qt.PlainText;
  * external text placed INSIDE our own markup goes through esc(), and an
    external URL becomes a link only through link().
"""
from __future__ import annotations

import html
from urllib.parse import urlsplit


def esc(value: object) -> str:
    """`value` as literal text inside rich text or an attribute."""
    return html.escape(str(value), quote=True)


def http_url(url: object) -> str:
    """`url` if it is a plain http(s) web address, else "".

    Only these two schemes are ever opened from a link: file://, UNC paths and
    custom URI handlers (ms-*, javascript:, …) hand control to whatever the
    sender picked. Quotes, whitespace and control characters are refused too —
    they are how a value breaks out of an href attribute.
    """
    text = str(url or "").strip()
    if not text or any(ch in text for ch in "'\"<>`\\") \
            or any(ch.isspace() or not ch.isprintable() for ch in text):
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    return text


def link(url: object, style: str = "", text: str = "") -> str:
    """A clickable anchor for a safe web address; otherwise the address as
    plain escaped text, so the user still sees what the provider sent.
    `text` (ours, trusted) replaces the address as the visible caption."""
    safe = http_url(url)
    if not safe:
        return esc(text or url)
    attr = f" style='{esc(style)}'" if style else ""
    return f"<a href='{esc(safe)}'{attr}>{esc(text or safe)}</a>"


def no_markup(value: object) -> str:
    """`value` with its angle brackets swapped for look-alikes.

    For the places where the text format cannot be forced to plain — a static
    QMessageBox decides by sniffing the string. Without `<` nothing in it can
    be a tag. Ordinary names and messages contain none and pass unchanged."""
    return str(value).replace("<", "\u2039").replace(">", "\u203a")
