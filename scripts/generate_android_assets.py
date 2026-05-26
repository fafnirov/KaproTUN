"""
Generate Android resource assets from the masters in `art/`.

Usage:
    python scripts/generate_android_assets.py

What it produces (overwrites in-place):

  android/app/src/main/res/
    mipmap-{m,h,xh,xxh,xxxh}dpi/
        ic_launcher.png            - legacy square launcher icon
        ic_launcher_round.png      - same, round mask
        ic_launcher_foreground.png - adaptive-icon foreground (safe-zone scaled)
        ic_launcher_monochrome.png - Android 13+ themed icon (alpha mask)
    drawable-nodpi/
        hero.png                   - empty-state illustration
        tile_idle.png              - Quick Settings tile state: idle
        tile_connecting.png        -                            connecting
        tile_connected.png         -                            connected
    drawable-{m,h,xh,xxh,xxxh}dpi/
        ic_stat_notification.png   - notification small-icon, monochrome silhouette
        tile_idle.png + variants   - tile icons need a density when used by code

Why the script
--------------
We don't have Android Studio Asset Studio on the CLI. Pillow does what
we need (resize + alpha + simple silhouette extraction) and the script
is reproducible from the masters in `art/` if any of them is updated.

Pixel sizes follow Android adaptive-icon guidelines:
    https://developer.android.com/training/multiscreen/screendensities
    https://developer.android.com/develop/ui/views/launch/icon_design_adaptive

Adaptive foreground canvas == 108dp. Visible content must fit inside the
inner 72dp circle (safe zone) -> content is scaled to 66.6%% of canvas.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image, ImageOps, ImageChops

# Masters in `art/` were exported in RGB with a near-white background instead
# of a transparent one. We chroma-key those pixels into alpha so the orange
# linework reads correctly on the app's dark theme and inside the adaptive
# launcher icon. splash-source.png is already dark, so we exclude it.
_WHITE_BG_FILES = {
    "icon-source.png",
    "tray_idle-source.png",
    "tray_connecting-source.png",
    "tray_connected-source.png",
    "hero.png",
    "done.png",
}

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "art"
RES = ROOT / "android" / "app" / "src" / "main" / "res"

# Density buckets and the pixel sizes for adaptive-icon foreground canvas
# (each canvas = 108dp). Legacy launcher icon is 48dp (m), 72dp (h), etc.
DENSITIES = {
    "mdpi":    (108, 48),
    "hdpi":    (162, 72),
    "xhdpi":   (216, 96),
    "xxhdpi":  (324, 144),
    "xxxhdpi": (432, 192),
}
# Inside the adaptive canvas, content must stay inside the 72dp safe zone
# (66.6% of canvas). 0.66 is the value Android Studio Image Asset uses.
SAFE_ZONE_RATIO = 0.66


def _open(name: str) -> Image.Image:
    path = ART / name
    if not path.exists():
        sys.exit(f"missing master: {path}")
    img = Image.open(path).convert("RGBA")
    if name in _WHITE_BG_FILES:
        img = _key_white_to_alpha(img)
    return img


def _key_white_to_alpha(img: Image.Image,
                         hi: int = 240, lo: int = 200) -> Image.Image:
    """Replace the near-white background with transparency.

    Min(R,G,B) per pixel: >= `hi` -> fully transparent, <= `lo` -> fully
    opaque, in between -> linear edge for soft anti-aliasing on glyph
    borders. Conservative defaults so we don't accidentally erase the
    light-grey strokes of the idle tray K.
    """
    img = img.convert("RGBA")
    r, g, b, _ = img.split()
    # darker(a, b) returns the per-pixel minimum; chaining two darkers
    # gives min(R, G, B).
    min_chan = ImageChops.darker(ImageChops.darker(r, g), b)
    span = float(hi - lo) or 1.0
    alpha = min_chan.point(
        lambda p: 0 if p >= hi
        else 255 if p <= lo
        else int(round((hi - p) * 255 / span))
    )
    img.putalpha(alpha)
    return img


def _save(img: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG", optimize=True)
    print(f"  -> {out.relative_to(ROOT)} ({img.size[0]}x{img.size[1]})")


def _resize(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.LANCZOS)


def _fit_into_canvas(content: Image.Image, canvas_size: int,
                      content_ratio: float) -> Image.Image:
    """Place `content` centered into a transparent canvas of `canvas_size`,
    scaled so its longest side equals `canvas_size * content_ratio`."""
    target = int(round(canvas_size * content_ratio))
    # preserve aspect ratio (content is usually 1:1 but be safe)
    content_resized = ImageOps.contain(content, (target, target), Image.LANCZOS)
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    x = (canvas_size - content_resized.width) // 2
    y = (canvas_size - content_resized.height) // 2
    canvas.paste(content_resized, (x, y), content_resized)
    return canvas


def _to_alpha_silhouette(img: Image.Image, fill: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """Drop colours; keep only the alpha channel painted with `fill`.
    Used for notification small-icon (Android tints) and for themed-icon
    monochrome layer (Android masks)."""
    alpha = img.split()[-1]
    silhouette = Image.new("RGBA", img.size, (0, 0, 0, 0))
    coloured = Image.new("RGBA", img.size, fill + (255,))
    silhouette.paste(coloured, mask=alpha)
    return silhouette


# ── launcher icon (legacy + adaptive + themed) ──────────────────────────────

def launcher_icons() -> None:
    print("launcher icons:")
    src = _open("icon-source.png")

    # 1. legacy ic_launcher / ic_launcher_round
    for dpi, (_canvas_px, legacy_px) in DENSITIES.items():
        out_dir = RES / f"mipmap-{dpi}"
        legacy = _resize(src, legacy_px)
        _save(legacy, out_dir / "ic_launcher.png")
        _save(legacy, out_dir / "ic_launcher_round.png")

    # 2. adaptive foreground: content shrunk to safe zone on transparent canvas
    for dpi, (canvas_px, _legacy_px) in DENSITIES.items():
        fg = _fit_into_canvas(src, canvas_px, SAFE_ZONE_RATIO)
        _save(fg, RES / f"mipmap-{dpi}" / "ic_launcher_foreground.png")

    # 3. Android 13+ themed icon — alpha mask, system tints it for the user's
    #    chosen palette. Same canvas/safe-zone as foreground.
    silhouette = _to_alpha_silhouette(src)
    for dpi, (canvas_px, _legacy_px) in DENSITIES.items():
        mono = _fit_into_canvas(silhouette, canvas_px, SAFE_ZONE_RATIO)
        _save(mono, RES / f"mipmap-{dpi}" / "ic_launcher_monochrome.png")


# ── notification small-icon ─────────────────────────────────────────────────

def notification_icon() -> None:
    """Status-bar/notification small-icon must be a single-colour silhouette;
    Android applies the tint. We use the tray_idle master (already a clean K
    silhouette) and strip its colour."""
    print("notification small icon:")
    src = _open("tray_idle-source.png")
    silhouette = _to_alpha_silhouette(src)

    # Status-bar small icons are 24dp; allow a tiny margin so the K isn't
    # cropped to the rounded corners on some devices.
    sizes = {
        "mdpi":    24,
        "hdpi":    36,
        "xhdpi":   48,
        "xxhdpi":  72,
        "xxxhdpi": 96,
    }
    for dpi, px in sizes.items():
        icon = _fit_into_canvas(silhouette, px, 1.0)
        _save(icon, RES / f"drawable-{dpi}" / "ic_stat_notification.png")


# ── Quick Settings tile icons ────────────────────────────────────────────────

def tile_icons() -> None:
    """Tile uses Icon.createWithResource(R.drawable.tile_*) — we produce
    three drawables matching the three connection states. Tile-icons on
    Android are tinted by the system as well, so we strip colour and let
    Quick Settings render them in its own palette (greyed for inactive,
    accent for active). We still keep three files for explicitness even
    though after the silhouette pass they end up identical — Android-side
    we'd otherwise be picking by code state anyway."""
    print("Quick Settings tile icons:")
    masters = {
        "tile_idle":       "tray_idle-source.png",
        "tile_connecting": "tray_connecting-source.png",
        "tile_connected":  "tray_connected-source.png",
    }
    sizes = {
        "mdpi":    24,
        "hdpi":    36,
        "xhdpi":   48,
        "xxhdpi":  72,
        "xxxhdpi": 96,
    }
    for resname, master in masters.items():
        src = _open(master)
        silhouette = _to_alpha_silhouette(src)
        for dpi, px in sizes.items():
            icon = _fit_into_canvas(silhouette, px, 1.0)
            _save(icon, RES / f"drawable-{dpi}" / f"{resname}.png")


# ── hero illustration for empty states ──────────────────────────────────────

def hero_illustration() -> None:
    print("hero illustration:")
    src = _open("hero.png")
    # Hero is wide (3:1-ish); we keep it full-bleed and let Compose size it.
    out = RES / "drawable-nodpi" / "hero_split_routing.png"
    _save(src, out)


def main() -> None:
    if not ART.exists():
        sys.exit(f"art directory not found: {ART}")
    print(f"masters at: {ART}")
    print(f"writing to: {RES}\n")

    launcher_icons()
    print()
    notification_icon()
    print()
    tile_icons()
    print()
    hero_illustration()
    print("\ndone.")


if __name__ == "__main__":
    main()
