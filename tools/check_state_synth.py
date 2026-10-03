#!/usr/bin/env python
"""合成信号自检：用「数学上精确」的费用条喂状态机，检查计数有没有系统性偏差。

离线对比（compare_reference.py）验证的是「读得对不对」，check_live.py 验证的是
「实时链路和离线链路一致不一致」；这个脚本验证的是第三件事：**状态机本身的计数有没有
系统性偏差**。因为喂进去的信号是理想线性的，真值可以精确算出来：

    采样 60Hz、2 倍速 -> 每个采样恰好 1 个逻辑帧，条走 4px（条宽 120）

期望误差 0 帧。如果这里出现成比例的误差，说明状态机在丢余量（曾经的一个真 bug：
换通道时把余量清零，几十次换通道就少算好几帧）。

用法：python tools/check_state_synth.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.features import BarReading, PauseReading, SpeedReading   # noqa: E402
from akframe.state import Counter                                     # noqa: E402

BAR_W = 120
PX_PER_FRAME = BAR_W / 30.0
RUN = PauseReading(ratio=0.30, running=True)
PAUSED = PauseReading(ratio=0.05, running=False)
SPEED = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=2.0,
                     bullet=False, conf='ok')


def bar(fill, state):
    return BarReading(fill=fill, grey146=BAR_W if state == 'MAX' else 0, grey88=0,
                      state=state, row=np.zeros(BAR_W, np.int16))


def simulate(segments, fps=60.0):
    """segments: [(采样数, 模式)]，模式为 run / max / pause。返回 (计数器, 真值)。"""
    c = Counter(BAR_W, fps=fps)
    dt = 1.0 / fps
    t = 0.0
    idx = 0
    phase = 0.0
    truth = 0
    for n, mode in segments:
        for _ in range(n):
            idx += 1
            t += dt
            if mode == 'pause':
                c.step(idx, t, bar(0, 'EMPTY'), PAUSED, SPEED)
                continue
            phase += 1.0
            if mode == 'max':
                c.step(idx, t, bar(0, 'MAX'), RUN, SPEED)
            else:
                fill = int((phase % 30) * PX_PER_FRAME) % BAR_W
                c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED)
            truth += 1
    return c.elapsed, truth


def main():
    segs = []
    for _ in range(60):
        segs += [(120, 'run'), (60, 'max'), (30, 'run'), (45, 'pause'), (75, 'run')]
    got, truth = simulate(segs)
    err = got - truth
    pct = 100.0 * err / truth
    print('真值 %d 逻辑帧，状态机 %d，误差 %+d 帧 (%.4f%%)' % (truth, got, err, pct))
    ok = abs(err) <= max(2, truth // 10000)
    print('结论：%s' % ('✅ 通过' if ok else '❌ 有系统性偏差'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
