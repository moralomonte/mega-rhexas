"""Write packaging/assets/triskelion.ico. Stdlib only, so the installer icon needs no Pillow."""
from __future__ import annotations

import struct
from pathlib import Path


def _pixel(x: int, y: int, size: int) -> tuple[int, int, int, int]:
    cx = cy = (size - 1) / 2
    dx, dy = x - cx, y - cy
    r2 = dx * dx + dy * dy
    if r2 > (size * 0.48) ** 2:
        return (8, 10, 28, 0)
    # Three arms, 120 degrees apart, in the dashboard cyan / indigo / violet.
    import math
    angle = math.atan2(dy, dx)
    arm = int((angle + math.pi) / (2 * math.pi / 3)) % 3
    colors = ((165, 243, 252, 255), (129, 140, 248, 255), (192, 132, 252, 255))
    radius = math.sqrt(r2)
    spoke = abs(((angle + math.pi) % (2 * math.pi / 3)) - (math.pi / 3))
    on_arm = radius < size * 0.42 and spoke < 0.45 and radius > size * 0.08
    on_hub = radius < size * 0.12
    if on_hub:
        return (255, 255, 255, 255)
    if on_arm:
        return colors[arm]
    return (8, 10, 28, 255)


def _dib(size: int) -> bytes:
    header = struct.pack(
        "<IiiHHIIiiII",
        40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0,
    )
    xor_rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            red, green, blue, alpha = _pixel(x, y, size)
            row += bytes((blue, green, red, alpha))
        xor_rows.append(bytes(row))
    mask_row = b"\x00" * ((size + 31) // 32 * 4)
    mask = mask_row * size
    return header + b"".join(xor_rows) + mask


def write_ico(path: Path, sizes: tuple[int, ...] = (16, 32, 48)) -> None:
    images = [_dib(size) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    blobs = b""
    for size, blob in zip(sizes, images):
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32,
            len(blob), offset,
        )
        offset += len(blob)
        blobs += blob
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + blobs)


if __name__ == "__main__":
    dest = Path(__file__).resolve().parents[1] / "assets" / "triskelion.ico"
    write_ico(dest)
    print(dest, dest.stat().st_size)
