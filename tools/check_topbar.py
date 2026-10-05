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

另外两条**合成信号**自检（不需要录像，永远跑）：条被挡住看不见之后，等它的那段时间
要还给挂钟通道（不能白丢）；但暂停键闪烁那种「两三拍假运行」一分一秒都不许算进去。

缓存 out/cache/<录像名>_tb_<指纹>/ 是抽流的一次性产物（topbar.raw + meta.json），
目录被 .gitignore 掉了、不会跟着仓库走 —— 没有缓存就跳过（会打印跳过原因）。

用法：python tools/check_topbar.py
"""

import glob
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.capture import iter_cached, load_meta            # noqa: E402
from akframe.geometry import Layout                           # noqa: E402
from akframe.features import (BarReading, PauseReading, SpeedReading,  # noqa: E402
                              TopBarReading, read_bar, read_pause, read_speed,
                              read_topbar)
from akframe.state import Counter                             # noqa: E402

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


BAR_W = 120
RUN = PauseReading(ratio=0.30, running=True)
PAUSED = PauseReading(ratio=0.05, running=False)
# 子弹时间：速率是确定的 6 逻辑帧/秒，和倍速设置无关（见 docs 3.1）
BULLET = SpeedReading(digit_ratio=0.30, corr_2x=0.2, has_digit=False, m=1.0,
                      bullet=True, conf='ok')
MAXBAR = BarReading(fill=0, grey146=BAR_W, grey88=0, state='MAX',
                    row=np.zeros(BAR_W, np.int16))
NOTB = TopBarReading(present=False, cells=None, edge=-1, level=0.0, look=0.0)


def _tb(cells):
    return TopBarReading(present=True, cells=cells, edge=0, level=190.0, look=30.0)


def _feed(c, t, idx, running, topbar):
    return c.step(idx, t, MAXBAR, RUN if running else PAUSED, BULLET, topbar=topbar)


def wait_timeout_refunded():
    """条读不到超过 TOPBAR_HOLD_SECONDS 之后，那段等待时间要还给挂钟通道。

    真值怎么算：全程子弹时间 = 6 逻辑帧/秒，60Hz 采样下每拍 0.1 逻辑帧。
    修之前只记 5 帧：挂钟自己只补了「确认完之后」的那 54 拍，等条的 35 拍
    （0.583 秒 = 3.5 逻辑帧）整段白丢。
    """
    c = Counter(BAR_W, fps=60.0)
    t = 0.0
    for i in range(5):                      # 前 5 拍读得到条，先把基准取上
        t += 1 / 60.0
        _feed(c, t, i + 1, True, _tb(3))
    base = c.elapsed
    n = 90                                  # 之后读不到，游戏一直在跑
    for i in range(n):
        t += 1 / 60.0
        _feed(c, t, 6 + i, True, NOTB)
    return c.elapsed - base, n / 60.0 * 6.0


def pause_flicker_not_counted():
    """暂停时暂停键图标闪成「运行中」的那两三拍，一分一秒都不许算进挂钟。

    实测出处：一段 76 秒的暂停录像里闪了 28 次，旧逻辑每次凭空多 1 帧、总共多 30 帧
    （见 _clock_gated 的说明）。顶部条这条新路必须同样挡住它。
    """
    c = Counter(BAR_W, fps=60.0)
    t = 0.0
    idx = 0
    for _ in range(60):                     # 60 轮：闪 2 拍，再暂停 60 拍
        for _ in range(2):
            idx += 1
            t += 1 / 60.0
            _feed(c, t, idx, True, NOTB)
        for _ in range(60):
            idx += 1
            t += 1 / 60.0
            _feed(c, t, idx, False, NOTB)
    return c.elapsed

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
    got, want = wait_timeout_refunded()
    # 累计帧是 floor 取整，末尾不足 1 帧的相位留在余量里（这里剩 0.8 帧），
    # 所以允许比真值少 1 帧；**多**出来不许 —— 多 = 两套通道把同一段时间记了两遍。
    # 修之前只有 5 帧（挂钟自己补的那 54 拍），等条的 35 拍白丢，落在区间外面。
    ok0 = got > 0 and want - 1.5 <= got <= want + 0.5
    print('  等条回来超时后还给挂钟：%d 帧（真值 %.1f 帧，floor 取整；修之前只有 5 帧）'
          % (got, want))
    print('  %s 等待那段时间没有被白丢' % ('✅' if ok0 else '❌'))

    flick = pause_flicker_not_counted()
    ok0b = flick == 0
    print('  暂停键闪烁 60 轮（每轮闪 2 拍）：多记 %d 帧' % flick)
    print('  %s 闪烁的假运行没有凭空补帧' % ('✅' if ok0b else '❌'))
    print()

    cache = find_cache()
    if cache is None:
        print('  ⏭  跳过（没有基准录像 %s 的抽流缓存；先跑\n'
          '     run_reader.py "<那段录像.mp4>" --client 2558x1494 --roi-offset=2,45 '
          '--topbar）' % REF_VIDEO)
        print()
        print('结论：%s' % ('✅ 合成自检通过（录像那部分跳过）' if (ok0 and ok0b)
                            else '❌ 有回归'))
        return 0 if (ok0 and ok0b) else 1
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
    ok = ok0 and ok0b and ok1 and ok2 and ok3
    print('结论：%s' % ('✅ 全部通过' if ok else '❌ 有回归'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
