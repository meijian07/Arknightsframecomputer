# -*- coding: utf-8 -*-
"""框选校准自检：参照图的参照物、左端吸附、以及「表里那份 ROI 对不对」的判据。

为什么单独一个检查：这一段是**用户实测踩出来的洞**（原话：「手动截取不准确，需要参照物」
「现在完全识别不到」），而它全靠几条像素判据撑着 —— 判据宽一点就会把错位的 ROI 当成好的
（那正是整条录像链路只数出 0 帧的原因），紧一点又会把好 ROI 误判成坏的。所以这里用
**造出来的**费用条灰度栈把每一条边界都钉一遍，不碰 ffmpeg、不碰文件，跑完一两秒。

检查什么：
    1. judge_bar_layout：白块左端压在 ROI 左边缘 -> 通过；
    2. 错位的 ROI（实测那份：白块起点比 ROI 左边缘右 29px）-> 否掉，并报出差多少；
    3. ROI 左边缘压在条槽里面（反向错位）-> 也要否掉（否则相位会整体偏）；
    4. 条空着 / 全程满费（一帧白块都没有）-> 返回「没法判断」，不能当成错；
    5. 白块左端在抖（那不是条槽）-> 否掉；
    6. layout_from_bar_box：条槽宽 -> 客户区尺寸 / 原点换算，含右端被画面裁掉；
    7. snap_box：框的左端圈进来一截背景 -> 吸附到白块起点，右端不动；
    8. 参照图 / 放大镜画得出来（尺寸对、不越界）。
"""

import os
import sys
from dataclasses import replace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe import boxpick                                              # noqa: E402
from akframe.geometry import (BAR_ROI_H, Rect, layout_for,               # noqa: E402
                              layout_from_bar_box)
from akframe.features import fill_run                                  # noqa: E402
from akframe.video import bar_probe, judge_bar_layout                    # noqa: E402

FAIL = []


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def make_layout(vw, vh, bar):
    """造一份只有 bar 有用的 layout（judge_bar_layout 只看 bar_band 和 width）。"""
    base = layout_for(min(vw, 1920), min(vh, 1080), 1.0)
    out = replace(base, width=int(vw), height=int(vh), bar=bar)
    out.validate()
    return out


def make_strip(n, slot_left, fill0, per_frame, w=400, h=BAR_ROI_H, slot_right=None,
               jitter=0):
    """造一段费用条灰度栈：深底 + 暗轨道 + 白填充（每帧长 per_frame 像素）。"""
    strip = np.full((n, h, w), 30, np.uint8)
    right = w - 1 if slot_right is None else int(slot_right)
    rng = np.random.RandomState(7)
    for i in range(n):
        strip[i, :, int(slot_left):right + 1] = 60            # 轨道
        start = int(slot_left) + (int(rng.randint(-jitter, jitter + 1)) if jitter else 0)
        fw = fill0 + i * per_frame
        if fw > 0:
            end = min(right, start + fw - 1)
            if end >= start:
                strip[i, :, start:end + 1] = 255              # 白填充
    return strip


# 下面这些 strip 的列 0 = 画面 x=0（判据里的 left_col 就是「ROI 左边缘在 strip 里的列号」；
# 真链路里 strip 从 ROI 左边 lpad 列开始，所以 left_col = lpad）。
# ---- 1. 白块左端压在 ROI 左边缘：通过 ----
# 条槽左端在 40，ROI 左边缘也在 40（layout 的约定就是 bar.x = 条槽左端），填充每帧长 3px
strip = make_strip(12, slot_left=40, fill0=30, per_frame=3)
lay = make_layout(400, 200, Rect(40, 100, 300, BAR_ROI_H))
ok, why = judge_bar_layout(strip, lay, left_col=40)
check('白块压在 ROI 左边缘 -> 通过', ok is True, why)

# ---- 2. 实测那份错位的 ROI：白块起点比 ROI 左边缘右 29px -> 否掉 ----
# 用户表里那份 bar.x=2334，而画面里白块从 2363 开始；这里把 ROI 左边缘放在白块左边 29px
lay_off = make_layout(400, 200, Rect(11, 100, 300, BAR_ROI_H))
ok, why = judge_bar_layout(strip, lay_off, left_col=11)
check('错位 29px 的 ROI -> 否掉', ok is False and '+29' in why, why)

# ---- 3. 反向错位：ROI 左边缘压在条槽里面（白块起点在框外左边）-> 否掉 ----
ok, why = judge_bar_layout(strip, lay, left_col=46)
check('ROI 左边缘偏右 6px -> 否掉', ok is False and '-6' in why, why)

# ---- 4. 一帧白块都没有（条空着 / 满费）-> 「没法判断」，不能算错 ----
dark = np.full((12, BAR_ROI_H, 400), 40, np.uint8)
ok, why = judge_bar_layout(dark, lay, left_col=40)
check('没有白块 -> 没法判断（None）', ok is None, why)

# ---- 5. 白块左端在抖（不是条槽）-> 否掉 ----
jittery = make_strip(12, slot_left=40, fill0=30, per_frame=3, jitter=8)
ok, why = judge_bar_layout(jittery, lay, left_col=40)
check('左端抖动 -> 否掉', ok is False and '抖' in why, why)

# ---- 6. 条槽宽 -> 客户区换算 ----
# 1920 宽的客户区：条槽 180px。框 180 宽 -> 客户区 1920x1080，原点由框定
client, offset, lay2 = layout_from_bar_box((2560, 1600), Rect(2363, 1124, 180, 17), 1.0)
check('框 180px -> 客户区 1920x1080', client == (1920, 1080), str(client))
check('框左端 = 条槽左端', lay2.bar.x == 2363 and lay2.bar.w == 180, str(lay2.bar))
check('条槽宽记成 180（真宽）', abs(lay2.bar_width_f - 180.0) < 1e-6, str(lay2.bar_width_f))
# 2549 宽的客户区（游戏窗口 2549x1441 那种）：s=1.3278 -> 条槽 239.x
client3, _off3, lay3 = layout_from_bar_box((2560, 1600), Rect(2300, 1100, 239, 17), 1.0)
check('框 239px -> 客户区 2549x?, 条槽 239', client3[0] == 2549, str(client3))
# 框跑到画面外面：宁可报错也不要算出一份错的 ROI
try:
    layout_from_bar_box((2560, 1600), Rect(2450, 1100, 239, 17), 1.0)
    raised = False
except ValueError as exc:
    raised = '框跑到窗口外面' in str(exc)
check('框超出画面 -> 报错（不硬算）', raised)

# ---- 7. 吸附左端：框把条槽左边多圈进来 12px ----
# 注意坐标：judge_bar_layout 吃的是**帧栈**（多帧一起判），snap_box 吃的是**单帧图**
# （界面上拖动时会实时吸一次，此时手上只有光标下那一帧）。所以这里要把首帧取出来，
# 框的 y 也就从「栈里的行」变成「帧里的行」——帧高就是 BAR_ROI_H。
bar0 = strip[0]                   # 单帧（h x w），白块从 40 开始
box = Rect(28, 0, 320, BAR_ROI_H)
new, shift, why = boxpick.snap_box(bar0, box, (4, 11))
check('吸附左端：右移 12px', shift == 12 and new.x == 40, '%s %s' % (shift, new))
check('吸附左端：右端不动', new.x + new.w == box.x + box.w, str(new))
# 框里没有白块（条空着）-> 原样返回
box2 = Rect(28, 0, 320, BAR_ROI_H)
dark_box, shift2, why2 = boxpick.snap_box(dark[0], box2, (4, 11))
check('条空着时吸附不动', shift2 == 0 and dark_box == box2, why2)
# 白块在框外很远（超出吸附范围）-> 不动
far = make_strip(12, slot_left=300, fill0=40, per_frame=1)
box3 = Rect(20, 0, 200, BAR_ROI_H)
_box4, shift3, why3 = boxpick.snap_box(far[0], box3, (4, 11))
check('白块离框太远 -> 不吸（那多半是框错了）', shift3 == 0, why3)

# ---- 8. 参照图 / 放大镜 ----
frame = np.full((1600, 2560), 50, np.uint8)
frame[1124:1141, 2363:2543] = 255
gbar = make_layout(2560, 1600, Rect(2363, 1124, 180, BAR_ROI_H))
img = boxpick.guide_image(frame, gbar)
check('参照图尺寸 = 画面尺寸', img.size == (2560, 1600), str(img.size))
zoom = boxpick.magnify(frame, Rect(2363, 1120, 180, 25), panel=(556, 186),
                       layout=gbar, band=(1129, 1136))
check('放大镜出图且不越界', zoom.size[0] <= 560 and zoom.size[1] <= 200, str(zoom.size))
band0, col0 = bar_probe(gbar)
check('bar_probe 往左留出 %d 列' % 12, band0.x == 2363 - 12 and col0 == 12,
      '%s left_col=%d' % (band0, col0))
# 贴到画面左上角时不能出界
gbar2 = make_layout(2560, 1600, Rect(3, 5, 180, BAR_ROI_H))
band1, col1 = bar_probe(gbar2)
check('ROI 贴左边缘时 bar_probe 不越界', band1.x == 0 and col1 == 3, str(band1))

# ---- 9. 「跳到最满的一帧」：扫的必须是**条带那几行**，不是整块探针框 ----
# 实测踩过（真录像里量出来的）：探针框 = 条 ROI 上下各留 4 行（probe_rect），而白块只占
# 中间那几行 —— 对整块 26 行取中位数，白块会被上下 8 行轨道灰稀释掉，结果是「一帧白块都
# 没扫到」，「跳到最满的一帧」永远返回 -1。
check('探针框的条带行号（26 行的框）', boxpick.band_rows_of(Rect(20, 100, 220, 26)) == (9, 16),
      str(boxpick.band_rows_of(Rect(20, 100, 220, 26))))
check('条 ROI 自己传进来时退化成 (5, 12)',
      boxpick.band_rows_of(Rect(0, 0, 180, BAR_ROI_H)) == (5, 12),
      str(boxpick.band_rows_of(Rect(0, 0, 180, BAR_ROI_H))))

probe = Rect(20, 100, 220, 26)          # 条竖直居中：条顶在 100 + (26-17)//2 = 104
strip2 = np.full((8, 26, 220), 30, np.uint8)
for i in range(8):
    strip2[i, 9:17, 30:200] = 60                          # 只有条带那 8 行是轨道
    strip2[i, 9:17, 30:30 + 13 + i * 4] = 255             # 白块每帧长 4px
# 先证明「对整块取中位数」确实扫不到（这就是修之前那条路）
naive = fill_run(np.median(strip2[0].astype(np.int16), axis=0))
check('整块中位数扫不到白块（修之前那条路的病）', naive is None, str(naive))
ends, starts = boxpick.fill_extents(strip2, probe)
check('fill_extents 扫到白块（条带行）',
      int(starts[0]) == 20 + 30 and int(ends[0]) == 20 + 42,
      '左端 %d 右端 %d' % (starts[0], ends[0]))
k = int(np.argmax(ends))
check('最满的一帧 = 第 7 帧（白块最宽那帧）', k == 7, 'argmax=%d' % k)
strip3 = np.full((5, 26, 220), 40, np.uint8)              # 全程没有白块（条空着）
e3, s3 = boxpick.fill_extents(strip3, probe)
check('没白块的帧记 -1（不参与比最满）', bool((e3 == -1).all() and (s3 == -1).all()), str(e3))

print()
if FAIL:
    print('FAILED: %s' % ', '.join(FAIL))
    sys.exit(1)
print('全部通过')