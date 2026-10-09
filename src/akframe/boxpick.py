# -*- coding: utf-8 -*-
"""手动框选费用条的**参照物**：取一帧画面 + 在上面画出「这里应该是什么样」。

用户实测反馈：「改成手动截取，手动截取不准确，需要参照物」。为什么会不准 ——
在 2560x1600 的录像里，费用条只占 180x17 个像素，而框选是在**缩放过的**画面上用鼠标
拖的：屏幕上 1 个像素 = 画面里 1.5~2 个像素，手一抖就是 3~4 个逻辑帧的相位误差。
只靠眼睛拖框，做不到「一费 30 帧」。

所以这里给三样参照物，把「凭手感」换成「对着东西对」：

1. **刻度尺**（guide_image）：费用条的设计是「条槽宽 = 30 个逻辑帧」。把预测的条槽
   位置画成一个被 30 等分的尺子，用户框的时候是**对着刻度**对两端，而不是对着空气；
2. **放大镜**（magnify）：把框内那一条带放大（横向 x 倍、纵向可以拉得更狠），并把
   框的左右边缘画成红 / 绿竖线、把 read_bar 真正取样的行画成蓝线 —— 白块的左端到底
   压没压在红线上，一眼就能看出来；
3. **吸附 + 最满的一帧**：白块左端就是条槽左端（填充是从条槽左端开始长的），所以
   「吸附左端」能把左边缘直接钉到像素上（见 snap_box）；「跳到最满的一帧」让用户拿到
   白块右端 = 条槽右端的那一帧（见 scan_fill_extents），右边缘也就不用猜了。

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
BAND_LINE = (90, 190, 255)     # read_bar 真正取样的那条带

# 白块短于这么多列就不当数（和 calib.MIN_RUN 同口径：场景里的高光都很短）
MIN_RUN = 6
# 吸附左端时允许往回找的范围：框的左边缘往左这么多像素内找白块起点。
# 用户框的时候通常会把条槽左边多框进来十几个像素，30 以内够用。
SNAP_BACK = 40
# 刻度尺画多少格：一个费用周期 = 30 逻辑帧，这是游戏的机制常数（见 docs）。
TICKS = 30


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
    这一带、条槽有多宽、30 格刻度长什么样。真正定相位的是放大镜和吸附。
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


def magnify(frame: np.ndarray, box: Rect, panel=(520, 170), layout: Layout = None,
            band=(0, 0)) -> Image.Image:
    """把框内那一块放大：红 / 绿 = 框的左右边缘，蓝 = read_bar 取样的行。

    横向和纵向分开放大（纵向拉得更狠）：条只有 17 行高，等比放大会得到一条扁带子，
    看不出「白块左端有没有压在红线上」；拉高之后一眼就能判。
    """
    h, w = frame.shape[:2]
    x0 = max(0, min(int(box.x), w - 1))
    y0 = max(0, min(int(box.y), h - 1))
    x1 = max(x0 + 1, min(int(box.x + box.w), w))
    y1 = max(y0 + 1, min(int(box.y + box.h), h))
    crop = frame[y0:y1, x0:x1]
    ch, cw = crop.shape[:2]
    kx = max(1, min(16, panel[0] // cw))
    ky = max(1, min(32, panel[1] // ch))
    img = Image.fromarray(crop, mode='L').convert('RGB').resize(
        (cw * kx, ch * ky), Image.NEAREST)
    d = ImageDraw.Draw(img)
    d.line([(0, 0), (0, ch * ky - 1)], fill=EDGE_LEFT, width=2)
    d.line([(cw * kx - 1, 0), (cw * kx - 1, ch * ky - 1)], fill=EDGE_RIGHT, width=2)
    r0, r1 = band
    if r1 > r0:
        for r in (r0, r1):
            yy = int((r - int(box.y) + 0.5) * ky)
            if 0 <= yy < ch * ky:
                d.line([(0, yy), (cw * kx - 1, yy)], fill=BAND_LINE)
    if layout is not None:
        for x, col in ((layout.bar.x, GUIDE_BAR), (layout.bar.x2, GUIDE_BAR)):
            xx = int((x - int(box.x) + 0.5) * kx)
            if 0 <= xx < cw * kx:
                d.line([(xx, 0), (xx, ch * ky - 1)], fill=col, width=1)
    return img


def snap_box(frame: np.ndarray, box: Rect, band=(0, 0)) -> tuple:
    """把框的**左边缘**吸附到框里白块的左端（条槽左端）。

    为什么这个吸附是安全的：填充是从条槽左端开始往右长的，所以「白块最左那一列」
    就等于「条槽左端」——这是画面上一条**与生俱来**的尺子，不用猜、不用模型。
    找不到白块（条正空着 / 满费变灰底）就原样返回，什么也不动。

    返回 (新框, 移动了多少像素, 说明)。宽度跟着缩：右边缘是用户拖的，不该被这次吸附改掉。
    """
    h, w = frame.shape[:2]
    x0 = max(0, min(int(box.x), w - 1))
    x1 = max(x0 + 1, min(int(box.x + box.w), w))
    r0, r1 = band
    y0 = max(0, min(int(box.y) + r0, h - 1))
    y1 = max(y0 + 1, min(int(box.y) + r1 + 1, h))
    row = np.median(frame[y0:y1, x0:x1], axis=0).astype(np.int16)
    run = fill_run(row)
    if run is None or run[1] - run[0] + 1 < MIN_RUN:
        return box, 0, '框里没看见白块（条空着 / 满费？），左边缘没法吸附'
    start = x0 + int(run[0])
    shift = start - int(box.x)
    if shift <= 0:
        return box, 0, '白块左端就在框的左边缘上（或更左），不动'
    if shift > SNAP_BACK:
        return box, 0, ('白块左端在框左边缘右边 %dpx，超出吸附范围（%dpx），'
                        '请先把框拖近一点' % (shift, SNAP_BACK))
    new = Rect(start, box.y, max(2, box.w - shift), box.h)
    return new, shift, '左边缘吸附到白块起点：右移 %dpx（条槽左端就在那儿）' % shift


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
    """扫 / 取帧用的探针框：用户框出来的那条带，左右各留一点余量（吸附、找最满用）。"""
    w, h = int(size[0]), int(size[1])
    x0 = max(0, int(box.x) - pad)
    x1 = min(w, int(box.x + box.w) + pad)
    yc = int(box.y) + int(box.h) // 2
    y0 = max(0, yc - BAR_ROI_H // 2 - 4)
    y1 = min(h, yc + BAR_ROI_H // 2 + 5)
    return Rect(x0, y0, max(8, x1 - x0), max(8, y1 - y0))