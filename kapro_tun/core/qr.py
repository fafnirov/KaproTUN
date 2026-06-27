"""Optional QR-code decoding for subscription / config import.

Decoding a QR image needs a barcode reader (zxing-cpp) + an image loader
(Pillow). Those are intentionally OPTIONAL — the lean default bundle may not
ship them, and the text-paste import path always works without them. So every
function here imports the decoder lazily and degrades gracefully: a missing
decoder or an unreadable / QR-less image yields None, never an exception.

Use `decoder_available()` to decide whether to even offer the QR button, and
`decode_qr_image(path)` to pull the text payload out of a saved image file.
"""
from __future__ import annotations

from typing import Optional


def decoder_available() -> bool:
    """True only if both the barcode reader and an image loader are importable
    in this build. The UI uses this to show an honest 'not bundled' message
    instead of a button that silently does nothing."""
    try:
        import zxingcpp  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def _read_pil(img) -> Optional[str]:
    """Decode the first non-empty QR payload from a PIL image. None on any
    failure (no decoder, no code, unsupported image mode)."""
    try:
        import zxingcpp
    except Exception:
        return None
    try:
        for res in zxingcpp.read_barcodes(img):
            text = getattr(res, "text", "") or ""
            if text.strip():
                return text.strip()
    except Exception:
        return None
    return None


def decode_qr_image(path: str) -> Optional[str]:
    """Return the text payload of the first QR code in the image at `path`,
    or None if there's no decoder, the file can't be opened, or it has no QR.
    Never raises."""
    try:
        from PIL import Image
        img = Image.open(path)
    except Exception:
        return None
    return _read_pil(img)
