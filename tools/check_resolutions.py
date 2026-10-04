#!/usr/bin/env python
"""常见分辨率的布局体检。

layout_for() 对任何尺寸都会给出一套 ROI，但只有在 16:9 和已实测的尺寸上才敢信。
这个脚本把 COMMON_RESOLUTIONS 全部过一遍、做完整体检再打印，用来回答
「换个分辨率还能不能识别」。

用法：
    python tools/check_resolutions.py
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from akframe.geometry import (  # noqa: E402
    COMMON_RESOLUTIONS, MIN_CLIENT_H, MIN_CLIENT_W, layout_for, resolution_status)

MIN_BAR_W = 60          # 条槽再窄，1px 读数抖动就等于好几个逻辑帧
MIN_PX_PER_FRAME = 2.0  # 每个逻辑帧至少占这么多列，否则条动一格就是一大步


def _overlap(a, b) -> bool:
    return a.x <= b.x2 and b.x <= a.x2 and a.y <= b.y2 and b.y <= a.y2


def check(width: int, height: int) -> tuple:
    """返回 (layout, 失败项列表)。"""
    lay = layout_for(width, height)
    bad = []
    if lay.bar.w < MIN_BAR_W:
        bad.append('条槽 %dpx < %d' % (lay.bar.w, MIN_BAR_W))
    if lay.px_per_frame < MIN_PX_PER_FRAME:
        bad.append('每逻辑帧 %.2fpx < %.1f' % (lay.px_per_frame, MIN_PX_PER_FRAME))
    for name in ('pause', 'speed'):
        r = getattr(lay, name)
        if r.w < 32 or r.h < 24:
            bad.append('%s ROI 只有 %dx%d' % (name, r.w, r.h))
    if _overlap(lay.pause, lay.speed):
        bad.append('暂停键和倍速键 ROI 叠在一起')
    if _overlap(lay.bar, lay.speed):
        bad.append('费用条和倍速键 ROI 叠在一起')
    if width < MIN_CLIENT_W or height < MIN_CLIENT_H:
        bad.append('小于建议下限 %dx%d' % (MIN_CLIENT_W, MIN_CLIENT_H))
    return lay, bad


def main() -> int:
    head = ('分辨率', '比', 's', '条槽', '每帧px', '费用条ROI', '暂停键ROI', '状态')
    print('%-10s %-5s %-7s %-6s %-7s %-16s %-18s %-9s %s' % (
        head[0], head[1], head[2], head[3], head[4], head[5], head[6], head[7], '说明'))
    bad_total = 0
    for w, h, label in COMMON_RESOLUTIONS:
        lay, bad = check(w, h)
        status, why = resolution_status(w, h)
        bad_total += len(bad)
        print('%-10s %-5s %-7s %-6d %-7.3f %-16s %-18s %-9s %s' % (
            '%dx%d' % (w, h), label, '%.4f' % lay.scale, lay.bar.w, lay.px_per_frame,
            'x%d y%d %dx%d' % (lay.bar.x, lay.bar.y, lay.bar.w, lay.bar.h),
            'x%d y%d %dx%d' % (lay.pause.x, lay.pause.y, lay.pause.w, lay.pause.h),
            status, why))
        for b in bad:
            print('%38s !! %s' % ('', b))
    print()
    print('失败项合计：%d' % bad_total)
    return 1 if bad_total else 0


if __name__ == '__main__':
    sys.exit(main())
