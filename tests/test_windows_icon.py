"""Windows icon and Inno Setup wizard images, drawn from the public-site triskelion."""
import io
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ICO = ROOT / "packaging" / "assets" / "triskelion.ico"
PACKAGE_ICO = ROOT / "src" / "rhexas" / "desktop" / "icons" / "triskelion.ico"
WIZARD = ROOT / "packaging" / "assets" / "wizard-image.bmp"
SMALL = ROOT / "packaging" / "assets" / "wizard-small.bmp"
# Site gradient midpoint, #818cf8, at the hub of site/img/triskelion.svg.
_INDIGO = (129, 140, 248, 255)
# Inno Setup 6 keeps this aspect. 492×942 is 3× the classic 164×314 frame.
_WIZARD_SIZE = (164 * 3, 314 * 3)
# Largest documented WizardSmallImageFile slot (250% DPI). Square.
_SMALL_SIZE = 159


def _ico_entries(data: bytes) -> list[tuple[int, int, int]]:
    assert data[:4] == b"\x00\x00\x01\x00"
    count = struct.unpack_from("<H", data, 4)[0]
    entries = []
    for index in range(count):
        width, height, _colors, _reserved, _planes, _bits, nbytes, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * index
        )
        entries.append((width or 256, height or 256, offset))
        assert nbytes > 0
        assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
    return entries


def _bmp_size(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    assert data[:2] == b"BM"
    header = int.from_bytes(data[14:18], "little")
    assert header >= 40
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    bpp = int.from_bytes(data[28:30], "little")
    # Bottom-up 24-bit rows start at the bottom-left pixel, stored BGR.
    pixel = 14 + header
    assert data[pixel:pixel + 3] == bytes((0x0F, 0x06, 0x05))
    return width, abs(height), bpp


def test_icon_file_has_every_windows_size_and_matches_the_package():
    assert ICO.is_file() and PACKAGE_ICO.is_file()
    blob = ICO.read_bytes()
    assert blob == PACKAGE_ICO.read_bytes()
    sizes = [size for size, _height, _offset in _ico_entries(blob)]
    assert sizes == [16, 24, 32, 48, 64, 128, 256]


def test_wizard_bitmaps_match_inno_sizes_and_the_site_background():
    assert _bmp_size(WIZARD) == (*_WIZARD_SIZE, 24)
    assert _WIZARD_SIZE[0] * 314 == _WIZARD_SIZE[1] * 164
    assert _bmp_size(SMALL) == (_SMALL_SIZE, _SMALL_SIZE, 24)
    iss = (ROOT / "packaging" / "windows" / "mega-rhexas.iss").read_text(encoding="utf-8")
    assert "WizardImageFile=..\\assets\\wizard-image.bmp" in iss
    assert "WizardSmallImageFile=..\\assets\\wizard-small.bmp" in iss
    assert "WizardImageBackColor=$0F0605" in iss
    assert "WizardSmallImageBackColor=$0F0605" in iss
    assert "SetupIconFile=..\\assets\\triskelion.ico" in iss
    assert "IconFilename: \"{app}\\triskelion.ico\"" in iss


def _frames():
    Image = pytest.importorskip("PIL.Image")
    blob = ICO.read_bytes()
    frames = {}
    for width, height, _offset in _ico_entries(blob):
        image = Image.open(io.BytesIO(blob))
        image.size = (width, height)
        image.load()
        frames[width] = image.convert("RGBA")
    return frames


def test_tray_sizes_are_the_site_mark_and_crisper_than_a_downscale():
    frames = _frames()
    for size, inset in ((16, 2), (32, 4)):
        image = frames[size]
        left, top, right, bottom = image.getbbox()
        assert left >= inset and top >= inset
        assert right <= size - inset and bottom <= size - inset
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((size - 1, size - 1))[3] == 0
        assert image.getpixel((size // 2, size // 2)) == _INDIGO
    Image = pytest.importorskip("PIL.Image")
    blurred = frames[256].resize((16, 16), Image.Resampling.LANCZOS)
    assert blurred.getbbox()[0] < frames[16].getbbox()[0]
    assert blurred.tobytes() != frames[16].tobytes()


def test_tray_image_saves_the_native_sixteen_and_thirty_two():
    Image = pytest.importorskip("PIL.Image")
    from rhexas.desktop.tray import triskelion_image

    image = triskelion_image()
    assert image is not None
    assert image.size == (32, 32)
    assert image.getpixel((16, 16)) == _INDIGO
    handle = io.BytesIO()
    image.save(handle, format="ICO")
    assert handle.getvalue() == ICO.read_bytes()
    saved = Image.open(io.BytesIO(handle.getvalue()))
    assert {(16, 16), (32, 32)} <= set(saved.info["sizes"])
