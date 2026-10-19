#!/usr/bin/env python
"""ROI 边界自检：人工校准偏移再大，也不能让布局越界 / 启动崩溃。

用户 2026-10-18 实测：手动校准把框挪得太多后程序闪退，重启终端也打不开。
根因是旧分辨率（2558x1440）存下的 bar dx=+10，换到 1920x1080 后把费用条
推到 x2=1927 > 1919，apply_deltas 里的 validate() 直接抛异常；坏校准文件又会在
下次启动时被自动读回来，于是每次都崩。

覆盖：
  1. 2558x1440 的旧校准套到 1920x1080，必须能应用且全部 ROI 在画面内；
  2. 三个 ROI 各自 ±1000000 的偏移都能夹回画面内；
  3. 宽高被改成极大 / 极小时也合法；
  4. 常见分辨率全跑一遍极大偏移，不允许出现越界异常。

用法：python tools/check_layout_bounds.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.geometry import (COMMON_RESOLUTIONS, apply_deltas,  # noqa: E402
                              fit_rect, layout_for, Rect)

FAIL = []


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def inside(lay, name):
    r = getattr(lay, name)
    return (r.x >= 0 and r.y >= 0 and r.w >= 1 and r.h >= 1
            and r.x2 < lay.width and r.y2 < lay.height)


def all_inside(lay):
    return all(inside(lay, n) for n in ('bar', 'pause', 'speed', 'sign'))


def main():
    # 1) 用户机器上真实存在的那份坏校准：2558x1440 的 +10，套到 1920x1080
    base = layout_for(1920, 1080, 1.0)
    old = {'bar': (10, 0, 0, 0), 'pause': (0, 0, 0, 0), 'speed': (0, 0, 0, 0)}
    try:
        lay = apply_deltas(base, old, 1.3278)
        lay.validate()
        check('旧 2558x1440 校准套到 1920x1080', all_inside(lay), str(lay.bar))
    except ValueError as exc:
        check('旧 2558x1440 校准套到 1920x1080', False, str(exc))

    # 2) 三个 ROI 各自极大偏移
    for name in ('bar', 'pause', 'speed'):
        ok = True
        detail = ''
        for value in (1000000, -1000000):
            try:
                lay = apply_deltas(base, {name: (value, value, 0, 0)}, None)
                lay.validate()
                ok = ok and all_inside(lay)
                detail = '%s=%s' % (name, getattr(lay, name))
            except ValueError as exc:
                ok = False
                detail = str(exc)
        check('极大偏移夹取 %s' % name, ok, detail)

    # 3) 宽高极端值
    ok = True
    detail = ''
    for value in (1000000, -1000000):
        try:
            lay = apply_deltas(base, {'bar': (0, 0, value, value)}, None)
            lay.validate()
            ok = ok and all_inside(lay) and lay.bar.w >= 4 and lay.bar.h >= 4
            detail = 'bar=%s' % (lay.bar,)
        except ValueError as exc:
            ok = False
            detail = str(exc)
    check('极大宽高夹取', ok, detail)

    # 4) 常见分辨率 + 极大偏移
    ok = True
    detail = ''
    huge = {'bar': (1000000, -1000000, 1000000, -1000000),
            'pause': (-1000000, 1000000, -1000000, 1000000),
            'speed': (1000000, 1000000, -1000000, -1000000)}
    for width, height, _tag in COMMON_RESOLUTIONS:
        try:
            lay = apply_deltas(layout_for(width, height, 1.0), huge, None)
            lay.validate()
            if not all_inside(lay):
                ok = False
                detail = '%dx%d 越界' % (width, height)
        except ValueError as exc:
            ok = False
            detail = '%dx%d: %s' % (width, height, exc)
    check('常见分辨率极大偏移', ok, detail)

    # 5) fit_rect 本身：优先保宽高，装不下才缩
    r = fit_rect(Rect(5000, -50, 180, 17), 1920, 1080)
    check('fit_rect 平移保宽高', r == Rect(1740, 0, 180, 17), str(r))
    r = fit_rect(Rect(0, 0, 5000, 5000), 1920, 1080)
    check('fit_rect 装不下才缩', r == Rect(0, 0, 1920, 1080), str(r))

    print()
    if FAIL:
        print('FAILED: %d 项' % len(FAIL))
        return 1
    print('ALL OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
