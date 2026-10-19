#!/usr/bin/env python
"""费用周期边界自检：每次条满 -> 条空都必须落在整数个完整回费周期上。

用户 2026-10-18 实测：满费锚点之外仍会 40 秒漂 3 帧。原因是逐拍像素累加
的余量会跨费用周期慢慢攒。这里在**每一次回卷**都把累计帧号钉回：

    上一个条空时刻 + frames_per_cost（负费翻倍）

并记录满条次数 bar_fill_count。正常从条空开始计数时，面板就是
n * 周期 + x；x 是本费已走的帧数，条空时 x = 0。

覆盖：
  1. 30 / 60 / 120 / 负费 240 周期都严格等于周期长度；
  2. 人为注入 +3 帧漂移，下一次条空自动拨回；
  3. 满条次数 n 与实际回卷次数一致；
  4. 半路开始（条已经走了 10 帧）不硬把第一次条空改成整周期 —— 不从起点
     凭空补帧，之后的周期仍然严格等长。
  5. 清零落在本费第 2 帧时，第一次条空正好差 2 帧；首费补平到 30，不再出现
     「满条 1 ｜ -2」。清零落在更靠前的位置时只建立基准，不凭空补。
  6. 正费中途切负费：这一费真值 30 + n/2 帧，**不许**被边界锚点硬掰回 30；
     逐周期长度和累计帧数都要对得上。

用法：python tools/check_cycle_anchor.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import numpy as np  # noqa: E402

from akframe.features import (EMPTY_MAX_FILL, BarReading, PauseReading,  # noqa: E402
                              SignReading, SpeedReading)
from akframe.fp24 import (SPEED_NEGATIVE, SPEED_NORMAL, CostTiming,  # noqa: E402
                          required_from_frames)
from akframe.state import Counter                                     # noqa: E402
from akframe.synthesis import synthesized_width                       # noqa: E402

BAR_W = 120
RUN = PauseReading(ratio=0.30, running=True)
SPEED = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=2.0,
                     bullet=False, conf='ok')


def bar(fill, state):
    return BarReading(fill=fill, grey146=BAR_W if state == 'MAX' else 0,
                      grey88=0, state=state, row=np.zeros(BAR_W, np.int16))


def phase_frac(n, per):
    return (n % per) / float(per)


def run_case(per, neg=False, cycles=40, start_phase=1, drift_at=0, fps=60.0):
    """逐拍喂合成条；返回 (counter, 真值帧数)。drift_at>0 时在该次回卷后注入 +3。"""
    period = int(per * (2 if neg else 1))
    c = Counter(BAR_W, fps=fps, frames_per_cost=per)
    sign = SignReading(negative=neg, run=40 if neg else 18,
                       rows=3 if neg else 0, thr=21)
    t = 0.0
    n = start_phase
    injected = False
    # 从 start_phase 开始走 cycles 个完整周期
    samples = cycles * period
    truth = 0
    for i in range(1, samples + 1):
        t += 1.0 / fps
        n += 1
        truth += 1
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n, period))
        c.step(i, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
               RUN, SPEED, sign)
        if drift_at and not injected and c.bar_fill_count >= drift_at:
            # 模拟累计余量让面板多走了 3 帧；相位模型不动。
            c.pixel_frames += 3
            c.elapsed += 3
            c.elapsed_f += 3
            injected = True
    return c, truth


def run_reset_case(start_phase, fps=60.0):
    """模拟「游戏在本费第 start_phase 帧时点清零，然后点开始」。

    reset()/begin_counting() 会连续调 mark_zero_start(fill) 和
    mark_zero_start(None)；后一次不能把前一次记下的清零相位擦掉。
    返回每次条空时的 (elapsed, bar_fill_count)。
    """
    c = Counter(BAR_W, fps=fps, frames_per_cost=30.0)
    fill0 = synthesized_width(BAR_W, float(BAR_W), phase_frac(start_phase, 30))
    c.mark_zero_start(fill0)
    c.resync()
    c.mark_zero_start(None)
    t = 0.0
    idx = 0
    phase = start_phase
    # 清零时游戏通常还停在暂停菜单里：先喂一拍暂停，再恢复。
    idx += 1
    t += 1.0 / fps
    c.step(idx, t, bar(fill0, 'TRACK' if fill0 > EMPTY_MAX_FILL else 'EMPTY'),
           PauseReading(ratio=0.05, running=False), SPEED)
    boundaries = []
    for _ in range(70):
        idx += 1
        t += 1.0 / fps
        phase += 1
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase, 30))
        r = c.step(idx, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                   RUN, SPEED)
        if r.mode == 'wrap':
            boundaries.append((c.elapsed, c.bar_fill_count))
    return c, boundaries


def run_mixed_case(neg_frames, fps=60.0):
    """前 3 费全正费，第 4 费开头走 neg_frames 帧负费、之后转回正费。

    这一段周期的真值是 30 + neg_frames/2：负费每逻辑帧只攒一半。
    返回 (counter, 每次条空的 (sample, elapsed, n, bar_fill_frames))。
    """
    c = Counter(BAR_W, fps=fps, frames_per_cost=30.0)
    timing = CostTiming(required_from_frames(30.0))
    dt = 1.0 / fps
    t = 0.0
    boundaries = []
    for i in range(260):
        neg = 90 <= i < 90 + neg_frames
        timing.advance_one_frame(SPEED_NEGATIVE if neg else SPEED_NORMAL)
        fill = synthesized_width(BAR_W, float(BAR_W), timing.phase())
        sign = SignReading(negative=neg, run=40 if neg else 18,
                           rows=3 if neg else 0, thr=21)
        t += dt
        prev = c.bar_fill_count
        c.step(i + 1, t,
               bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
               RUN, SPEED, sign)
        if c.bar_fill_count != prev:
            boundaries.append((i + 1, c.elapsed, c.bar_fill_count,
                               c.bar_fill_frames))
    return c, boundaries


def main():
    bad = []
    for tag, per, neg in (('30 帧', 30.0, False), ('60 帧', 60.0, False),
                          ('120 帧', 120.0, False), ('30 帧 + 负费', 30.0, True),
                          ('60 帧 + 负费', 60.0, True), ('120 帧 + 负费', 120.0, True)):
        period = int(per * (2 if neg else 1))
        c, truth = run_case(per, neg, cycles=40)
        if sorted(set(c.cycle_lengths)) != [period]:
            bad.append((tag, '逐周期长度', c.cycle_lengths[:8], period))
        if c.bar_fill_count != 40:
            bad.append((tag, '满条次数', c.bar_fill_count, 40))
        if abs(c.elapsed - truth) > 1:
            bad.append((tag, '累计帧数', c.elapsed, truth))
        # 从条空开始：每个条空时刻都应是 n * 周期。
        if c.elapsed != c.bar_fill_count * period + 0:
            bad.append((tag, '条空时不是 n*周期', c.elapsed,
                        c.bar_fill_count * period))
        if c.bar_fill_frames != c.elapsed:
            bad.append((tag, '最后一个条空位置', c.bar_fill_frames, c.elapsed))

    # 注入漂移：下一个条空必须拨回整数周期，最终累计仍然对得上。
    c, truth = run_case(30.0, False, cycles=40, drift_at=10)
    if sorted(set(c.cycle_lengths)) != [30]:
        bad.append(('注入 +3 帧', '逐周期长度', c.cycle_lengths[:8], 30))
    if abs(c.elapsed - truth) > 1:
        bad.append(('注入 +3 帧', '没有拨回', c.elapsed, truth))

    # 半路起锚：第一次条空是「从起点到条空」的不足一个周期，不能凭空补成 30；
    # 之后记录的完整周期仍必须严格 30。（cycle_lengths 从第二次条空才开始记。）
    c2, _ = run_case(30.0, False, cycles=10, start_phase=10)
    if any(v != 30 for v in c2.cycle_lengths):
        bad.append(('半路起锚', '后续周期不等长', c2.cycle_lengths[:6], 30))

    # 用户 2026-10-18 录像：画面在本费第 2 帧时清零，旧逻辑第一次条空只有 28 帧，
    # 面板显示 `满条 1 ｜ -2`。清零后离整周期只差 2 帧，应当补平到 30；
    # 如果是在第 10 帧清零（离条空还有 20 帧），则第一次条空只是基准，n 仍为 0。
    c3, b3 = run_reset_case(2)
    if not b3 or b3[0] != (30, 1):
        bad.append(('清零落在第 2 帧', '第一次条空', b3[:2], (30, 1)))
    c4, b4 = run_reset_case(10)
    if (len(b4) < 2 or b4[0][1] != 0 or b4[1][1] != 1
            or b4[1][0] - b4[0][0] != 30):
        bad.append(('清零落在第 10 帧', '第一次不完整、下一次完整', b4[:2],
                    '首条空 n=0，下一条空 n=1 且 +30'))

    # 用户 2026-10-18：正费中途切负费，旧逻辑把 34 帧的周期硬掰回 30。
    # 负费只走了 4 / 8 / 20 帧时，真值分别是 32 / 34 / 40 帧；累计帧数也要
    # 一帧不差，面板的「本费余量」在条空那一刻必须是 0。
    for neg_frames, period in ((4, 32), (8, 34), (20, 40)):
        c5, b5 = run_mixed_case(neg_frames)
        tag = '正费切负费 %d 帧' % neg_frames
        if c5.elapsed != 260:
            bad.append((tag, '累计帧数', c5.elapsed, 260))
        if c5.cycle_lengths[2:3] != [period]:
            bad.append((tag, '混合周期长度', c5.cycle_lengths[:5], period))
        target = [b for b in b5 if b[2] == 4]
        if not target or target[0][1] != 90 + period or target[0][3] != 90 + period:
            bad.append((tag, '混合周期条空位置', target[:1],
                        (90 + period, 90 + period)))
        if any(b[1] != b[3] for b in b5):
            bad.append((tag, '条空时本费余量不为 0', b5[:3], 'elapsed == 条空位置'))

    print('费用周期边界锚点自检：')
    if bad:
        for item in bad:
            print('  ❌ ' + '：'.join(str(x) for x in item))
    else:
        print('  ✅ 通过（30/60/120/240 周期逐周期严格相等；注入 +3 帧后下一次条空拨回；'
              '满条次数 n 与回卷次数一致；第 2 帧清零首费补平到 30、第 10 帧清零不硬补；'
              '正费中途切负费的混合周期不被硬掰回 30）')
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
