#!/usr/bin/env python
"""顶部「逐帧退格」条自检：读得准不准 + 会不会把场景当成条。

只有少数关卡在 HUD 下面画这条细横条，**退一格 = 1 个逻辑帧**，满费那段用它数帧
（算法与依据见 docs/费用条读取逻辑.md 第 16 节）。它只有一个稳的特征：填充段右边
紧挨着就是暗轨道，那一步能差 89~165 个灰阶（场景里的亮缝最陡处只有 18）。

拿一段实测录像当基准（数值上一切精确可算）：全程 2 倍速 + 子弹时间，游戏被反复
暂停 / 继续，条正好在「退一格、停一段、再退一格」之间来回。验三件事：

    1. 25 个退格点全部对上，且一次只退一格（不许跳格）；
    2. 不该报的地方不许报：条出现之前（f < 82）与条退完消失之后（f = 1757）必须读不到；
    3. 走完整状态机后「顶部条计入的帧数」= 最大格数 - 1。

缓存 out/cache/<录像名>_tb_<指纹>/ 是抽流的一次性产物（topbar.raw + meta.json），
目录被 .gitignore 掉了、不会跟着仓库走 —— 没有缓存就跳过（会打印跳过原因）。

用法：python tools/check_topbar.py
"""

import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.capture import iter_cached, load_meta            # noqa: E402
from akframe.geometry import Layout                           # noqa: E402
from akframe.features import read_bar, read_pause, read_speed, read_topbar   # noqa: E402
from akframe.state import Counter                            # noqa: E402

# 基准：哪一帧退到第几格。这 25 个数不是程序跑出来的，是用另一套完全不同的算法
# （在 x>=900 里找最陡的那一步）在 raw 上独立数出来的，两边逐点一致才算通过。
# 只比 read_topbar 自己跑两遍是没有意义的自证。
TRUTH = [(82, 1), (216, 2), (285, 3), (335, 4), (381, 5), (501, 6), (638, 7), (720, 8),
         (783, 9), (794, 10), (950, 11), (1033, 12), (1091, 13), (1145, 14), (1197, 15),
         (1246, 16), (1300, 17), (1310, 18), (1351, 19), (1389, 20), (1429, 21),
         (1469, 22), (1479, 23), (1536, 24), (1579, 25)]
BAR_FIRST = TRUTH[0][0]        # 条第一次被画出来
BAR_LAST = 1620                # 条退完之后只剩场景，往后任何读数都是假报


# 基准录像：2026-10-05 21:43 那段（25 个退格点就是从这里数出来的）
REF_VIDEO = 'Desktop 2026.10.05 - 21.43.13.03'


def find_cache():
    """找基准录像的抽流缓存。没有就返回 None（跳过，不报错）。"""
    pats = sorted(glob.glob(os.path.join(ROOT, 'out', 'cache', '*' + REF_VIDEO + '*tb*')))
    for d in pats:
        if os.path.isfile(os.path.join(d, 'topbar.raw')):
            return d
    return None


def layout_of(cache):
    return Layout.from_dict(load_meta(cache)['layout'])


def scan(cache, lay):
    """逐帧读条，返回 (退格点列表, 假报列表, 最大格数)。"""
    marks, bogus, seen = [], [], None
    for fr in iter_cached(cache):
        r = read_topbar(fr.topbar, lay)
        if r.present:
            if fr.index < BAR_FIRST or fr.index > BAR_LAST:
                bogus.append((fr.index, r.cells))
            if r.cells != seen:
                seen = r.cells
                if fr.index <= BAR_LAST:
                    marks.append((fr.index, r.cells))
    return marks, bogus, (seen if seen is not None else 0)


def run_state_machine(cache):
    """走一遍状态机，返回 (顶部条计入的帧数, 累计逻辑帧, 最大格数)。"""
    lay = layout_of(cache)
    c = Counter(lay.model_bar_w, fps=60.0, bar_width_frac=lay.bar_width_f,
                visible_w=lay.bar.w, frames_per_cost=30.0)
    topbar_frames = 0
    cells_max = 0
    for fr in iter_cached(cache):
        bar = read_bar(fr.bar, lay)
        pause = read_pause(fr.pause, lay)
        speed = read_speed(fr.speed, lay, ())
        tb = read_topbar(fr.topbar, lay)
        res = c.step(fr.index, fr.t, bar, pause, speed, topbar=tb)
        if res.mode.startswith('topbar'):
            topbar_frames += res.delta
            cells_max = max(cells_max, res.topbar_cells)
    return topbar_frames, c.elapsed, cells_max


def main():
    cache = find_cache()
    if cache is None:
        print('  ⏭  跳过（没有基准录像 %s 的抽流缓存；先跑\n'
          '     run_reader.py "<那段录像.mp4>" --client 2558x1494 --roi-offset=2,45 '
          '--topbar）' % REF_VIDEO)
        return 0
    print('缓存：out/cache/%s' % os.path.basename(cache))

    lay = layout_of(cache)
    marks, bogus, cells_max = scan(cache, lay)
    print('  退格点 %d 个，最大格数 %d' % (len(marks), cells_max))
    for got, want in zip(marks, TRUTH):
        if got != want:
            print('  ❌ 第 %d 个退格点：实际 f=%d cells=%d，期望 f=%d cells=%d'
                  % (TRUTH.index(want) + 1, got[0], got[1], want[0], want[1]))
    ok1 = marks == TRUTH
    if ok1:
        print('  ✅ 25 个退格点逐点对上，且每次只退一格（没有跳格）')
    elif len(marks) == len(TRUTH):
        print('  ❌ 格数对，但落帧位置有偏差')

    if bogus:
        for f, c in bogus:
            print('  ❌ 不该报的地方报了：f=%d cells=%d' % (f, c))
    else:
        print('  ✅ 条出现之前（f<%d）和退完消失之后都没有假报' % BAR_FIRST)
    ok2 = not bogus

    topbar_frames, elapsed, cells_max = run_state_machine(cache)
    print('  状态机：顶部条计入 %d 帧，累计 %d 帧，最大格数 %d' % (topbar_frames, elapsed, cells_max))
    ok3 = topbar_frames == cells_max - 1 == len(TRUTH) - 1
    print('  %s 顶部条计入的帧数 = 最大格数 - 1 = %d'
          % ('✅' if ok3 else '❌', len(TRUTH) - 1))

    print()
    ok = ok1 and ok2 and ok3
    print('结论：%s' % ('✅ 全部通过' if ok else '❌ 有回归'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
