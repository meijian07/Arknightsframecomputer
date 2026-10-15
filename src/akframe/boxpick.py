# -*- coding: utf-8 -*-
"""手动框选费用条的**参照物**：取一帧画面 + 在上面画出「这里应该是什么样」。

用户实测反馈：「改成手动截取，手动截取不准确，需要参照物」。为什么会不准 ——
在 2560x1600 的录像里，费用条只占 180x17 个像素，而框选是在**缩放过的**画面上用鼠标
拖的：屏幕上 1 个像素 = 画面里 1.5~2 个像素，手一抖就是 3~4 个逻辑帧的相位误差。
只靠眼睛拖框，做不到「一费 30 帧」。

所以这里给两样参照物，把「凭手感」换成「对着东西对」：

1. **刻度尺**（guide_image）：费用条的设计是「条槽宽 = 30 个逻辑帧」。把预测的条槽
   位置画成一个被 30 等分的尺子，用户框的时候是**对着刻度**对两端，而不是对着空气；
2. **放大镜**（magnify）：把框那一块放大（横向 x 倍、纵向可以拉得更狠），并把**框的
   四条边都画出来**（左 / 右 = 红 / 绿竖线，上 / 下 = 品红横线），read_bar 真正取样的
   行画成蓝线 —— 白块的左端压没压在红线上、白条在上下两条品红线之间居没居中，一眼就能
   看出来；放大镜还能**按住平移**（pan）把框周围一起拉进来对、**滚轮局部放大**（zoom）
   只看框里的一小块（2026-10-11 追加）。

这个模块里全是纯函数 / 只依赖 numpy + PIL + 已有的 features / align，不碰 Tk：
界面（ui.BoxPicker）只负责把这里画出来的图贴到画布上。
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from .features import fill_run
from .geometry import BAR_BAND_BOTTOM, BAR_BAND_TOP, BAR_ROI_H, Layout, Rect

# 参照图的配色：条槽用橙色（和游戏 HUD 的蓝白都不撞），按钮用蓝色。
GUIDE_BAR = (255, 150, 30)
GUIDE_BTN = (60, 170, 255)
GUIDE_TEXT = (255, 235, 180)
EDGE_LEFT = (255, 70, 70)      # 放大镜里框的左边缘
EDGE_RIGHT = (70, 230, 110)    # 框的右边缘
EDGE_TOP = (255, 90, 220)      # 框的上边缘
EDGE_BOTTOM = (255, 90, 220)   # 框的下边缘（上下同色：它们是一对，只有左右要分开认）
BAND_LINE = (90, 190, 255)     # read_bar 真正取样的那条带
BAND_FILL = (40, 90, 140)      # 条带整条填的一层淡色（比两条细线好认，见 magnify）
BAND_FILL_ALPHA = 120          # 0..255
GHOST = (150, 235, 255)        # 「虚影」的填充（暂停键字形应该长在哪）
GHOST_EDGE = (215, 250, 255)   # 虚影的描边
GHOST_ALPHA = 105              # 0..255：**要能看见底下那层真字形**，所以不能太实

# 白块短于这么多列就不当数（和 calib.MIN_RUN 同口径：场景里的高光都很短）
MIN_RUN = 6
# 刻度尺画多少格：一个费用周期 = 30 逻辑帧，这是游戏的机制常数（见 docs）。
TICKS = 30
# 局部放大（用户 2026-10-11：「做局部放大！类似 ctrl+滚轮 的还是框选放大都行」）：
# zoom = 1 看不大于整个框的一块画面，越大看得越少、放得越大，小于 1 是往外缩（看框周围）。
#
# 2026-10-15 第八轮（用户：「移动框时放大小窗反向移动，应该和框同方向移动；放大小窗缩放
# 很奇怪，固定 4:3 的比例，包括框选的位置；ctrl 放大范围大一点」）三条：
#
#   * **视窗跟着框走、形状也跟着框走**：视窗以**当前的框**为中心（不再冻在「打开取景器
#     那一刻的框」上）。原来那份冻住的基准让放大图固定成某个比例，挪框 / 拉框时画面跟框
#     对不上 —— 用户看到的「固定 4:3 的比例，包括框选的位置」就是它；
#   * **不变形（两个轴一个倍数）**：原来 kx = 面板宽 / 视窗宽、ky = 面板高 / 视窗高，
#     框是扁的（费用条框 239x25）时纵向被拉 3 倍多，画面越看越不像原样。现在视窗撑到
#     面板的宽高比、两个轴同一个倍数 —— 画面就是原样的放大版，四条彩线照旧画在框上；
#   * **倍数连续 + 范围更大**：原来 kx / ky 是整数除法（滚一格可能反而变小一截），
#     下限还是「1 画面像素 = 1 屏像素」；现在倍数是浮点、连续，缩到底就是整幅画面
#     （见 zoom_floor）—— 可缩的范围从 1 倍放宽到 0.1 倍上下，这就是用户要的「范围大一点」。
#
# ZOOM_MAX 保守地留在 16（用户 2026-10-15 只说「范围大一点」，没说「放大到看不清」）：
# 视窗得落在整数画面像素上，撑面板时天生差半像素 -> 放大图比面板差 ≤ 3px（见 fit_panel
# 下面那段推导），到这个倍数为止；再往上（25 / 32）宽度会忽大忽小几十像素，面板上会
# 出现肉眼可见的空白条。
ZOOM_MIN = 0.05                 # 绝对下限：再小整幅画面就只有几十列了，看也白看
ZOOM_MAX = 16.0



def guide_image(frame: np.ndarray, layout: Layout) -> Image.Image:
    """参照图：原始画面 + 预测的条槽刻度尺 + 右上角两个按钮的框。

    预测位置**不保证对**（录像里游戏窗口在哪儿、多大，只有画面自己知道），所以这里
    画的是「参考」而不是「答案」：它的作用是给用户一个可对照的东西 —— 费用条在不在
    这一带、条槽有多宽、30 格刻度长什么样。真正定相位的是放大镜。
    """
    img = Image.fromarray(frame, mode='L').convert('RGB')
    d = ImageDraw.Draw(img)
    w, h = img.size
    bar = layout.bar
    top = max(0, bar.y - 5)
    bot = min(h - 1, bar.y2 + 5)
    d.rectangle([bar.x, top, bar.x2, bot], outline=GUIDE_BAR)
    for k in range(TICKS + 1):
        x = bar.x + int(round(bar.w * k / float(TICKS)))
        x = max(0, min(w - 1, x))
        if k % 5 == 0 or k in (0, TICKS):
            d.line([(x, top), (x, bot)], fill=GUIDE_BAR)     # 每 5 帧一条长刻度
        else:
            d.line([(x, top), (x, min(bot, top + 4))], fill=GUIDE_BAR)
            d.line([(x, max(top, bot - 4)), (x, bot)], fill=GUIDE_BAR)
    d.text((bar.x + 2, max(0, top - 13)), '条槽参考 x=%d 宽 %dpx（%d 帧）'
           % (bar.x, bar.w, TICKS), fill=GUIDE_TEXT)
    if layout.buttons_visible:
        for name, label in (('pause', '暂停'), ('speed', '倍速')):
            r = getattr(layout, name)
            if r.w > 2 and r.h > 2:
                d.rectangle([r.x, r.y, r.x2, r.y2], outline=GUIDE_BTN)
                d.text((r.x + 2, max(0, r.y - 13)), label, fill=GUIDE_BTN)
    return img


def clamp_zoom(zoom, lo=ZOOM_MIN, hi=ZOOM_MAX) -> float:
    """把 zoom 夹进 [lo, hi]。读不出来（nan / 不是数）一律当 1（看不大于整个框）。"""
    try:
        z = float(zoom)
    except (TypeError, ValueError):
        return 1.0
    if z != z:
        return 1.0
    return max(float(lo), min(float(hi), z))


def fit_panel(vw, vh, panel=(556, 186)) -> tuple:
    """把视窗**只放大**到面板的宽高比，返回 (视窗宽, 视窗高)（画面像素）。

    为什么非撑不可：撑完 k = 面板宽 / 视窗宽 = 面板高 / 视窗高，两个轴同一个倍数 ——
    画面不变形；同时放大图正好铺满面板，不会在右边 / 下边剩一条黑。只放大不缩小，
    所以框本身永远整个落在视窗里。

    撑到面板的比例之后，视窗尺寸是**浮点**；真正裁画面时才取整（见 zoom_view），所以
    画出来的图会比面板差半个画面像素以内（乘上倍数 = 放大图差 ≤ 3px，16 倍以内）。
    """
    pw, ph = float(panel[0]), float(panel[1])
    vw, vh = max(1.0, float(vw)), max(1.0, float(vh))
    if vw / vh >= pw / ph:
        return (vw, vw * ph / pw)
    return (vh * pw / ph, vh)


def zoom_window(box, panel=(556, 186), zoom=1.0) -> tuple:
    """放大镜的视窗尺寸 (宽, 高)（画面像素）= 框 / zoom，再撑到面板的比例（见 fit_panel）。

    zoom = 1 -> 视窗刚好装下整个框；zoom > 1 -> 只看框里的一小块（放得更大）；
    zoom < 1 -> 视窗比框大，能看见框周围一圈。
    """
    z = clamp_zoom(zoom)
    return fit_panel(box.w / z, box.h / z, panel)


def zoom_scale(box, panel=(556, 186), zoom=1.0) -> tuple:
    """放大镜的倍数 (k, k)：两个轴**一样**，所以画面不会被拉扁。

    倍数只由**框和 zoom** 决定、不由「看哪儿」决定 —— 拖动是平移，不该顺手把画面缩掉，
    否则「挪一下看清楚点」会变成「越挪越小」。

    倍数按**没取整的视窗**算（zoom_window 给的就是浮点），取整只发生在视窗本身 —— 这样
    放大图正好铺满面板（差不到 1 像素），右边不会莫名其妙空出一条黑边。
    """
    vw, vh = zoom_window(box, panel, zoom)
    k = min(float(panel[0]) / vw, float(panel[1]) / vh)
    return (k, k)


def zoom_floor(box, size, panel=(556, 186)) -> float:
    """缩小的下限：视窗正好装下**整幅画面**时的 zoom（再小就看不见框周围还有什么了）。

    口径：视窗 = 框 / zoom（撑到面板比例），要求它整块落在画面里。zoom = 1 时视窗
    只比框大一圈，所以 1.0 永远给得出来 —— 下限不会挡住「看整个框」。
    """
    vw, vh = fit_panel(box.w, box.h, panel)
    w = float(max(1, int(size[0])))
    h = float(max(1, int(size[1])))
    z = max(vw / w, vh / h)
    return max(ZOOM_MIN, min(1.0, z))


def zoom_view(box, panel=(556, 186), pan=(0, 0), zoom=1.0, size=None) -> tuple:
    """放大镜「看哪儿、多大倍数」的唯一出口：(vx, vy, vw, vh, k, k)。

    vx / vy = 视窗左上角在画面里的坐标；vx / vy 给了 size = (宽, 高) 就**已经夹好**
    （视窗一定整块在画面里）。pan = 视窗相对「**以框为中心**」的偏移（画面像素）——
    所以 pan = (0, 0) 就是框摆在正中间，挪框时视窗跟着框一起走（用户 2026-10-15：
    「移动框时放大小窗反向移动，应该和框同方向移动」）。

    为什么要有这个函数：界面算「光标底下是哪个画面点」和 magnify 画图必须是**同一份
    几何**，否则滚轮缩放时盯着鼠标的那一点会慢慢漂走（各算一遍最容易差在取整上）。
    倍数一律问 zoom_scale（同一份口径），视窗尺寸才是这里取整的那一份。
    """
    k, _ky = zoom_scale(box, panel, zoom)
    vw, vh = zoom_window(box, panel, zoom)
    vw, vh = max(1, int(round(vw))), max(1, int(round(vh)))
    cx = int(box.x) + (int(box.w) - vw) / 2.0 + pan[0]
    cy = int(box.y) + (int(box.h) - vh) / 2.0 + pan[1]
    if size is None:
        return max(0, int(round(cx))), max(0, int(round(cy))), vw, vh, k, k
    w, h = int(size[0]), int(size[1])
    vw, vh = min(vw, w), min(vh, h)
    vx = max(0, min(int(round(cx)), w - vw))
    vy = max(0, min(int(round(cy)), h - vh))
    return vx, vy, vw, vh, k, k


def pan_of_view(box, panel=(556, 186), zoom=1.0, vx=0.0, vy=0.0) -> list:
    """反过来：视窗左上角落在 (vx, vy) 时 pan 该是多少（界面对齐 / 夹边界都用它）。

    「pan 是相对框居中的偏移」这条约定只在这个函数和 zoom_view 里各写一次，
    界面那边不再自己推导 —— 各算一份最容易差在取整上（差 1 像素就是 1/8 个逻辑帧）。
    """
    vw, vh = zoom_window(box, panel, zoom)
    vw, vh = max(1, int(round(vw))), max(1, int(round(vh)))
    return [float(vx) - int(box.x) - (int(box.w) - vw) / 2.0,
            float(vy) - int(box.y) - (int(box.h) - vh) / 2.0]


def edge_lines(box, panel=(556, 186), pan=(0, 0), zoom=1.0, size=None) -> tuple:
    """框的四条边**画在放大图的哪一列 / 行**：(左列, 右列, 上行, 下行)，左闭。

    抽成一个函数是给界面用的：放大图里「鼠标是不是抓住了某条边」必须和 magnify
    画那条线用的是**同一套取整**，否则手感是「明明压在线上却没抓住」（差 1 像素就够）。
    口径（和 magnify 里画线时完全一致）：第 x 列的左边界是 int((x - vx) * k)，
    所以框占了 x..x+w-1 这些列，右那条线画在 int((x + w - vx) * k) - 1 上。
    """
    vx, vy, _vw, _vh, k, _ky = zoom_view(box, panel, pan, zoom, size)

    def sx(x):
        return int((x - vx) * k)

    def rt(y):
        return int((y - vy) * k)

    return (sx(int(box.x)), sx(int(box.x) + int(box.w)) - 1,
            rt(int(box.y)), rt(int(box.y) + int(box.h)) - 1)


# 暂停键的「双竖条虚影」：字形该长在按钮 ROI 的哪儿（占 ROI 宽 / 高的比例）。
#
# 为什么要有（用户 2026-10-12：「手动框选暂停键参考不明确，最好加一个暂停键的虚影
# 方便对齐」）：费用条有刻度尺可以对着数，暂停键什么都没有 —— 框挪到哪儿才算「压在
# 字形上」全靠猜。虚影就是把这个问题的答案画出来：两块淡青色的竖条**跟着框走**，
# 把框挪到虚影和画面里真的 ❚❚ 重合，对齐就完成了。
#
# 数值的来源（2026-10-12 量用户那段 Desktop 录像的第 420 帧，s ≈ 1.19）：
#   * 竖条顶边 = 读取条带的最上一行（PAUSE_SHAPE_BAND 的上沿）。这不是巧合 ——
#     那正是「条带要压住字形最上边」这条对齐判据本身；
#   * 字形高 51px、按钮 ROI 高 143px -> 0.357；
#   * 两根竖条各占 ROI 宽的 11%、中间空 10%（字形整体横跨框宽的 38%，居中）。
#
# **字形本身是斜的**（用户 2026-10-12：「暂停键是斜着的，虚影对不上」）：游戏把 ❚❚ 画成
# 平行四边形 —— 是**斜切**不是旋转，上下两条边照样是水平线，只有左右两条竖边往右倾。
# 量了两段真录像的逐行白块（每 2 行取一次，报的是该行白块的左右端）：
#
#   Desktop 2026.10.09 22.36.10.14   左条 2382..2399 @y131 -> 2392..2408 @y183
#   Arknights 2026.10.03 23.53.21.03 左条 2394..2414 @y129 -> 2404..2423 @y177
#
# 两段一致：走完整个字形高（54~55px）底边往右挪 10~10.5px，即 0.19 个字形高。所以虚影
# 不能再画成矩形 —— 正矩形不管怎么挪都有一半压在字形外面，用户看到的就是「对不上」。
PAUSE_GHOST_TOP = 38 / 160
PAUSE_GHOST_H = 0.357
PAUSE_GHOST_BARS = ((0.31, 0.42), (0.52, 0.63))
PAUSE_GHOST_SHEAR = 0.19       # 底边相对顶边往右挪「字形高的 19%」（见上面两段实测）


def pause_ghost(box: Rect) -> tuple:
    """暂停键的「双竖条虚影」在**画面坐标**里的两个**平行四边形**（见上面那段注释）。

    只按框算：虚影的含义是「如果框对了，字形该长在这儿」，所以它必须跟着框动 ——
    框挪到哪儿虚影跟到哪儿，重合的那一刻就是对上了的那一刻。

    每个平行四边形是四个点，顺序是 左上 / 右上 / 右下 / 左下（画面像素坐标）。上下两条
    边照旧一样宽，**下面那条边整体往右挪** PAUSE_GHOST_SHEAR x 字形高 —— 游戏画出来的
    ❚❚ 就是这个斜率，不斜切的话不管怎么挪框都有一半压在字形外面。
    """
    h = max(1, int(box.h))
    w = max(1, int(box.w))
    top = int(box.y) + int(round(PAUSE_GHOST_TOP * h))
    bot = top + max(1, int(round(PAUSE_GHOST_H * h))) - 1
    shift = int(round(PAUSE_GHOST_SHEAR * (bot - top + 1)))
    out = []
    for a, b in PAUSE_GHOST_BARS:
        x0 = int(box.x) + int(round(a * w))
        x1 = int(box.x) + int(round(b * w)) - 1
        out.append(((x0, top), (x1, top), (x1 + shift, bot), (x0 + shift, bot)))
    return tuple(out)


def _translucent(img: Image.Image, polys, color, alpha: int) -> Image.Image:
    """把 polys（放大图坐标的**多边形**，每个是 3 个以上 (x, y)）半透明地糊在 img 上。

    为什么不用 ImageDraw 直接填：draw 只会**盖住**底下那层像素，而这里要的正是
    「看得见底下的游戏画面」—— 虚影的全部意义就是「和真字形叠着比」。所以画一张同色
    实心层 + 一张灰度蒙版，用 Image.composite 按蒙版混过去（蒙版 105/255 ≈ 41%）。

    收多边形而不是矩形：暂停键字形本身是斜的（见 PAUSE_GHOST_SHEAR），矩形怎么挪都对
    不上；条带那层淡色填的是正矩形，写成四个点的多边形就行。
    """
    if img.width < 1 or img.height < 1 or not polys:
        return img
    mask = Image.new('L', img.size, 0)
    md = ImageDraw.Draw(mask)
    for poly in polys:
        md.polygon([(int(x), int(y)) for x, y in poly], fill=int(alpha))
    return Image.composite(Image.new('RGB', img.size, color), img, mask)


def magnify(frame: np.ndarray, box: Rect, panel=(520, 170), layout: Layout = None,
            band=(0, 0), pan=(0, 0), zoom=1.0, ghost=(), band_fill=False) -> Image.Image:
    """把框那一块放大，并把**框的四条边**画在上面：红 / 绿 = 左右，品红 = 上下。

    pan = 视窗相对**框的中心**的偏移（画面像素，正 = 往右下看），(0,0) = 框摆在正中。
    为什么要有它（用户 2026-10-11：「手动校准界面右边的放大图要可以自己移动，不然很难
    对准」）：框只框住按钮那一小块时，放大镜里除了按钮什么都没有 —— 按钮偏出去多少、
    往哪边偏，全看不见。能挪之后就能把周围一起拉进来对着看。

    四条边为什么一直画（用户紧接着的下一条：「费用条难以准确框选，加一个可移动的局部
    放大功能，查看上下左右是否对齐」）：只标左右的话，上下对没对上只能猜。平移之后它们
    跟着框走，于是「框相对条偏了多少」四条边一起看得见。越界的那条不画：它确实不在
    视窗里，画在边上等于撒谎。

    zoom = **局部放大**倍数（见 zoom_window）：zoom = 1 是「框（加一圈周围）铺满面板」，
    zoom > 1 就是「只看框里的一小块，放得更大」（用户 2026-10-11：「做局部放大！类似
    ctrl＋滚轮 的还是框选放大都行」），zoom < 1 是往外缩（缩到底 = 整幅画面，见
    zoom_floor）。它和 pan 一样，只改「看哪儿 / 多大」，不改取样的行。

    倍数（zoom_scale）只由**框和 zoom** 决定、不由看哪儿决定 —— 拖动是平移，不该顺手把
    画面缩掉，否则「挪一下看清楚点」会变成「越挪越小」。两个轴同一个倍数（用户 2026-10-15
    「放大小窗缩放很奇怪，固定 4:3 的比例」）：画面就是原样的放大版，不会被拉扁。

    band_fill：把两条蓝线**中间整条填一层淡色**（用户 2026-10-12：「暂停键参考不
    明确」）。两条 1px 的线要回答「字的顶边压在线的哪一侧」，本来就容易看错；一条带
    的**上沿**就没这个歧义了。

    ghost：一串**多边形**（每个是 4 个**画面坐标**的点），画成半透明的「虚影」—— 给暂停键
    用的「字形应该长在这儿」（造它的是 pause_ghost，跟着框走）。它跟真字形一样是**斜的**
    （平行四边形），所以能叠着对得上；和 pan / zoom 一样只是**画上去**的参考，不参与读数。
    """
    h, w = frame.shape[:2]
    vx, vy, vw, vh, kx, ky = zoom_view(box, panel, pan, zoom, (w, h))
    crop = frame[vy:vy + vh, vx:vx + vw]
    ch, cw = crop.shape[:2]
    if ch < 1 or cw < 1:
        return Image.new('RGB', (1, 1))
    iw, ih = max(1, int(round(cw * kx))), max(1, int(round(ch * ky)))
    img = Image.fromarray(crop, mode='L').convert('RGB').resize((iw, ih), Image.NEAREST)
    d = ImageDraw.Draw(img)

    def sx(x):
        """画面列 x 的左边界画在放大图的哪一列上。"""
        return int((x - vx) * kx)

    def rowtop(y):
        return int((y - vy) * ky)

    def rowmid(y):
        return int((y - vy + 0.5) * ky)

    # 条带先整条填一层淡色（两条蓝线再压上去，见 band_fill 的说明）。
    r0, r1 = band
    if band_fill and r1 > r0:
        y0, y1 = rowtop(r0), rowtop(r1 + 1) - 1
        img = _translucent(img, [((0, y0), (iw - 1, y0), (iw - 1, y1), (0, y1))],
                           BAND_FILL, BAND_FILL_ALPHA)
    # 虚影：画在边线 / 蓝线**下面**一层，这样四条彩线和蓝线照样压在最上面看得清。
    if ghost:
        polys = []
        for g in ghost:
            pts = [(sx(int(x)), rowtop(int(y))) for x, y in g]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if max(xs) < 0 or max(ys) < 0 or min(xs) >= iw or min(ys) >= ih:
                continue                       # 整块在视窗外，画了也是撒谎
            polys.append(pts)
        if polys:
            img = _translucent(img, polys, GHOST, GHOST_ALPHA)
            d = ImageDraw.Draw(img)
            for pts in polys:
                d.polygon(pts, outline=GHOST_EDGE)
    # 框的四条边（左 / 右画在框的**第一列 / 最后一列**，上 / 下画在第一行 / 最后一行）。
    # 口径统一成「左闭」，和 boxpick.edge_lines 用的是同一份（界面照着它判「抓住哪条边」）。
    el, er, et, eb = edge_lines(box, panel, pan, zoom, (w, h))
    for xx, col in ((el, EDGE_LEFT), (er, EDGE_RIGHT)):
        if 0 <= xx < iw:
            d.line([(xx, 0), (xx, ih - 1)], fill=col, width=2)
    for yy, col in ((et, EDGE_TOP), (eb, EDGE_BOTTOM)):
        if 0 <= yy < ih:
            d.line([(0, yy), (iw - 1, yy)], fill=col, width=2)
    if r1 > r0:
        for r in (r0, r1):
            yy = rowmid(r)
            if 0 <= yy < ih:
                d.line([(0, yy), (iw - 1, yy)], fill=BAND_LINE)
    if layout is not None:
        for x in (layout.bar.x, layout.bar.x2):
            xx = int((x - vx + 0.5) * kx)
            if 0 <= xx < iw:
                d.line([(xx, 0), (xx, ih - 1)], fill=GUIDE_BAR, width=1)
    return img


def move_boxes(boxes: dict, size, dx: int, dy: int, names=None) -> dict:
    """把 boxes 里 names 这几个框**整体**挪 (dx, dy) 像素，一起夹在画面里。

    为什么联动的那几个要共用同一个位移（而不是各自夹）：右上角两个按钮在游戏里固定
    间距、一起锚在客户区右上角，「挪一个另一个跟着走」才是对的；各自夹到一起以后
    相对位置会散开，等于把其中一个挪到别的地方去了。

    size 是画面尺寸 (宽, 高)；names 默认 = boxes 的全部键。返回新的 dict（不改入参）。
    """
    names = list(boxes) if names is None else [n for n in names if n in boxes]
    if not names:
        return dict(boxes)
    w, h = int(size[0]), int(size[1])
    sel = [boxes[n] for n in names]
    left = min(b.x for b in sel)
    top = min(b.y for b in sel)
    right = max(b.x2 + 1 for b in sel)
    bottom = max(b.y2 + 1 for b in sel)
    dx = max(-left, min(int(dx), w - right))
    dy = max(-top, min(int(dy), h - bottom))
    out = dict(boxes)
    for n in names:
        b = boxes[n]
        out[n] = Rect(b.x + dx, b.y + dy, b.w, b.h)
    return out


def resize_box(was: Rect, kind: str, dx: int, dy: int, size, min_w: int = 20,
               min_h: int = 3) -> Rect:
    """把一次拖动算成新框：动**一条边 / 一个角**，或者整块挪。

    kind：'move' | 'l' | 'r' | 't' | 'b' | 'tl' | 'tr' | 'bl' | 'br'（见 ui.BoxPicker._hit）。
    (dx, dy) 是相对**按下那一刻**的位移 —— 逐拍累加会攒下每一步的取整误差，手抖一下再抖
    回来框就回不到原位，而这是「对 1 个像素」的活儿（实测 6px = 1 个逻辑帧）。所以调用方
    存的是按下那一刻的框，这里每次都从它算。
    新框夹在画面里、且不小于 (min_w, min_h)：拉过头就停在边界上，不会翻成负数。
    """
    w_fr, h_fr = int(size[0]), int(size[1])
    if kind == 'move':
        return Rect(max(0, min(was.x + dx, w_fr - was.w)),
                    max(0, min(was.y + dy, h_fr - was.h)), was.w, was.h)
    x, y, w, h = was.x, was.y, was.w, was.h
    if kind in ('l', 'tl', 'bl'):
        dx = max(-x, min(dx, w - min_w))
        x, w = x + dx, w - dx
    elif kind in ('r', 'tr', 'br'):
        dx = max(min_w - w, min(dx, w_fr - (x + w)))
        w = w + dx
    if kind in ('t', 'tl', 'tr'):
        dy = max(-y, min(dy, h - min_h))
        y, h = y + dy, h - dy
    elif kind in ('b', 'bl', 'br'):
        dy = max(min_h - h, min(dy, h_fr - (y + h)))
        h = h + dy
    return Rect(x, y, w, h)


def mirror_resize(was_all: dict, name: str, new_box: Rect, size, names=None,
                  min_w: int = 20, min_h: int = 3) -> dict:
    """联动的那一个跟着走：把这个框「四条边各动了多少」原样搬过去。

    为什么按**边**搬（而不是把位移复制一遍）：挪框时两条边动的一样多（等于
    move_boxes），拉边时只有一条动 —— 搬四条边的差值就同时覆盖了这两件事，而且两个框
    永远一样大（游戏里右上角那两个按钮本来就一样大，尺寸不一致等于把其中一个框错）。

    was_all 是**按下那一刻**的全部框，name 是用户正在动的那个，names 是要跟着走的那些
    （默认全部）。返回新的 dict（不改入参）。
    """
    was = was_all[name]
    w_fr, h_fr = int(size[0]), int(size[1])
    names = [n for n in (was_all if names is None else names) if n in was_all]
    d0x, d0y = new_box.x - was.x, new_box.y - was.y
    d1x = (new_box.x + new_box.w) - (was.x + was.w)
    d1y = (new_box.y + new_box.h) - (was.y + was.h)
    out = dict(was_all)
    out[name] = new_box
    for n in names:
        if n == name:
            continue
        b = was_all[n]
        w = max(min_w, b.w + (d1x - d0x))
        h = max(min_h, b.h + (d1y - d0y))
        out[n] = Rect(max(0, min(b.x + d0x, w_fr - w)),
                      max(0, min(b.y + d0y, h_fr - h)), w, h)
    return out


def band_rows_of(rect: Rect) -> tuple:
    """探针框里「条带」占哪几行（相对 rect 顶边的行号，左右都含）。

    条是**画面里很细的一条**，而探针框上下各留了几行余量。读条必须只看
    条带那几行 —— 对整块取中位数会把白块稀释掉：

        实测（Desktop 2026.10.07 那段录像）白块只占探针框中间 4~8 行，
        `median(整个 26 行)` 的结果是轨道灰（60），一个白块都扫不到 ——
        「跳到最满的一帧」于是永远返回 -1。

    rect 里条的位置按「竖直居中」定死，条高恒为 BAR_ROI_H；量条带的行号
    用和 read_bar 同一套 BAR_BAND_TOP / BAR_BAND_BOTTOM（见 geometry）。
    传进来的 rect 本来就等于条 ROI 时（h = BAR_ROI_H）退化成 (5, 12)，还是对的。
    """
    top = max(0, (int(rect.h) - BAR_ROI_H) // 2)
    return top + BAR_BAND_TOP, top + BAR_BAND_BOTTOM - 1


def fill_extents(strip: np.ndarray, rect: Rect) -> tuple:
    """帧栈 -> (白块右端数组, 白块左端数组)，坐标是**画面坐标**（已加 rect.x）。

    看不见白块（条空着 / 满费 / 那一小块压根没有条）的那一帧记 -1 —— 调用方要的是
    「哪一帧最满」，没有白块本来就不该参与比较。
    """
    r0, r1 = band_rows_of(rect)
    ends, starts = [], []
    for f in strip:
        row = np.median(f[r0:r1 + 1, :].astype(np.int16), axis=0)
        run = fill_run(row)
        if run is None or run[1] - run[0] + 1 < MIN_RUN:
            ends.append(-1)
            starts.append(-1)
        else:
            starts.append(int(run[0]) + rect.x)
            ends.append(int(run[1]) + rect.x)
    return np.array(ends, dtype=int), np.array(starts, dtype=int)

