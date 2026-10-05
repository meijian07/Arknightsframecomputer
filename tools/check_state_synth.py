#!/usr/bin/env python
"""合成信号自检：用「数学上精确」的费用条喂状态机，检查计数有没有系统性偏差。

离线对比（compare_reference.py）验证的是「读得对不对」，check_live.py 验证的是
「实时链路和离线链路一致不一致」；这个脚本验证的是第三件事：**状态机本身的计数有没有
系统性偏差**。因为喂进去的信号是理想线性的，真值可以精确算出来：

    采样 60Hz、2 倍速 -> 每个采样恰好 1 个逻辑帧

**相位怎么走**：一费的 30 个逻辑帧对应相位 1/30, 2/30, ..., 29/30, 30/30(=1.0)，
也就是可见宽度 0, 8, 17, ..., 233, **240**（回费帧把条画满，下一帧才归零）。
这是从实测录像里数出来的：回卷处的宽度序列是 `... 223, 232, 240, 0, 8 ...`。
早先这里写成 `(phase % 30)/30`（回费帧宽度 0），等于给模型喂了一条
「有两个连续 0 宽帧、没有 240」的信号 —— 那和游戏不一样，自检也就测不出
回费帧匹配不上的问题。

期望误差 0 帧。合成信号必须按游戏真实的「相位 -> 可见宽度」曲线来画（synthesis.py），
不能假设条宽是线性的：那样喂进去的就不是游戏里的条，测出来的差异说明不了任何问题
（实测线性假设下多算 59 帧，换成本曲线后误差归零）。

这里主要防四类回归：
    1. 状态机丢余量（曾经换个通道就把余量清零，几十次换通道少算好几帧）；
    2. 相位重同步选错 k（比如在「头两帧条还没动」的平段上把 k 选大/选小）；
    3. 帧操时「回费后条归零的那一拍」不当场计数（要等条重新长出来才补 2 帧，
       实测晚 0.9 秒 —— 就是用户报的「第一帧记不到」）；
    4. 暂停期间条读不出来时凭空补帧（实测一段 76 秒暂停录像被多记 61 帧）；
    5. 漏采狠到跨过 1/4 个周期时相位模型把自己锁死（见 stale_phase_reanchor）。

用法：python tools/check_state_synth.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.features import BarReading, PauseReading, SpeedReading   # noqa: E402
from akframe.state import (ANCHOR_MAX_CATCHUP, REJECT_RESYNC_SECONDS,  # noqa: E402
                           Counter)
from akframe.synthesis import synthesized_width                       # noqa: E402

BAR_W = 120
RUN = PauseReading(ratio=0.30, running=True)
PAUSED = PauseReading(ratio=0.05, running=False)
SPEED = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=2.0,
                     bullet=False, conf='ok')


def phase_frac(n: float, per: float = 30.0) -> float:
    """第 n 个逻辑帧应该画在什么相位上（1/per .. per/per，循环）。

    实测：回费那一帧相位是 1.0（条画满 = 宽 240），下一帧才回到 1/per（宽 0）。
    per = 一费几个逻辑帧：普通关卡 30，回费速度 -50% 是 60，-75% 是 120。
    """
    return (((n - 1) % per) + 1) / float(per)


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
                # 暂停时游戏把条**冻在原地**（实测 2026-10-04 19-36-06：暂停菜单里费用条
                # 照常画着、宽度一动不动），不是「条消失」。所以暂停段喂当前相位的那根条，
                # 真值不前进。喂 EMPTY 不像游戏，还会把「回费后条被收起来那一帧」的判据搅进来。
                fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase)) if phase > 0 else 0
                c.step(idx, t, bar(fill, 'TRACK' if fill > 4 else 'EMPTY'), PAUSED, SPEED)
                continue
            phase += 1.0
            if mode == 'max':
                c.step(idx, t, bar(0, 'MAX'), RUN, SPEED)
            else:
                # 按游戏真实渲染画条：相位 -> 可见宽度（含头两帧的死区与尾端 clamp）
                fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase))
                c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED)
            truth += 1
    return c.elapsed, truth


def cycle_lengths(frames=3000, fps=60.0, speed_m=2.0, frames_per_cost=30.0):
    """连续跑（全速、无暂停）时，逐费用周期各走了几个逻辑帧。

    这是「一费 = 几帧」唯一不依赖任何帧率/倍速假设的口径：
    普通关卡应当是 30，偶尔 31 —— 定点累加器的取整误差每 11 个周期攒够一次，
    所以第 11 费会多花一帧（见 fp24.py 的说明）。带上回费减速 tag 时是 60 / 39 / 45 / 120。
    """
    c = Counter(BAR_W, fps=fps, frames_per_cost=frames_per_cost)
    dt = 1.0 / fps
    t = 0.0
    phase = 0.0
    sp = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=speed_m,
                      bullet=False, conf='ok')
    for i in range(1, frames + 1):
        t += dt
        phase += 1.0
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase, frames_per_cost))
        c.step(i, t, bar(fill, 'TRACK'), RUN, sp)
    return c.cycle_lengths


def frame_step_precision(steps=300, fps=60.0):
    """帧操（暂停着一下一下推）时，每一拍都必须**当场** +1 —— 包括回费后条归零的那一拍。

    实测录像 `2026-10-04 19-36-06`（2560x1600@60fps，客户区 2558x1494 @(-5,45)）：
    条走满之后游戏会把条收起来一拍做回费动画，那一帧读出来是 fill=0 / EMPTY。
    旧逻辑把它归到「暂停 + 空条 -> 不计数」，于是回费边界的两帧要攒到条重新长出来
    才一次补上（这次晚了 55 个视频帧 = 0.9 秒），打轴对不上，用户看到的就是
    「第一帧记不到」。

    返回「没有当场记 1 帧」的那些拍（空列表 = 通过）。
    """
    c = Counter(BAR_W, fps=fps)
    dt = 1.0 / fps
    t = 0.0
    phase = 0
    started = False
    missed = []
    for i in range(1, steps + 1):
        t += dt
        phase += 1
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase))
        r = c.step(i, t, bar(fill, 'TRACK' if fill > 4 else 'EMPTY'), PAUSED, SPEED)
        if not started:
            # 起锚那一拍单独看（见 anchor_head_catchup）：它要补上「费用周期开头
            # 还没长出像素的那几帧」，所以不是恒等于 1。
            if r.mode != 'idle':
                started = True
            continue
        if r.delta != 1:
            missed.append((i, fill, r.bar_state, r.mode, r.delta))
    return missed


def anchor_head_catchup(fps=60.0):
    """起锚那一拍要把「费用周期开头还没长出像素的帧」补上。

    条在周期第 1 帧画出来宽度是 0 —— 和关卡外根本没画条长得一模一样，所以第一次
    读到条时通常已经是第 2 帧（实测 fill=6/7/8）或第 3 帧（fill=15）。老逻辑起锚
    一律记 0 帧，于是「费用条空（第 0 帧）到出现第一个像素（第 1 帧）」那一帧被吞掉，
    整段录像的帧号从此差 1。现在起锚这一拍补 (n0 - 1) 帧（n0 = 起锚落在周期第几帧）。

    返回失败项列表（每个元素：(起锚相位, (mode, delta), 期望 delta, 一周期实测帧数)）。
    """
    bad = []
    for n0 in (2, 3, 4, 16):
        c = Counter(BAR_W, fps=fps)
        dt = 1.0 / fps
        t = 0.0
        idx = 0
        for _ in range(5):        # 关卡外待几拍：条没画出来
            idx += 1
            t += dt
            c.step(idx, t, bar(0, 'EMPTY'), RUN, SPEED)
        got, at_anchor, at_same = None, None, None
        for n in range(n0, n0 + 61):
            idx += 1
            t += dt
            fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n))
            r = c.step(idx, t, bar(fill, 'TRACK' if fill > 4 else 'EMPTY'), PAUSED, SPEED)
            if got is None:
                got = (r.mode, r.delta)
                at_anchor = c.elapsed
            if n == n0 + 30:
                at_same = c.elapsed
        want = n0 - 1 if n0 - 1 <= ANCHOR_MAX_CATCHUP else 0
        if got[1] != want:
            bad.append((n0, got, want, None))
        elif at_same - at_anchor != 30:
            bad.append((n0, got, want, at_same - at_anchor))
    return bad


def clear_zero_start(fps=60.0):
    """清零之后的第一拍起锚不许补帧（清零 = 「从这一刻起从 0 开始数」）。

    用户报的「清零计数功能异常」就是这里：清零时游戏停在暂停菜单里、条读数是本费
    第 2 帧（fill=7），老逻辑照「起锚补帧」把周期开头那 1~2 帧补上，于是数字从 0
    变成 1 并卡在 1 不动（录像 2026-10-05 11:01 的面板实测显示 1 卡了 6 秒）。

    三种情形：
        A 清零时界面没读到画面（条宽未知）—— 保守起见一律不补，数字一直 0；
        B 清零时条已经有像素（fill=第一个像素）—— 清零那一拍 delta=0
          （那 1 帧清零前就数过了），之后正常 +1；
        C 清零时条是空的（本费第 0 帧，fill=0）—— 清零之后走到「第一个像素」
          那 1 帧必须记（用户报的「第零帧到第一帧记不到」就是这里被吃掉的）。

    返回失败项列表（每项：(场景, 拍号, (mode, delta), 期望 delta, elapsed, 期望 elapsed)）。
    """
    bad = []
    dt = 1.0 / fps
    first_px = synthesized_width(BAR_W, float(BAR_W), phase_frac(2))

    c = Counter(BAR_W, fps=fps)
    c.mark_zero_start()                      # 等价于界面上的「清零计数」；条宽未知
    t, idx = 0.0, 0
    for k in range(20):
        idx += 1
        t += dt
        r = c.step(idx, t, bar(first_px, 'TRACK'), PAUSED, SPEED)
        if r.delta != 0 or c.elapsed != 0:
            bad.append(('A 暂停中清零（条宽未知）', k, (r.mode, r.delta), 0, c.elapsed, 0))
            break

    c = Counter(BAR_W, fps=fps)
    c.mark_zero_start(first_px)              # 清零时条已经在第一个像素上
    t, idx = 0.0, 0
    for k in range(40):
        idx += 1
        t += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(2 + k))
        r = c.step(idx, t, bar(fill, 'TRACK' if fill > 4 else 'EMPTY'), RUN, SPEED)
        want_d = 0 if k == 0 else 1
        if r.delta != want_d or c.elapsed != k:
            bad.append(('B 跑动中清零（条已在第一个像素）', k, (r.mode, r.delta),
                        want_d, c.elapsed, k))
            break

    c = Counter(BAR_W, fps=fps)
    c.mark_zero_start(0)                     # 清零时条是空的（本费第 0 帧）
    t, idx = 0.0, 0
    for k in range(40):
        idx += 1
        t += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(2 + k))
        r = c.step(idx, t, bar(fill, 'TRACK' if fill > 4 else 'EMPTY'), RUN, SPEED)
        want_e = k + 1                       # 第一个像素那一拍就要有 1 帧
        if r.delta != 1 or c.elapsed != want_e:
            bad.append(('C 清零时条空', k, (r.mode, r.delta), 1, c.elapsed, want_e))
            break
    return bad


def wrap_frame_sampled_or_missed(fps=60.0):
    """回费边界那两拍：归零帧「采到了」和「漏采了」必须能被分开。

    1 倍速下一个逻辑帧只有 33.3ms，采样一旦卡一下就会整帧没采到。两种情况的
    帧号总数都对（都是 +2），但落点不同：

        采到  ：240 -> 0（wrap,     +1）-> 6（pixel,     +1）
        漏采  ：240 ->            6（wrap-missed, +2）

    用户报的「有时候第 0 帧还是不读、第一帧补了两帧」就是后者。跑得快（≥120Hz）
    采样就基本不会发生；这个自检保证「真发生了也能看出来」，不会和普通漏帧混在一起。

    返回失败项列表。
    """
    bad = []
    for sampled in (True, False):
        c = Counter(BAR_W, fps=fps)
        dt = 1.0 / fps
        t = 0.0
        idx = 0
        # 先跑完一个周期，把相位推到 240（周期第 30 帧）
        for n in range(1, 31):
            idx += 1
            t += dt
            fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n))
            c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED)
        rows = []
        if sampled:
            idx += 1
            t += dt
            rows.append(c.step(idx, t, bar(0, 'EMPTY'), RUN, SPEED))   # 周期第 1 帧
        for n in (2, 3):
            idx += 1
            t += dt
            fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n))
            rows.append(c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED))
        got = [(r.mode, r.delta) for r in rows]
        want = [('wrap', 1), ('pixel', 1), ('pixel', 1)] if sampled \
            else [('wrap-missed', 2), ('pixel', 1)]
        if got != want:
            bad.append(('采到归零帧' if sampled else '漏采归零帧', got, want))
    return bad


def stale_phase_reanchor(fps=60.0):
    """漏采狠到跨过 1/4 个周期时，相位模型必须能自己解开死锁。

    病根：`_pixel` 只在「上一拍相位 > WRAP_MIN_PREV_PHASE(0.75)」时才允许跨回费边界
    （这道门是防「暂停时闪一下 fill=0 就绕一整圈」的）。万一某一拍漏采得太狠
    （缺口盖过 1/4 个周期），下一拍相位模型还停在旧周期末尾、画面已经进到新周期开头 ——
    于是每一拍都匹配失败、相位**再也不前进**。实测没有退路时会一路卡到「新周期的
    某个宽度偶然撞上卡住的旧宽度」（整整一个费用周期之后，白丢 30 帧）才自愈。

    修法：连续 REJECT_RESYNC_SECONDS 秒匹配不上，就认定模型本身不可信，按当前读数
    重新起锚（delta = 0）—— 丢掉的帧无法证明，宁可少记也不猜。这条退路必须满足三点：

      * 会自愈：重锚后一拍就恢复 +1；
      * 不自作聪明：自愈之前**一帧都不许凭空虚补**（delta 必须恒为 0）；
      * 不误伤：连续正常推进时一次都不能触发（否则每触发一次就丢一段帧）。

    返回失败项列表。
    """
    bad = []
    c = Counter(BAR_W, fps=fps)
    dt = 1.0 / fps
    t = 0.0
    idx = 0
    for n in range(1, 21):                       # 相位推到 20/30 = 0.667，够不到 0.75
        idx += 1
        t += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n))
        c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED)
    if abs(c.last_phase - 20 / 30.0) > 1e-5:      # fp24 定点有量化误差
        bad.append(('前置相位没推到 20/30', c.last_phase, 20 / 30.0))
    # 漏采：新周期第 1 帧整拍没被采到（缺口 = 12 个逻辑帧），从第 2 帧（fill=5）才读到
    recover_at = None
    invented = 0
    for j, n in enumerate(range(32, 32 + 15), start=1):
        idx += 1
        t += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n))
        r = c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED)
        if recover_at is None:
            if r.delta == 1:
                recover_at = j                      # 自愈：这一拍起恢复每拍 +1
            else:
                invented += r.delta                 # 自愈之前凭空补的帧
    limit = int(REJECT_RESYNC_SECONDS / dt) + 3      # 阈值是「严格超过」，末尾多两拍
    if invented:
        bad.append(('自愈之前凭空多记', invented, 0))
    if recover_at is None:
        bad.append(('一直没自愈（死锁）', None, '<=%d 拍内恢复' % limit))
    elif recover_at > limit:
        bad.append(('自愈太慢', recover_at, '<=%d 拍' % limit))
    # 对照：连续正常推进不该触发重锚，累计帧数也必须正好等于真值
    c2 = Counter(BAR_W, fps=fps)
    t2 = 0.0
    triggered = 0
    for i, n in enumerate(range(2, 122), start=1):
        t2 += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n))
        r = c2.step(i, t2, bar(fill, 'TRACK'), RUN, SPEED)
        if r.warn == 'phase-resync':
            triggered += 1
    if triggered:
        bad.append(('连续正常推进误触发重锚', triggered, 0))
    if c2.elapsed != 120:
        bad.append(('对照段累计帧数', c2.elapsed, 120))
    return bad


def pause_missing_bar(run=45, gap=240, fps=60.0):
    """暂停期间条读不出来（画面被遮住 / 切到菜单），暂停键还来回闪 —— 这段一帧都不能多记。

    对应实测踩过的坑：一段 76 秒的暂停录像被多记 61 帧（相位重同步在暂停期间凭空
    跨了一整个周期）。现在空条读数进像素通道的唯一入口是「上一拍条是满的」；
    这里让暂停正好落在半程（相位 15/30），必须一帧都不放行。
    返回暂停段里多记的帧数（0 = 通过）。
    """
    c = Counter(BAR_W, fps=fps)
    dt = 1.0 / fps
    t = 0.0
    phase = 0
    for i in range(1, run + 1):
        t += dt
        phase += 1
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase))
        c.step(i, t, bar(fill, 'TRACK'), RUN, SPEED)
    before = c.elapsed
    for k in range(gap):
        t += dt
        # 暂停键在闪：奇偶拍交替读成「运行中 / 已暂停」
        c.step(run + 1 + k, t, bar(0, 'EMPTY'), RUN if k % 2 else PAUSED, SPEED)
    return c.elapsed - before


def reduced_cost_rate_cycles():
    """带减费 tag 时（一费 60 / 120 逻辑帧），逐周期长度必须跟着变成 60 / 120。

    这是本轮的核心回归：曾经状态机不管三七二十一按 30 帧/费解释，一条 2 秒/费的
    条被数成半速（一费 30 帧），整段录像的绝对帧号直接减半。
    返回失败项列表。
    """
    bad = []
    for per in (60, 120):
        cl = cycle_lengths(frames=per * 60, fps=60.0, speed_m=2.0, frames_per_cost=per)
        if not cl:
            bad.append((per, '一个完整周期都没走完'))
            continue
        s_ = sorted(cl)
        med = s_[len(s_) // 2]
        if any(v < per or v > per + 1 for v in cl) or not (per <= med <= per + 1):
            bad.append((per, cl[:12], '期望 %d / %d' % (per, per + 1)))
    return bad


def rate_switch_rescale(fps=60.0):
    """跑动中认出减费速率时，**已经数出来的**帧要按新旧速率比例换算。

    为什么必须换算：速率是跑起来之后才认出来的，认出来之前那一段是按 30 帧/费数的。
    不换算的话整段录像的绝对帧号从头就偏 —— 打轴器要的正是绝对帧号。
    返回失败项列表。
    """
    bad = []
    c = Counter(BAR_W, fps=fps, frames_per_cost=30.0)
    dt = 1.0 / fps
    t, phase = 0.0, 0.0
    for i in range(1, 31):
        t += dt
        phase += 1.0
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase))
        c.step(i, t, bar(fill, 'TRACK'), RUN, SPEED)
    if c.elapsed != 30:
        bad.append(('换算前累计帧数', c.elapsed, 30))
    c.set_frames_per_cost(60.0)
    if c.elapsed != 60:
        bad.append(('换算后累计帧数', c.elapsed, 60))
    if c.elapsed != c.pixel_frames + c.clock_frames:
        bad.append(('elapsed 与分批计数不一致', c.elapsed,
                    c.pixel_frames + c.clock_frames))
    # 换算完相位模型也要跟着换（否则下一拍会按旧速率继续推）
    if abs(c.frames_per_cost - 60.0) > 1e-9:
        bad.append(('frames_per_cost', c.frames_per_cost, 60.0))
    return bad


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

    cl = cycle_lengths()
    cl_sorted = sorted(cl)
    med = cl_sorted[len(cl_sorted) // 2] if cl else 0
    lo = cl_sorted[0] if cl else 0
    hi = cl_sorted[-1] if cl else 0
    print()
    print('逐费用周期长度（连续 2X 跑 %d 个采样）：' % 3000)
    print('  完整周期 %d 个，中位 %d 帧，范围 %d..%d 帧' % (len(cl), med, lo, hi))
    print('  前 15 个：%s' % (cl[:15],))
    ok2 = len(cl) >= 90 and lo >= 30 and hi <= 31
    print('结论：%s' % ('✅ 通过（一费恒为 30 帧，偶尔 31 是定点取整的边界效应）'
                      if ok2 else '❌ 周期长度不对'))

    missed = frame_step_precision()
    print()
    print('帧操逐帧精度（暂停着一下一下推 300 拍）：')
    if missed:
        print('  ❌ 有 %d 拍没有当场记 1 帧，前 8 个 (拍号, fill, 状态, 通道, delta)：' % len(missed))
        for m in missed[:8]:
            print('     %s' % (m,))
    else:
        print('  ✅ 通过（含回费后条归零的那一拍，每一拍都当场 +1）')
    ok3 = not missed

    bad_wrap = wrap_frame_sampled_or_missed()
    print()
    print('回费边界两拍（归零帧采到 / 漏采）：')
    if bad_wrap:
        for b in bad_wrap:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（采到 -> wrap+1 / pixel+1；漏采 -> wrap-missed+2，总数一样）')
    ok6 = not bad_wrap

    bad_anchor = anchor_head_catchup()
    print()
    print('起锚补帧（条第一次出现时落在周期的第 2 / 3 / 4 / 16 帧）：')
    if bad_anchor:
        print('  ❌ 有 %d 项不对 (起锚相位, (mode, delta), 期望, 一周期帧数)：' % len(bad_anchor))
        for b in bad_anchor:
            print('     %s' % (b,))
    else:
        print('  ✅ 通过（第 2/3 帧补 1/2 帧；第 4/16 帧是半路重现，按 0 起锚；'
              '一个费用周期恒为 30 帧）')
    ok5 = not bad_anchor

    bad_clear = clear_zero_start()
    print()
    print('清零起步（只补清零之后才走的帧）：')
    if bad_clear:
        for b in bad_clear:
            print('  ❌ %s：第 %d 拍 (mode, delta)=%s，期望 delta=%s；elapsed=%s '
                  '期望 %s' % b)
    else:
        print('  ✅ 通过（条宽未知 -> 一直 0；条已在第一个像素 -> 那一拍 0、之后 +1；'
              '条空时清零 -> 第一个像素那拍就 +1）')
    ok10 = not bad_clear

    bad_stale = stale_phase_reanchor()
    print()
    print('漏采跨过 1/4 个周期（相位模型把自己锁死）后的自愈：')
    if bad_stale:
        for b in bad_stale:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（约 %.1fs 内重锚，自愈前 0 帧凭空补；连续推进不误触发）'
              % REJECT_RESYNC_SECONDS)
    ok7 = not bad_stale

    added = pause_missing_bar()
    print()
    print('暂停期间条读不出来（暂停键来回闪 240 拍）：多记 %d 帧' % added)
    print('结论：%s' % ('✅ 通过（一帧都没多记）' if added == 0 else '❌ 暂停期间凭空多记'))
    ok4 = added == 0

    bad_rate = reduced_cost_rate_cycles()
    print()
    print('减费环境逐周期长度（一费 60 / 120 逻辑帧）：')
    if bad_rate:
        for b in bad_rate:
            print('  ❌ %s：%s，%s' % b)
    else:
        print('  ✅ 通过（60 帧/费、120 帧/费都逐周期稳定，偶尔 +1 是定点取整的边界效应）')
    ok8 = not bad_rate

    bad_switch = rate_switch_rescale()
    print()
    print('跑动中认出速率后换算已数的帧：')
    if bad_switch:
        for b in bad_switch:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（30 -> 60 时已数的 30 帧换算成 60 帧，绝对帧号不从头偏）')
    ok9 = not bad_switch

    return 0 if (ok and ok2 and ok3 and ok4 and ok5 and ok6 and ok7
                 and ok8 and ok9 and ok10) else 1


if __name__ == '__main__':
    sys.exit(main())
