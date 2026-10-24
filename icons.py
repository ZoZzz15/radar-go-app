"""Dessine les icônes de l'appli Radar GO (docs/icon-192.png et docs/icon-512.png), sans bibliothèque."""
import math
import os
import struct
import zlib

BG, RING, DOT, SWEEP = (11, 24, 32), (79, 179, 217), (242, 182, 50), (31, 122, 158)


def pixel(x, y, n):
    c = n / 2
    dx, dy = x - c, y - c
    r = math.hypot(dx, dy) / (n * 0.36)  # rayon du radar ~ 72 % de l'icône
    if r > 1.04:
        return BG
    if r > 0.96:
        return RING
    if r < 0.09:
        return DOT
    for ring in (0.33, 0.66):
        if abs(r - ring) < 0.02:
            return RING
    angle = (math.degrees(math.atan2(dy, dx)) + 360) % 360
    if 270 <= angle <= 330:  # balayage du radar
        t = (angle - 270) / 60
        return tuple(int(BG[i] + (SWEEP[i] - BG[i]) * t) for i in range(3))
    return BG


def png(path, n):
    rows = b"".join(b"\x00" + bytes(v for x in range(n) for v in pixel(x, y, n)) for y in range(n))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)


if __name__ == "__main__":
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    for size in (192, 512):
        target = os.path.join(here, f"icon-{size}.png")
        if not os.path.exists(target):
            png(target, size)
