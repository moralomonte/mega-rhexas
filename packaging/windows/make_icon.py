"""Rasterize the public-site triskelion into the Windows icon and Inno wizard images.

The source is ``site/img/triskelion.svg``, the mark megarhexas.com shows in the
header. Needs ``rsvg-convert`` and Pillow. Installers and the tray ship the
files this writes; they are not regenerated on every build.

Icon sizes are drawn from the same path with the header glow removed, each at
its own pixel size, so 16 and 32 stay sharp. The wizard images keep the glow
and sit on the site background (``#05060f``).
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

# Inno Setup keeps WizardImageFile at 164:314. Three times the classic
# 164×314 frame is an exact match and larger than the 200% DPI area (430×824).
WIZARD_IMAGE_SIZE = (164 * 3, 314 * 3)  # 492 × 942
# WizardSmallImageFile is square. 159 is the 250% DPI slot, so Setup does not scale it up.
WIZARD_SMALL_SIZE = 159
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
# Fraction of the icon left empty on each side so Windows does not clip the arms.
ICON_PAD = 0.10

_BG = (5, 6, 15)  # site --bg, #05060f
_PURPLE = (26, 16, 64)  # site header glow, #1a1040
_BLUE = (12, 28, 56)  # site header glow, #0c1c38

_ROOT = Path(__file__).resolve().parents[2]
_SVG = _ROOT / "site" / "img" / "triskelion.svg"
_ICO = _ROOT / "packaging" / "assets" / "triskelion.ico"
_WIZARD = _ROOT / "packaging" / "assets" / "wizard-image.bmp"
_SMALL = _ROOT / "packaging" / "assets" / "wizard-small.bmp"
_PACKAGE_ICO = _ROOT / "src" / "rhexas" / "desktop" / "icons" / "triskelion.ico"


def _render_svg(svg: str, size: int) -> Image.Image:
    with tempfile.NamedTemporaryFile(suffix=".png") as handle:
        subprocess.run(
            ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", handle.name],
            input=svg.encode("utf-8"),
            check=True,
        )
        with Image.open(handle.name) as image:
            return image.convert("RGBA")


def _icon_svg(source: str, pad: float = ICON_PAD) -> str:
    """The site path, without the header blur, inset inside the square."""
    text = re.sub(r"<filter\b[^>]*>.*?</filter>", "", source, count=1, flags=re.S)
    text = text.replace(' filter="url(#gl)"', "")
    scale = 1 / (1 - 2 * pad)
    side = 721 * scale
    extra = (side - 721) / 2
    origin = -20 - extra
    return re.sub(
        r'viewBox="[^"]+"',
        f'viewBox="{origin:.4f} {origin:.4f} {side:.4f} {side:.4f}"',
        text,
        count=1,
    )


def _mix(base: tuple[int, int, int], tint: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(int(base[i] + (tint[i] - base[i]) * amount) for i in range(3))


def site_background(width: int, height: int) -> Image.Image:
    """Dark field in the site's colors. The bottom corners stay ``#05060f``."""
    image = Image.new("RGB", (width, height), _BG)
    pixels = image.load()
    # Ellipses in the same places as the site header glows, scaled to this frame.
    spots = (
        (0.92, -0.02, 0.95, 0.42, _PURPLE),
        (-0.05, 0.18, 0.70, 0.32, _BLUE),
    )
    for y in range(height):
        fy = y / height
        for x in range(width):
            fx = x / width
            color = _BG
            for cx, cy, rx, ry, tint in spots:
                dx = (fx - cx) / rx
                dy = (fy - cy) / ry
                distance = (dx * dx + dy * dy) ** 0.5
                if distance < 1:
                    color = _mix(color, tint, (1 - distance) ** 2 * 0.9)
            pixels[x, y] = color
    return image


def _place_mark(background: Image.Image, mark: Image.Image, *, center_y: float) -> Image.Image:
    canvas = background.copy()
    x = (canvas.width - mark.width) // 2
    y = int(canvas.height * center_y) - mark.height // 2
    y = max(0, min(y, canvas.height - mark.height))
    canvas.paste(mark, (x, y), mark)
    return canvas


def write_icon(source: str, dest: Path) -> None:
    svg = _icon_svg(source)
    frames = [_render_svg(svg, size) for size in ICON_SIZES]
    by_size = {frame.size: frame for frame in frames}
    largest = by_size[max(by_size)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    largest.save(
        dest,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
        append_images=[by_size[(size, size)] for size in ICON_SIZES if (size, size) != largest.size],
    )


def write_wizard_images(source: str, wizard: Path, small: Path) -> None:
    width, height = WIZARD_IMAGE_SIZE
    mark = _render_svg(source, int(width * 0.78))
    panel = _place_mark(site_background(width, height), mark, center_y=0.46)
    wizard.parent.mkdir(parents=True, exist_ok=True)
    panel.save(wizard, format="BMP")

    small_mark = _render_svg(source, int(WIZARD_SMALL_SIZE * 0.78))
    tile = _place_mark(site_background(WIZARD_SMALL_SIZE, WIZARD_SMALL_SIZE), small_mark, center_y=0.5)
    tile.save(small, format="BMP")


def main() -> None:
    source = _SVG.read_text(encoding="utf-8")
    write_icon(source, _ICO)
    _PACKAGE_ICO.parent.mkdir(parents=True, exist_ok=True)
    _PACKAGE_ICO.write_bytes(_ICO.read_bytes())
    write_wizard_images(source, _WIZARD, _SMALL)
    print(_ICO, _ICO.stat().st_size)
    print(_WIZARD, _WIZARD.stat().st_size)
    print(_SMALL, _SMALL.stat().st_size)


if __name__ == "__main__":
    main()
