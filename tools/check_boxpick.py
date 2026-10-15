# -*- coding: utf-8 -*-
"""框选校准自检：参照图的参照物、放大镜（四条边 / 平移 / 局部放大）、以及
「表里那份 ROI 对不对」的判据。

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
    7. 参照图 / 放大镜画得出来（尺寸对、不越界）—— 含按钮 ROI 那一档；
    8. 「跳到最满的一帧」：扫的必须是**条带那几行**，不是整块探针框；
    9. move_boxes：两个按钮的框**一起**挪、一起夹边界（相对位置不能散）；
    10. magnify 的 pan、局部放大（zoom）与四条边：视窗撑到面板的宽高比、两个轴同一个
        倍数（画面不变形）、放大图铺满面板；pan = 视窗相对**框中心**的偏移（挪框时视窗
        跟着走 = 彩线在图上的列不动，用户 2026-10-15：「应该和框同方向移动」）；
        越界的边不画；zoom > 1 只看框里的一小块、zoom < 1 能看见框周围
        （缩到底 = 整幅画面）、超出 ZOOM_MAX 被夹住。
    11. resize_box / mirror_resize（2026-10-12 第 48 轮：用户要的是「就像 Windows 的截图
        一样可以拖动框放大缩小移动」）：八个 kind 各只动该动的那条边 / 那个角；位移是
        相对**按下那一刻**算的（抖出去再抖回来就回到原框，不攒取整误差）；拉过头夹在画面
        和最小尺寸上；联动那一对按**四条边的差值**搬 —— 挪块和拉边都覆盖，两个框永远一样大。
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

# ---- 7. 参照图 / 放大镜 ----
frame = np.full((1600, 2560), 50, np.uint8)
frame[1124:1141, 2363:2543] = 255
gbar = make_layout(2560, 1600, Rect(2363, 1124, 180, BAR_ROI_H))
img = boxpick.guide_image(frame, gbar)
check('参照图尺寸 = 画面尺寸', img.size == (2560, 1600), str(img.size))
zoom = boxpick.magnify(frame, Rect(2363, 1120, 180, 25), panel=(556, 186),
                       layout=gbar, band=(1129, 1136))
check('放大镜出图且不越界', zoom.size[0] <= 560 and zoom.size[1] <= 200, str(zoom.size))
# 按钮 ROI（那一档 200x160）也要放得下、而且真的是**放大**（不是被裁掉一块）
zoom2 = boxpick.magnify(frame, Rect(2280, 40, 200, 160), panel=(556, 186))
check('按钮 ROI 的放大镜：出图、不越界、比原图大',
      zoom2.size[0] <= 556 and zoom2.size[1] <= 186
      and zoom2.size[0] * zoom2.size[1] > 200 * 160, str(zoom2.size))
band0, col0 = bar_probe(gbar)
check('bar_probe 往左留出 %d 列' % 12, band0.x == 2363 - 12 and col0 == 12,
      '%s left_col=%d' % (band0, col0))
# 贴到画面左上角时不能出界
gbar2 = make_layout(2560, 1600, Rect(3, 5, 180, BAR_ROI_H))
band1, col1 = bar_probe(gbar2)
check('ROI 贴左边缘时 bar_probe 不越界', band1.x == 0 and col1 == 3, str(band1))

# ---- 8. 「跳到最满的一帧」：扫的必须是**条带那几行**，不是整块探针框 ----
# 实测踩过（真录像里量出来的）：探针框 = 条 ROI 上下各留 4 行，而白块只占
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

# ---- 9. 两个按钮的框：一起挪、一起夹边界（取景器框按钮用）----
# 用户 2026-10-11：「暂停键倍速键也会截取不到，需要手动校准」。那两个按钮的框是
# **联动**的 —— 游戏里它们固定间距、一起锚在客户区右上角，联动就靠这个纯函数：
# 一组框共用同一个位移，各自夹边界会把相对位置弄散（等于把一个框挪到别处去）。
size10 = (1378, 844)
pause0 = Rect(1100, 30, 200, 160)
speed0 = Rect(880, 30, 200, 160)
boxes = {'bar': Rect(1189, 560, 114, 17), 'pause': pause0, 'speed': speed0}
moved = boxpick.move_boxes(boxes, size10, 7, -12, ('pause', 'speed'))
check('联动：两个按钮按同一个位移走',
      moved['pause'] == Rect(1107, 18, 200, 160)
      and moved['speed'] == Rect(887, 18, 200, 160),
      '%s / %s' % (moved['pause'], moved['speed']))
check('联动：费用条那一根不动', moved['bar'] == boxes['bar'], str(moved['bar']))
check('联动：入参没被改（返回的是新字典）',
      boxes['pause'] == pause0 and boxes['speed'] == speed0, str(boxes['pause']))
far = boxpick.move_boxes(boxes, size10, 9999, 0, ('pause', 'speed'))
check('夹边界：停在画面右边', far['pause'].x + far['pause'].w == size10[0], str(far['pause']))
check('夹边界：相对位置没散（还是同一个位移）',
      far['pause'].x - far['speed'].x == pause0.x - speed0.x and far['pause'].y == pause0.y,
      '%s / %s' % (far['pause'], far['speed']))
up = boxpick.move_boxes(boxes, size10, 0, -9999, ('pause', 'speed'))
check('夹边界：停在画面顶边', up['pause'].y == 0 and up['speed'].y == 0, str(up['pause']))
check('不传 names：全部一起挪（费用条也跟着）',
      boxpick.move_boxes(boxes, size10, 1, 0)['bar'].x == 1190, 'bar.x')
check('空字典 / 空 names 都不炸',
      boxpick.move_boxes({}, size10, 3, 4) == {}
      and boxpick.move_boxes(boxes, size10, 3, 4, ()) == boxes)

# ---- 10. 放大图：平移（pan）、局部放大（zoom）、四条边 ----
# 用户 2026-10-11 第 44 轮：「手动校准界面右边的放大图要可以自己移动，不然很难对准」；
# 第 45 轮：「费用条难以准确框选，加一个可移动的局部放大功能，查看上下左右是否对齐」；
# 第 46 轮：「做局部放大！类似 ctrl＋滚轮 的还是框选放大都行」；
# 2026-10-15 第 55 轮：「移动框时放大小窗反向移动，应该和框同方向移动；放大小窗缩放很奇怪，
# 固定 4:3 的比例，包括框选的位置；ctrl 放大范围大一点」。这一节的口径跟着换成：
#
#   (1) pan = 视窗相对**框中心**的偏移 —— 挪框时视窗跟着一起走，彩线在图上的位置不动
#       （这就是「同方向」；上一版把 pan 定义成相对框左上角再反向补，彩线才会乱跑）；
#   (2) 视窗撑到**面板的宽高比**、两个轴**同一个倍数** —— 画面是原样的放大版，不拉扁，
#       也不会被冻成「打开取景器那一刻的框」那个比例；
#   (3) zoom 连续、可大可小：> 1 只看框里一小块，< 1 能看见框周围一圈（缩到底 = 整幅画面）。
grad = (np.tile(np.arange(2560, dtype=np.uint16), (1600, 1)) % 190).astype(np.uint8)
PANEL = (556, 186)
SIZE10 = (2560, 1600)
gbx = Rect(2301, 40, 199, 159)          # 右上角按钮那一档（真录像里的几何）
z0 = boxpick.magnify(grad, gbx, panel=PANEL)
z1 = boxpick.magnify(grad, gbx, panel=PANEL, pan=(-300, 0))
kx, ky = boxpick.zoom_scale(gbx, PANEL)
check('平移不改倍数（平移前后图一样大）', z0.size == z1.size,
      '%s / %s k=%s' % (z0.size, z1.size, (kx, ky)))
a0, a1 = np.asarray(z0), np.asarray(z1)
check('平移真的换了一块画面', bool((a0 != a1).any()), '一样？')
check('两个轴同一个倍数（画面不变形 —— 第 55 轮：「固定 4:3 的比例」）', kx == ky,
      '%s' % ((kx, ky),))
# 放大图铺满面板：视窗得落在整数画面像素上，撑面板时天生差半个像素 —— 差一点点没关系，
# 但不能剩一条大黑边（也不能把右边裁掉一大截）。16 倍以内实测差 ≤ 6px。
for _z in (1.0, 2.0, 4.0, 8.0, 16.0):
    _iw, _ih = boxpick.magnify(grad, gbx, panel=PANEL, zoom=_z).size
    check('放大图铺满面板（zoom=%.2f）' % _z,
          abs(_iw - PANEL[0]) <= 8 and abs(_ih - PANEL[1]) <= 8,
          '%dx%d / 面板 %s' % (_iw, _ih, PANEL))


def edge_pixels(im, col):
    """整幅图上颜色**精确等于** col 的像素的 (列集合, 行集合)。

    能够精确比色：背景是 0..189 的渐变（见上面的 grad），三个通道都到不了
    190；而这三条边线都带一个 >= 200 的通道（红 (255,70,70) / 绿 (70,230,110) /
    品红 (255,90,220)）。四条边的具体行列取决于 PIL 画线的扁移，所以下面
    用「集合的头 / 尾」断言，而不把整个集合写死。
    """
    m = (im[:, :, 0] == col[0]) & (im[:, :, 1] == col[1]) & (im[:, :, 2] == col[2])
    rows, cols = np.nonzero(m)
    return sorted(set(cols.tolist())), sorted(set(rows.tolist()))


iw0, ih0 = z0.size
# 「框摆在正中」得挑画面**中间**的框量：gbx 贴着画面右边，视窗会被画面夹住，框自然不正中
# （那是夹边界该有的样子，下面单独钉一条）。
mbx = Rect(1200, 700, 199, 159)
amid = np.asarray(boxpick.magnify(grad, mbx, panel=PANEL))
lcm, _ = edge_pixels(amid, boxpick.EDGE_LEFT)
rcm, _ = edge_pixels(amid, boxpick.EDGE_RIGHT)
_, trm = edge_pixels(amid, boxpick.EDGE_TOP)
_, brm = edge_pixels(amid, boxpick.EDGE_BOTTOM)
check('pan=0：框摆在视窗正中（上下左右四条边都等距，差 ≤ 3px）',
      abs(lcm[0] - (iw0 - 1 - rcm[-1])) <= 3 and abs(trm[0] - (ih0 - 1 - brm[-1])) <= 3,
      '左 %s 右 %s 上 %s 下 %s / 图 %dx%d'
      % (lcm[:2], rcm[-2:], trm[:2], brm[-2:], iw0, ih0))
check('左右两条离得开（红在框左边、绿在框右边，不是同一条）', lcm[-1] < rcm[0],
      '红 %s / 绿 %s' % (lcm, rcm))
lc0, _ = edge_pixels(a0, boxpick.EDGE_LEFT)
rc0, _ = edge_pixels(a0, boxpick.EDGE_RIGHT)
_, tr0 = edge_pixels(a0, boxpick.EDGE_TOP)
_, br0 = edge_pixels(a0, boxpick.EDGE_BOTTOM)
_vwr, _vhr = int(round(boxpick.zoom_window(gbx, PANEL, 1.0)[0])), int(
    round(boxpick.zoom_window(gbx, PANEL, 1.0)[1]))
check('视窗不许跑出画面：贴着右边的框 -> 视窗顶到画面右边（框就不再正中了，这是对的）',
      boxpick.zoom_view(gbx, PANEL, (0, 0), 1.0, SIZE10)[0] == 2560 - _vwr,
      'vx=%d 期望 %d' % (boxpick.zoom_view(gbx, PANEL, (0, 0), 1.0, SIZE10)[0],
                         2560 - _vwr))
el, er, et, eb = boxpick.edge_lines(gbx, PANEL, (0, 0), 1.0, SIZE10)
check('edge_lines 和 magnify 画的是同一份（界面照着它判「抓住哪条边」）',
      abs(lc0[0] - el) <= 1 and abs(rc0[-1] - er) <= 1
      and abs(tr0[0] - et) <= 1 and abs(br0[-1] - eb) <= 1,
      'edge_lines %s / 量到 左 %d 右 %d 上 %d 下 %d'
      % ((el, er, et, eb), lc0[0], rc0[-1], tr0[0], br0[-1]))
# 「同方向」：框往右挪，视窗跟着往右挪同样多 —— 彩线在图上的列**一动不动**，动的是画面。
# 用画面中间的框，免得撞上「视窗顶到画面边」的夹取（夹住之后两条都钉死，测不出什么）。
mid_a = mbx
mid_b = Rect(1320, 700, 199, 159)
am_a = np.asarray(boxpick.magnify(grad, mid_a, panel=PANEL))
am_b = np.asarray(boxpick.magnify(grad, mid_b, panel=PANEL))
lca, _ = edge_pixels(am_a, boxpick.EDGE_LEFT)
lcb, _ = edge_pixels(am_b, boxpick.EDGE_LEFT)
check('挪框 120px（同方向）：视窗跟着走，彩线在图上的列一动不动',
      lca == lcb and bool(lca), '挪前 %s / 挪后 %s' % (lca, lcb))
check('挪框 120px（同方向）：底下那块画面确实跟着换了',
      bool((am_a != am_b).any()), '一样？')
vxa = boxpick.zoom_view(mid_a, PANEL, (0, 0), 1.0, SIZE10)[0]
vxb = boxpick.zoom_view(mid_b, PANEL, (0, 0), 1.0, SIZE10)[0]
check('挪框 120px（同方向）：视窗左上角也正好跟着挪 120px', vxb - vxa == 120,
      '%d -> %d' % (vxa, vxb))
# 平移（pan）：视窗往下 40px 看 -> 框的两条边在图上往上走 40*k 行。
et40 = boxpick.edge_lines(gbx, PANEL, (0, 40), 1.0, SIZE10)[2]
check('pan 往下 40px：框的边在图上往上走 40*k 行（视窗挪了，彩线跟着挪）',
      abs((et - et40) - 40 * kx) <= 1, '上边 %d -> %d（40*k=%.1f）' % (et, et40, 40 * kx))
# 越界的那条不画：pan 拉到画面底下，框整个跑到视窗上面去 -> 品红一条都不该有。
_, tr_far = edge_pixels(np.asarray(boxpick.magnify(grad, gbx, panel=PANEL,
                                                   pan=(0, 10 ** 6))), boxpick.EDGE_TOP)
check('越界的边不画（框跑到视窗外面去了 -> 品红一条都不画，不是画在画布边上）',
      tr_far == [], '品红行 %s' % (tr_far,))
# 夹边界：pan 再大也只是把视窗顶到画面边上 —— 量**画出来的那一格**是画面的哪一格。
_vwf, _vhf = boxpick.zoom_window(gbx, PANEL, 1.0)
vwr, vhr = int(round(_vwf)), int(round(_vhf))
zr = np.asarray(boxpick.magnify(grad, gbx, panel=PANEL, pan=(10 ** 6, 10 ** 6)))
zl = np.asarray(boxpick.magnify(grad, gbx, panel=PANEL, pan=(-10 ** 6, -10 ** 6)))
check('pan 越界被夹在画面里（右下顶到边 = 画的就是画面右下角那一块）',
      int(zr[0, 0, 0]) == int(grad[1600 - vhr, 2560 - vwr]) and zr.shape[:2] == (ih0, iw0),
      '%d / %d' % (int(zr[0, 0, 0]), int(grad[1600 - vhr, 2560 - vwr])))
check('pan 越界被夹在画面里（左上顶到边 = 画的就是画面左上角那一格）',
      int(zl[0, 0, 0]) == int(grad[0, 0]), '%d / %d' % (int(zl[0, 0, 0]), int(grad[0, 0])))
# 倍数只跟「框和 zoom」有关：同一个框平移前后 kx / ky 一样（顺带把「不由看哪儿决定」钉住）
check('zoom_scale 只看框和 zoom、不看 pan（签名里只有 box / panel / zoom）',
      boxpick.zoom_scale(gbx, PANEL) == (kx, ky), '%s' % ((kx, ky),))

# ---- 局部放大（zoom）：视窗 = 框 / zoom（再撑到面板比例），倍数 = 面板 / 视窗 ----
# 用户 2026-10-11 第 46 轮：「做局部放大！类似 ctrl＋滚轮 的还是框选放大都行」。
def col_step(im, row=5):
    """这一行上像素值变化的**间隔** = 横向倍数 k（一个画面像素占 k 列）。

    背景是每列差 1 的渐变（见 grad），所以只有跳到下一个画面像素时才变。取中位数是为了
    躲开头一下（第 0 / 1 列被红线盖了，以及渐变 mod 190 绕一圈那一列）。
    """
    r = np.asarray(im)[row, :, 0].astype(np.int16)      # 只看一个通道（R），三个一起算会把间隔弄乱
    ch = np.nonzero(np.diff(r) != 0)[0]
    gaps = np.diff(ch)
    return int(np.median(gaps)) if len(gaps) else 0


zag = boxpick.magnify(grad, gbx, panel=PANEL, zoom=2.0)
kx2, ky2 = boxpick.zoom_scale(gbx, PANEL, 2.0)
vw2f, vh2f = boxpick.zoom_window(gbx, PANEL, 2.0)
check('zoom=2：视窗（高那一侧）正好减半、倍数翻倍（宽那侧撑到面板比例）',
      abs(vh2f - 159 / 2.0) < 0.51
      and abs(vw2f - (159 / 2.0) * PANEL[0] / PANEL[1]) < 1.0
      and abs(kx2 - 2 * kx) < 0.02 and abs(ky2 - 2 * ky) < 0.02,
      '视窗 %.1fx%.1f k=%s' % (vw2f, vh2f, (kx2, ky2)))
check('放大图上 1 个画面像素 = k 列（zoom=1 时 %d 列、zoom=2 时 %d 列）'
      % (round(kx), round(kx2)),
      col_step(z0) == int(round(kx)) and col_step(zag) == int(round(kx2)),
      '%d / %d' % (col_step(z0), col_step(zag)))
zag_mid = np.asarray(boxpick.magnify(grad, mbx, panel=PANEL, zoom=2.0))
la2, _ = edge_pixels(zag_mid, boxpick.EDGE_LEFT)
ra2, _ = edge_pixels(zag_mid, boxpick.EDGE_RIGHT)
check('zoom=2 且 pan=0：框照样摆在视窗正中（差 ≤ 3px）',
      abs(la2[0] - (zag_mid.shape[1] - 1 - ra2[-1])) <= 3,
      '左 %s 右 %s / 图 %s' % (la2[:2], ra2[-2:], (zag_mid.shape[1], zag_mid.shape[0])))
check('zoom 超过 ZOOM_MAX 被夹住（不会算出 1 像素的窗口）',
      bool((np.asarray(boxpick.magnify(grad, gbx, panel=PANEL, zoom=99)) ==
            np.asarray(boxpick.magnify(grad, gbx, panel=PANEL,
                                       zoom=boxpick.ZOOM_MAX))).all()),
      'ZOOM_MAX=%s' % boxpick.ZOOM_MAX)
# zoom < 1 = 往外缩：视窗比框还大（能看见框周围一圈），图还是铺满面板。
big = boxpick.magnify(grad, gbx, panel=PANEL, zoom=0.5)
vbw, vbh = boxpick.zoom_window(gbx, PANEL, 0.5)
v1w, v1h = boxpick.zoom_window(gbx, PANEL, 1.0)
check('zoom=0.5：视窗比框大一倍（缩出去看框周围）',
      abs(vbw - 2 * v1w) < 1e-6 and abs(vbh - 2 * v1h) < 1e-6 and vbw > gbx.w,
      '视窗 %.1fx%.1f（框 %dx%d）' % (vbw, vbh, gbx.w, gbx.h))
check('zoom=0.5：图还是铺满面板（缩出去也不会剩一条黑）',
      abs(big.size[0] - PANEL[0]) <= 8 and abs(big.size[1] - PANEL[1]) <= 8, '%s' % (big.size,))
lo = boxpick.zoom_floor(gbx, SIZE10, PANEL)
lfw, lfh = boxpick.zoom_window(gbx, PANEL, lo)
check('缩小的下限 = 整幅画面刚好装进面板（再小就没意义了）',
      lo < 1.0 and lfw <= 2560 + 1e-6 and lfh <= 1600 + 1e-6,
      '下限 %.4f 时视窗 %.1fx%.1f / 画面 %s' % (lo, lfw, lfh, SIZE10))
check('下限本身 ≤ 1（「看整个框」这档永远给得出来）', lo <= 1.0, str(lo))
# 画出来的就是 zoom_view 说的那一块：画面 (vx, vy) 那一格就是图的 左上角
v = boxpick.zoom_view(gbx, PANEL, (60, 20), 2.0, SIZE10)
zg = np.asarray(boxpick.magnify(grad, gbx, panel=PANEL, pan=(60, 20), zoom=2.0))
check('画出来的就是 zoom_view 说的那一块（左上角像素 = 画面那一格）',
      int(zg[0, 0, 0]) == int(grad[v[1], v[0]]),
      '%d / %d' % (int(zg[0, 0, 0]), int(grad[v[1], v[0]])))
vf = boxpick.zoom_view(gbx, PANEL, (10 ** 6, 10 ** 6), 2.0, SIZE10)
check('zoom_view：视窗整块夹在画面里（右 / 下顶到边，尺寸 = 视窗）',
      vf == (2560 - vf[2], 1600 - vf[3], vf[2], vf[3], vf[4], vf[4])
      and vf[2] == int(round(boxpick.zoom_window(gbx, PANEL, 2.0)[0]))
      and vf[4] == vf[5], str(vf))

# ---- 11. resize_box / mirror_resize：左边那张图「Windows 截图」那套手势的几何 ----
# 用户 2026-10-12：「放大做的什么鬼，先在左侧原画面创建一个框，就像 Windows 的截图一样
# 可以拖动框放大缩小移动」。取景器把「按下那一刻的框 + 位移」交给 resize_box 算新框，
# **不**逐拍累加 —— 逐拍累加会攒下每一步的取整误差，手抖一下再抖回来框就回不到原位，
# 而这是「对 1 个像素」的活儿（实测条槽 6px = 1 个逻辑帧）。
SZ = (800, 600)
B = Rect(300, 200, 120, 40)
check('resize_box l：左端左移 7（右端不动，x / w 一起变）',
      boxpick.resize_box(B, 'l', -7, 0, SZ) == Rect(293, 200, 127, 40),
      str(boxpick.resize_box(B, 'l', -7, 0, SZ)))
check('resize_box r：右端右移 7（左端不动）',
      boxpick.resize_box(B, 'r', 7, 0, SZ) == Rect(300, 200, 127, 40),
      str(boxpick.resize_box(B, 'r', 7, 0, SZ)))
check('resize_box t：上端上移 7（下端不动）',
      boxpick.resize_box(B, 't', 0, -7, SZ) == Rect(300, 193, 120, 47),
      str(boxpick.resize_box(B, 't', 0, -7, SZ)))
check('resize_box b：下端下移 7（上端不动）',
      boxpick.resize_box(B, 'b', 0, 7, SZ) == Rect(300, 200, 120, 47),
      str(boxpick.resize_box(B, 'b', 0, 7, SZ)))
check('resize_box tl：左上角一起走（右下不动）',
      boxpick.resize_box(B, 'tl', -7, -7, SZ) == Rect(293, 193, 127, 47),
      str(boxpick.resize_box(B, 'tl', -7, -7, SZ)))
check('resize_box tr：右上角（左端 / 下端不动）',
      boxpick.resize_box(B, 'tr', 7, -7, SZ) == Rect(300, 193, 127, 47),
      str(boxpick.resize_box(B, 'tr', 7, -7, SZ)))
check('resize_box bl：左下角（右端 / 上端不动）',
      boxpick.resize_box(B, 'bl', -7, 7, SZ) == Rect(293, 200, 127, 47),
      str(boxpick.resize_box(B, 'bl', -7, 7, SZ)))
check('resize_box br：右下角一起走（左上不动）',
      boxpick.resize_box(B, 'br', 7, 7, SZ) == Rect(300, 200, 127, 47),
      str(boxpick.resize_box(B, 'br', 7, 7, SZ)))
check('resize_box move：整块挪（宽高一点不变）',
      boxpick.resize_box(B, 'move', -7, 7, SZ) == Rect(293, 207, 120, 40),
      str(boxpick.resize_box(B, 'move', -7, 7, SZ)))
check('位移相对「按下那一刻」：抖出去 5 再抖回来（位移 0）= 原框，不攒取整误差',
      boxpick.resize_box(B, 'r', 5, 0, SZ) == Rect(300, 200, 125, 40)
      and boxpick.resize_box(B, 'r', 0, 0, SZ) == B
      and boxpick.resize_box(B, 'br', 5, 5, SZ) == Rect(300, 200, 125, 45),
      '%s' % (boxpick.resize_box(B, 'br', 5, 5, SZ),))
check('move 拉出画面右下：停在画面里（不会算出画外的框）',
      boxpick.resize_box(B, 'move', 10 ** 6, 10 ** 6, SZ) == Rect(680, 560, 120, 40),
      str(boxpick.resize_box(B, 'move', 10 ** 6, 10 ** 6, SZ)))
check('move 往反方向拉出去：停在左上角',
      boxpick.resize_box(B, 'move', -10 ** 6, -10 ** 6, SZ) == Rect(0, 0, 120, 40),
      str(boxpick.resize_box(B, 'move', -10 ** 6, -10 ** 6, SZ)))
check('l 拉到越过右端：停在 min_w 宽（不会翻成负数宽）',
      boxpick.resize_box(B, 'l', 10 ** 6, 0, SZ) == Rect(400, 200, 20, 40),
      str(boxpick.resize_box(B, 'l', 10 ** 6, 0, SZ)))
check('r 往左拉到底：停在 min_w 宽',
      boxpick.resize_box(B, 'r', -10 ** 6, 0, SZ) == Rect(300, 200, 20, 40),
      str(boxpick.resize_box(B, 'r', -10 ** 6, 0, SZ)))
check('t 拉到越过下端：停在 min_h 高',
      boxpick.resize_box(B, 't', 0, 10 ** 6, SZ) == Rect(300, 237, 120, 3),
      str(boxpick.resize_box(B, 't', 0, 10 ** 6, SZ)))
check('拉出画面左边 / 右边：停在画面边上',
      boxpick.resize_box(B, 'l', -10 ** 6, 0, SZ) == Rect(0, 200, 420, 40)
      and boxpick.resize_box(B, 'r', 10 ** 6, 0, SZ) == Rect(300, 200, 500, 40),
      '%s / %s' % (boxpick.resize_box(B, 'l', -10 ** 6, 0, SZ),
                   boxpick.resize_box(B, 'r', 10 ** 6, 0, SZ)))
check('按钮那一档的最小尺寸也是参数（取景器给暂停键传 BTN_MIN）',
      boxpick.resize_box(B, 'r', -10 ** 6, 0, SZ, 8, 8) == Rect(300, 200, 8, 40),
      str(boxpick.resize_box(B, 'r', -10 ** 6, 0, SZ, 8, 8)))

SZ2 = (1200, 900)                     # 联动这一档用大一点的画面：别让夹边界搅进来
boxes2 = {'pause': Rect(600, 40, 200, 160), 'speed': Rect(380, 40, 200, 160),
          'bar': Rect(300, 500, 240, 17)}
mv = boxpick.mirror_resize(boxes2, 'pause',
                           boxpick.resize_box(boxes2['pause'], 'move', 5, -3, SZ2),
                           SZ2, names=('pause', 'speed'))
check('mirror_resize 挪块：另一个按同一个位移走、一样大',
      mv['pause'] == Rect(605, 37, 200, 160) and mv['speed'] == Rect(385, 37, 200, 160),
      '%s / %s' % (mv['pause'], mv['speed']))
check('mirror_resize 挪块：费用条那一根不动', mv['bar'] == boxes2['bar'], str(mv['bar']))
check('mirror_resize 不改入参（返回的是新字典）',
      boxes2['pause'] == Rect(600, 40, 200, 160) and boxes2['speed'] == Rect(380, 40, 200, 160))
rs = boxpick.mirror_resize(boxes2, 'pause',
                           boxpick.resize_box(boxes2['pause'], 'r', 11, 0, SZ2),
                           SZ2, names=('pause', 'speed'))
check('mirror_resize 拉右端：另一个也横向长同样多（两个框永远一样大）',
      (rs['pause'].w, rs['pause'].h, rs['pause'].x) == (211, 160, 600)
      and (rs['speed'].w, rs['speed'].h, rs['speed'].x) == (211, 160, 380),
      '%s / %s' % (rs['pause'], rs['speed']))
ls = boxpick.mirror_resize(boxes2, 'pause',
                           boxpick.resize_box(boxes2['pause'], 'l', -11, 0, SZ2),
                           SZ2, names=('pause', 'speed'))
check('mirror_resize 拉左端：另一个的左端也一起挪、一样宽',
      ls['pause'] == Rect(589, 40, 211, 160) and ls['speed'] == Rect(369, 40, 211, 160),
      '%s / %s' % (ls['pause'], ls['speed']))
tiny2 = {'pause': Rect(600, 40, 200, 160), 'speed': Rect(380, 40, 30, 30)}
ts = boxpick.mirror_resize(tiny2, 'pause',
                           boxpick.resize_box(tiny2['pause'], 'r', -10 ** 6, 0, SZ2),
                           SZ2, names=('pause', 'speed'))
check('mirror_resize 里另一个缩到下限就停（不会缩成 0 宽）',
      ts['pause'].w == 20 and ts['speed'].w == 20, '%s / %s' % (ts['pause'], ts['speed']))
allmv = boxpick.mirror_resize(boxes2, 'pause',
                              boxpick.resize_box(boxes2['pause'], 'br', 9, 9, SZ2), SZ2)
check('mirror_resize 不传 names：全部一起搬（费用条也按同样的四个边差走）',
      allmv['bar'] == Rect(300, 500, 249, 26), str(allmv['bar']))

print()
if FAIL:
    print('FAILED: %s' % ', '.join(FAIL))
    sys.exit(1)
print('全部通过')