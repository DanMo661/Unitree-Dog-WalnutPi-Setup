# -*- coding: utf-8 -*-
"""生成 GO2控制台 App 图标: 192x192 深蓝渐变圆底 + 白色狗爪印, 纯手写 PNG 无依赖"""
import struct
import zlib
import pathlib

W = H = 192
CX = CY = 96.0
R = 94.0

# 爪印几何: 大肉垫 + 四个趾垫 (椭圆)
PAD = (96.0, 120.0, 32.0, 25.0)          # 大肉垫 cx, cy, rx, ry
TOES = [                                   # 趾垫 cx, cy, rx, ry (扇形排开)
    (54.0, 82.0, 11.0, 15.0),
    (75.0, 62.0, 11.0, 16.0),
    (117.0, 62.0, 11.0, 16.0),
    (138.0, 82.0, 11.0, 15.0),
]

def in_ellipse(x, y, cx, cy, rx, ry):
    return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0

def px(x, y):
    if (x - CX) ** 2 + (y - CY) ** 2 > R * R:
        return (0, 0, 0, 0)
    t = (x + y) / (W + H)
    r = int(0x3B + (0x1E - 0x3B) * t)
    g = int(0x82 + (0x3A - 0x82) * t)
    b = int(0xF6 + (0x8A - 0xF6) * t)
    white = in_ellipse(x, y, *PAD)
    if not white:
        for toe in TOES:
            if in_ellipse(x, y, *toe):
                white = True
                break
    return (255, 255, 255, 255) if white else (r, g, b, 255)

rows = []
for y in range(H):
    rows.append(b"\x00" + bytes(v for x in range(W) for v in px(x + 0.5, y + 0.5)))

def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
       + chunk(b"IEND", b""))

out = pathlib.Path(__file__).parent / "res" / "drawable-nodpi" / "ic_paw.png"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(png)
print("图标已生成:", out, len(png), "bytes")
