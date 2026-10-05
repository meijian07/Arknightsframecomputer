#!/usr/bin/env python
"""回费速率识别自检（减费环境支持）。

三件事，各自防一类错：

1. 定点累加器与「边界周期表」对得上（tools 之外，fp24 的 required 那条路）
   前人文档 docs/cost-bar-boundary-cycle.md 用黑盒测试给出了各档速率下「第几个周期
   开始多 1 帧」的表。我们的 fp24.required_from_frames（战前 tag 改的是 required 而
   不是 speed）必须逐项复现它，否则「一费几帧」在战场上会一帧一帧地漂。

2. 合成台阶能认出档位
   按游戏真实的相位->宽度曲线造一条带抖动/跳变的台阶，看 infer_rate 认不认得出。
   抖动与跳变是照着实测录像调的（±1px、偶发丢一帧）。

3. 认不出来时要诚实
   50 / 75 帧每费这种游戏里不存在的档位，必须报 unknown，而不是硬落到 60 / 90 上。
   错落一档会让绝对帧号整段漂移，比不认更糟。

4.（有实测录像缓存时）拿两段真录像验一遍：50% 减费 -> 60，75% 减费 -> 120。

5. 回费速率设置项（手动档）
   「一费几帧」改成玩家在设置面板里手填（写 cost_rate.json），自动识别降级成可选
   功能。所以这里要钉住：设置文件读写稳（坏文件/缺字段一律退回 30，不抛异常）、
   手动档真的落到 Counter 上、切回 'auto' 能切回去。

用法：python tools/check_cost_rate.py
"""

import io
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.cost_rate import (CANDIDATE_RATES, RateEstimator,  # noqa: E402
                               collect_cycles, infer_rate, model_levels)
from akframe.fp24 import (SPEED_NORMAL, CostTiming,  # noqa: E402
                          required_from_frames)
from akframe.synthesis import synthesized_width  # noqa: E402

BAR_W = 240
BAR_FRAC = 239.8125

# 前人文档里的边界周期表：档位 -> (基准周期帧数, 第几个周期第一次多 1 帧, 位置容差)
# 容差：文档自己写了「实际边界帧数目前仅能锁定在 [312, 315] 区间内」，是黑盒观测的
# 近似值；39 帧/费那档文档写「39*8, 40」（第 9 个周期），fp24 精确解给的是第 8 个。
# 其余各档两者逐项吻合，所以只给 39 留 ±1 的容差。
BOUNDARY_TABLE = {
    30: (30, 11, 0),
    39: (39, 9, 1),
    40: (40, 8, 0),
    45: (45, 7, 0),
    60: (60, 6, 0),
    120: (120, 3, 0),
}


def cycle_lengths_for(frames_per_cost: int, cycles: int = 16) -> list:
    """用 fp24 的 required 跑一遍，返回逐费用周期的帧数。"""
    timing = CostTiming(required_from_frames(frames_per_cost))
    out = []
    since = 0
    guard = 0
    while len(out) < cycles and guard < 10 ** 6:
        guard += 1
        since += 1
        if timing.advance_one_frame(SPEED_NORMAL):
            out.append(since)
            since = 0
    return out


def boundary_cycle_table():
    """fp24 的 required 必须复现前人文档的边界周期表。

    检查两件事：逐周期的帧数恒在 {基准, 基准+1} 之内；第一次多 1 帧的位置落在文档
    给的位次附近（容差见 BOUNDARY_TABLE 的注释）。
    """
    bad = []
    for fpc, (base, extra_at, tol) in BOUNDARY_TABLE.items():
        got = cycle_lengths_for(fpc)
        if any(v not in (base, base + 1) for v in got):
            bad.append((fpc, got[:extra_at + 3], '周期长度只能是 %d / %d' % (base, base + 1)))
            continue
        # 第一次出现 base+1 的位次（1 起数）
        pos = next((i + 1 for i, v in enumerate(got) if v == base + 1), None)
        if pos is None or abs(pos - extra_at) > tol:
            bad.append((fpc, '第 %s 个周期第一次 +1' % pos, '第 %d 个（±%d）' % (extra_at, tol)))
    return bad


def synth_cycle(frames_per_cost: int, rng, hold: int = 20,
                p_spike: float = 0.05, p_drop: float = 0.01):
    """照游戏真实曲线造一条带噪声的完整台阶（含回费那一帧画满、随后归零）。

    hold      每个逻辑帧在录像里重复多少拍（逐帧推进的录像就是这样，一拍一格）
    p_spike   一个逻辑帧偶尔被读高 1px 的概率（实测抖动）
    p_drop    一个逻辑帧偶尔被漏采的概率
    """
    fills, states = [], []
    for j in range(1, frames_per_cost + 1):
        if rng.random() < p_drop:
            continue
        w = synthesized_width(BAR_W, BAR_FRAC, j / float(frames_per_cost))
        if rng.random() < p_spike:
            w += 1
        state = 'TRACK' if w > 0 else 'EMPTY'
        fills.extend([w] * hold)
        states.extend([state] * hold)
    fills.extend([BAR_W] * hold)          # 回费帧把条画满
    states.extend(['TRACK'] * hold)
    fills.extend([0] * 5)                 # 下一帧才归零
    states.extend(['EMPTY'] * 5)
    return fills, states


def synthetic_rate_recovery(trials: int = 7, seed: int = 11):
    """每个已知档位造若干条合成台阶，必须认对。"""
    rng = random.Random(seed)
    bad = []
    for fpc in CANDIDATE_RATES:
        got = []
        for _ in range(trials):
            fills, states = synth_cycle(int(fpc), rng)
            est = infer_rate(collect_cycles(fills, states, BAR_W), BAR_W, BAR_FRAC, BAR_W)
            got.append(est.frames_per_cost)
        if any(g != fpc for g in got):
            bad.append((fpc, got))
    return bad


def nonstandard_is_honest(seed: int = 23):
    """游戏里不存在的档位必须报 unknown，不能硬落档。"""
    rng = random.Random(seed)
    bad = []
    for fpc in (50, 75, 100):
        for _ in range(5):
            fills, states = synth_cycle(fpc, rng)
            est = infer_rate(collect_cycles(fills, states, BAR_W), BAR_W, BAR_FRAC, BAR_W)
            if est.frames_per_cost is not None:
                bad.append((fpc, est.frames_per_cost, est.conf))
                break
    return bad


def model_levels_monotone():
    """模型种类数必须随档位单调不减 —— infer_rate 的连续反推靠这条性质。"""
    bad = []
    prev = 0
    for c in range(15, 181):
        cur = len(model_levels(BAR_W, BAR_FRAC, c, BAR_W))
        if cur < prev:
            bad.append((c, prev, cur))
        prev = cur
    return bad


def estimator_online_matches_batch(seed: int = 31):
    """在线收集器（实时链路用）和离线一遍跑（离线链路用）结论要一致。"""
    rng = random.Random(seed)
    bad = []
    for fpc in (30, 60, 120):
        for _ in range(3):
            fills, states = synth_cycle(fpc, rng)
            est = infer_rate(collect_cycles(fills, states, BAR_W), BAR_W, BAR_FRAC, BAR_W)
            online = RateEstimator(BAR_W, BAR_FRAC, BAR_W)
            for f, s in zip(fills, states):
                online.feed(f, s)
            if online.estimate.frames_per_cost != est.frames_per_cost:
                bad.append((fpc, est.frames_per_cost, online.estimate.frames_per_cost))
    return bad


def real_recordings():
    """有实测录像缓存时，验两段减费录像（50% -> 60、75% -> 120）。

    缓存目录 out/cache/<录像名>_<ROI 指纹>/ 是一次性抽流的产物（bar.raw / meta.json），
    这里直接读它，不需要 ffmpeg，也不需要先跑 run_reader.py。没有缓存就跳过。
    返回 (结果列表, 跳过原因)。
    """
    import json

    from akframe.capture import iter_cached
    from akframe.features import read_bar
    from akframe.geometry import Layout

    cases = [
        # 减费环境（用户专门录的：暂停着逐帧推完一整个费用周期）
        ('2026-10-04 22-18-05_c2558x1494_om5_45', 60),   # 回费速度 -50%
        ('2026-10-04 22-20-20_c2558x1494_om5_45', 120),  # 回费速度 -75%
        # 普通 1 倍速关卡：这里最要紧的是**别认错**。以前没这道自检时，
        # 19-36-06（帧操录像）被那条 6->13->14->15 的渲染过渡台阶数成 36 种宽度，
        # 于是误判成 39 帧/费，帧数从 31 变成 40。
        ('2026-10-04 19-36-06_c2558x1494_om5_45', 30),
        ('2026-10-04 19-08-39_c2558x1494_om5_45', 30),
        ('2026-10-04 17-01-38_c2558x1440_om2_45', 30),
        ('2026-10-04 20-02-06_c2558x1494_om5_45', 30),
        ('2026-10-04 15-54-49_c2558x1440_om2_45', 30),
    ]
    res = []
    for cache_name, want in cases:
        d = os.path.join(ROOT, 'out', 'cache', cache_name)
        if not os.path.isfile(os.path.join(d, 'meta.json')):
            return [], '没有实测录像缓存 out/cache/%s' % cache_name
        with open(os.path.join(d, 'meta.json'), encoding='utf-8') as fh:
            lay = Layout.from_dict(json.load(fh)['layout'])
        fills, states = [], []
        for fr in iter_cached(d):
            bar = read_bar(fr.bar, lay)
            fills.append(bar.fill)
            states.append(bar.state)
        est = infer_rate(collect_cycles(fills, states, lay.model_bar_w),
                         lay.model_bar_w, lay.bar_width_f, lay.bar.w)
        res.append((cache_name, want, est.frames_per_cost, est.conf, est.median_levels))
    return res, ''


def settings_round_trip():
    """设置文件（回费速率手动档）的读写必须稳。

    为什么这么在意坏文件：界面启动时就读它，一个坏文件不能让前端打不开；
    用户在输入框里也什么都可能敲进来，所以夹取要兜住。
    返回失败项列表。
    """
    import json
    import shutil
    import tempfile

    from akframe.settings import (DEFAULT_FRAMES_PER_COST, clamp_frames_per_cost,
                                  format_frames_per_cost, load_cost_rate, save_cost_rate)
    bad = []
    if abs(DEFAULT_FRAMES_PER_COST - 30.0) > 1e-9:
        bad.append(('默认档', DEFAULT_FRAMES_PER_COST, 30.0))
    d = tempfile.mkdtemp(prefix='akframe-rate-')
    try:
        p = os.path.join(d, 'cost_rate.json')
        if abs(load_cost_rate(p) - 30.0) > 1e-9:
            bad.append(('文件不存在', load_cost_rate(p), 30.0))
        save_cost_rate(p, 60.0)
        if abs(load_cost_rate(p) - 60.0) > 1e-9:
            bad.append(('存 60 再读', load_cost_rate(p), 60.0))
        for raw, why in (('{ 这不是 json', '坏 json'), ('{"frames_per_cost": -5}', '负值'),
                         ('{"frames_per_cost": "abc"}', '不是数'), ('[1, 2]', '不是对象')):
            with io.open(p, 'w', encoding='utf-8') as fh:
                fh.write(raw)
            if abs(load_cost_rate(p) - 30.0) > 1e-9:
                bad.append((why, load_cost_rate(p), 30.0))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # 夹取：输入框里什么都可能被敲进来
    for raw, want in ((-1, 30.0), (0, 30.0), ('abc', 30.0), (None, 30.0),
                      (1e9, 300.0), (45, 45.0), ('60', 60.0)):
        got = clamp_frames_per_cost(raw)
        if abs(got - want) > 1e-9:
            bad.append(('夹取 %r' % (raw,), got, want))
    if not format_frames_per_cost(60).startswith('60 帧/费'):
        bad.append(('文案', format_frames_per_cost(60), '60 帧/费（2.00 秒 1 费）'))
    return bad


def manual_rate_applies():
    """手动档要真的落到 Counter 上；切回 'auto' 要能切回去。

    本轮主线：默认 = 手动档 30，自动识别降级成可选功能（界面上的「自动识别」按钮、
    命令行的 --cost-rate auto）。造一个不带窗口的 LiveMonitor 就能验完这条链。
    返回失败项列表。
    """
    from akframe.geometry import layout_for
    from akframe.live import LiveMonitor
    lay = layout_for(2560, 1600, 1.0)
    bad = []
    mon = LiveMonitor(lay, cost_rate=60.0)
    if abs(mon.counter.frames_per_cost - 60.0) > 1e-9:
        bad.append(('按 60 建', mon.counter.frames_per_cost, 60.0))
    if mon.rate_est is not None or not mon.rate_locked:
        bad.append(('手工档不该建识别器 / 应当直接锁死',
                    (mon.rate_est, mon.rate_locked), (None, True)))
    mon.set_cost_rate(120.0)
    if abs(mon.counter.frames_per_cost - 120.0) > 1e-9:
        bad.append(('切 120', mon.counter.frames_per_cost, 120.0))
    mon.set_cost_rate('auto')
    if mon.rate_est is None or mon.rate_locked:
        bad.append(('auto 要建识别器且不预锁',
                    (mon.rate_est, mon.rate_locked), ('RateEstimator', False)))
    if abs(mon.counter.frames_per_cost - 30.0) > 1e-9:
        bad.append(('auto 认出来之前按 30 算', mon.counter.frames_per_cost, 30.0))
    mon.set_cost_rate(45.0)
    if abs(mon.counter.frames_per_cost - 45.0) > 1e-9 or mon.rate_est is not None:
        bad.append(('从 auto 切回手工档', (mon.counter.frames_per_cost, mon.rate_est),
                    (45.0, None)))
    if abs(LiveMonitor(lay).counter.frames_per_cost - 30.0) > 1e-9:
        bad.append(('不传参数的默认档', LiveMonitor(lay).counter.frames_per_cost, 30.0))
    return bad


def main():
    ok = True
    bad = boundary_cycle_table()
    print('边界周期表（fp24 required 复现前人文档）：')
    if bad:
        ok = False
        for fpc, got, want in bad:
            print('  ❌ %d 帧/费：实际 %s，期望 %s' % (fpc, got, want))
    else:
        print('  ✅ 通过（30/39/40/45/60/120 的「第几个周期多 1 帧」逐项吻合）')

    bad = model_levels_monotone()
    print()
    print('模型种类数单调性：')
    if bad:
        ok = False
        for b in bad:
            print('  ❌ 档位 %d 反而比 %d 少：%d -> %d' % (b[0], b[0] - 1, b[1], b[2]))
    else:
        print('  ✅ 通过（15..180 单调不减，连续反推不会来回跳）')

    bad = synthetic_rate_recovery()
    print()
    print('合成台阶认档位（带 ±1px 抖动与偶发漏采）：')
    if bad:
        ok = False
        for fpc, got in bad:
            print('  ❌ 真值 %g 帧/费，认成 %s' % (fpc, got))
    else:
        print('  ✅ 通过（%s 全部认对）' % '/'.join('%g' % c for c in CANDIDATE_RATES))

    bad = nonstandard_is_honest()
    print()
    print('非标档位必须诚实报 unknown：')
    if bad:
        ok = False
        for b in bad:
            print('  ❌ 真值 %g，却认成 %g（%s）' % b)
    else:
        print('  ✅ 通过（50 / 75 / 100 帧每费一律 unknown，不硬落档）')

    bad = estimator_online_matches_batch()
    print()
    print('在线收集器 vs 离线一遍跑：')
    if bad:
        ok = False
        for b in bad:
            print('  ❌ 真值 %g：离线 %s，在线 %s' % b)
    else:
        print('  ✅ 通过（两者结论一致）')

    bad = settings_round_trip()
    print()
    print('回费速率设置文件：')
    if bad:
        ok = False
        for b in bad:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（存 60 读回 60；坏 json / 负值 / 不是数 / 不是对象 一律退回 30；'
              '夹取 1..300）')

    bad = manual_rate_applies()
    print()
    print('手动档落到状态机（默认 30，自动识别可选）：')
    if bad:
        ok = False
        for b in bad:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（60 / 120 / 45 都能立刻生效，切 auto 建识别器并退回按 30 算）')

    res, why = real_recordings()
    print()
    print('实测录像：')
    if why:
        print('  ⏭  跳过（%s）' % why)
    else:
        for name, want, got, conf, med in res:
            mark = '✅' if got == want else '❌'
            if got != want:
                ok = False
            print('  %s %s：期望 %d，认出 %s（%s）；数出 %.0f 种宽度'
                  % (mark, name, want, got, conf, med))

    print()
    print('结论：%s' % ('✅ 全部通过' if ok else '❌ 有失败项'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
