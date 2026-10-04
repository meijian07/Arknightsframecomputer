#!/usr/bin/env python
"""合成信号自检：用「数学上精确」的费用条喂状态机，检查计数有没有系统性偏差。

离线对比（compare_reference.py）验证的是「读得对不对」，check_live.py 验证的是
「实时链路和离线链路一致不一致」；这个脚本验证的是第三件事：**状态机本身的计数有没有
系统性偏差**。因为喂进去的信号是理想线性的，真值可以精确算出来：

    采样 60Hz、2 倍速 -> 每个采样恰好 1 个逻辑帧

期望误差 0 帧。合成信号必须按游戏真实的「相位 -> 可见宽度」曲线来画（synthesis.py），
不能假设条宽是线性的：那样喂进去的就不是游戏里的条，测出来的差异说明不了任何问题
（实测线性假设下多算 59 帧，换成本曲线后误差归零）。

这里主要防两类回归：
    1. 状态机丢余量（曾经换个通道就把余量清零，几十次换通道少算好几帧）；
    2. 相位重同步选错 k（比如在「头两帧条还没动」的平段上把 k 选大/选小）。

用法：python tools/check_state_synth.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.features import BarReading, PauseReading, SpeedReading   # noqa: E402
from akframe.state import Counter                                     # noqa: E402
from akframe.synthesis import synthesized_width                       # noqa: E402

BAR_W = 120
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
                # 按游戏真实渲染画条：相位 -> 可见宽度（含头两帧的死区与尾端 clamp）
                fill = synthesized_width(BAR_W, float(BAR_W), (phase % 30.0) / 30.0)
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
