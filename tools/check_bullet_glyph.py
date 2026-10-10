#!/usr/bin/env python
"""子弹时间（倍速键上的「三个三角 >>>」）判据的体检。

用户 2026-10-10 的原话是「这 3 个图标一直都在」：子弹时间下倍速键上**不画数字**，
画的是**三个三角**（亮度峰值 234~239，而画数字时是纯白 255）。老判据「在箭头带
（ROI 行 70..115）里找实心白块」扫不到它 —— 三角最亮的那块在**行 40..81**，只有
最下面十来行压进带里：同一套阈值在一段录像里靠场景亮块顶出 1277/1277 命中、
在另一段里 0/583 命中（见 docs 第 32 节）。
现在改认**形状**：features._glyph_score 拿这一小块跟 templates/bullet.npy 求最大
相关系数，在几档缩放 + 横纵平移里搜（两段录像里三角宽度差了 1.28 倍）。

这个检查钉三件事：
    A. tools/fixtures/*.png —— 从**两段真录像**里裁出来的倍速键 ROI（7 张，~100KB），
       逐张断言「三角 -> bullet」「有数字 -> 不是 bullet」「淡入 -> 不是 bullet」，
       并打印形状分的两类分布（中间要留出余量）；
    B. 形状分缓存（features._glyph_tpl_flat）算出来的必须和现算的一模一样 ——
       它是本轮为了把这条判据从 1.9ms 压到 0.3ms 加的；
    C. 两段录像还在本机的话整段逐帧跑一遍，钉住命中率（a 1186 / b 583）和
       「有数字的帧一个都不判三角」（a 717 / b 865）。录像不在就跳过。

为什么形状分不是「第二个判据」：实测数字帧的形状分能到 +0.549（a）/ +0.647（b），
跟真三角（a 稳定段 >= +0.597 / b >= +0.686）是**重叠**的 —— 分得开靠的是先问
「这一帧有没有数字」那道门，形状分只在**读不到数字**时才用。fixtures 里的
speed_b_digit 就是这一条：它单独看形状分（+0.647）是过阈值的。

用法：python tools/check_bullet_glyph.py
"""

import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.capture import iter_cached, open_video                      # noqa: E402
from akframe.cli import load_templates                                    # noqa: E402
from akframe.features import (GLYPH_NCC_MIN, _glyph_norm,                 # noqa: E402
                              _glyph_norm_flat, _glyph_score, _glyph_templates,
                              _glyph_tpl_flat, _half2, _resize_nearest, read_speed)
from akframe.geometry import Rect, layout_for, reframe_layout             # noqa: E402

FAIL = []
FIXTURES = os.path.join(ROOT, 'tools', 'fixtures')
TEMPLATES = os.path.join(ROOT, 'templates')

# 夹具是从 2560x1600 桌面录像（客户区 2560x1440 落在画面 (0,54)）里裁的，
# 所以布局要按同一套几何算 —— 对不上就说明夹具跟代码不再配套。
FRAME = (2560, 1600)
CLIENT = (2560, 1440)
OFFSET = (0, 54)
SPEED_ROI = Rect(2082, 94, 199, 159)

# (文件名, 类别, 说明)；类别 tri = 三个三角，digit = 有数字，none = 都没有
CASES = [
    ('speed_a_tri', 'tri', 'a t=24.03 三个三角（a 稳定段里分最低的）'),
    ('speed_b_tri', 'tri', 'b t=10.04 三个三角（老判据在这一段 0/583）'),
    ('speed_a_digit_1x', 'digit', 'a t=20.00 1X 数字'),
    ('speed_a_digit_2x', 'digit', 'a t=31.66 2X 数字'),
    ('speed_a_digit_2x_worst', 'digit', 'a t=31.51 2X 数字（数字帧里分最高的）'),
    ('speed_b_digit', 'digit', 'b t=6.27 有数字（分过阈值，靠数字门拦住）'),
    ('speed_a_fade', 'none', 'a t=6.56 进关卡按钮淡入（负类里分最高的）'),
]

# 整段录像的真值：三角帧数 / 有数字帧数 / 三角帧里最低的形状分（2026-10-10 实测）
VIDEO_DIRS = [os.path.join(os.path.expanduser('~'), 'Videos', 'NVIDIA', 'Desktop')]
VIDEOS = [
    ('Desktop 2026.10.09 - 22.36.10.14.mp4', 'a 负费 + 子弹时间', 'a', 1186, 717, 0.59),
    ('Desktop 2026.10.06 - 18.32.40.02.mp4', 'b 满费 + 子弹时间', 'b', 583, 865, 0.68),
]


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def fixture(name):
    from PIL import Image
    return np.asarray(Image.open(os.path.join(FIXTURES, name + '.png'))).astype(np.float32)


def part_a(tpl, shape_tpl, lay):
    """夹具：三角 -> bullet；有数字 / 淡入 -> 不是 bullet。"""
    print('倍速键 ROI 夹具（tools/fixtures，真录像里裁的）：speed %s，布局 %s'
          % (SPEED_ROI, CLIENT))
    print()
    print('%-6s %-24s %8s %-7s %-7s %-5s %s' % ('类别', '名字', '形状分', 'bullet',
                                                '有数字', 'm', '说明'))
    got = {'tri': [], 'digit': [], 'none': []}
    for name, kind, note in CASES:
        roi = fixture(name)
        if roi.shape != (SPEED_ROI.h, SPEED_ROI.w):
            check('夹具尺寸 / ' + name, False,
                  '%s != %s —— 夹具跟 SPEED_ROI 不配套了' % (roi.shape, (SPEED_ROI.h, SPEED_ROI.w)))
            continue
        sp = read_speed(roi, lay, tpl)
        g = _glyph_score(roi, shape_tpl)
        got[kind].append((name, g))
        print('%-6s %-24s %+8.3f %-7s %-7s %-5s %s'
              % (kind, name, g, sp.bullet, sp.has_digit,
                 '—' if sp.m != sp.m else '%.0fX' % sp.m, note))
        if kind == 'tri':
            check('三角 -> bullet / ' + name,
                  bool(sp.bullet) and g >= GLYPH_NCC_MIN + 0.10,
                  'bullet=%s 形状分 %+.3f（阈值 %.2f，要求 >= %.2f）'
                  % (sp.bullet, g, GLYPH_NCC_MIN, GLYPH_NCC_MIN + 0.10))
        elif kind == 'digit':
            check('有数字 -> 不是 bullet / ' + name,
                  bool(sp.has_digit) and not sp.bullet,
                  'has_digit=%s bullet=%s（形状分 %+.3f；这一帧单独看形状分可以过阈值，'
                  '靠「有没有数字」那道门拦住）' % (sp.has_digit, sp.bullet, g))
        else:
            # drawn 这里不断言：drawn 是「箭头带有实心块」（判关卡内外的），跟形状无关，
            # 这一帧恰好过线。这条判据只管 bullet。
            check('淡入 -> 不是 bullet / ' + name,
                  (not sp.has_digit) and (not sp.bullet),
                  'has_digit=%s bullet=%s drawn=%s 形状分 %+.3f'
                  % (sp.has_digit, sp.bullet, sp.drawn, g))
    if got['tri'] and got['none']:
        # 注意余量只跟「也没数字、也判不出三角」这一类比：那才是形状分真正在定生死的
        # 地方。有数字的帧形状分跟三角是**重叠**的（下面单独打一行），靠数字门分开。
        lo = min(g for _n, g in got['tri'])
        hi = max(g for _n, g in got['none'])
        check('形状分定生死的两类之间留出余量', lo - hi >= 0.10,
              '三角最低 %+.3f − 无数字无三角最高 %+.3f = %.3f（要求 >= 0.10；阈值 %.2f 夹在中间）'
              % (lo, hi, lo - hi, GLYPH_NCC_MIN))
    if got['digit']:
        gs = [g for _n, g in got['digit']]
        print('      有数字的帧形状分 %+.3f..%+.3f —— 跟三角重叠，先被「有没有数字」'
              '那道门拦下（不参与上面那条余量）' % (min(gs), max(gs)))
    nan_case = _glyph_score(fixture('speed_a_tri'), ())
    check('没有形状模板时形状分是 nan', nan_case != nan_case,
          '_glyph_score(roi, ()) = %s' % nan_case)
    print()


def part_b(shape_tpl):
    """缓存里的模板归一化结果必须和现算的一模一样。"""
    tpl = shape_tpl[0]
    base = _half2(np.asarray(tpl, dtype=np.float32))
    base = _glyph_norm(base)
    for th, tw in ((35, 80), (40, 92), (51, 116)):
        arr = base if base.shape == (th, tw) else _resize_nearest(base, th, tw)
        fresh = _glyph_norm_flat(arr.reshape(1, -1))[0]
        cached = _glyph_tpl_flat(tpl, th, tw)
        check('缓存 == 现算 (%d,%d)' % (th, tw),
              cached is not None and np.array_equal(cached, fresh),
              '%d 维最大差 %.2e' % (len(fresh), float(np.abs(cached - fresh).max())))
    print()


def part_c(tpl, lay_ref):
    """整段录像（本机上才有）：命中率 + 零误报。"""
    for name, note, tag, n_tri, n_digit, min_g in VIDEOS:
        path = None
        for d in VIDEO_DIRS:
            if os.path.isfile(os.path.join(d, name)):
                path = os.path.join(d, name)
                break
        if path is None:
            print('skip 整段实测·%s：本机找不到录像 %s' % (note, name))
            continue
        cache = os.path.join(ROOT, 'out', 'cache', 'check_bullet_' + tag)
        lay, _fps, it = open_video(path, cache, client_size=CLIENT, offset=OFFSET)
        if lay.speed != lay_ref.speed:
            print('skip 整段实测·%s：这份录像的倍速键 ROI 是 %s，跟夹具的 %s 不同'
                  '（换了分辨率 / UI 缩放就别拿老数字比）' % (note, lay.speed, SPEED_ROI))
            continue
        rows = []
        for fr in it:
            sp = read_speed(fr.speed, lay, tpl)
            rows.append((fr.index, sp.has_digit, sp.bullet,
                         _glyph_score(fr.speed, _glyph_templates(tpl))))
        tri = [r for r in rows if r[2]]
        digit = [r for r in rows if r[1]]
        bad = [r[0] for r in rows if r[1] and r[2]]
        gs = [r[3] for r in tri]
        print('     %s：三角 %d 帧、有数字 %d 帧、形状分最低 %+.3f（最高 %+.3f）'
              % (note, len(tri), len(digit), min(gs) if gs else float('nan'),
                 max(gs) if gs else float('nan')))
        check('整段实测·三角帧数 / %s' % tag, abs(len(tri) - n_tri) <= 2,
              '%d 帧（真值 %d）' % (len(tri), n_tri))
        check('整段实测·有数字帧数 / %s' % tag, abs(len(digit) - n_digit) <= 2,
              '%d 帧（真值 %d）' % (len(digit), n_digit))
        check('整段实测·有数字的帧一个都不判三角 / %s' % tag, not bad,
              '误报 %d 帧 %s' % (len(bad), bad[:6]))
        check('整段实测·三角帧里最低的形状分 / %s' % tag, gs and min(gs) >= min_g - 0.01,
              'min %+.3f（真值 >= %+.2f）' % (min(gs), min_g))


def main() -> int:
    tpl = load_templates(TEMPLATES)
    shape_tpl = _glyph_templates(tpl)
    lay = reframe_layout(layout_for(CLIENT[0], CLIENT[1]), OFFSET[0], OFFSET[1],
                         FRAME[0], FRAME[1])
    print('形状模板：%s（%d 个）  阈值 GLYPH_NCC_MIN = %.2f'
          % (os.path.basename(os.path.join(TEMPLATES, 'bullet.npy')), len(shape_tpl),
             GLYPH_NCC_MIN))
    check('形状模板加载到了', len(shape_tpl) == 1 and lay.speed == SPEED_ROI,
          '%d 个；离线布局的倍速键 ROI = %s' % (len(shape_tpl), lay.speed))
    print()
    if not os.path.isdir(FIXTURES):
        print('FAIL 找不到夹具目录 %s' % FIXTURES)
        FAIL.append('夹具目录')
    else:
        try:
            part_a(tpl, shape_tpl, lay)
        except ImportError:
            print('skip A 组：没装 Pillow（夹具是 PNG，读不了）')
    part_b(shape_tpl)
    part_c(tpl, lay)
    if FAIL:
        print('失败项：%s' % '、'.join(FAIL))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
