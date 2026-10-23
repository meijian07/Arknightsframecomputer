#!/usr/bin/env python
"""启动起锚自检：点「开始」那一帧的相位基线必须算对。

用户 2026-10-19 实测：程序启动后第一次计数总是少 1 帧。根因是
LiveMonitor.begin_counting() 过去固定调用 mark_zero_start(None)，计数器
不知道「点开始时条是空的」还是「已经走过 1 帧」，起锚只能记 0 帧 ——
点开始后条从空走到第一个像素的那一帧就被吞掉了。

修法：启动时同步读一次当前条宽（LiveMonitor._probe_bar_fill），把
「点开始这一刻条画到第几帧」交给 mark_zero_start。这里用合成条验证：

  1. 条空（第 0 帧）时启动，第一次读到可见的第 2 帧要记 2 帧（旧代码记 0）；
  2. 第 1 帧在画面上仍是空条（不进入关卡判定），不能误当成「没开始」；
  3. 从空条启动，第一次条空累计正好 30 帧（不是 29）；
  4. 条已经在本费第 2 帧时启动，前一帧不能补，之后正常；
  5. 条在本费第 10 帧时启动，第一个条空只记 20 帧，第二个条空起每费 30 帧；
  6. begin_counting() 没收到 bar_fill 时会自己 probe，不能把基线丢掉。

用法：python tools/check_start_anchor.py
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import numpy as np  # noqa: E402

from akframe.features import (EMPTY_MAX_FILL, BarReading, PauseReading,  # noqa: E402
                              SpeedReading)
from akframe.live import LiveMonitor                                     # noqa: E402
from akframe.state import Counter                                        # noqa: E402
from akframe.synthesis import synthesized_width                          # noqa: E402

BAR_W = 120
PER = 30
RUN = PauseReading(ratio=0.30, running=True)
PAUSED = PauseReading(ratio=0.05, running=False)
SPEED = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=1.0,
                     bullet=False, conf='ok')


def bar(fill, state):
    return BarReading(fill=fill, grey146=BAR_W if state == 'MAX' else 0,
                      grey88=0, state=state, row=np.zeros(BAR_W, np.int16))


def width(phase):
    return synthesized_width(BAR_W, float(BAR_W), (phase % PER) / float(PER))


def feed(c, idx, t, phase):
    fill = width(phase)
    state = 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'
    return c.step(idx, t, bar(fill, state), RUN, SPEED)


def started_at(fill):
    """模拟「点开始」：Counter + LiveMonitor.begin_counting 的真实路径。"""
    c = Counter(BAR_W, fps=60.0, frames_per_cost=30.0)
    fake = types.SimpleNamespace(counter=c, _probe_bar_fill=lambda: fill)
    LiveMonitor.begin_counting(fake)
    return c


def run_to_boundary(c, idx, t, phase):
    """从 phase 往后逐帧喂到条空，返回 (idx, t, phase, 边界帧号)。"""
    while True:
        idx += 1
        t += 1.0 / c.fps
        phase += 1
        r = feed(c, idx, t, phase)
        if r.mode == 'wrap':
            return idx, t, phase, c.elapsed


def main():
    bad = []
    dt = 1.0 / 60.0

    # 1. 条空启动：第 1 帧画面仍是空条，读不到；第 2 帧第一次可见。
    #    旧代码把可见的第 2 帧当起锚（delta=0），整局从第一费就少 2 帧。
    #    新代码基线是第 0 帧，这一拍要把 1、2 两帧都记上。
    c = started_at(0)
    r1 = feed(c, 1, dt, 1)
    if r1.delta != 0 or c.elapsed != 0:
        bad.append(('空条启动，第 1 帧不可见', (r1.delta, c.elapsed), (0, 0)))
    r2 = feed(c, 2, 2 * dt, 2)
    if r2.delta != 2 or c.elapsed != 2:
        bad.append(('空条启动，首见第 2 帧', (r2.delta, c.elapsed), (2, 2)))
    # 继续走到下一费，第一费必须是完整 30 帧。
    idx, t, phase, end = run_to_boundary(c, 2, 2 * dt, 2)
    if end != 30 or c.bar_fill_count != 1:
        bad.append(('空条启动，首费条空', (end, c.bar_fill_count), (30, 1)))
    if c.bar_fill_frames != 30:
        bad.append(('空条启动，首费位置', c.bar_fill_frames, 30))

    # 2. 条已在第 2 帧时启动：前 2 帧是启动前的，不能补。
    c = started_at(width(2))
    r3 = feed(c, 1, dt, 2)
    if r3.delta != 0 or c.elapsed != 0:
        bad.append(('第 2 帧启动，首拍不补', (r3.delta, c.elapsed), (0, 0)))
    # 启动后走 1 帧，应当只加 1。
    r4 = feed(c, 2, 2 * dt, 3)
    if r4.delta != 1 or c.elapsed != 1:
        bad.append(('第 2 帧启动，下一拍', (r4.delta, c.elapsed), (1, 1)))

    # 2b. 游戏暂停时点开始：首拍只能对齐相位，绝不能补开始前的 2 帧。
    #     用户 2026-10-23 录像：首拍 running=0，面板却从 0 跳到 2。
    c = started_at(width(2))
    fill2 = width(2)
    rp = c.step(1, dt, bar(fill2, 'TRACK'), PAUSED, SPEED)
    if rp.delta != 0 or c.elapsed != 0:
        bad.append(('暂停中启动，首拍不补', (rp.delta, c.elapsed), (0, 0)))
    fill3 = width(3)
    rp2 = c.step(2, 2 * dt, bar(fill3, 'TRACK'), RUN, SPEED)
    if rp2.delta != 1 or c.elapsed != 1:
        bad.append(('暂停中启动，恢复后 +1', (rp2.delta, c.elapsed), (1, 1)))

    # 3. 第 10 帧启动：第一个条空只记 20 帧（不是硬补成 30），
    #    第二个条空起恢复完整 30 帧。
    c = started_at(width(10))
    feed(c, 1, dt, 10)             # 启动这一拍，相位对齐，不补帧
    idx, t, phase, end1 = run_to_boundary(c, 1, dt, 10)
    if end1 != 20 or c.bar_fill_count != 0:
        bad.append(('第 10 帧启动，首个条空', (end1, c.bar_fill_count), (20, 0)))
    idx, t, phase, end2 = run_to_boundary(c, idx, t, phase)
    if end2 - end1 != 30 or c.bar_fill_count != 1:
        bad.append(('第 10 帧启动，第二个条空',
                    (end2 - end1, c.bar_fill_count), (30, 1)))

    # 4. begin_counting() 自动 probe：模拟界面没有显式传 bar_fill 的路径。
    c = Counter(BAR_W, fps=60.0, frames_per_cost=30.0)
    fake = types.SimpleNamespace(counter=c, _probe_bar_fill=lambda: 0)
    LiveMonitor.begin_counting(fake)
    feed(c, 1, dt, 1)
    r5 = feed(c, 2, 2 * dt, 2)
    if r5.delta != 2 or c.elapsed != 2:
        bad.append(('自动 probe，空条首见第 2 帧', (r5.delta, c.elapsed), (2, 2)))

    # 5. probe 失败（None）时保持保守语义：不补启动前的帧。
    c = Counter(BAR_W, fps=60.0, frames_per_cost=30.0)
    fake = types.SimpleNamespace(counter=c, _probe_bar_fill=lambda: None)
    LiveMonitor.begin_counting(fake)
    feed(c, 1, dt, 1)
    r6 = feed(c, 2, 2 * dt, 2)
    if r6.delta != 0 or c.elapsed != 0:
        bad.append(('probe 失败，保守不补', (r6.delta, c.elapsed), (0, 0)))

    print('启动起锚自检：')
    if bad:
        for item in bad:
            print('  ❌ ' + '：'.join(str(x) for x in item))
    else:
        print('  ✅ 通过（空条启动首见第 2 帧记 2；首费条空正好 30；'
              '半路启动不补启动前帧；第 10 帧启动首个条空 20、后续 30；'
              'probe 失败仍保守）')
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
