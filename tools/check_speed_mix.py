#!/usr/bin/env python
"""变速混合自测：「一费 = 30 逻辑帧」在各种倍速下是不是都成立。

check_state_synth.py 只测 2 倍速一种情况。这个脚本把 1X / 2X、暂停、满费、
满费+子弹时间、关卡外、**手动暂停计数**全混在一起，喂给状态机，和**精确真值**比总帧数。

子弹时间的真值就是**固定 6 帧/秒**（= 30/5），和倍速设置无关 ——
「无视当前倍速把游戏速率调至 1/5」调的是 [1 倍速] 的 1/5（见 state.BULLET_FPS）。
实测（用费用条自己当尺子，与倍速无关）：2X 录像 11.01.51.01 的子弹时间段量出
5.0 / 5.5 / 5.9 / 6.1 帧/秒，同一段视频正常 2X 处恰好 60.00 帧/秒。
所以「满费+子弹时间」在两段用例里真值一样，只是倍速设置不同。

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
BULLET_FPS = 30.0 / 5.0     # 子弹时间下游戏被压到的速率：固定 6 逻辑帧/秒

RUN = PauseReading(ratio=0.30, running=True)          # ❚❚：正在运行
PAUSED = PauseReading(ratio=0.05, running=False)      # ▶：已暂停
# 关卡外：HUD 根本没画（暂停键 ROI 压在菜单的文字 / 高亮块上）。
# ratio 故意给 0.24（够到「运行中」那条线）：占比高不等于画着字形 ——
# 密密麻麻的细笔画占比能到 0.24，但腐蚀一遍什么都剩不下（drawn=False）。
NO_HUD = PauseReading(ratio=0.24, running=True, drawn=False)
BLIND_SPEED = SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                           m=float('nan'), bullet=False, conf='low', drawn=False)


def speed_reading(m, bullet=False, drawn=True):
    """倍速键读数：有数字 -> m；子弹时间 -> 没数字、只有实心箭头。

    drawn=False 表示这个键**根本没画出来**（按钮淡出 / 被部署遮罩糊掉 / 不在关卡里），
    关卡判定要靠它把「不在关卡里」认出来（见 features.SpeedReading.drawn）。
    """
    if bullet:
        return SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                            m=float('nan'), bullet=True, conf='ok')
    if not drawn:
        return BLIND_SPEED
    return SpeedReading(digit_ratio=0.30, corr_2x=0.99, has_digit=True, m=float(m),
                        bullet=False, conf='ok')


def bar(fill, state):
    grey = BAR_W if state == 'MAX' else 0
    return BarReading(fill=fill, grey146=grey, grey88=0, state=state,
                      row=np.zeros(BAR_W, np.int16))


def phase_frac(frames: float) -> float:
    """游戏钟走了 frames 个逻辑帧时，条应该画在什么相位上。

    实测（2026-10-15 拿 -50% 的录像逐帧核过）：**回费那一帧条是空的**（相位 0），
    它的后一帧相位只有 1/30、画出来还是 0 宽 —— 一费的收尾在画面上是
    `..., 236, 0, 0, 5, ...`（那条费用周期里条从没画到过满宽）。详见
    check_state_synth.py 的模块说明。

    早先这里写成「回费帧画满（240）、下一帧才归零」（p == 0 时取 30），等于把
    「回费前最后一帧」当成了回费帧 —— 喂给状态机的信号和游戏不一样，相位模型
    每费会多算一帧。

    这里的 frames 是浮点（1 倍速 + 60Hz 采样 = 每拍 0.5 帧），所以按小数处理。
    """
    if frames <= 0.0:
        return 0.0
    return (frames % 30.0) / 30.0


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
                fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(frames))
                c.step(idx, t, bar(fill, 'TRACK'), PAUSED, speed_reading(1.0))
                continue
            if mode == 'manual':
                # 手动暂停计数：游戏照常跑，只是数字不动
                frames += speed * 30.0 * dt
                fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(frames))
                c.step(idx, t, bar(fill, 'TRACK'), RUN, speed_reading(speed))
                continue
            if mode == 'blur':
                # 倍速键上**画着数字**、但模板对不上（corr 低、m 读成 nan）：
                # 应当沿用上次读到的倍速继续数，而不是冻结（实测真实会话里有
                # 2898/20436 拍是这种读数，见 docs 第 23 节）。
                frames += speed * 30.0 * dt
                truth += speed * 30.0 * dt
                fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(frames))
                c.step(idx, t, bar(fill, 'TRACK'), RUN,
                       SpeedReading(digit_ratio=0.30, corr_2x=0.42, has_digit=True,
                                    m=float('nan'), bullet=False, conf='low'))
                continue
            if mode.startswith('fake'):
                # 关卡外：条 ROI 压在菜单 / 仓库的高亮 UI 上，读出一条亮块。HUD 没画出来，
                # 所以**一拍都不许记**（真值 0）—— 这是「在别的界面也计数」那条回归。
                #   fakebar     亮块是死的（宽度一点不变）
                #   fakebarmove 亮块在动（滚动列表 / 动画），旧代码会跟着数
                fill = 239 if mode == 'fakebar' else 100 + (idx % 40)
                c.step(idx, t, bar(fill, 'TRACK'), NO_HUD, speed_reading(1.0, drawn=False))
                continue
            if mode == 'runblind':
                # 条正常推进，但倍速键**整块没画**（从没见过数字的场景要用它）：
                # 像素通道不看倍速，所以这一段照样该记 30 帧/秒。
                truth += speed * 30.0 * dt
                frames += speed * 30.0 * dt
                fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(frames))
                c.step(idx, t, bar(fill, 'TRACK'), RUN, BLIND_SPEED)
                continue
            if mode.startswith('max'):
                # 满费：整条灰底，相位被游戏钉在 0；退出后从 0 重新涨（实测结论）。
                # 倍速键这一刻的形态决定速率：
                #   max        -> 画着数字，速率 = 30 x 设置
                #   maxbullet  -> 子弹时间 >>>，速率**固定** 6 帧/秒（与设置无关）
                #   maxblind   -> 按钮淡出 / 被遮罩糊掉，速率未知 -> 真值 0（计数器应冻结）
                frames = 0.0
                if mode.endswith('blind'):
                    rd = BLIND_SPEED
                else:
                    rd = speed_reading(speed, bullet=mode.endswith('bullet'))
                    truth += (BULLET_FPS if rd.bullet else speed * 30.0) * dt
                c.step(idx, t, bar(0, 'MAX'), RUN, rd)
            else:
                truth += speed * 30.0 * dt
                frames += speed * 30.0 * dt
                fill = synthesized_width(BAR_W, float(BAR_W), phase_frac(frames))
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
        # 满费 + 子弹时间（拖干员）：倍速键变 >>>，速率固定 6 帧/秒。
        # 两段的**真值一样**（各 30 帧），因为子弹时间和倍速设置无关。
        ('满费+子弹 1X设置', [(300, 1.0, 'run'), (300, 1.0, 'maxbullet'),
                             (300, 1.0, 'run')]),
        ('满费+子弹 2X设置', [(300, 2.0, 'run'), (300, 2.0, 'maxbullet'),
                             (300, 2.0, 'run')]),
        ('暂停 60 秒', [(3600, 1.0, 'pause')]),
        # 手动暂停计数：按住的 5 秒不计，放开后不跳（重新起锚）
        ('手动暂停计数', [(300, 1.0, 'run'), (300, 1.0, 'manual'),
                                 (300, 1.0, 'run')]),
        ('关卡外 10 秒', [(600, 1.0, 'idle')]),
        # 关卡外：条 ROI 压在菜单 / 仓库的高亮 UI 上（死的和在动的都来一遍）。
        # HUD 没画出来 -> 从关卡外**进**不了关卡（state.Counter 里那条规则）。
        ('关卡外假白条', [(600, 1.0, 'fakebar')]),
        ('关卡外假白条在动', [(600, 1.0, 'fakebarmove')]),
        # 倍速数字画着但模板对不上：沿用上次读到的倍速接着数，不要冻结丢帧。
        ('2X 数字糊了', [(300, 2.0, 'run'), (300, 2.0, 'blur')]),
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
    # 满费 + 倍速键**整块空白**：这一条是**用户 2026-10-10 定的规则** ——
    # 拖干员进子弹时间时游戏把那颗键整块收起来（>>> 我们读不到），所以改成
    # 「这一局里读到过倍速数字」之后再空白就按子弹时间算（恒 BULLET_FPS = 6 帧/秒），
    # 不再冻在那儿（用户报的「满费下子弹时间过帧无法识别」就是冻结那一段）。
    # 注意**和「数字糊了」的区别**：那一种是「键画着、字形认不出来」（走记忆倍速），
    # 这种是「键压根没画」—— 判据就是 features.SpeedReading.drawn。
    # 代价：万一某次空白其实是按钮淡出而不是子弹时间，这一段会按 1/5 记（少记 5 倍），
    # 所以 warn 单列 bullet-assumed，面板 / CSV 上看得见。见 docs 第 32 节。
    got, truth, _ = simulate([(300, 1.0, 'run'), (300, 1.0, 'maxblind'), (300, 1.0, 'run')])
    expect = 300 + 30     # 两段 1X 各 150 帧；中间满费空白 5 秒按 6 帧/秒 = 30 帧
    ok = abs(got - expect) <= 2
    print('%-18s %10d %10d %8s  %s'
          % ('满费+空白(见过数字)', got, expect, '%+d' % (got - expect), 'OK ' if ok else 'BAD'))
    if not ok:
        bad += 1
    # 对照组：**从没见过**倍速数字（刚进关卡就是空白）-> 仍旧冻结，不许按 1/5 猜。
    got, truth, _ = simulate([(300, 1.0, 'runblind'), (300, 1.0, 'maxblind'),
                              (300, 1.0, 'runblind')])
    expect = 300          # 两段 1X 各 150 帧；中间 300 拍冻结
    ok = abs(got - expect) <= 2
    print('%-18s %10d %10d %8s  %s'
          % ('满费+空白(没见过数字)', got, expect, '%+d' % (got - expect), 'OK ' if ok else 'BAD'))
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
