#!/usr/bin/env python
"""演示模式自检：假输入推着面板跑，帧数对不对、轴会不会按时到。

为什么单独一个检查：--demo-axis 是「没有游戏画面也能看刻度表」的唯一入口，
它要是自己算错了，看刻度表的人会把错误当成界面的问题。这里用**假钟**逐拍推演，
所以不依赖真实时间，跑完只要一秒多点。

检查什么：
    1. 倍速 -> 帧率的换算：1X 一秒 30 帧、2X 60、子弹时间恒 6（和倍速设置无关）；
    2. 满费那档真的走了挂钟通道（mode = clock），而且按**带小数**的读数算下来不多不少；
    3. 暂停窗口里帧号一动不动，恢复后也不补帧、不丢帧；
    4. 回费速率 60 帧/费时，周期长度实测就是 60 帧；
    5. 布局命中的是实测校准表（不是推算值）；
    6. 演示轴解析出 16 条，并且计数器走到每一帧时提词器都刚好触发那一条
       （这是「刻度表在正确的帧往下滚」的前一半，后一半在 tools/check_scale.py）。

两个「看数据才知道」的地方，写在断言里免得以后误判：
    * 满费走挂钟时，整数帧号会 0/2 交替跳（state.Counter._clock 攒的余量够了才吐一帧），
      精确值是 elapsed_f —— 所以这里验的是 elapsed_f，不是整数。
    * 费用周期边界上计数器会把两帧并成一次 +2（mode = wrap-missed），
      所以轴可能晚 1 帧才触发，但**绝不会提前**。

用法：python tools/check_demo.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.demo import DEMO_AXIS_TEXT, DEMO_SIZE, DemoMonitor   # noqa: E402
from akframe.geometry import measured_for                        # noqa: E402
from akframe.state import BULLET_FPS, LOGICAL_FPS                # noqa: E402
from akframe.timeline import Teleprompter, parse_lines           # noqa: E402

FAIL = []


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


class FakeClock:
    """假挂钟：推一下走一步。演示里所有时间都从它来，所以能瞬间跑完几秒钟。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def step(self, dt):
        self.t += dt


class Runner:
    """按假钟推着一个 DemoMonitor 走，并能「走到第 t 秒」。"""

    def __init__(self, rate=120.0, **kw):
        self.clock = FakeClock()
        self.mon = DemoMonitor(**kw, rate=rate, clock=self.clock)
        self.dt = 1.0 / rate

    def to(self, t_end):
        while self.mon._vt < t_end - 1e-9:
            self.clock.step(self.dt)
            self.mon._sample()
        return self.mon

    def run(self, seconds):
        return self.to(self.mon._vt + seconds)


def measure(seconds_warm, seconds_meas, **kw):
    """预热一会儿再量一段：躲开起锚那一拍，量出来的才是稳定速率。"""
    r = Runner(**kw)
    r.run(seconds_warm)
    i0, f0 = r.mon.counter.elapsed, r.mon.counter.elapsed_f
    r.run(seconds_meas)
    last = r.mon.latest.result
    return (r.mon.counter.elapsed - i0, r.mon.counter.elapsed_f - f0, last, r)


# ---- 1. 倍速 -> 帧率 ----
for kw, want, name in (
        ({}, 90, '1X：3 秒 90 帧'),
        ({'speed': 2.0}, 180, '2X：3 秒 180 帧'),
        ({'speed': 0.5}, 45, '0.5X：3 秒 45 帧'),
        ({'bullet': True}, 18, '子弹时间：3 秒 18 帧（恒 6 帧/秒）'),
        ({'bullet': True, 'speed': 2.0}, 18,
         '子弹时间顶掉倍速设置：2X 下也是 6 帧/秒（那是 1/5 倍逻辑帧速，不是 2X/5）'),
):
    got, _, _, _ = measure(2.0, 3.0, **kw)
    check(name, got == want, '实得 %d' % got)

# ---- 2. 满费：走挂钟通道，按带小数的读数算 ----
got_i, got_f, last, _ = measure(2.0, 3.0, maxed=True)
check('满费：3 秒 90 帧（按 elapsed_f 算，不多不少）', abs(got_f - 90.0) < 1e-6,
      '实得 +%.3f（整数帧号 %+d —— 挂钟通道把不足 1 帧的余量攒着，'
      '整数位在 0/2 之间跳，精确值只有小数位有）' % (got_f, got_i))
check('满费走的是挂钟通道', last is not None and last.mode.startswith('clock'),
      '通道 %s' % (last.mode if last else '—'))
got_i, got_f, _, _ = measure(2.0, 3.0, maxed=True, bullet=True)
check('满费 + 子弹时间：3 秒 18 帧', abs(got_f - 18.0) < 1e-6 and got_i == 18,
      'elapsed_f +%.3f' % got_f)

# ---- 3. 暂停：窗口里一动不动，恢复后不补也不丢 ----
r = Runner(pause_every=1.0, pause_len=0.4)
r.to(1.30)                       # 落在 1.0..1.4 那个暂停窗口里
e1 = r.mon.counter.elapsed
res = r.mon.latest.result
check('暂停中面板读数是「已暂停」', res is not None and res.mode == 'pause',
      '通道 %s' % (res.mode if res else '—'))
r.to(1.39)
check('暂停窗口里帧号一动不动', r.mon.counter.elapsed == e1,
      '%d -> %d' % (e1, r.mon.counter.elapsed))
r.to(2.00)                       # 跑完这个 0.6 秒的运行窗口
check('恢复后不补也不丢：0.6 秒游戏时间 = 18 帧（±1 量化）',
      17 <= r.mon.counter.elapsed - e1 <= 18, '实得 %+d' % (r.mon.counter.elapsed - e1))

# ---- 4. 回费速率 60 帧/费：2 秒一个周期 ----
got, _, _, r = measure(2.0, 4.0, cost_rate=60.0)
check('回费速率 60 帧/费：4 秒 120 帧', got == 120, '实得 %d' % got)
n_cyc, med, lo, hi = r.mon.counter.cycle_stats()
check('实测周期长度就是 60 帧', n_cyc >= 1 and med == 60,
      '周期数 %d 中位 %d 范围 %d..%d' % (n_cyc, med, lo, hi))

# ---- 5. 布局来自实测校准表 ----
rec, how = measured_for(*DEMO_SIZE)
_, _, _, r = measure(0.5, 0.5)
check('默认尺寸命中实测校准表', rec is not None,
      '%dx%d 条槽 %.1f px（%s）'
      % (DEMO_SIZE[0], DEMO_SIZE[1], r.mon.layout.bar_width_f, how))

# ---- 6. 演示轴：提词器跟着计数器走，一条都不落 ----
tl = parse_lines(DEMO_AXIS_TEXT.splitlines(), source='(演示轴)')
check('演示轴解析出 16 条', len(tl.cues) == 16, '实得 %d 条' % len(tl.cues))
check('演示轴没有解析告警', not tl.warnings, '；'.join(tl.warnings[:2]))

tp = Teleprompter(tl)
frames = [c.frame for c in tl.cues]
fired = []
r = Runner()
last_elapsed = -1
while r.mon.counter.elapsed < frames[-1] + 30:
    r.run(1.0 / 120.0)
    res = r.mon.latest.result
    if res is None or res.elapsed == last_elapsed:
        continue
    last_elapsed = res.elapsed
    for c in tp.update(res.elapsed).newly:
        fired.append((res.elapsed, c.frame))
check('每一条轴都被触发（一条不落）', [f for _, f in fired] == frames,
      '触发 %d 条' % len(fired))
late = [e - f for e, f in fired]
check('触发时刻不早于轴上的帧号，且最多晚 1 帧',
      bool(late) and min(late) >= 0 and max(late) <= 1,
      '晚 %d..%d 帧（晚 1 帧只出现在费用周期边界上：计数器会把两帧并成一次 +2，'
      '见 state.py 的 wrap-missed）' % (min(late), max(late)))
check('跑完之后提词器的 done 走到最后一条', tp.done == len(frames), 'done=%d' % tp.done)

# ---- 7. 还没到第一个逻辑帧就不该有输出（第一拍 dt=0） ----
r = Runner()
before = r.mon.latest.result
r.clock.step(r.dt)
r.mon._sample()
check('第一拍（dt=0）不产生帧',
      before is None and r.mon.latest.result is None and r.mon.counter.elapsed == 0,
      'elapsed=%d' % r.mon.counter.elapsed)

print()
if FAIL:
    print('%d 项没过：%s' % (len(FAIL), '；'.join(FAIL)))
    sys.exit(1)
print('全部通过。BULLET_FPS=%.1f  LOGICAL_FPS=%.1f' % (BULLET_FPS, LOGICAL_FPS))
