#!/usr/bin/env python
"""减号框（局内负费）的几何体检：框住没有、会不会误报、跟不跟着条走。

为什么单独一个检查：负费判错不是「多记一帧」，是整段被数成**一半**（见 docs 第 31 节）。
判据（纯白 + 连续够长）由 check_state_synth 的第 12 组管，但那组喂的是**合成读数**；
「这个框到底框住减号没有」只有几何说了算 —— 而这里最容易错的就是锚点：框按画面右下角算
还是按客户区 / 费用条算，在桌面录像里能差 105px。

所以这里用一段**真录像量出来的坐标**画一张假画面（不读文件、不算游戏），把四件事钉死：
    1. 两条真路（实时客户区 / 录像框选）算出来的框，减号是不是真在里面、判据是不是真的亮；
    2. 把减号擦掉之后，数字笔画 / 菱形图标 /「剩余可放置角色」那行字会不会被误判；
    3. 旧锚点（按整幅画面右下角算）为什么会漏 —— 它是**反例**，钉在这儿不许再回去；
    4. 条挪了框跟着挪（sign 是按条反推的，不是按画面底边）。

钉的这组数字来自真负费录像 `Desktop 2026.10.09 - 22.36.10.14.mp4`
（2560x1600 桌面录像、客户区 2560x1440 落在画面 (0,54)、条槽 240px、减号 41x5）。
整段 1994 帧用真 read_sign 跑过：负费段 1768/1768 判对、闲时 226 帧零误报（最长横杠 7px）。

用法：python tools/check_sign_box.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.features import read_sign                                    # noqa: E402
from akframe.geometry import (Rect, layout_for, layout_from_bar_box,      # noqa: E402
                              reframe_layout, replace, sign_rect)

FAIL = []

# ---- 真录像实测坐标（画面坐标；客户区从这里看就是 y54 往下那 1440 行）----
FRAME = (2560, 1600)
CLIENT = (2560, 1440)
OFFSET = (0, 54)
BAR_BOX = Rect(2320, 1133, 240, 8)        # 条槽 x2320..2559，条身 y1133..1140
MINUS = (2422, 2462, 1079, 1083)          # 减号 41 x 5
TEXT_TOP = 1160                           # 「剩余可放置角色：7」那行字的最高一行
THR_UNIT = 21.0

_TEXT = ((1160, 1160, 2333, 2361), (1161, 1161, 2378, 2393), (1162, 1162, 2417, 2432),
         (1163, 1163, 2492, 2508), (1168, 1168, 2372, 2399), (1169, 1169, 2410, 2438),
         (1170, 1170, 2331, 2362), (1175, 1175, 2375, 2399), (1179, 1179, 2413, 2437),
         (1183, 1183, 2374, 2399), (1191, 1191, 2331, 2363), (1191, 1191, 2416, 2438))


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def _band(a, y0, y1, x0, x1, value=255):
    a[y0:y1 + 1, x0:x1 + 1] = value


def make_frame(with_minus=True):
    """按上面那组真坐标画一张假 HUD：满条、数字、菱形、那行字。"""
    a = np.full((FRAME[1], FRAME[0]), 40, np.uint8)
    _band(a, 1133, 1140, 2320, 2559)          # 费用条（满）
    _band(a, 1050, 1113, 2519, 2524)          # 数字 "4" 的竖笔
    _band(a, 1092, 1096, 2477, 2525)          # 数字 "4" 的横笔（49px，比阈值长）
    _band(a, 1085, 1086, 2334, 2347)          # 菱形图标左半
    _band(a, 1085, 1086, 2358, 2391)          # 菱形图标右半（34px，比阈值长）
    for y0, y1, x0, x1 in _TEXT:              # 「剩余可放置角色：7」
        _band(a, y0, y1, x0, x1)
    if with_minus:
        _band(a, MINUS[2], MINUS[3], MINUS[0], MINUS[1])
    return a


def layouts():
    """三条路各给一份布局（都换算成**画面坐标**）。"""
    live = reframe_layout(layout_for(CLIENT[0], CLIENT[1]), OFFSET[0], OFFSET[1],
                          FRAME[0], FRAME[1])
    _client, _off, video = layout_from_bar_box(FRAME, BAR_BOX, 1.0)
    # 反例：直接按整幅画面（而不是客户区）的右下角算 —— 桌面录像里就是这么偏掉的
    wrong = replace(video, sign=sign_rect(FRAME[0], FRAME[1], video.scale))
    return (('实时客户区', live), ('录像框选', video), ('旧锚点', wrong))


def read(a, lay):
    sg = lay.sign
    return read_sign(a[sg.y:sg.y + sg.h, sg.x:sg.x + sg.w], lay)


def thr_of(lay) -> int:
    return max(6, int(round(THR_UNIT * lay.scale)))


def main() -> int:
    a_neg = make_frame(True)
    a_pos = make_frame(False)              # 减号擦掉，其余照旧
    print('减号真值 x%d..%d y%d..%d（%d x %d）  条身 y1133..1140  文字行 y%d 起'
          % (MINUS[0], MINUS[1], MINUS[2], MINUS[3], MINUS[1] - MINUS[0] + 1,
             MINUS[3] - MINUS[2] + 1, TEXT_TOP))
    lays = layouts()
    for tag, lay in lays:
        sg = lay.sign
        print('     %-10s sign x%d..%d y%d..%d' % (tag, sg.x, sg.x2, sg.y, sg.y2))
    print()

    real = lays[:2]
    # 1) 两条真路：横向必须把 41px 的减号**整条**框进去（横向一切 run 就短了，那是靠阈值在赌），
    #    纵向只要压住减号的一行就够（run 是沿 x 数的，横杠整行都在框里就是全宽）。
    for tag, lay in real:
        sg = lay.sign
        full_x = sg.x <= MINUS[0] and sg.x2 >= MINUS[1]
        rows_in = max(0, min(sg.y2, MINUS[3]) - max(sg.y, MINUS[2]) + 1)
        r = read(a_neg, lay)
        check('框住减号 / ' + tag,
              full_x and rows_in >= 1 and r.negative and r.run == MINUS[1] - MINUS[0] + 1,
              '横向%s；减号 5 行里压住 %d 行；run=%d（阈值 %d）判=%s'
              % ('含' if full_x else '切', rows_in, r.run, thr_of(lay), r.negative))
    # 2) 擦掉减号：数字 / 菱形 / 那行字都不许顶上来
    for tag, lay in real:
        r = read(a_pos, lay)
        check('无减号不误报 / ' + tag, r.run == 0 and r.negative is False,
              'run=%d（阈值 %d；真录像闲时 226 帧最长 7px）' % (r.run, thr_of(lay)))
    # 3) 反例：旧锚点看不见减号 —— 真录像里整段 1768 帧一帧都没认出来
    tag, lay = lays[2]
    r = read(a_neg, lay)
    dt = lay.sign.y - MINUS[3]
    check('旧锚点漏掉减号（反例）/ ' + tag, (not r.negative) and r.run < thr_of(lay),
          '框在减号下方 %dpx，run=%d（阈值 %d）判=%s —— 这一段会整段按双倍速率推'
          % (dt, r.run, thr_of(lay), r.negative))
    check('旧锚点压在文字行上（反例）/ ' + tag, lay.sign.y2 >= TEXT_TOP,
          'sign y%d..%d 盖住文字行 y%d..' % (lay.sign.y, lay.sign.y2, TEXT_TOP))
    # 4) 真路的框不许碰到条 / 文字行（碰上了就是靠阈值在赌）
    for tag, lay in real:
        sg = lay.sign
        check('框不碰条和文字行 / ' + tag, sg.y2 < lay.bar.y and sg.y2 < TEXT_TOP,
              'sign y%d..%d  条 y%d  文字 y%d' % (sg.y, sg.y2, lay.bar.y, TEXT_TOP))
    # 5) 条挪多少，框挪多少（sign 是按条反推的）
    _c, _o, base = layout_from_bar_box(FRAME, BAR_BOX, 1.0)
    moved_box = Rect(BAR_BOX.x - 30, BAR_BOX.y - 25, BAR_BOX.w, BAR_BOX.h)
    _c2, _o2, moved = layout_from_bar_box(FRAME, moved_box, 1.0)
    dx = moved.sign.x - base.sign.x
    dy = moved.sign.y - base.sign.y
    check('条挪框跟着挪', abs(dx + 30) <= 1 and abs(dy + 25) <= 1,
          '条 -30,-25 -> 框 %+d,%+d' % (dx, dy))
    print()
    if FAIL:
        print('失败项：%s' % '、'.join(FAIL))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())