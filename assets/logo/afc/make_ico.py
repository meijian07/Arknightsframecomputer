#!/usr/bin/env python
"""重新生成 afc.ico。

为什么要单独做小尺寸：任务栏在 150% DPI 下取 24/32px。把 512 的完整标志
（取景框 + 30 刻度 + 播放头）直接缩到 16~32px，所有细节会糊成一团。
所以 16~48px 用一套像素级简化标：深色圆角方块 + 粗 A + 橙色播放头，
不放取景框和刻度；64px 以上继续用完整 SVG。

用法：
    python assets/logo/afc/make_ico.py

依赖：Pillow；64+ 用 cairosvg（没有就回退到 preview/afc-icon-1024.png）。
"""

from __future__ import annotations

import io
import os
import struct

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, 'afc-icon.svg')
ICO = os.path.join(HERE, 'afc.ico')
PREVIEW = os.path.join(HERE, 'preview')

BG = '#1B1D21'
WHITE = '#F7F8FA'
ORANGE = '#FF9F1C'
BORDER = '#2B2F36'
SMALL_SIZES = (16, 20, 24, 28, 32, 36, 40, 48)
DETAILED_SIZES = (64, 128, 256)


def small_icon(size: int) -> Image.Image:
    """像素级简化标：粗 A + 橙色播放头。"""
    s = int(size)
    im = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    radius = max(2, int(round(s * 0.22)))
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BG,
                        outline=(BORDER if s >= 20 else None), width=1)

    line = max(1, int(round(s / 16.0)))
    cx = s / 2.0
    x0 = int(round(cx - line / 2.0))
    x1 = x0 + line - 1
    d.rectangle([x0, int(round(s * 0.12)), x1, int(round(s * 0.88))],
                fill=ORANGE)

    # 16x16 网格，按尺寸取整；小尺寸不要小数描边，避免抗锯齿糊边。
    outer = [(2, 14), (8, 2), (14, 14), (10.8, 14),
             (9.3, 10.5), (6.7, 10.5), (5.2, 14)]
    hole = [(8, 6.1), (7.0, 8.6), (9.0, 8.6)]

    def scale(p):
        return (int(round(p[0] * s / 16.0)), int(round(p[1] * s / 16.0)))

    d.polygon([scale(p) for p in outer], fill=WHITE)
    d.polygon([scale(p) for p in hole], fill=BG)
    return im


def detailed_icon(size: int) -> Image.Image:
    """64px 以上用完整 SVG；没有 cairosvg 时回退到 1024 预览图。"""
    try:
        import cairosvg
        png = cairosvg.svg2png(url=SVG, output_width=size, output_height=size)
        return Image.open(io.BytesIO(png)).convert('RGBA')
    except Exception:
        src = os.path.join(PREVIEW, 'afc-icon-1024.png')
        im = Image.open(src).convert('RGBA')
        return im.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: str, frames) -> None:
    """写多尺寸 PNG-ICO（每档用各自的图，不让 Pillow 从 256 再缩一遍）。"""
    blobs = []
    for size, im in frames:
        buf = io.BytesIO()
        im.save(buf, format='PNG')
        blobs.append((size, buf.getvalue()))
    count = len(blobs)
    header = struct.pack('<HHH', 0, 1, count)
    offset = 6 + 16 * count
    entries = []
    payload = b''
    for size, data in blobs:
        dim = 0 if size >= 256 else size
        entries.append(struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32,
                                   len(data), offset))
        offset += len(data)
        payload += data
    with open(path, 'wb') as fh:
        fh.write(header + b''.join(entries) + payload)


def contact_sheet(frames) -> Image.Image:
    """小尺寸预览：上排放大、下排 1:1，方便看任务栏清晰度。"""
    pad = 10
    cell = 140
    sheet = Image.new('RGB', (len(frames) * cell + pad, 210), '#202020')
    d = ImageDraw.Draw(sheet)
    for i, (size, im) in enumerate(frames):
        big = im.resize((120, 120), Image.Resampling.NEAREST)
        sheet.paste(big, (pad + i * cell, 10), big)
        sheet.paste(im, (pad + i * cell + 60 - size // 2, 145), im)
        d.text((pad + i * cell, 170), str(size), fill='#DDDDDD')
    return sheet


def main() -> int:
    os.makedirs(PREVIEW, exist_ok=True)
    small = [(s, small_icon(s)) for s in SMALL_SIZES]
    detailed = [(s, detailed_icon(s)) for s in DETAILED_SIZES]
    frames = small + detailed
    write_ico(ICO, frames)
    contact_sheet(small).save(os.path.join(PREVIEW, 'afc-icon-small-sizes.png'))
    print('wrote', ICO, [(s, im.size) for s, im in frames])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
