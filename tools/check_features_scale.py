#!/usr/bin/env python
"""模板缩放 / ROI 复用的自检（2026-10-18，参考 MaaFramework / MAA 的两条工程做法）。

A. 面积平均缩放（features._resize_area）
   数字模板在 1080p / 720p 下要缩小才能跟条带比。最近邻是「抽点」，会跳过细笔画；
   面积平均按覆盖比例摊亮度。这里把 tools/fixtures 的真帧缩到 0.75 / 0.5 倍，
   钉住「面积平均的相关系数至少比最近邻高 0.05」。

B. RoiMemo（像素没变就复用）
   同一块 ROI 逐像素相同 -> 分类函数不再调用，直接用上一拍结果；
   有任何像素变化 -> 必须重新算；clear() 之后也必须重算。

用法：python tools/check_features_scale.py
"""

import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import akframe.features as F                                             # noqa: E402
from akframe.cli import load_templates                                   # noqa: E402
from akframe.geometry import Layout, Rect                                # noqa: E402
from akframe.live import LiveMonitor                                     # noqa: E402

FIXTURES = os.path.join(ROOT, 'tools', 'fixtures')
TEMPLATES = os.path.join(ROOT, 'templates')

FAILED = []


def check(name, ok, detail=''):
    if not isinstance(detail, str):
        detail = str(detail)
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAILED.append(name)


def fixture(name, scale=1.0):
    img = Image.open(os.path.join(FIXTURES, name + '.png')).convert('L')
    w = max(8, int(round(img.width * scale)))
    h = max(8, int(round(img.height * scale)))
    return np.asarray(img.resize((w, h), Image.LANCZOS)).astype(np.float32)


def fake_layout(w, h):
    """speed ROI = 整张图；read_speed 只看 speed 那几个 band。"""
    return Layout(width=w, height=h, scale=1.0,
                  bar=Rect(0, 0, 10, 10), pause=Rect(0, 0, 10, 10),
                  speed=Rect(0, 0, w, h), sign=Rect(0, 0, 10, 10),
                  buttons_visible=True)


def read_both(roi, tpls):
    """同一张图分别用「面积平均」和「最近邻」读一遍（清缓存，避免串味）。"""
    F._TPL_SHAPE_CACHE.clear()
    area = F.read_speed(roi, fake_layout(roi.shape[1], roi.shape[0]), tpls)
    real = F._resize_area
    try:
        F._resize_area = F._resize_nearest
        F._TPL_SHAPE_CACHE.clear()
        near = F.read_speed(roi, fake_layout(roi.shape[1], roi.shape[0]), tpls)
    finally:
        F._resize_area = real
        F._TPL_SHAPE_CACHE.clear()
    return area, near


def part_a(tpls):
    print('A. 面积平均缩放（数字模板）')
    check('常数图缩放之后还是常数',
          np.allclose(F._resize_area(np.full((7, 9), 42.0, np.float32), 3, 4), 42.0))
    check('放大也能用（输出尺寸对）',
          F._resize_area(np.zeros((5, 6), np.float32), 11, 13).shape == (11, 13))
    check('参数非法时返回 None（不抛异常）',
          F._resize_area(np.zeros((5, 6), np.float32), 0, 4) is None)

    cases = [('speed_a_digit_1x', 1.0), ('speed_a_digit_2x', 2.0),
             ('speed_a_digit_2x_worst', 2.0)]
    for k in (1.0, 0.75, 0.5):
        for name, want_m in cases:
            roi = fixture(name, k)
            area, near = read_both(roi, tpls)
            gain = area.corr_2x - near.corr_2x
            same_m = area.m == near.m == want_m
            need = -1e-6 if k == 1.0 else 0.05
            check('%s @ %.2fx：面积平均不比最近邻差（%+.3f）' % (name, k, gain),
                  gain >= need and same_m,
                  'area %+.3f / nearest %+.3f / m=%s' % (area.corr_2x, near.corr_2x, area.m))

    check('缓存：同一个模板 + 同一个尺寸只缩一次（返回同一个对象）',
          F._tpl_for_shape(tpls[0][2], 20, 30) is F._tpl_for_shape(tpls[0][2], 20, 30))
    check('缓存：尺寸变了会重新缩（尺寸对）',
          F._tpl_for_shape(tpls[0][2], 21, 31).shape == (21, 31))


def part_b():
    print()
    print('B. RoiMemo（像素没变就复用）')
    calls = []

    def classify(img, tag):
        calls.append(tag)
        return int(img.sum())

    memo = F.RoiMemo()
    a = np.zeros((8, 8), np.float32)
    v1 = memo.read(a, classify, 'a')
    v2 = memo.read(a.copy(), classify, 'a')      # 内容相同、不是同一个对象
    check('同一张图读两次：分类函数只调一次', calls == ['a'], calls)
    check('复用的是上一拍的结论', v1 == v2 == 0, (v1, v2))

    b = a.copy()
    b[2, 3] = 1.0
    v3 = memo.read(b, classify, 'b')
    check('只要变了一个像素就必须重算', calls == ['a', 'b'] and v3 == 1, calls)

    memo.clear()
    memo.read(b, classify, 'c')
    check('clear() 之后下一次必须重算', calls == ['a', 'b', 'c'], calls)

    memo.read(np.zeros((5, 5), np.float32), classify, 'd')
    check('尺寸变了也必须重算', calls == ['a', 'b', 'c', 'd'], calls)

    lay = fake_layout(320, 180)
    mon = LiveMonitor(layout=lay, exe='__no_such_window__')
    check('LiveMonitor 里挂上了暂停 / 倍速两个 memo',
          isinstance(mon._pause_memo, F.RoiMemo) and isinstance(mon._speed_memo, F.RoiMemo))
    check('LiveMonitor 的两个 memo 一开始都是空的',
          mon._pause_memo._img is None and mon._speed_memo._img is None)


def part_c():
    """部署 / 拖拽遮罩：灰底 + 左端白条 = MAX_COVER，不是满费。"""
    print()
    print('C. 部署遮罩与暗态暂停键')
    W = 120
    lay = Layout(width=W, height=17, scale=1.0,
                 bar=Rect(0, 0, W, 17), pause=Rect(0, 0, 199, 159),
                 speed=Rect(0, 0, 199, 159), sign=Rect(0, 0, 10, 10),
                 buttons_visible=True)

    def read(row):
        roi = np.tile(np.asarray(row, np.int16), (17, 1))
        return F.read_bar(roi, lay)

    # 实测 2026-10-25 13.20.51：部署网格把 80px 之后压成 146 灰，
    # 左端白条是 154（HUD 被压暗后的白）。
    row = np.full(W, 146, np.int16)
    row[:80] = 154
    r = read(row)
    check('部署遮罩：左端白条 + 右侧 146 灰 -> MAX_COVER（fill=80）',
          r.state == 'MAX_COVER' and r.fill == 80,
          'state=%s fill=%d grey146=%d' % (r.state, r.fill, r.grey146))

    # 条 ROI 里只要别处有一块纯白，max 就是 255；154 的白条仍要认出来。
    row = np.full(W, 146, np.int16)
    row[:80] = 154
    row[100] = 255
    r = read(row)
    check('遮罩白条 154 + 别处纯白 255：仍能数出左端 80px',
          r.state == 'MAX_COVER' and r.fill == 80,
          'state=%s fill=%d grey146=%d' % (r.state, r.fill, r.grey146))

    # 真满费：整条 146 灰，白色 MAX 字在中段，左端没有白条。
    row = np.full(W, 146, np.int16)
    row[50:70] = 255
    r = read(row)
    check('真满费：左端没有白条 -> MAX（不能被 MAX 字骗成遮罩）',
          r.state == 'MAX' and r.fill == 0,
          'state=%s fill=%d grey146=%d' % (r.state, r.fill, r.grey146))

    row = np.full(W, 55, np.int16)
    row[:80] = 255
    r = read(row)
    check('正常推进仍是 TRACK', r.state == 'TRACK' and r.fill == 80,
          'state=%s fill=%d' % (r.state, r.fill))

    # 压暗 HUD 的整条白（154）不能被 146±10 误判成满费。
    row = np.full(W, 154, np.int16)
    r = read(row)
    check('整条压暗白仍是 TRACK（不是 MAX）',
          r.state == 'TRACK' and r.fill == W,
          'state=%s fill=%d' % (r.state, r.fill))

    # 压暗白条 + 暗轨道（154 / 55）：这是正常的暗态进度，必须走像素通道，
    # 不能判成遮罩后冻结（否则面板会等到恢复亮度才一次跳过去）。
    row = np.full(W, 55, np.int16)
    row[:80] = 154
    r = read(row)
    check('压暗白条 154 + 暗轨道仍是 TRACK（显示要连续走）',
          r.state == 'TRACK' and r.fill == 80,
          'state=%s fill=%d core=%d' % (r.state, r.fill, r.grey146_core))

    # 暗态暂停键：双竖条本体只有 ~80，ROI 里一个反光点 193。
    # 阈值必须跟着 99 分位走，不能被单个亮点抬到 86。
    roi = np.full((159, 199), 29, np.uint8)
    roi[40:100, 75:105] = 80
    roi[40:100, 120:150] = 80
    roi[20, 20] = 193
    p = F.read_pause(roi, lay)
    check('暗态暂停键：单个反光点不影响阈值，双竖条判运行中',
          p.running and p.drawn and F.rel_threshold(roi, robust=True) < 80,
          'ratio=%.3f solid=%.3f thr=%.1f' % (p.ratio, p.solid,
                                              F.rel_threshold(roi, robust=True)))


def main():
    tpls = load_templates(TEMPLATES)
    print('模板：%s' % [(v, n, a.shape) for v, n, a in tpls])
    part_a(tpls)
    part_b()
    part_c()
    print()
    if FAILED:
        print('%d 项没过：%s' % (len(FAILED), FAILED))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
