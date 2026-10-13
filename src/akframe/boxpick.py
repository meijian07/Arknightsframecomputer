# -*- coding: utf-8 -*-
"""手动框选费用条的**参照物**：取一帧画面 + 在上面画出「这里应该是什么样」。

用户实测反馈：「改成手动截取，手动截取不准确，需要参照物」。为什么会不准 ——
在 2560x1600 的录像里，费用条只占 180x17 个像素，而框选是在**缩放过的**画面上用鼠标
拖的：屏幕上 1 个像素 = 画面里 1.5~2 个像素，手一抖就是 3~4 个逻辑帧的相位误差。
只靠眼睛拖框，做不到「一费 30 帧」。

所以这里给三样参照物，把「凭手感」换成「对着东西对」：

1. **刻度尺**（guide_image）：费用条的设计是「条槽宽 = 30 个逻辑帧」。把预测的条槽
   位置画成一个被 30 等分的尺子，用户框的时候是**对着刻度**对两端，而不是对着空气；
2. **放大镜**（magnify）：把框那一块放大（横向 x 倍、纵向可以拉得更狠），并把**框的
   四条边都画出来**（左 / 右 = 红 / 绿竖线，上 / 下 = 品红横线），read_bar 真正取样的
   行画成蓝线 —— 白块的左端压没压在红线上、白条在上下两条品红线之间居没居中，一眼就能
   看出来；放大镜还能**按住平移**（pan）把框周围一起拉进来对、**滚轮局部放大**（zoom）
   只看框里的一小块（2026-10-11 追加）；
3. **最满的一帧**（scan_fill_extents）：让用户直接拿到「白块右端 = 条槽右端」的那一帧，
   右边缘不用猜；左边缘靠放大镜里的红线对。（原来还有一个「吸附左端」按钮，
   用户 2026-10-11：「不要搞吸附功能了，实测下来一次没用过」—— 连函数一起删了）。

这个模块里全是纯函数 / 只依赖 numpy + PIL + 已有的 features / align，不碰 Tk：
界面（ui.BoxPicker）只负责把这里画出来的图贴到画布上。
"""

from __future__ import annotations

import subprocess

import numpy as np
from PIL import Image, ImageDraw

from .align import extract_strip
from .features import fill_run
from .geometry import (BAR_BAND_BOTTOM, BAR_BAND_TOP, BAR_ROI_H, Layout,
                       Rect)

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
# 局部放大（用户 2026-10-11：「做局部放大！类似 ctrl+滚轮 的还是框选放大都行」）的
# 范围：1 = 窗口正好是框（原来的行为），越大看得越少、放得越大。
#
# 2026-10-13 第七轮（用户：「放大图也能用 ctrl 滚轮缩放，放大鼠标单击的地方，不要
# 跟随框会很乱」）两条：
#   * ZOOM_MIN 现在只管「不传 base」的老口径。取景器传了 base 之后，下限由 zoom_floor
#     按**面板装得下**算 —— 最多缩到 1 画面像素 = 1 屏像素（1:1）。再缩就得把画面
#     缩小（k < 1），「1 个画面像素 = k 列」这条对齐判据就没了，不做。
#   * base = 倍数的**基准窗口**（画面像素），默认 None = 用当前的框。取景器把它定在
#     「打开取景器 / 重画框」那一刻的框上：改框只动四条彩线，倍数不动（见 _zoom_window）。
ZOOM_MIN = 1.0
ZOOM_MAX = 16.0
ZOOM_FLOOR_CAP = 0.2            # 再小也不给（面板装不下，给了也是缩成一片糊）


def grab_frame(path: str, ffmpeg: str, size, index: float, fps: float) -> np.ndarray:
    """从录像里取第 index 帧（灰度，h x w）。

    用 -ss 快速定位（放在 -i 前面）：拿静帧只要「大概这一帧」，而精确定位要把前面
    所有帧都解一遍 —— 拖一次滑块等十几秒是不能接受的。ffmpeg 会从关键帧解到目标时间，
    所以拿到的确实是那一帧（不是关键帧本身）。
    """
    vw, vh = int(size[0]), int(size[1])
    t = max(0.0, float(index) / max(1e-6, float(fps)))
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', '%.3f' % t,
           '-i', path, '-frames:v', '1', '-vf', 'format=gray',
           '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    out = subprocess.run(cmd, check=True, stdout=subprocess.PIPE).stdout
    if len(out) < vw * vh:
        raise RuntimeError('取第 %d 帧失败：ffmpeg 只给了 %d 字节（要 %d）'
                           % (index, len(out), vw * vh))
    return np.frombuffer(out[:vw * vh], dtype=np.uint8).reshape(vh, vw)


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


def zoom_floor(base, panel=(556, 186)) -> float:
    """缩小的下限（zoom 最小能到多少）。

    口径：视窗放到**面板装得下**为止，也就是 1 画面像素 = 1 屏像素（1:1）—— 再往下
    就要把画面缩小（k < 1），「1 个画面像素 = k 列」这条对齐判据就没了。视窗 =
    base / zoom，所以 zoom >= base / 面板尺寸，两个轴取紧的那个。

    base 比面板还大时（框拉得比放大图还宽）保住 1.0：那种框本来就画不下，老口径
    是拿 1.0 兜的，不在这轮改。
    """
    bw = max(1, int(base[0]))
    bh = max(1, int(base[1]))
    return max(ZOOM_FLOOR_CAP, min(1.0, max(float(bw) / panel[0], float(bh) / panel[1])))


def clamp_zoom(zoom, base=None, panel=None) -> float:
    """把 zoom 夹进 [下限, ZOOM_MAX]。不传 base / panel = 老口径（下限 1）。"""
    lo = ZOOM_MIN if (base is None or panel is None) else zoom_floor(base, panel)
    return max(lo, min(ZOOM_MAX, float(zoom)))


def _zoom_window(box: Rect, zoom=1.0, base=None, panel=None) -> tuple:
    """放大镜的窗口尺寸 (宽, 高)（画面像素）= base / zoom，至少 1 像素。

    zoom = 1 就是原来的行为（窗口正好是整个框）；zoom > 1 是局部放大：看框里的
    一小块，因此倍数 = 面板 / 窗口 也跟着变大。

    base = 基准窗口（画面像素），默认 = 当前的框。取景器传的是**打开取景器那一刻**
    的框（见 ZOOM_MIN 那段）：否则拖手柄 / 微调改框时会顺手把整张放大图缩放掉，
    刚对准的地方又跑了。
    """
    bw, bh = (int(box.w), int(box.h)) if base is None else (int(base[0]), int(base[1]))
    z = clamp_zoom(zoom, base, panel)
    return (max(1, int(round(bw / z))), max(1, int(round(bh / z))))


def zoom_scale(box: Rect, panel=(556, 186), zoom=1.0, base=None) -> tuple:
    """放大镜的倍数 (kx, ky)：横纵分开，横向 1..16 倍、纵向 1..32 倍。

    纵向拉得更狠是有原因的：条只有 17 行高，等比放大会得到一条扁带子，看不出「白块左端
    有没有压在红线上」。倍数只由**base 和 zoom** 决定（不由看哪儿决定）—— 拖动放大图是
    平移，不该顺手把画面缩掉，否则「挪一下看清楚点」会变成「越挪越小」；base 默认是
    当前的框，传了冻结的 base 就与框无关（见 _zoom_window）。
    """
    vw, vh = _zoom_window(box, zoom, base, panel)
    kx = max(1, min(16, int(panel[0]) // vw))
    ky = max(1, min(32, int(panel[1]) // vh))
    return kx, ky


def zoom_view(box: Rect, panel=(556, 186), pan=(0, 0), zoom=1.0, size=None,
              base=None) -> tuple:
    """放大镜「看哪儿、多大倍数」的唯一出口：(vx, vy, vw, vh, kx, ky)。

    vx / vy = 视窗左上角在画面里的坐标；给了 size = (宽, 高) 就**已经夹好**
    （视窗一定整块在画面里）；pan = 视窗左上角**相对框左上角**的偏移（画面像素）。

    为什么要有这个函数：界面算「光标底下是哪个画面点」和 magnify 画图必须是**同一份
    几何**，否则滚轮缩放时盯着鼠标的那一点会慢慢漂走（各算一遍最容易差在取整上）。
    """
    kx, ky = zoom_scale(box, panel, zoom, base)
    vw, vh = _zoom_window(box, zoom, base, panel)
    if size is None:
        vx = max(0, int(box.x) + int(pan[0]))
        vy = max(0, int(box.y) + int(pan[1]))
        return vx, vy, vw, vh, kx, ky
    w, h = int(size[0]), int(size[1])
    vw, vh = min(vw, w), min(vh, h)
    # pan 是浮点（滚轮缩放时按「光标底下那一点不动」算出来的），这里四舍五入而不是截断 ——
    # 截断在负数上会向 0 靠，每一格滚轮都向同一侧漂 半个像素。
    vx = max(0, min(int(round(int(box.x) + pan[0])), w - vw))
    vy = max(0, min(int(round(int(box.y) + pan[1])), h - vh))
    return vx, vy, vw, vh, kx, ky


def edge_lines(box: Rect, panel=(556, 186), pan=(0, 0), zoom=1.0, size=None,
               base=None) -> tuple:
    """框的四条边**画在放大图的哪一列 / 行**：(左列, 右列, 上行, 下行)，左闭。

    抽成一个函数是给界面用的：放大图里「鼠标是不是抓住了某条边」必须和 magnify
    画那条线用的是**同一套取整**，否则手感是「明明压在线上却没抓住」（差 1 像素就够）。

    口径（和 magnify 里画线时完全一致）：第 x 列的左边界是 int((x - vx) * kx)，
    所以框占了 x..x+w-1 这些列，右那条线画在 int((x + w - vx) * kx) - 1 上。
    """
    vx, vy, _vw, _vh, kx, ky = zoom_view(box, panel, pan, zoom, size, base)

    def sx(x):
        return int((x - vx) * kx)

    def rt(y):
        return int((y - vy) * ky)

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
            band=(0, 0), pan=(0, 0), zoom=1.0, ghost=(), band_fill=False,
            base=None) -> Image.Image:
    """把框那一块放大，并把**框的四条边**画在上面：红 / 绿 = 左右，品红 = 上下。

    pan = 放大镜**相对框**的偏移（画面像素，正 = 往右下看），(0,0) 就是「只看框里」。
    为什么要有它（用户 2026-10-11：「手动校准界面右边的放大图要可以自己移动，不然很难
    对准」）：框只框住按钮那一小块时，放大镜里除了按钮什么都没有 —— 按钮偏出去多少、
    往哪边偏，全看不见。能挪之后就能把周围一起拉进来对着看。

    四条边为什么一直画（用户紧接着的下一条：「费用条难以准确框选，加一个可移动的局部
    放大功能，查看上下左右是否对齐」）：只标左右的话，上下对没对上只能猜。默认视图
    （pan=0）下这四条正好压在画布的四边上 —— 它们同时也是「图到这儿就没了」的界线
    （画布比图大，右边 / 下面会空出一截）；平移之后它们跟着框走，于是「框相对条偏了
    多少」四条边一起看得见。越界的那条不画：它确实不在视窗里，画在边上等于撒谎。

    zoom = **局部放大**倍数：窗口 = base / zoom，所以 zoom > 1 就是「只看框里的一小块，
    放得更大」（用户 2026-10-11：「做局部放大！类似 ctrl＋滚轮 的还是框选放大都行」），
    zoom < 1 就是往外缩（最多缩到面板装得下，见 zoom_floor）。它和 pan 一样，只改
    「看哪儿 / 多大」，不改取样的行。base = 基准窗口，默认是当前的框（见 _zoom_window）。

    倍数（zoom_scale）只由**框和 zoom** 决定、不由看哪儿决定 —— 拖动是平移，不该顺手把画面缩掉，
    否则「挪一下看清楚点」会变成「越挪越小」。

    band_fill：把两条蓝线**中间整条填一层淡色**（用户 2026-10-12：「暂停键参考不
    明确」）。两条 1px 的线要回答「字的顶边压在线的哪一侧」，本来就容易看错；一条带
    的**上沿**就没这个歧义了。

    ghost：一串**多边形**（每个是 4 个**画面坐标**的点），画成半透明的「虚影」—— 给暂停键
    用的「字形应该长在这儿」（造它的是 pause_ghost，跟着框走）。它跟真字形一样是**斜的**
    （平行四边形），所以能叠着对得上；和 pan / zoom 一样只是**画上去**的参考，不参与读数。
    """
    h, w = frame.shape[:2]
    vx, vy, vw, vh, kx, ky = zoom_view(box, panel, pan, zoom, (w, h), base)
    crop = frame[vy:vy + vh, vx:vx + vw]
    ch, cw = crop.shape[:2]
    if ch < 1 or cw < 1:
        return Image.new('RGB', (1, 1))
    iw, ih = cw * kx, ch * ky
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
    el, er, et, eb = edge_lines(box, panel, pan, zoom, (w, h), base)
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

    条是**画面里很细的一条**，而探针框上下各留了几行余量（probe_rect）。读条必须只看
    条带那几行 —— 对整块取中位数会把白块稀释掉：

        实测（Desktop 2026.10.07 那段录像）白块只占探针框中间 4~8 行，
        `median(整个 26 行)` 的结果是轨道灰（60），一个白块都扫不到 ——
        「跳到最满的一帧」于是永远返回 -1。

    rect 里条的位置由 probe_rect 定死（竖直居中），条高恒为 BAR_ROI_H；量条带的行号
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


def scan_fill_extents(path: str, ffmpeg: str, rect: Rect, stride: int = 12) -> tuple:
    """按 stride 扫一遍录像的 rect 这一块，返回 (帧号数组, 白块右端数组, 白块左端数组)。

    用途：**跳到费用条最满的那一帧** —— 那一帧的白块右端就是条槽右端。有了它，
    框的右边缘也不用猜（用户自己等一整个费用周期太慢）。
    只解 rect 这一小块、每隔 stride 帧取一帧，所以扫整段录像也就一两秒。
    """
    strip = extract_strip(ffmpeg, path, rect, stride)
    ends, starts = fill_extents(strip, rect)
    idx = np.arange(strip.shape[0]) * max(1, int(stride))
    return idx, ends, starts


def fullest_frame(path: str, ffmpeg: str, rect: Rect, stride: int = 12) -> int:
    """「最满的那一帧」的帧号（扫不到白块就返回 -1）。"""
    idx, ends, _starts = scan_fill_extents(path, ffmpeg, rect, stride)
    if ends.size == 0:
        return -1
    k = int(np.argmax(ends))
    return int(idx[k]) if ends[k] > 0 else -1


def probe_rect(size, box: Rect, pad: int = 40) -> Rect:
    """扫 / 取帧用的探针框：用户框出来的那条带，左右各留一点余量（找最满用）。"""
    w, h = int(size[0]), int(size[1])
    x0 = max(0, int(box.x) - pad)
    x1 = min(w, int(box.x + box.w) + pad)
    yc = int(box.y) + int(box.h) // 2
    y0 = max(0, yc - BAR_ROI_H // 2 - 4)
    y1 = min(h, yc + BAR_ROI_H // 2 + 5)
    return Rect(x0, y0, max(8, x1 - x0), max(8, y1 - y0))