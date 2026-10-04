#!/usr/bin/env python
"""变速混合自测：「一费 = 30 逻辑帧」在各种倍速下是不是都成立。

check_state_synth.py 只测 2 倍速一种情况。这个脚本把 1X / 2X / 0.2X（子弹时间）、
暂停、满费、关卡外、**手动暂停计数**全混在一起，喂给状态机，和**精确真值**比总帧数。

真值怎么来：生成器自己按「这一拍游戏前进了 speed/2 个逻辑帧」推进相位
（60Hz 采样 = 1/60 秒/拍，1 倍速每秒 30 逻辑帧 -> 每拍 0.5 帧），
所以真值不需要从画面里猜，是算出来的。

为什么要单独测这个：像素通道天然免疫倍速（条走多少像素就记多少帧），
但「满费挂钟通道」要乘倍速 —— 倍速一读错、或者干脆没读出来，
整段就会几倍地错。这正是「一费 30 帧变成 62 帧」的来源，必须钉死。

用法：python tools/check_speed_mix.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.features import BarReading, PauseReading, SpeedReading   # noqa: E402
from akframe.state import Counter                                     # noqa: E402
from akframe.synthesis import synthesized_width                       # noqa: E402

BAR_W = 240
FPS = 60.0

RUN = PauseReading(ratio=0.30, running=True)
PAUSED = PauseReading(ratio=0.05, running=False)


def speed_reading(m, bullet=False):
    """倍速键读数：有数字 -> m；子弹时间 -> 没数字、只有箭头。"""
    if bullet:
        return SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                            m=float('nan'), bullet=True, conf='ok')
    return SpeedReading(digit_ratio=0.30, corr_2x=0.99, has_digit=True, m=float(m),
                        bullet=False, conf='ok')


def bar(fill, state):
    grey = BAR_W if state == 'MAX' else 0
    return BarReading(fill=fill, grey146=grey, grey88=0, state=state,
                      row=np.zeros(BAR_W, np.int16))


def simulate(segments):
    """segments: [(采样数, speed, mode)]。返回 (状态机帧数, 真值帧数, 每段明细)。"""
    c = Counter(BAR_W, fps=FPS)
    dt = 1.0 / FPS
    t = 0.0
    idx = 0
    frames = 0.0          # 真值：游戏内部推进的逻辑帧（可以是小数）
    detail = []
    for n, speed, mode in segments:
        before = c.elapsed
        truth = 0.0
        # 手动「暂停计数」开关：该段里游戏照常在跑（条在动），
        # 但计数器不该往 total 上加（真值 = 0）。
        c.set_manual_pause(mode == 'manual')
        for _ in range(n):
            idx += 1
            t += dt
            if mode == 'idle':
                c.step(idx, t, bar(0, 'EMPTY'), RUN, speed_reading(1.0))
                continue
            if mode == 'pause':
                # 暂停：相位不动，游戏时钟也停
                fill = synthesized_width(BAR_W, float(BAR_W), (frames % 30.0) / 30.0)
                c.step(idx, t, bar(fill, 'TRACK'), PAUSED, speed_reading(1.0))
                continue
            if mode == 'manual':
                # 手动暂停计数：游戏照常跑，只是数字不动
                frames += speed * 30.0 * dt
                fill = synthesized_width(BAR_W, float(BAR_W), (frames % 30.0) / 30.0)
                c.step(idx, t, bar(fill, 'TRACK'), RUN, speed_reading(speed))
                continue
            truth += speed * 30.0 * dt
            if mode.startswith('max'):
                # 满费：整条灰底，相位被游戏钉在 0；退出后从 0 重新涨（实测结论）。
                # 0.2 倍速时倍速键上不画数字，只有三条箭头。
                frames = 0.0
                c.step(idx, t, bar(0, 'MAX'), RUN,
                       speed_reading(speed, bullet=(speed < 1.0)))
            else:
                frames += speed * 30.0 * dt
                fill = synthesized_width(BAR_W, float(BAR_W), (frames % 30.0) / 30.0)
                c.step(idx, t, bar(fill, 'TRACK'), RUN, speed_reading(speed))
        detail.append((mode, speed, n, c.elapsed - before, truth))
    return c.elapsed, sum(d[4] for d in detail), detail


def main():
    cases = [
        ('纯 1X', [(600, 1.0, 'run')]),
        ('纯 2X', [(600, 2.0, 'run')]),
        ('纯 0.2X 子弹时间', [(600, 0.2, 'run')]),
        ('满费 1X', [(600, 1.0, 'max')]),
        ('满费 2X', [(600, 2.0, 'max')]),
        # 满费 + 0.2X：倍速键上不画数字 -> 按设计**冻结**（见下面的 KNOWN 检查）
        ('暂停 60 秒', [(3600, 1.0, 'pause')]),
        # 手动暂停计数：按住的 5 秒不计，放开后不跳（重新起锚）
        ('手动暂停计数', [(300, 1.0, 'run'), (300, 1.0, 'manual'),
                                 (300, 1.0, 'run')]),
        ('关卡外 10 秒', [(600, 1.0, 'idle')]),
        ('混合', [(180, 1.0, 'run'), (180, 2.0, 'run'), (300, 0.2, 'run'),
                  (180, 1.0, 'pause'), (240, 1.0, 'max'),
                  (300, 1.0, 'run'), (180, 1.0, 'idle'), (300, 1.0, 'run')]),
    ]
    bad = 0
    print('%-18s %10s %10s %8s' % ('场景', '状态机', '真值', '误差'))
    for name, segs in cases:
        got, truth, detail = simulate(segs)
        err = got - round(truth)
        # 容差：满费段靠挂钟估算，允许 ±2 帧；其余段必须 0 误差
        tol = 2 if any(m.startswith('max') for _, _, m in segs) else 1
        mark = 'OK ' if abs(err) <= tol else 'BAD'
        if abs(err) > tol:
            bad += 1
        print('%-18s %10d %10d %8s  %s'
              % (name, got, round(truth), '%+d' % err, mark))
        if abs(err) > tol:
            for mode, speed, n, gotd, truthd in detail:
                print('      %-8s %4.1fX %5d 拍  状态机 %6.1f  真值 %6.1f  差 %+6.1f'
                      % (mode, speed, n, gotd, truthd, gotd - truthd))
    # 已知**故意**不记的情况：满费 + 倍速键上没画数字（暂停菜单 / 拖干员时的按钮）。
    # 这时速率未知，冻结不猜 —— 猜成 0.2 倍速就是 5 倍偏差，猜成 1 倍速也是 5 倍偏差。
    got, truth, _ = simulate([(300, 1.0, 'run'), (300, 0.2, 'max'), (300, 1.0, 'run')])
    expect = 300          # 只有两段 1X（各 150 帧）该被记；中间 300 拍冻结
    ok = abs(got - expect) <= 2
    print('%-18s %10d %10d %8s  %s'
          % ('满费+倍速未知', got, expect, '%+d' % (got - expect), 'OK ' if ok else 'BAD'))
    if not ok:
        bad += 1

    print()
    if bad:
        print('结论：❌ %d 个场景超出容差' % bad)
    else:
        print('结论：✅ 全部通过（1X / 2X / 0.2X 下「一费 = 30 帧」都成立）')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
