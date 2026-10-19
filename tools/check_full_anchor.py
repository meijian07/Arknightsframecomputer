#!/usr/bin/env python
"""满费锚点自检：长期累计漂移必须被「MAX 上升沿」拉回整数个费用周期。

用户实测：逐帧计数本身稳定，但跑 20 秒左右会偏 1 帧。原因是像素通道每拍
只走整数帧、满费挂钟通道还有不足一帧的余量，误差几十个周期后才会显形。
修法不是「每次 MAX 都硬加 30」—— 玩家中途用费、技能提前回满时，满费间隔
本来就不是完整周期。这里验证的是：

  1. 正常回费：偏差 1 帧会被拉回上次满费 + n 个周期；
  2. 30 / 60 / 120 / 240 帧周期都对；
  3. MAX 连续亮很多拍、MAX -> MAX_FADE -> MAX 都只算一次事件；
  4. 偏差超过 2 帧（中途用费）只重新起锚，不硬改帧号；
  5. 清零 / 换回费速率会重置锚点，重新校准（adopt）会保留。

用法：python tools/check_full_anchor.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.features import (EMPTY_MAX_FILL, BarReading, PauseReading,  # noqa: E402
                              SignReading, SpeedReading)
from akframe.state import FULL_ANCHOR_TOL, Counter                   # noqa: E402
from akframe.synthesis import synthesized_width                      # noqa: E402

BAR_W = 120
RUN = PauseReading(ratio=0.30, running=True)
PAUSED = PauseReading(ratio=0.05, running=False)
NO_HUD = PauseReading(ratio=0.05, running=False, drawn=False)
SPEED = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=2.0,
                     bullet=False, conf='ok')
SPEED_1X = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=1.0,
                        bullet=False, conf='ok')
NO_HUD_SPEED = SpeedReading(digit_ratio=0.0, corr_2x=0.0, has_digit=False,
                            m=1.0, bullet=False, conf='ok', drawn=False)


def bar(fill, state):
    return BarReading(fill=fill, grey146=BAR_W if state == 'MAX' else 0,
                      grey88=0, state=state, row=None)


def set_total(c, total):
    """把累计帧号直接设成 total（测试里模拟「已经漂到这里」）。"""
    whole = int(total)
    c.pixel_frames = whole
    c.clock_frames = 0
    c._clock_frac = float(total) - whole
    c.elapsed = whole
    c.elapsed_f = float(total)


def feed(c, idx, state, pause=RUN, speed=SPEED, n=1, sign=None):
    dt = 1.0 / c.fps
    for _ in range(n):
        idx += 1
        c.step(idx, idx * dt, bar(0, state), pause, speed, sign)
    return idx


def anchor_math():
    bad = []
    for name, period in (('30 帧', 30.0), ('60 帧', 60.0),
                         ('120 帧', 120.0), ('240 帧', 240.0)):
        c = Counter(BAR_W, fps=60.0)
        c._cycle_ref = 990
        set_total(c, 1000)
        c._full_anchor_event(period)
        if c.full_anchor != 1000 or c.full_anchor_corrections != 0:
            bad.append((name, '首次起锚', (c.full_anchor, c.full_anchor_corrections)))
            continue
        set_total(c, 1000 + period + 1)
        c._full_anchor_event(period)
        if c.elapsed != int(1000 + period) or abs(c.full_anchor - (1000 + period)) > 1e-9:
            bad.append((name, '偏 +1 帧未拉回',
                        (c.elapsed, c.full_anchor, 1000 + period)))
        if c._cycle_ref != 989:
            bad.append((name, '_cycle_ref 未同步', (c._cycle_ref, 989)))
        # 下一轮再偏 -1，必须继续拉回
        set_total(c, 1000 + 2 * period - 1)
        c._full_anchor_event(period)
        if c.elapsed != int(1000 + 2 * period):
            bad.append((name, '偏 -1 帧未拉回', (c.elapsed, 1000 + 2 * period)))
        if c._cycle_ref != 990:
            bad.append((name, '_cycle_ref 第二次未同步', (c._cycle_ref, 990)))
    return bad


def big_gap_no_force():
    bad = []
    c = Counter(BAR_W, fps=60.0)
    set_total(c, 1000)
    c._full_anchor_event(30.0)
    # 中途用费太多：间隔 110 帧，最近的整数周期是 4 个（120），差 10 帧。
    set_total(c, 1110)
    c._full_anchor_event(30.0)
    if c.elapsed != 1110:
        bad.append(('差 10 帧仍被硬改', c.elapsed, 1110))
    if c.full_anchor != 1110:
        bad.append(('大偏差后未重新起锚', c.full_anchor, 1110))
    if c.full_anchor_corrections != 0:
        bad.append(('大偏差被计成校正', c.full_anchor_corrections, 0))
    # 子帧偏差也要拨平（0.6 帧）：只拨整数部分会留下长期余量。
    set_total(c, 1139.4)
    c._full_anchor_event(30.0)
    if abs(c.elapsed_f - 1140.0) > 1e-9:
        bad.append(('子帧偏差未拨平', c.elapsed_f, 1140.0))
    if abs(c.full_anchor - 1140.0) > 1e-9:
        bad.append(('子帧锚点不对', c.full_anchor, 1140.0))
    return bad


def edge_detection():
    bad = []
    c = Counter(BAR_W, fps=60.0)
    idx = feed(c, 0, 'TRACK', n=6)
    idx = feed(c, idx, 'MAX', n=15)
    if c.full_anchor_events != 1:
        bad.append(('连续 MAX 15 拍', c.full_anchor_events, 1))
    idx = feed(c, idx, 'MAX_FADE', n=3)
    idx = feed(c, idx, 'MAX', n=4)
    if c.full_anchor_events != 1:
        bad.append(('MAX_FADE 后仍是同一次满费', c.full_anchor_events, 1))
    idx = feed(c, idx, 'TRACK', n=5)
    idx = feed(c, idx, 'MAX', n=2)
    if c.full_anchor_events != 2:
        bad.append(('离开满费后再次 MAX', c.full_anchor_events, 2))
    # 暂停中的 MAX 不算；恢复运行后的第一次 MAX 才算。
    idx = feed(c, idx, 'TRACK', n=3)
    idx = feed(c, idx, 'MAX', pause=PAUSED, n=5)
    if c.full_anchor_events != 2:
        bad.append(('暂停中的 MAX 被算成事件', c.full_anchor_events, 2))
    idx = feed(c, idx, 'MAX', pause=RUN, n=1)
    if c.full_anchor_events != 3:
        bad.append(('恢复运行后的 MAX 未起锚', c.full_anchor_events, 3))
    # 关卡外（没有 HUD）的 MAX 不能起锚。
    c2 = Counter(BAR_W, fps=60.0)
    feed(c2, 0, 'MAX', pause=NO_HUD, speed=NO_HUD_SPEED, n=8)
    if c2.full_anchor is not None or c2.full_anchor_events != 0:
        bad.append(('关卡外起了锚', (c2.full_anchor, c2.full_anchor_events), (None, 0)))
    # step() 里给负费传的周期必须是 60，不是 30。
    c3 = Counter(BAR_W, fps=60.0)
    neg = SignReading(negative=True, run=40, rows=3, thr=21)
    idx = feed(c3, 0, 'TRACK', speed=SPEED_1X, n=6, sign=neg)
    idx = feed(c3, idx, 'MAX', speed=SPEED_1X, n=1, sign=neg)
    a = c3.full_anchor
    idx = feed(c3, idx, 'TRACK', speed=SPEED_1X, n=1, sign=neg)
    set_total(c3, a + 61.0)
    feed(c3, idx, 'MAX', speed=SPEED_1X, n=1, sign=neg)
    if abs(c3.elapsed_f - (a + 60.0)) > 1e-9:
        bad.append(('负费周期未按 60 校正', c3.elapsed_f, a + 60.0))
    return bad


def reset_and_adopt():
    bad = []
    c = Counter(BAR_W, fps=60.0)
    set_total(c, 500)
    c._full_anchor_event(30.0)
    c.mark_zero_start(0)
    if c.full_anchor is not None:
        bad.append(('清零未重置锚点', c.full_anchor, None))
    # adopt 要保留另一只计数器的锚点与统计
    other = Counter(BAR_W, fps=60.0)
    set_total(other, 900)
    other._full_anchor_event(30.0)
    c.adopt(other)
    if c.full_anchor != 900 or c.full_anchor_events != 1:
        bad.append(('adopt 未保留锚点', (c.full_anchor, c.full_anchor_events), (900, 1)))
    # 换回费速率必须重新起锚
    c.set_frames_per_cost(60.0)
    if c.full_anchor is not None:
        bad.append(('换回费速率未重置锚点', c.full_anchor, None))
    return bad


def injected_drift_recovers():
    """端到端：人为把累计帧号拨快 1 帧，下一次满费必须自己拉回来。"""
    bad = []
    c = Counter(BAR_W, fps=60.0)
    idx = 0
    truth = 0
    for cyc in range(40):
        for ph in range(1, 30):
            idx += 1
            truth += 1
            fill = synthesized_width(BAR_W, float(BAR_W), ph / 30.0)
            c.step(idx, idx / 60.0,
                   bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                   RUN, SPEED)
        idx += 1
        truth += 1
        c.step(idx, idx / 60.0, bar(0, 'MAX'), RUN, SPEED)
        if cyc == 20:
            # 模拟「跑了 20 秒后多记了 1 帧」
            c.clock_frames += 1
            c.elapsed += 1
            c.elapsed_f += 1
    if c.elapsed != truth:
        bad.append(('40 个周期后未回到真值', c.elapsed, truth))
    if c.full_anchor_corrections < 1:
        bad.append(('注入的漂移没触发校正', c.full_anchor_corrections, '>=1'))
    return bad


def main():
    checks = (('满费锚点数学（30/60/120/240）', anchor_math()),
              ('大偏差不硬改 / 子帧拨平', big_gap_no_force()),
              ('MAX 上升沿判定', edge_detection()),
              ('清零 / adopt / 换速率', reset_and_adopt()),
              ('注入 1 帧漂移后自动拉回', injected_drift_recovers()))
    ok = True
    for name, bad in checks:
        print(name + '：')
        if bad:
            ok = False
            for item in bad:
                print('  ❌ ' + '：'.join(str(x) for x in item))
        else:
            print('  ✅ 通过')
    print()
    print('结论：%s（容差 %.1f 帧）' % ('✅ 全部通过' if ok else '❌ 有失败项',
                                        FULL_ANCHOR_TOL))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
