#!/usr/bin/env python
"""三件 UI 的状态判定自检（费用条 / 暂停键 / 倍速键）—— 重点：**别在关卡外计数**。

为什么单独一个检查：这三个读数**各自单看**都会被「不在关卡里」的画面骗，实测三种都有：
  - 费用条：菜单 / 仓库 / 切场景时条 ROI 压在别的高亮 UI 上，会读出一条**宽度恒定**的
    假白条（2026-10-05 那段会话：fill=154 连着 3.1 秒、fill=215 连着 1.5 秒），
    状态是 TRACK —— 只按「条在不在」判就会一路放行；
  - 暂停键：那些画面上 ROI 里只有细笔画 / 底色，亮点占比能到 0.09，看着像「运行中」；
  - 倍速键：同上，场景亮块让「箭头带实心占比」过 0.02，被读成子弹时间（速率差 5 倍）。
所以两个读数的判据都落到「**画出来没有**」—— 3x3 腐蚀一遍之后的实心占比：
  - 暂停键  >= PAUSE_GLYPH_MIN = 0.020（实测关卡内 0.031~0.235，关卡外 0.000~0.0146）
  - 倍速箭头 >= ARROW_SOLID_MIN  = 0.020（实测真箭头 p5=0.0310，非箭头 max=0.0113）
    —— 2026-10-10 起它**只用来判「这颗键画出来没有」**（state 的关卡判定）。
    子弹时间改用**三个三角的形状分**（features._glyph_score，阈值 GLYPH_NCC_MIN = 0.55）：
    实心占比那条在亮背景关卡上两头不靠 —— 真三角在箭头带**上方**（ROI 行 40..81，
    带是行 70..115），本文件下面留了一条**反例**钉着这件事。
关卡判定（state.Counter）：从关卡外**进**关卡必须看到 HUD；菜单压出来的亮块是死的，
画不出 HUD，所以永远进不来。已经在关卡里才放宽成「HUD 或者条这一拍确实在动」。

检查分两段：
  1. 合成：假白条（死的 / 在动的）+ HUD 没画 -> 一拍都不许记；对照：同样在动、
     但 HUD 画着 -> 照常记（证明「记 0 帧」不是因为假条读不出来）。
  2. 实测帧：拿录像里的真像素钉住上面两组阈值，以及「关卡外的加载画面不许被
     认成子弹时间」。录像在用户机器上，找不到就跳过。

用法：python tools/check_ui_state.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.cli import DEFAULT_TEMPLATES, load_templates            # noqa: E402
from akframe.features import (GLYPH_NCC_MIN, BarReading, PauseReading,  # noqa: E402
                              SpeedReading, read_bar, read_pause, read_speed)
from akframe.geometry import PAUSE_GLYPH_MIN                        # noqa: E402
from akframe.state import Counter                                    # noqa: E402
from akframe.video import VideoSource                                # noqa: E402

BAR_W = 240
FPS = 60.0
FAIL = []

# ---- 合成读数 ------------------------------------------------------------------
# HUD 画着：❚❚（运行中，实心占比 0.22）
HUD_RUN = PauseReading(ratio=0.24, running=True, drawn=True, solid=0.22)
# HUD 没画：菜单里 ROI 压到文字 / 高亮块。ratio 故意给 0.24（够到「运行中」那条线），
# 实心占比给 0.004（实测关卡外 <= 0.0146）—— 占比高不等于画着字形。
NO_HUD = PauseReading(ratio=0.24, running=True, drawn=False, solid=0.004)
# 倍速键：画着数字 2X / 什么都没画（实测关卡外箭头实心占比 <= 0.0113）
SPEED_2X = SpeedReading(digit_ratio=0.30, corr_2x=0.99, has_digit=True, m=2.0,
                        bullet=False, conf='ok', solid=0.050)
NO_SPEED = SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                        m=float('nan'), bullet=False, conf='low', drawn=False, solid=0.011)


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name,
                         ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def bar(fill, state='TRACK'):
    return BarReading(fill=fill, grey146=0, grey88=0, state=state,
                      row=np.zeros(BAR_W, np.int16))


def count(fills, pause, speed):
    """把一串条宽喂给状态机，返回累计逻辑帧。"""
    c = Counter(BAR_W, fps=FPS)
    for i, f in enumerate(fills):
        c.step(i, i / FPS, bar(f), pause, speed)
    return c.elapsed


# 一费 30 帧的正常台阶（1 倍速 + 60Hz 采样 = 每拍 0.5 帧 -> 条宽 4px 一档）
STAIRS = []
for j in range(120):                       # 4 个费用周期
    STAIRS.append(min(BAR_W, int(round(j % 30 * BAR_W / 30.0))))


def synth():
    """合成：假白条（死的 / 在动的）都不许记；同样在动但 HUD 画着要照常记。"""
    dead = [239] * 600
    moving = [100 + (i % 40) for i in range(600)]
    got_dead = count(dead, NO_HUD, NO_SPEED)
    check('关卡外·死的假白条（HUD 没画）一拍不记', got_dead == 0, '记了 %d 帧' % got_dead)
    got_move = count(moving, NO_HUD, NO_SPEED)
    check('关卡外·在动的假白条（HUD 没画）一拍不记', got_move == 0, '记了 %d 帧' % got_move)
    # 对照：把同一个「在动的假白条」配上画着的 HUD，条动照记 —— 说明上面那个 0
    # 是「HUD 没画」这条规则管的，不是因为条读不出来。
    got_ctl = count(moving, HUD_RUN, SPEED_2X)
    check('对照·同样的条在动、HUD 画着 -> 照常记', got_ctl > 0, '记了 %d 帧' % got_ctl)
    got_real = count(STAIRS, HUD_RUN, SPEED_2X)
    check('关卡内的一费台阶照常记（4 个周期）', 110 <= got_real <= 122,
          '记了 %d 帧（真值 120）' % got_real)


# ---- 实测帧 --------------------------------------------------------------------

# 实测录像：这几段在**用户机器上**，找不到就整段跳过（公开仓库里不该有录像）。
# 路径是 NVIDIA / OBS 的默认录制目录，文件名对得上就行。
VIDEO_DIRS = [
    os.path.join(os.path.expanduser('~'), 'Videos', 'obs'),
    os.path.join(os.path.expanduser('~'), 'Videos', 'NVIDIA', 'Arknights'),
]

VIDEOS = [
    # (录像文件名, 说明, 关卡外的帧号区间列表)；(起, 止) 闭区间，止写 None = 到片尾
    ('2026-10-04 22-20-20.mp4',
     '2026-10-04 22-20-20（2 倍速 + 拖干员）',
     [(0, 52), (2944, None)]),      # 开头是进关卡前的加载画面，片尾是打完之后的界面
    ('Arknights 2026.10.03 - 23.53.21.03.mp4',
     '2026-10-03 23.53.21（满费 + 暂停，全程关卡内）', []),
]


def find_video(name):
    for d in VIDEO_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def scan(path):
    """逐帧跑一遍录像，返回读数列表。

    走 VideoSource（不是直接读 out/cache 里的 raw）：ROI 是**外扩**抽流的，
    贴边的那几个像素要按 pad 偏移切出来，VideoSource.frame() 才是切好的那份。
    """
    tpl = load_templates(DEFAULT_TEMPLATES)
    src = VideoSource(path, save_on_auto=False, progress=lambda *a: None)
    c = Counter(src.layout.bar_w, fps=src.fps)
    rows = []
    try:
        for i in range(len(src)):
            b, p, s = src.frame(i)
            br = read_bar(b, src.layout)
            pr = read_pause(p, src.layout)
            sr = read_speed(s, src.layout, tpl)
            r = c.step(i, i / src.fps, br, pr, sr)
            rows.append(dict(i=i, state=br.state, fill=br.fill, p_drawn=pr.drawn,
                             p_solid=pr.solid, s_drawn=sr.drawn, s_solid=sr.solid,
                             has_digit=sr.has_digit, bullet=sr.bullet,
                             s_glyph=sr.glyph, mode=r.mode, delta=r.delta))
    finally:
        src.close()
    return rows


def outside_ranges(ranges, n):
    out = set()
    for a, b in ranges:
        b = n - 1 if b is None else min(b, n - 1)
        out.update(range(a, b + 1))
    return out


def real_frames():
    for name, note, outside in VIDEOS:
        path = find_video(name)
        if path is None:
            print('⏭  跳过（找不到录像 %s）' % name)
            continue
        rows = scan(path)
        print('\n%s' % note)
        n_out = sum(1 for r in rows if not r['p_drawn'] and not r['s_drawn'])
        print('     %d 帧；两件 UI 都没画的帧 %d' % (len(rows), n_out))
        dead = outside_ranges(outside, len(rows))
        # 1) 关卡外那一段：一拍都不许记（真值 0）
        if dead:
            seg = [r for r in rows if r['i'] in dead]
            got = sum(int(r['delta']) for r in seg)
            check('实测·关卡外（加载画面 / 片尾界面）：一帧都没记', got == 0,
                  '%d 帧里记了 %d 帧' % (len(seg), got))
        # 2) 关卡内：暂停键一直画着（实测只在进出的那一两拍上读不到）
        near = set()
        for i in dead:                       # 关卡内外的交界那几拍不算（正在切场景）
            near.update(range(i - 8, i + 9))
        inside = [r for r in rows if r['i'] not in dead and r['i'] not in near]
        miss = [r['i'] for r in inside if not r['p_drawn']]
        check('实测·关卡内暂停键一直画着', len(miss) <= 3,
              '读不到的有 %d 帧（%s）' % (len(miss), miss[:6]))
        solids = [r['p_solid'] for r in inside if r['p_drawn']]
        if solids:
            check('实测·关卡内暂停键的实心占比都在阈值之上',
                  min(solids) >= PAUSE_GLYPH_MIN,
                  'min=%.4f 中位=%.4f max=%.4f（阈值 %.3f）'
                  % (min(solids), float(np.median(solids)), max(solids),
                     PAUSE_GLYPH_MIN))
        # 3) 子弹时间：判据是倍速键上**三个三角的形状分**（features._glyph_score），
        #    不是「箭头带里有没有实心白块」—— 后者为什么不行，看紧接着的那条反例。
        #    只统计关卡内的帧（片尾那些界面里另有 >>> 按钮，不属于子弹时间）。
        batt = [r for r in rows if r['i'] not in dead]
        tri = [r for r in batt if r['bullet']]
        other = [r for r in batt if not r['bullet'] and not r['has_digit']]
        if tri and other:
            lo = min(r['s_glyph'] for r in tri)
            hi = max(r['s_glyph'] for r in other)
            check('实测·形状分把「三个三角」和「什么都没画」分开',
                  lo > hi and lo - hi >= 0.03 and hi < GLYPH_NCC_MIN,
                  '三角 min=%+.3f（%d 帧）｜其它 max=%+.3f（%d 帧）｜差 %.3f｜阈值 %.2f'
                  % (lo, len(tri), hi, len(other), lo - hi, GLYPH_NCC_MIN))
            if len(tri) <= 4:
                print('     注意：这一段里只有 %d 帧判成三角（%s），是场面顶过去的误报，'
                      '不是真子弹时间'
                      % (len(tri), sorted(round(r['s_glyph'], 3) for r in tri)))
        # 4) 反例（钉在这儿不许再回去）：老判据看的「箭头带实心占比」分不开这两类 ——
        #    真三角帧的实心占比能低到 0.0000（三角在带**上方**），没画三角的帧能高到 0.5+
        #    （亮背景关卡里场景亮块顶上来）。修之前就是靠它判子弹时间的。
        if tri and other:
            a_min = min(r['s_solid'] for r in tri)
            b_max = max(r['s_solid'] for r in other)
            check('实测·老判据（箭头带实心占比）分不开这两类（反例）', a_min <= b_max,
                  '三角 min=%.4f｜其它 max=%.4f —— 真三角也能是 0.0000' % (a_min, b_max))


def main() -> int:
    synth()
    real_frames()
    print()
    if FAIL:
        print('%d 项没过：%s' % (len(FAIL), '；'.join(FAIL)))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
