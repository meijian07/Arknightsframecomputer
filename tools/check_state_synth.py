#!/usr/bin/env python
"""合成信号自检：用「数学上精确」的费用条喂状态机，检查计数有没有系统性偏差。

离线对比（compare_reference.py）验证的是「读得对不对」，check_live.py 验证的是
「实时链路和离线链路一致不一致」；这个脚本验证的是第三件事：**状态机本身的计数有没有
系统性偏差**。因为喂进去的信号是理想线性的，真值可以精确算出来：

    采样 60Hz、2 倍速 -> 每个采样恰好 1 个逻辑帧

**相位怎么走**：一费的 30 个逻辑帧对应相位 1/30, 2/30, ..., 29/30, 0 —— 最后
那一帧（第 30 帧）就是回费那一帧，**它的条是空的**。所以一费的收尾在画面上是
`..., 236, 0, 0, 5, ...`：回费帧和它的后一帧都是 0 宽，条要到第 3 帧才重新长出来。

2026-10-15 拿 -50% 的实测录像（Desktop 20.43.51.02，客户区 2558x1440 @(16,45)）
逐帧核过：那条费用周期里条**从没画到过满宽**，最后可见宽度 236，下一拍整条消失、
同一个采样上费用数字 +1（拿数字位的二值图逐帧比对，只有那一帧的字形变了）。

早先这里按 `... 223, 232, 240, 0, 8 ...` 喂（回费帧画满、后一帧才归零）—— 那等于
把「回费前最后一帧」当成了回费帧，相位模型因此每费多算一帧：60 数成 61、120 数成
121，正是用户 2026-10-15 报的那个。

期望误差 0 帧。合成信号必须按游戏真实的「相位 -> 可见宽度」曲线来画（synthesis.py），
不能假设条宽是线性的：那样喂进去的就不是游戏里的条，测出来的差异说明不了任何问题
（实测线性假设下多算 59 帧，换成本曲线后误差归零）。

这里主要防四类回归：
    1. 状态机丢余量（曾经换个通道就把余量清零，几十次换通道少算好几帧）；
    2. 相位重同步选错 k（比如在「头两帧条还没动」的平段上把 k 选大/选小）；
    3. 帧操时「回费那一拍」不当场计数（那一拍的读数能从「上一拍画满」推出来，
       见 state.WRAP_MIN_PREV_PHASE；不记就要等条重新长出来才补，实测晚 0.9 秒 ——
       就是用户报的「第一帧记不到」）；
    4. 暂停期间条读不出来时凭空补帧（实测一段 76 秒暂停录像被多记 61 帧）；
    5. 漏采狠到跨过 1/4 个周期时相位模型把自己锁死（见 stale_phase_reanchor）；
    6. 减费档位（60 / 120 帧/费）与局内负费下，一拍走不到一帧时按挂钟猜帧 ——
       几十个周期攒下来就是几帧偏差（见 reduced_rate_accuracy）。

用法：python tools/check_state_synth.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.features import (EMPTY_MAX_FILL, BarReading, PauseReading,  # noqa: E402
                              SignReading, SpeedReading)
from akframe.fp24 import (SCALE, SPEED_NEGATIVE, SPEED_NORMAL, CostTiming,  # noqa: E402
                         required_from_frames)
from akframe.state import (ANCHOR_MAX_CATCHUP, BULLET_FPS,  # noqa: E402
                           LOGICAL_FPS, REJECT_RESYNC_SECONDS, Counter,
                           speed_for_cost_state)
from akframe.synthesis import synthesized_width                       # noqa: E402

BAR_W = 120
RUN = PauseReading(ratio=0.30, running=True)
PAUSED = PauseReading(ratio=0.05, running=False)
SPEED = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=2.0,
                     bullet=False, conf='ok')


def phase_frac(n: float, per: float = 30.0) -> float:
    """第 n 个逻辑帧应该画在什么相位上（1/per, 2/per, ..., (per-1)/per, 0）。

    实测（2026-10-15 回费 -50% 录像逐帧核过）：**回费那一帧条是空的**（相位 0），画满的那一帧
    在它前面（宽度已经顶到条槽）。一个费用周期的相位序列是 1/per .. (per-1)/per 再到 0，
    和计数器内部那个 k（从费用周期原点往前推了几帧）**一一对应**：这里的 n 就是 k，
    一个周期 per 帧里最后一帧（n = per）正是回费那一帧。所以只能用 n % per；
    用 (n - 1) % per 会把起锚 / 回费边界全部错一帧。
    per = 一费几个逻辑帧：普通关卡 30，回费速度 -50% 是 60，-75% 是 120。
    """
    return (n % per) / float(per)


def bar(fill, state):
    return BarReading(fill=fill, grey146=BAR_W if state == 'MAX' else 0, grey88=0,
                      state=state, row=np.zeros(BAR_W, np.int16))


def cover_bar(fill):
    """部署 / 拖拽遮罩：左端 fill 白条 + 右边全是 146 灰。"""
    return BarReading(fill=fill, grey146=BAR_W - fill, grey88=0,
                      state='MAX_COVER', row=np.zeros(BAR_W, np.int16))


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
    普通关卡应当是 30。定点累加器的取整误差本来会让第 11 费多花一帧，但 2026-10-18
    加入周期边界锚点后，每次条满 -> 条空都把帧号钉回整数个周期，所以这里必须恒为 30。
    带上回费减速 tag 时是 60 / 120。
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
    """帧操（暂停着一下一下推）时总帧数必须一帧不差，回费那一拍必须当场记账。

    实测录像 `2026-10-04 19-36-06`（2560x1600@60fps，客户区 2558x1494 @(-5,45)）：
    条走满之后游戏会把条收起来做回费动画。2026-10-15 又拿 -50% 的录像（客户区
    2558x1440 @(16,45)）逐帧核过一遍：**回费那一帧条就是空的（相位 0）**，它的
    **后一帧**相位只有 1/per、画出来还是 0 宽 —— 一费的收尾在画面上是
    `..., 236, 0, 0, 5, ...`，中间连着两拍读不出位置信息。

    旧逻辑把这连续两拍都归到「暂停 + 空条 -> 不计数」，要攒到条重新长出来才一次
    补上（实测晚了 55 个视频帧 = 0.9 秒），打轴对不上 —— 用户报的「第一帧记不到」。

    现在：**回费帧当场 +1**（读数从「上一拍画满」推得出来，见 state.WRAP_MIN_PREV_PHASE）；
    它后面那拍 0 宽帧没有位置信息，只能冻结，由再下一拍一次 +2 补齐。

    返回失败项列表（空 = 通过）。断言：
      * 每拍 delta 属于 {0,1,2}，从不为负、也不一次补 3 帧以上；
      * 一次补 2 帧只准接在「冻结过的那拍」后面；
      * 只有读不出位置信息的拍（宽度 <= EMPTY_MAX_FILL）才准冻结；
      * 跑完之后累计帧数**正好等于游戏走过的逻辑帧数**（一拍 = 一个逻辑帧）。
        最后这条是这次回归的要害：相位模型一旦把回费帧当成「画满」，这里就会多 1。
    """
    c = Counter(BAR_W, fps=fps)
    game = CostTiming(SCALE)
    dt = 1.0 / fps
    t = 0.0
    bad = []
    prev_delta = 0
    idx = 0
    fill = 0
    for _ in range(steps):
        idx += 1
        t += dt
        game.advance_one_frame(SPEED_NORMAL)
        fill = synthesized_width(BAR_W, float(BAR_W), game.phase())
        r = c.step(idx, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                   PAUSED, SPEED)
        if not 0 <= r.delta <= 2:
            bad.append((idx, fill, r.mode, r.delta, 'delta 只能是 0/1/2'))
        elif r.delta == 2 and prev_delta != 0:
            bad.append((idx, fill, r.mode, r.delta, '只有补「冻结过的那拍」时准一次 +2'))
        elif r.delta == 0 and fill > EMPTY_MAX_FILL and idx > 5:
            bad.append((idx, fill, r.mode, r.delta, '读数里有位置信息就不该冻结'))
        prev_delta = r.delta
    # 收尾：最后几拍可能正好停在 0 宽帧上（它要等下一拍才补），喂到条重新画出来为止
    while fill <= EMPTY_MAX_FILL:
        idx += 1
        t += dt
        game.advance_one_frame(SPEED_NORMAL)
        fill = synthesized_width(BAR_W, float(BAR_W), game.phase())
        c.step(idx, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
               PAUSED, SPEED)
    if c.elapsed != idx:
        bad.append((idx, None, '累计帧数', c.elapsed, idx))
    return bad


def anchor_head_catchup(fps=60.0):
    """起锚那一拍要把「费用周期开头还没长出像素的帧」补上。

    条在周期第 1 帧画出来宽度是 0 —— 和关卡外根本没画条长得一模一样，所以第一次
    读到条时通常已经是第 2 帧（实测 fill=6/7/8）或第 3 帧（fill=15）。老逻辑起锚
    一律记 0 帧，于是「费用条空（第 0 帧）到出现第一个像素（第 1 帧）」那一帧被吞掉，
    整段录像的帧号从此差 1。现在起锚这一拍补 **n0 帧**（n0 = 起锚落在周期第几帧，
    也就是「含这一帧在内，本费已经走了几帧」—— 和 _start 停在累加器上的那个相位一致）。

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
        cap = max(ANCHOR_MAX_CATCHUP, c._unreadable_head_frames() + 2)
        want = n0 if n0 <= cap else 0
        if got[1] != want:
            bad.append((n0, got, want, None))
        elif want and at_anchor != n0:   # 补了帧的场合，elapsed 就该是本费走到的帧号
            bad.append((n0, got, 'elapsed=%d' % n0, at_anchor))
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
          那几帧必须记（用户报的「第零帧到第一帧记不到」就是这里被吃掉的）。
          注意第一个像素落在**本费第 2 帧**（第 1 帧画出来是 0 宽），所以清零后
          第一次读到条时 elapsed 该是 2，不是 1。

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
    # 只喂到本费第 29 帧：再往后就跨回费边界了（第 30 帧是归零帧），
    # 边界那两拍的行为由 frame_step_precision / wrap_frame_sampled_or_missed 专门覆盖。
    for k in range(28):
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
    for k in range(28):                      # 同上：不跨回费边界
        idx += 1
        t += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(2 + k))
        r = c.step(idx, t, bar(fill, 'TRACK' if fill > 4 else 'EMPTY'), RUN, SPEED)
        # 这一拍画面是「本费第 2 + k 帧」：清零时条是空的（第 0 帧），之后第 1、2 帧
        # 都真的走过了，所以 elapsed 从 2 起步。
        want_e = k + 2
        want_d = 2 if k == 0 else 1
        if r.delta != want_d or c.elapsed != want_e:
            bad.append(('C 清零时条空', k, (r.mode, r.delta), want_d, c.elapsed, want_e))
            break
    return bad


def wrap_frame_sampled_or_missed(fps=60.0):
    """回费边界那两拍：采样卡了一下不许算错，而且能看出来是哪一种。

    回费那一帧条是空的（相位 0），它的后一帧相位只有 1/per、画出来还是 0 宽 ——
    边界上**有两拍**读不出位置信息（逐帧核过，见 frame_step_precision）。采样卡一下
    就会吞掉其中一拍，三种走法都合法，落点各差一拍：

        每拍都采到    ： 0（wrap 当场 +1）-> 0（0 宽，冻结 0）-> 5（+2 补齐）
        漏采 0 宽那拍 ： 0（wrap 当场 +1）-> 5（+2 补齐）
        漏采归零那拍  ： 0（wrap 当场 +1，落在归零帧上）-> 5（+2 补齐）

    三种走法的累计帧数都一样（落到条重新画出来那拍时正好对上），差别只在中间
    差了一拍的相位 —— 也就是用户看到的「数字暂停了一下、然后一次跳 2」。

    用户报的「有时候第 0 帧还是不读、第一帧补了两帧」就是后面两种里的一种。
    采样够快（≥120Hz）基本不会发生；这里保证「真发生了也一帧不差」。

    返回失败项列表（每项：(场景, 实际 (mode, delta) 序列, 期望的 delta 序列,
    累计帧数, 真值)）。
    """
    bad = []
    cases = [
        ('每拍都采到', (30, 31, 32), [1, 0, 2]),
        ('漏采 0 宽那一拍', (30, 32), [1, 2]),
        ('漏采归零那一拍', (31, 32), [1, 2]),
    ]
    for name, fed, want in cases:
        c = Counter(BAR_W, fps=fps)
        game = CostTiming(SCALE)
        dt = 1.0 / fps
        t, idx = 0.0, 0
        beats = []
        for n in range(1, 33):
            game.advance_one_frame(SPEED_NORMAL)
            if n > 29 and n not in fed:
                continue                        # 这一拍整拍没采到：画面什么都没读
            idx += 1
            t += dt
            fill = synthesized_width(BAR_W, float(BAR_W), game.phase())
            r = c.step(idx, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                       RUN, SPEED)
            if n >= 30:
                beats.append((r.mode, r.delta))
        truth = max(fed)                        # 一拍一个逻辑帧 -> 真值就是帧号
        if [d for _, d in beats] != want or c.elapsed != truth:
            bad.append((name, beats, want, c.elapsed, truth))
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
    for i, n in enumerate(range(2, 123), start=1):
        t2 += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n))
        r = c2.step(i, t2, bar(fill, 'TRACK'), RUN, SPEED)
        if r.warn == 'phase-resync':
            triggered += 1
    if triggered:
        bad.append(('连续正常推进误触发重锚', triggered, 0))
    # 喂的是本费第 2..122 帧（121 拍），起锚落在第 2 帧上 —— 本费已经走过 2 帧
    # （第 1 帧画出来是 0 宽，但也走过了），所以累计是 122。
    # 为什么不是喂到第 121 帧：第 121 帧的相位只有 1/30、画出来还是 0 宽，那一拍
    # 没有位置信息，要等下一拍才补得上（宁可晚一拍也不猜）。少喂那一拍，累计就停在
    # 120 —— 看着像少记 1 帧，其实只是还没结账。
    if c2.elapsed != 122:
        bad.append(('对照段累计帧数（起锚补的是本费真走过的 2 帧）', c2.elapsed, 122))
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


def pause_bar_jitter_no_catchup(run=45, pause=60, resume_jump=8, fps=60.0):
    """暂停期间条读数被遮罩干扰时，暂停时长不能变成恢复后的追补窗口。

    对应实测 Desktop 2026.10.22 的 150 -> 199 跳变：暂停期间读数被拒，
    旧逻辑把暂停时长留进 ``_since_accept``；恢复后第一拍看到条跳了 8 帧，
    就一次性按 8 帧补进计数。现在暂停会把追补窗口清零，这一拍应拒绝。
    """
    c = Counter(BAR_W, fps=fps)
    dt = 1.0 / fps
    t = 0.0
    phase = 0
    idx = 0
    for _ in range(run):
        idx += 1
        t += dt
        phase += 1
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase))
        c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED)
    before = c.elapsed

    for _ in range(pause):
        idx += 1
        t += dt
        # 暂停遮罩让条读数变成和相位模型完全对不上的值；真实相位没有前进。
        c.step(idx, t, bar(115, 'TRACK'), PAUSED, SPEED)
    pause_added = c.elapsed - before
    since_after_pause = c._since_accept

    # 恢复后的第一拍：画面里的条比暂停前跳了 8 帧，但真实只过了 1/60 秒。
    idx += 1
    t += dt
    phase += resume_jump
    fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase))
    r = c.step(idx, t, bar(fill, 'TRACK'), RUN, SPEED)
    return pause_added, r.delta, since_after_pause


def fast_frame_step_at_max(fps=60.0):
    """满费 + 快速帧操：暂停 -> 只跑 2 拍 -> 再暂停。每一下的零点几帧都必须留住。

    实测用户录像 `Desktop 2026.10.06 - 18.32.40.02`（24.1 秒 / 1448 拍，全程满费）：
    29 段「运行中」里 20 段只有 1~3 拍，旧的「连续 5 拍确认」门把它们全吃掉 ——
    累计 21 帧，正确值是 41（前 16 段是 1X，从 11.25 秒起进子弹时间）。见 docs 第 8 节。

    真值不用猜：每一拍的真值就是 rate / fps 个逻辑帧（rate = 30 x 倍速，子弹时间是 6）。
    返回失败项列表。
    """
    bad = []
    dt = 1.0 / fps
    for name, m, bullet in (('1X', 1.0, False), ('2X', 2.0, False),
                            ('子弹时间', float('nan'), True)):
        rate = BULLET_FPS if bullet else 30.0 * m
        sp = (SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                           m=float('nan'), bullet=True, conf='ok') if bullet else
              SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=m,
                           bullet=False, conf='ok'))
        c = Counter(BAR_W, fps=fps)
        t, idx = 0.0, 0
        for _ in range(60):            # 先满费跑 60 拍，过掉起锚那一拍
            idx += 1
            t += dt
            c.step(idx, t, bar(0, 'MAX'), RUN, sp)
        base = c.elapsed_f
        truth = 0.0
        for _ in range(12):
            for _ in range(30):        # 暂停 30 拍（这一段一帧都不能记）
                idx += 1
                t += dt
                c.step(idx, t, bar(0, 'MAX'), PAUSED, sp)
            for _ in range(2):         # 只跑 2 拍：旧门限要 5 拍，于是整段记 0
                idx += 1
                t += dt
                c.step(idx, t, bar(0, 'MAX'), RUN, sp)
                truth += rate * dt
        got = c.elapsed_f - base
        if abs(got - truth) > 1e-9:
            bad.append((name, '%.4f' % got, '%.4f' % truth))
        if c.clock_frames > c.elapsed_f + 1e-9:
            bad.append((name, 'clock_frames 超过 elapsed_f',
                        c.clock_frames, c.elapsed_f))
    return bad


def drag_false_max(fps=60.0):
    """拖干员 / 部署：条槽被网格压成 MAX 灰，但游戏是子弹时间、暂停键没画。

    用户 2026-10-25 录像：费用才 16，拖干员时条槽被压成 146 灰，旧逻辑
    把它当 MAX，右上角暂停键又被压暗（drawn=False）—— 只要 running 读成
    True，就会走满费挂钟，凭空按 6 帧/秒涨。这里必须冻结。
    """
    bad = []
    sp = SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                      m=float('nan'), bullet=True, conf='ok')
    c = Counter(BAR_W, fps=fps)
    # 先正常运行一拍，把带滞回的 _run_prev 置成 True。
    c.step(1, 1.0 / fps, bar(0, 'MAX'), RUN, sp)
    base_f, base_clock = c.elapsed_f, c.clock_frames
    # 拖拽时暂停键被压暗：原始读数 running=False，但 ratio 落在滞回中间带，
    # 上一拍又是运行中 —— 旧逻辑 paused=False，会继续走挂钟。
    drag = PauseReading(ratio=0.10, running=False, drawn=True, solid=0.1)
    for i in range(2, 62):
        c.step(i, i / fps, bar(0, 'MAX'), drag, sp)
    if c.elapsed_f != base_f or c.clock_frames != base_clock:
        bad.append(('拖拽假 MAX', c.elapsed_f, base_f))
    return bad


def deploy_cover_recovery(fps=60.0, per=120):
    """部署遮罩盖住条 2 秒：按拖拽固定 6 帧/秒**实时**推进 12 帧。

    用户 2026-10-25 13.20.51：拖干员时网格把条右半段压成 146 灰，
    旧逻辑当成 MAX 后冻结；遮罩消失时又跳 22 帧，表现为「部署时数字不动，
    部署完突然跳到准确值」。真值：拖拽固定 6 帧/秒，2 秒 = 12 帧，
    这 12 帧必须在遮罩期间就一帧一帧显示出来，遮罩消失时不再跳。
    """
    bad = []
    c = Counter(BAR_W, fps=fps, frames_per_cost=per)
    dt = 1.0 / fps
    t = 0.0
    for n in range(1, 21):
        t += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n, per))
        c.step(n, t, bar(fill, 'TRACK'), RUN, SPEED)
    before = c.elapsed
    clock_before = c.clock_frames
    cover_fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(20, per))
    for k in range(1, 121):
        # 拖拽时双竖条仍在（只是被压暗）；robust 阈值会把它读成运行中。
        c.step(20 + k, t + k * dt, cover_bar(cover_fill), RUN, SPEED)
    cover_gain = c.elapsed - before
    if cover_gain not in (11, 12, 13):
        bad.append(('遮罩期间应实时显示 12 帧', cover_gain, 12))
    if c.clock_frames != clock_before:
        bad.append(('遮罩期间走像素通道、不该进 clock', c.clock_frames, clock_before))
    t += 120 * dt
    fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(32, per))
    r = c.step(141, t + dt, bar(fill, 'TRACK'), RUN, SPEED)
    if r.delta > 1:
        bad.append(('遮罩消失后不该再跳', r.delta, '<=1'))
    got = c.elapsed - before
    if got not in (11, 12, 13):
        bad.append(('遮罩 + 消失总计 12 帧', got, 12))
    return bad


def deploy_cover_true_pause(fps=60.0, per=120):
    """暂停键明确读到「已暂停」时，遮罩消失后的假跳不许补帧。"""
    bad = []
    c = Counter(BAR_W, fps=fps, frames_per_cost=per)
    dt = 1.0 / fps
    t = 0.0
    for n in range(1, 21):
        t += dt
        fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n, per))
        c.step(n, t, bar(fill, 'TRACK'), RUN, SPEED)
    before = c.elapsed
    cover_fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(20, per))
    for k in range(1, 301):
        c.step(20 + k, t + k * dt, cover_bar(cover_fill), PAUSED, SPEED)
    # 暂停界面淡出：条被误读成前进了 20 帧（HUD 位移），游戏其实没走。
    # 20 > 25% 档的合法窗口（60*dt + 开头 4 帧死区 + 3px 余量），必须拒绝。
    t += 300 * dt
    fake_fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(40, per))
    c.step(321, t + dt, bar(fake_fill, 'TRACK'), PAUSED, SPEED)
    got = c.elapsed - before
    if got != 0:
        bad.append(('暂停中假跳不该补帧', got, 0))
    return bad


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


def _blind_head_frames(per):
    """独立算一遍「费用周期开头有几帧读不出来」：宽度还没超过 EMPTY_MAX_FILL 的那几帧。

    故意不复用 Counter._unreadable_head_frames —— 两条路各算各的，对不上才说明有一边错了。
    """
    for n in range(1, int(per) + 1):
        w = synthesized_width(BAR_W, float(BAR_W), phase_frac(n, per))
        if w > EMPTY_MAX_FILL:
            return n - 1
    return 0


def reduced_rate_anchor_head(fps=60.0):
    """减费档位的起锚补帧：上限必须跟着回费速率一起变。

    一费开头有几帧读不出来 = 宽度还没超过 EMPTY_MAX_FILL 的那几帧。它随回费
    速率一起变多（30 / 60 / 120 帧/费 -> 1 / 2 / 4 帧，见 _blind_head_frames），
    可老代码把上限写死 2 —— 只在 30 帧/费那一档够用。

    实测（2026-10-13，用户两段录像）：25% = 120 帧/费、50% = 60 帧/费 的开战起锚
    都落在读得出来的第一帧，被写死的上限挡掉、判 anchor-late / delta=0，一开局就
    白丢好几帧。用户报的「开头几帧识别不到」「25% 完全不准」就是它。

    期望值是 blind + 1（不是 blind）：起锚落在第 blind + 1 帧，本费已经走过
    blind + 1 帧（第 1..blind 帧画出来是 0 宽，但它们照样走过了）。

    返回失败项列表（每项：(per, 实际, 期望)）。
    """
    bad = []
    for per in (30, 60, 120):
        blind = _blind_head_frames(per)
        if blind <= 0:
            bad.append((per, '几何算出来开头没有读不到的帧', '>0'))
            continue
        # ① 上限算对了没有
        got_blind = Counter(BAR_W, fps=fps, frames_per_cost=per)._unreadable_head_frames()
        if got_blind != blind:
            bad.append((per, '开头读不到的帧数 %s' % got_blind, blind))
        # ② 开战起锚：条第一次被读到就在第 (blind + 1) 帧 -> 那 blind 帧必须补上
        c = Counter(BAR_W, fps=fps, frames_per_cost=per)
        dt = 1.0 / fps
        t = 0.0
        idx = 0
        for _ in range(5):        # 关卡外待几拍：条没画出来
            idx += 1
            t += dt
            c.step(idx, t, bar(0, 'EMPTY'), RUN, SPEED)
        got = None
        for n in range(1, 4 * int(per) + 1):
            idx += 1
            t += dt
            fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n, per))
            # 和 features.read_bar 同一条规则：fill <= EMPTY_MAX_FILL 一律算空条
            r = c.step(idx, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                       RUN, SPEED)
            if got is None and r.mode != 'idle':
                got = (r.mode, r.delta)
        if got != ('anchor-head', blind + 1):
            bad.append((per, '开战起锚 %s' % (got,), ('anchor-head', blind + 1)))
        # ③ 半路重现（周期中段突然又能读到）一帧都不许补
        c2 = Counter(BAR_W, fps=fps, frames_per_cost=per)
        t, idx = 0.0, 0
        for _ in range(5):
            idx += 1
            t += dt
            c2.step(idx, t, bar(0, 'EMPTY'), RUN, SPEED)
        got2 = None
        for n in range(int(per) // 2, int(per) + 1):
            idx += 1
            t += dt
            fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n, per))
            r = c2.step(idx, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                        RUN, SPEED)
            if got2 is None and r.mode != 'idle':
                got2 = (r.mode, r.delta)
        if got2 != ('anchor', 0):
            bad.append((per, '半路重现 %s' % (got2,), ('anchor', 0)))
    return bad


def reduced_rate_accuracy(fps=60.0):
    """减费档位（60 / 120 帧/费）与局内负费的**逐周期准确性**。

    为什么单列一条：这几档一费要几十上百帧，采样一拍走不到一帧（1X 下 60Hz 采样
    每拍 0.5 帧、条连着两拍一般宽），任何「按挂钟补一拍」的写法都会把几十个周期
    下来误差攒成几帧 —— 用户 2026-10-15 实测报的就是「50% 一费被垫成 62 帧」
    「25% 到 119 帧只有 117 帧」。像素通道的规矩是不碰挂钟：条不动就不动。

    三段都验：
      ① 逐周期长度必须恰好是 per（周期边界锚点会把 fp24 的 +1 边界拨回去）；
      ② 累计帧数 == 真值（±1：起锚那一帧）；
      ③ 1X（每拍 0.5 帧，条连着两拍一般宽）和 2X（每拍 1 帧）都要成立 —— 采样率
        一变就错说明在按挂钟猜。

    返回失败项列表。
    """
    bad = []
    dt = 1.0 / fps
    # (一费几个逻辑帧, 是否局内负费, 每几拍走一个逻辑帧)
    cases = [(30.0, False, 1), (60.0, False, 1), (120.0, False, 1),
             (30.0, False, 2), (60.0, False, 2), (120.0, False, 2),
             (60.0, True, 1), (120.0, True, 1), (120.0, True, 2)]
    for per, neg, adv in cases:
        cycle = per * (2 if neg else 1)
        c = Counter(BAR_W, fps=fps, frames_per_cost=per)
        t = 0.0
        # 尽头多采一拍：条画满那一帧（相位 1.0）和 0.99 在画面上同宽（都被条槽宽截住），
        # 模型要等下一拍「条归零」才把它记上 —— 这是相位本身的歧义，不是丢帧。
        # 采样停在那一下回卷上，账才是平的（真值仍然按「走了几个逻辑帧」算）。
        samples = 3 * int(cycle) * adv + 1
        for i in range(1, samples + 1):
            t += dt
            n = (i - 1) // adv + 1
            fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(n, cycle))
            sign = SignReading(negative=neg, run=40 if neg else 18,
                               rows=3 if neg else 0, thr=21)
            c.step(i, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                   RUN, SPEED, sign)
        # 喂进去的相位是 n = 1 .. (samples - 1) // adv + 1，所以走的逻辑帧数就是这个
        # （adv = 2 时 samples // adv 会少算 1 帧 —— 末尾那一拍只走了半个逻辑帧）。
        truth = (samples - 1) // adv + 1
        tag = '%d 帧/费%s%s' % (per, ' + 负费' if neg else '', ' @1X' if adv == 2 else ' @2X')
        if any(v != cycle for v in c.cycle_lengths):
            bad.append((tag, '逐周期长度', c.cycle_lengths[:6], '%d' % cycle))
        if abs(c.elapsed - truth) > 1:
            bad.append((tag, '累计帧数', (c.elapsed, truth)))
        if c.elapsed != c.pixel_frames + c.clock_frames:
            bad.append((tag, 'elapsed 与分批计数不一致',
                        (c.elapsed, c.pixel_frames + c.clock_frames)))
    return bad


def reduced_rate_fp24_exact(fps=60.0, cycles=6, tail=24):
    """减费档位 / 局内负费：条由 **fp24 模型自己**画出来，周期长度必须一帧不差。

    上一条（reduced_rate_accuracy）用的是「每周期恰好 per 帧」的简化编码，而游戏的
    累加器有边界周期 —— required 是「每逻辑帧增量」凑出来的，攒够误差的那一费本来会
    多出 1 帧（fp24 的说明：30 帧/费是 `30*10, 31`）。

    这一条把 CostTiming 的输出直接当画面喂回去。2026-10-18 起，周期边界锚点要求
    **每个条满 -> 条空都恰好是 per 帧**：定点累加器给出的 61 / 121 / 241 边界也要被
    拨回 60 / 120 / 240。用户 2026-10-15 报的「50% 一费 62 帧」「25% 到 119 帧只有
    117 帧」两条仍然在这一条上现形：前者是垫帧垫多了，后者是回卷那一拍被物理上限误杀。

    累计帧数只要求「不吃掉超过开头读不出来的帧」：一费开头那几帧画出来是 0 宽，
    和「压根没画条」长得一样（`_unreadable_head_frames`），起锚时只能保守地少记
    —— 这是帧号起点差 1~2 帧，不是每费都漏，所以逐周期长度仍然必须严格相等。

    adv：每几拍走一个逻辑帧（1 = 2X / 120Hz 采样，2 = 1X）。
    返回失败项列表。
    """
    bad = []
    dt = 1.0 / fps
    # (一费几帧, 每逻辑帧攒多快, 画面里有没有减号, 每几拍走一个逻辑帧)
    cases = [(30.0, SPEED_NORMAL, False, 1), (30.0, SPEED_NORMAL, False, 2),
             (60.0, SPEED_NORMAL, False, 1), (60.0, SPEED_NORMAL, False, 2),
             (120.0, SPEED_NORMAL, False, 1), (120.0, SPEED_NORMAL, False, 2),
             (30.0, SPEED_NEGATIVE, True, 1), (30.0, SPEED_NEGATIVE, True, 2),
             (60.0, SPEED_NEGATIVE, True, 1), (60.0, SPEED_NEGATIVE, True, 2),
             (120.0, SPEED_NEGATIVE, True, 1), (120.0, SPEED_NEGATIVE, True, 2)]
    for per, speed, neg, adv in cases:
        game = CostTiming(required_from_frames(per))
        c = Counter(BAR_W, fps=fps, frames_per_cost=per)
        sign = SignReading(negative=neg, run=40 if neg else 18,
                           rows=3 if neg else 0, thr=21)
        t = 0.0
        idx = 0
        truth = 0
        ends = []          # 每个费用周期结束在「第几个逻辑帧」上（真值）
        # 走 cycles 个完整周期，再多走 tail 帧停在周期中间 —— 末端那一拍要读到条，
        # 计数器才有机会把最后几帧补上（末端正好压在「条归零」上时相位本身是歧义的）。
        while True:
            idx += 1
            t += dt
            if idx % adv == 0:
                truth += 1
                if game.advance_one_frame(speed):
                    ends.append(truth)
                    game.cycles = len(ends)
            fill = synthesized_width(BAR_W, float(BAR_W), game.phase())
            c.step(idx, t, bar(fill, 'TRACK' if fill > EMPTY_MAX_FILL else 'EMPTY'),
                   RUN, SPEED, sign)
            if len(ends) >= cycles and truth - ends[-1] >= tail:
                break
            if idx > 200000:
                break
        tag = '%d 帧/费%s%s' % (per, ' + 负费' if neg else '', ' @1X' if adv == 2 else ' @2X')
        # 周期边界锚点：不管 fp24 边界那一费本来要 61 / 121 / 241，面板都钉回整数周期。
        want_cycles = [int(round(per * (2 if neg else 1)))] * max(0, len(ends) - 1)
        head = c._unreadable_head_frames(speed_for_cost_state(neg))
        if list(c.cycle_lengths) != want_cycles:
            bad.append((tag, '逐周期长度', (list(c.cycle_lengths), want_cycles)))
        if not 0 <= truth - c.elapsed <= head:
            bad.append((tag, '累计帧数（只许丢开头读不出来的那几帧）',
                        (c.elapsed, truth, head)))
        if c.elapsed != c.pixel_frames + c.clock_frames:
            bad.append((tag, 'elapsed 与分批计数不一致',
                        (c.elapsed, c.pixel_frames + c.clock_frames)))
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


def _negative_run(samples, neg_of, fps=60.0, show_sign=True):
    """负费合成信号：游戏侧那个定点累加器按 speed 换挡，像素只从它的相位画出来。

    返回 (计数器, 那一拍画面里到底有没有减号)。

    真值就是**采样拍数**（60Hz + 1X -> 每拍正好 1 个逻辑帧，和 simulate() 同一套
    约定）：负费只改变「一费几个逻辑帧」（30 -> 60），**不改变逻辑帧率**。
    """
    game = CostTiming(SCALE)
    c = Counter(BAR_W, fps=fps)
    dt = 1.0 / fps
    t = 0.0
    for i in range(samples):
        neg = bool(neg_of(i))
        game.advance_one_frame(SPEED_NEGATIVE if neg else SPEED_NORMAL)
        fill = synthesized_width(BAR_W, float(BAR_W), game.phase())
        t += dt
        lit = neg and show_sign
        # 有减号时框里那条纯白横杠实测 40px 上下（阈值 21）；没有减号时正常画面最长
        # 只有 ~18px（用户截图实测，见 features.SIGN_MIN_RUN_UNIT）。
        sign = SignReading(negative=lit, run=40 if lit else 18,
                           rows=3 if lit else 0, thr=21)
        c.step(i + 1, t, bar(fill, 'TRACK'), RUN, SPEED, sign)
    return c, samples


def negative_cost_timing(fps=60.0):
    """局内负费：一费 30 -> 60 逻辑帧，进场 / 离场（含压回费边界）一帧都不丢。

    为什么这条是**必须**做的：不认减号时，相位按 1/30 往前推，_resync 只能挑到真值
    一半的 k —— 整段负费被数成一半（不是差一两帧）。下面「对照组」就是同一段信号
    把减号抹掉再跑一遍，差多少直接摆出来。

    **容差**：换挡那一拍（正 -> 负 / 负 -> 正）游戏用的是旧速度还是新速度，取决于
    它是在这一拍的画面取到之前还是之后换的 —— 一帧像素里分不出来，累加器会差半个
    负费增量（= 半个正常帧、约 4px，落在 _resync 的 5px 容差里面）。所以「中途换挡」
    的用例给 ±1 帧的容差（state.py 里 _boundary_correction 只修相位、不修帧数）；
    其余用例一帧都不许差。
    返回 (失败项列表, 报告行列表)。
    """
    bad, lines = [], []

    cases = [
        ('全负费 300 拍', 300, lambda i: True, 0),
        ('正 -> 负(第120拍) -> 正(第300拍) 420 拍', 420, lambda i: 120 <= i < 300, 1),
        ('负 -> 正(第150拍) 300 拍', 300, lambda i: i < 150, 0),
        ('转场正好压在回费边界上 400 拍', 400, lambda i: 30 <= i < 45, 0),
    ]
    for name, n, fn, tol in cases:
        c, truth = _negative_run(n, fn, fps)
        diff = c.elapsed - truth
        mark = 'OK' if abs(diff) <= tol else 'BAD'
        lines.append('  %-4s %-34s elapsed=%4d 真值=%4d 差 %+d%s  周期=%s'
                     % (mark, name, c.elapsed, truth, diff,
                        '' if tol == 0 else '（转场那一拍容差 ±%d）' % tol,
                        sorted(set(c.cycle_lengths))[:4]))
        if abs(diff) > tol:
            bad.append((name, c.elapsed, truth))

    # 长跑：负费期间本来第 11 费会被 fp24 累加成 61 帧；周期边界锚点现在把它拨回 60。
    long_n = 1800
    cl = _negative_run(long_n, lambda i: True, fps)[0].cycle_lengths
    n61 = cl.count(61)
    if sorted(set(cl)) != [60]:
        bad.append(('长跑逐周期长度', cl[:12], '只有 60'))
    elif n61 != 0:
        bad.append(('61 帧周期应当被周期锚点拨回 60', n61, 0))
    lines.append('  %-4s %-34s elapsed=%4d 真值=%4d  61 帧已拨回 60，共 %d 个'
                 % ('OK' if not bad else 'BAD', '全负费 1800 拍（周期锚点）',
                    long_n, long_n, n61))

    # 对照组：同一段信号，把画面里的减号抹掉（= 不认负费）
    ctrl, truth = _negative_run(300, lambda i: True, fps, show_sign=False)
    lines.append('  ---- 对照组：不认减号（同一条信号）elapsed=%d 真值=%d 差 %+d'
                 % (ctrl.elapsed, truth, ctrl.elapsed - truth))
    if ctrl.elapsed * 2 != truth:
        bad.append(('不认减号时应当正好数成一半', ctrl.elapsed, truth // 2))
    return bad, lines


BLIND = SpeedReading(digit_ratio=0.02, corr_2x=float('nan'), has_digit=False,
                     m=float('nan'), bullet=False, conf='low', drawn=False, solid=0.0)
DIGIT_1X = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=1.0,
                        bullet=False, conf='ok')
DIGIT_2X = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=2.0,
                        bullet=False, conf='ok')


def bullet_assumed_at_max(fps=60.0, seconds=1.0):
    """满费 + 倍速键「整块空白」：读到过数字就按子弹时间算，从没读到过才冻结。

    实测背景（见 state.BULLET_ASSUMED_WARN 上面那段）：拖干员进子弹时间时游戏把那颗
    倍速键整块收起来，`>>>` 在像素上读不到 —— 满费时速率就没有来源了。用户给的规则是
    「读到过 1X 之后识别不到就按子弹时间算」。这里验四条：

        (a) 先看到 1X，再整块空白 -> 按 6 帧/秒记（不是 30、也不是「凍结 0」）；
        (b) 先看到 2X，再整块空白 -> 还是 6 帧/秒（**不能**拿 last_m/5 算成 12）；
        (c) 从没读到过数字 -> 仍旧冻结 0 帧、warn=speed-unknown（宁可少记不瞎猜）；
        (d) 空白结束、倍速键又画回 2X -> 恢复 60 帧/秒。

    返回 (失败项列表, 说明行列表)。
    """
    dt = 1.0 / fps
    beats = int(round(fps * seconds))
    bad = []
    lines = []

    def at_max(c, idx, t, sp):
        """喂 `beats` 拍满费，返回 (idx, t, 新增整数帧, 新增带小数帧, warn, mode)。

        带小数的那个用于断言速率：60 拍 x (1/60) 秒在浮点下差最后一点，
        整数部分会少 1 帧而余量留在 _clock_frac 里（下一拍就还回来了）。
        """
        before, before_f = c.elapsed, c.elapsed_f
        for _ in range(beats):
            idx += 1
            t += dt
            r = c.step(idx, t, bar(0, 'MAX'), RUN, sp)
        return idx, t, c.elapsed - before, c.elapsed_f - before_f, r.warn, r.mode

    def warm(c, sp, idx, t, n=30):
        """起锚 + 进关卡：喂 n 拍正常推进的条（像素通道不看倍速）。"""
        phase = 0.0
        for _ in range(n):
            idx += 1
            t += dt
            phase += 1.0
            fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(phase))
            c.step(idx, t, bar(fill, 'TRACK'), RUN, sp)
        return idx, t

    # (a) 1X 之后空白
    c = Counter(BAR_W, fps=fps)
    idx, t = warm(c, DIGIT_1X, 0, 0.0)
    idx, t, _, _, _, _ = at_max(c, idx, t, DIGIT_1X)          # 先真的看到 1X
    idx, t, blind, blind_f, b_warn, b_mode = at_max(c, idx, t, BLIND)
    want = BULLET_FPS * seconds
    lines.append('  (a) 1X -> 空白 %d 拍：%.2f 帧（期望 %.2f = 6 帧/秒），warn=%s mode=%s'
                 % (beats, blind_f, want, b_warn, b_mode))
    if abs(blind_f - want) > 0.05 or b_warn != 'bullet-assumed':
        bad.append('(a) 1X 之后空白应记 %.2f 帧 / bullet-assumed，实际 %.2f / %s'
                   % (want, blind_f, b_warn))

    # (b) 2X 之后空白：仍是 6 帧/秒
    c = Counter(BAR_W, fps=fps)
    idx, t = warm(c, DIGIT_2X, 0, 0.0)
    idx, t, _, _, _, _ = at_max(c, idx, t, DIGIT_2X)
    idx, t, blind2, blind2_f, b_warn2, b_mode2 = at_max(c, idx, t, BLIND)
    lines.append('  (b) 2X -> 空白 %d 拍：%.2f 帧（期望 %.2f；拿 last_m/5 算会是 %.2f）'
                 % (beats, blind2_f, want, LOGICAL_FPS * 2.0 / 5.0 * seconds))
    if abs(blind2_f - want) > 0.05:
        bad.append('(b) 2X 之后空白应记 %.2f 帧（固定 6 帧/秒），实际 %.2f'
                   % (want, blind2_f))

    # (c) 从没读到过数字：冻结
    c = Counter(BAR_W, fps=fps)
    idx, t = warm(c, BLIND, 0, 0.0)
    idx, t, frozen, frozen_f, c_warn, c_mode = at_max(c, idx, t, BLIND)
    lines.append('  (c) 从没读到过数字 -> 满费空白 %d 拍：%d 帧（期望 0），warn=%s'
                 % (beats, frozen, c_warn))
    if frozen_f != 0.0 or c_warn != 'speed-unknown':
        bad.append('(c) 没读到过数字时应冻结（0 帧 / speed-unknown），实际 %.2f / %s'
                   % (frozen_f, c_warn))

    # (d) 空白之后倍速键又画回 2X
    idx, t, back, back_f, d_warn, _ = at_max(c, idx, t, DIGIT_2X)
    want2 = LOGICAL_FPS * 2.0 * seconds
    lines.append('  (d) 空白之后画回 2X：%.2f 帧（期望 %.2f = 60 帧/秒）' % (back_f, want2))
    if abs(back_f - want2) > 0.05:
        bad.append('(d) 画回 2X 应记 %.2f 帧，实际 %.2f' % (want2, back_f))
    return bad, lines


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
    ok2 = len(cl) >= 90 and lo == 30 and hi == 30
    print('结论：%s' % ('✅ 通过（周期边界锚点让一费恒为 30 帧）'
                      if ok2 else '❌ 周期长度不对'))

    missed = frame_step_precision()
    print()
    print('帧操逐帧精度（暂停着一下一下推 300 拍）：')
    if missed:
        print('  ❌ 有 %d 项不对 (拍号, fill, 状态, 通道, delta, 原因)，前 8 项：' % len(missed))
        for m in missed[:8]:
            print('     %s' % (m,))
    else:
        print('  ✅ 通过（回费那一拍当场 +1；它后面那拍 0 宽、由再下一拍一次补 2，'
              '累计一帧不差）')
    ok3 = not missed

    bad_wrap = wrap_frame_sampled_or_missed()
    print()
    print('回费边界两拍（归零帧采到 / 漏采）：')
    if bad_wrap:
        for b in bad_wrap:
            print('  ❌ %s：实际 %s，期望 delta=%s；累计 %s 期望 %s' % b)
    else:
        print('  ✅ 通过（每拍都采到 / 漏采 0 宽那拍 / 漏采归零那拍：落点差一拍，'
              '总数都对）')
    ok6 = not bad_wrap

    bad_anchor = anchor_head_catchup()
    print()
    print('起锚补帧（条第一次出现时落在周期的第 2 / 3 / 4 / 16 帧）：')
    if bad_anchor:
        print('  ❌ 有 %d 项不对 (起锚相位, (mode, delta), 期望, 一周期帧数)：' % len(bad_anchor))
        for b in bad_anchor:
            print('     %s' % (b,))
    else:
        print('  ✅ 通过（第 2/3 帧补 2/3 帧、elapsed 就是本费已经走的帧数；'
              '第 4/16 帧是半路重现，按 0 起锚；一个费用周期恒为 30 帧）')
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
              '条空时清零 -> 第一个像素那拍补 2 帧 = 本费已经走的帧数）')
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

    pause_added, resume_delta, since_after_pause = pause_bar_jitter_no_catchup()
    print()
    print('暂停读数被遮罩干扰（暂停 60 拍，恢复首拍条跳 8 帧）：')
    print('  暂停期间多记 %d 帧，恢复首拍 delta=%d，追补窗口 %.2fs'
          % (pause_added, resume_delta, since_after_pause))
    ok17 = pause_added == 0 and resume_delta == 0 and since_after_pause == 0.0
    print('结论：%s' % ('✅ 通过（暂停时长不进入追补窗口）'
                       if ok17 else '❌ 暂停时长被追补进了计数'))

    bad_fast = fast_frame_step_at_max()
    print()
    print('满费 + 快速帧操（暂停 30 拍 -> 只跑 2 拍，重复 12 轮）：')
    if bad_fast:
        for b in bad_fast:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（1X / 2X / 子弹时间下，每小段跑动的零点几帧都累进了 elapsed_f；'
              '旧逻辑三种速率下每轮分别丢 1 / 1 / 0.2 帧）')
    ok11 = not bad_fast

    bad_drag = drag_false_max()
    print()
    print('拖干员 / 部署：条槽假 MAX（暂停键没画 + 子弹时间）：')
    if bad_drag:
        for b in bad_drag:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（假 MAX 只冻结，不进满费挂钟）')
    ok18 = not bad_drag

    bad_cover = deploy_cover_recovery()
    print()
    print('部署遮罩：按 6 帧/秒实时推进，消失时不重复补帧：')
    if bad_cover:
        for b in bad_cover:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（2 秒遮罩 = 12 帧，遮罩期间 clock=0）')
    ok19 = not bad_cover

    bad_pause_cover = deploy_cover_true_pause()
    print()
    print('部署遮罩 + 明确暂停：假跳不补帧：')
    if bad_pause_cover:
        for b in bad_pause_cover:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（读到「已暂停」时窗口照旧清零）')
    ok20 = not bad_pause_cover

    bad_rate = reduced_cost_rate_cycles()
    print()
    print('减费环境逐周期长度（一费 60 / 120 逻辑帧）：')
    if bad_rate:
        for b in bad_rate:
            print('  ❌ ' + '：'.join(str(x) for x in b))
    else:
        print('  ✅ 通过（60 帧/费、120 帧/费都逐周期严格等于周期长度）')
    ok8 = not bad_rate

    bad_head = reduced_rate_anchor_head()
    print()
    print('减费档位的起锚补帧（一费开头读不出来的帧：' + ' / '.join(str(_blind_head_frames(p)) for p in (30, 60, 120)) + ' 帧）：')
    if bad_head:
        for b in bad_head:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（60 / 120 帧/费的开战起锚不再被判 anchor-late、那几帧全补回来；'
              '半路重现仍然一帧都不补）')
    ok14 = not bad_head

    bad_rate = reduced_rate_accuracy()
    print()
    print('减费档位 / 局内负费的逐周期准确性（一拍走不到一帧时不许按挂钟猜）：')
    if bad_rate:
        for b in bad_rate:
            print('  ❌ ' + '：'.join(str(x) for x in b))
    else:
        print('  ✅ 通过（30 / 60 / 120 帧/费、负费翻倍、1X 与 2X 采样：'
              '逐周期 per/per+1、累计帧数一帧不差）')
    ok15 = not bad_rate

    bad_exact = reduced_rate_fp24_exact()
    print()
    print('减费档位 / 局内负费：条按 fp24 模型画出来，帧数一帧不差：')
    if bad_exact:
        for b in bad_exact:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（累计帧数、逐周期长度都严格相等；'
              'fp24 的 121 / 241 边界也被周期锚点拨回 120 / 240）')
    ok16 = not bad_exact

    bad_switch = rate_switch_rescale()
    print()
    print('跑动中认出速率后换算已数的帧：')
    if bad_switch:
        for b in bad_switch:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（30 -> 60 时已数的 30 帧换算成 60 帧，绝对帧号不从头偏）')
    ok9 = not bad_switch

    bad_neg, neg_lines = negative_cost_timing()
    print()
    print('局内负费（费用条上出现减号：每逻辑帧只攒一半，一费 30 -> 60 逻辑帧）：')
    for line in neg_lines:
        print(line)
    if bad_neg:
        for b in bad_neg:
            print('  ❌ %s：实际 %s，期望 %s' % b)
    else:
        print('  ✅ 通过（负费期间一费 60 帧；进场 / 离场 / 压在回费边界上都不丢帧；'
              '不认减号会整整少一半）')
    ok12 = not bad_neg

    bad_blind, blind_lines = bullet_assumed_at_max()
    print()
    print('满费 + 倍速键「整块空白」（拖干员：>>> 读不到，按子弹时间算）：')
    for line in blind_lines:
        print(line)
    if bad_blind:
        for b in bad_blind:
            print('  ❌ %s' % b)
    else:
        print('  ✅ 通过（读到过数字之后按固定 6 帧/秒；从没读到过就冻结不猜）')
    ok13 = not bad_blind

    return 0 if (ok and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8
                 and ok9 and ok10 and ok11 and ok12 and ok13 and ok14 and ok15
                 and ok16 and ok17 and ok18 and ok19 and ok20) else 1


if __name__ == '__main__':
    sys.exit(main())
