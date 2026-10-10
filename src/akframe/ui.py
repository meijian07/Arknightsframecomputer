"""简易测试前端：实时帧数显示 + 三个 ROI 的实时预览 + ROI 校准工具。

为什么要有预览和校准：geometry.py 里的尺寸常数目前只在 2560x1600 这一个窗口尺寸上
实测过。换个窗口大小 / 换个平台（目标是「多样接口」），ROI 可能偏几个像素，而偏几像素
就足以让读数全错。所以前端的第一职责不是好看，而是**让你能一眼看出 ROI 有没有对准**：

    预览图上的红线 = 特征提取实际使用的那条带
    屏幕上加粗的方框（K 键）= 当前 ROI 位置，直接叠在游戏画面上

操作：
    Tab 切换要校准的 ROI   方向键 移动   Shift+方向键 改大小
    P 暂停计数（数字按住不动）
    K 显示/隐藏屏幕上的 ROI 框    S 保存校准   R 恢复默认
    C 清零计数    L 开始/停止记录 CSV    P 暂停/继续计数（手动）
    T 导入 md 轴文件    F5 重新校准（采样停了 / 窗口动了）    Esc 退出

回费速率（减费 tag）：默认 30 帧/费（1 秒 1 费）。点了「回费速度 -50% / -75%」的 tag
时，在面板「回费速率」那行选 60 / 120，或直接填数字后点「应用」——改完立刻生效，
并写回设置文件 cost_rate.json，下次启动自动带上。

打轴器（提词器）：导入一个 md 轴文件，或直接在下面的输入框里写（Ctrl+Enter 应用），
计数器走到对应帧号时，面板上就把那条说明放大显示出来。轴文件格式见 timeline.py 顶部注释
和 maps/example.md。

读窗口（第 3 条需求「多样接口」）：**不解码文件**，直接读屏幕上正在显示的那个窗口 ——
把录屏用播放器打开、置顶，点「读取窗口…」选它 → 自动校准（没把握就点「框选费用条」
对着参照图手动圈）→ **点「开始计数」** 才起采样线程。游戏窗口本身也走这条路，所以
「识别游戏」和「识别视频」是同一套代码，只是 ROI 从 window_calib.json 来
（键 = 进程名 + 客户区尺寸）—— 见 window.py。
读窗口时目标窗口会被自动钉在最上层，面板则每 0.7 秒被重新抬到它上面（置顶是一层，
同一层里最后抬起来的在最上面）—— 见 _keep_pin / _raise_panel 和 docs 第 28 节。
（原先还有「打开录像文件…」直接喂 mp4 的那一条，已按用户要求删掉，见 docs 第 27 节。）
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog

from PIL import Image, ImageDraw, ImageTk

from . import boxpick
from .geometry import (BAR_BAND_BOTTOM, BAR_BAND_TOP, Rect, apply_deltas, layout_for,
                       load_calibration)
from .live import LiveMonitor
from .settings import (COST_RATE_PRESETS, DEFAULT_FRAMES_PER_COST, NEGATIVE_LABELS,
                       NEGATIVE_MODES, clamp_frames_per_cost, format_frames_per_cost,
                       parse_negative, save_cost_rate, save_negative_mode)
from .scale import ScaleGeom, Theme, build as build_scale, clip, one_line
from .scale_render import render as render_scale
from .state import LOGICAL_FPS
from .timeline import Teleprompter, parse_lines
from .win32 import (ScreenGrabber, arknights_ui_scaler, client_rect, is_window,
                    raise_window, set_click_through, set_topmost, to_gray, top_level,
                    virtual_screen_bounds, work_area)
from .window import WindowMonitor, WindowSource, build_window_layout


def grab_window_client(win, rect: Rect):
    """抓一个窗口**客户区**里的一块，返回灰度图（框选参照图要用）。

    和 window.WindowSource._grab 是同一件事，只是这里不需要一个跑起来的帧源对象：
    截图窗口当参照物只要一次抓屏。
    """
    cx, cy = client_rect(win.hwnd)[:2]
    with ScreenGrabber() as grabber:
        arr = grabber.grab_bgra(cx + int(rect.x), cy + int(rect.y),
                                max(1, int(rect.w)), max(1, int(rect.h)))
    return to_gray(arr)


# 输入框里的初始提示（# 开头 = 注释，解析器会跳过，不会变成轴）
# 注意：提示行必须用 // 开头（解析器整行跳过）。写成 # 开头的话，'# 120 部署银灰'
# 剥掉井号后正好是一条合法轴，会被当成真的轴吃掉。
AXIS_HINT = ('// 在这里直接写轴：帧号开头，后面跟说明，和 md 轴文件同一个格式\n'
             '//   120 部署银灰\n'
             '//   350 开技能\n'
             '//   1:05 撤退        （也可以用 分:秒，1:05 = 1950 帧）\n'
             '// 写完点右边的「应用到提词器」，或按 Ctrl+Enter\n')

# 读窗口那条链路的阶段名（window.WindowSource：没有探测和抽流，多了一步自检）
WINDOW_STAGES = {'align': '自动对齐费用条', 'measure': '量费用条槽',
                 'buttons': '搜右上角两个按钮', 'verify': '自检：费用条在不在动',
                 'ready': '就绪'}

# 「读窗口」的目标窗口每隔多少秒重新钉一次置顶（见 MonitorApp._keep_pin）。
# 0.7s 是「够快」和「别每拍都去调 SetWindowPos」之间的折中：真的被盖住时，用户看到的
# 是下次采样（8ms 一拍）读不到画面，把这一层延迟压到一秒以内就够用了。
PIN_KEEP_S = 0.7

# ---- 配色：照着游戏内的 HUD 抄 ----
# 实测那张 HUD 截图（费用「◆C 56」）：面板是深炭灰、字和线一律纯白、没有第二个强调色，
# 标志是**大数字下面压着一条 6px 粗的纯白线**。所以这里的主色只有白，靠线的粗细分层；
# 只有要命的状态才带颜色。选中态统一用「白底深字」反色（和游戏里 flash 的处理一致）。
UI_BG = '#151517'        # 窗口底
UI_FG = '#ffffff'        # 正文一律纯白
UI_DIM = '#9b9ba0'       # 次要信息
FAINT = '#616167'        # 帮助行这类「几乎不用看」的
CARD = '#1d1d1f'         # 一块一块的面板（游戏里那层深炭灰色块）
CARD_HI = '#2a2a2d'      # 按钮 / 悬停
SUNKEN = '#0f0f11'       # 预览画布、输入框：比底更深，像游戏里的凹槽
RULE = '#ffffff'         # 白色分隔线 / 大数字下面那条粗线
RULE_DIM = '#3a3a3e'     # 1px 的淡分隔
ACCENT = '#ffffff'       # 主强调色 = 白（游戏 HUD 只有白）
WARN = '#e6a33c'
ERR = '#e0574c'          # 采样线程停了这类「必须去看一眼」的状态
# ROI 三色是**功能色**（叠在游戏画面上时靠颜色认是哪一路），不能跟着单色化走，
# 只是把亮度压到和 HUD 一个档；「正在校准的那一路」仍用纯白标出来。
ROI_COLORS = {'bar': '#5aa7ff', 'pause': '#b9b9c0', 'speed': '#e6a33c'}
# 字体：数字用 Bahnschrift（窄体，刻度表上也是它，和游戏 HUD 的观感接近），
# 界面文字用微软雅黑；轴文本编辑器用等宽，方便对齐帧号。
FONT_UI = 'Microsoft YaHei UI'
FONT_NUM = 'Bahnschrift SemiBold'
FONT_MONO = 'Consolas'
# 校准目标 / ROI 框 / 预览按这个顺序走。
ROI_ORDER = ('bar', 'pause', 'speed')

# 刻度缩放档：1 个逻辑帧画几个像素。轴点挤在一起就调小，隔得远就调大。
SCALE_ZOOMS = ((0.5, '0.5x'), (1.0, '1x'), (1.5, '1.5x'), (3.0, '3x'))
SCALE_ZOOM_DEFAULT = 1.5

# 采样线程多久没有心跳就判「停了」。选 1 秒：正常采样 120Hz（心跳每 8ms 一次），
# 1 秒 = 120 次心跳一次都没来，绝不可能是抖动；而 1 倍速下一个逻辑帧才 33ms，
# 卡满 1 秒已经是肉眼可见的停顿，必须提示。
STALL_WARN_S = 1.0

# 满费的两个状态（state.BAR_PRESENT_STATES 去掉 TRACK）：满费时大数字印两位小数。
FULL_COST_STATES = ('MAX', 'MAX_FADE')
# 大数字右边留的空白（像素）。_sync_rule 里预留列宽时要把这一条加回去：grid 的
# minsize 管的是**整格**（控件 + 这一侧的 padx），漏掉它最后一位数字会被切掉。
NUM_RIGHT_PAD = 13
# 满费结束后还按「满费格式」多印几拍再切回整数。为什么要有这个保持：MAX / TRACK 的
# 边界上偶尔会读错一拍，不保持的话数字会在 123 和 123.00 之间闪一下（8ms 一拍）。
FULL_COST_HOLD_TICKS = 12


# 「自己填一个数」的那个下拉项。选中它不切档，只把光标丢进输入框。
RATE_CUSTOM_LABEL = '自定义（在右边输入）'
AUTO_RATE_LABEL = '自动识别中…'
# 负费档的下拉框用的是「说明文字」，要靠这张表翻回档名
_NEG_BY_LABEL = {v: k for k, v in NEGATIVE_LABELS.items()}


def _rate_label(frames: float) -> str:
    """档位 -> 下拉框里显示的文字。落在已知档位上就显示那个档，否则显示数字。"""
    f = clamp_frames_per_cost(frames)
    for v, desc in COST_RATE_PRESETS:
        if abs(v - f) < 1e-9:
            return '%.0f  %s' % (v, desc)
    return '%.0f  自定义' % f


def _fmt_time(seconds: float) -> str:
    """把秒数格式化成 mm:ss.cc（游戏内时间，1 倍速等效）。"""
    if seconds < 0:
        seconds = 0.0
    m = int(seconds // 60)
    s = seconds - m * 60
    return '%02d:%05.2f' % (m, s)


class BoxPicker:
    """手动框选费用条的**取景器**：摆一帧画面当参照物，让用户对着它把费用条圈出来。

    为什么不再用实时那条全屏半透明遮罩（MonitorApp.box_bar）：那是「蒙住屏幕、凭手感
    拖」的 —— 2560x1600 的画面缩到屏幕上，鼠标差 2 个像素就是 3~4 个逻辑帧的相位差。
    用户的原话是「手动截取不准确，需要参照物」，所以这里给三样：

        * 刻度尺：费用条的设计是「条槽宽 = 30 个逻辑帧」，参照图上把预测的条槽画成
          30 等分的橙尺 —— 对的是刻度，不是空气；
        * 放大镜：框内那一条带放大（横向 x 倍、纵向拉得更狠），红 / 绿竖线就是框的
          左右边缘，白块的左端压没压在红线上，一眼就能看出来；
        * 吸附 + 最满的一帧：「吸附左端」把左边缘钉到白块起点（那是画面上本来就有的
          尺子）；「跳到最满的一帧」让用户拿到白块右端 = 条槽右端的那一帧。

    对象用完即弃（应用 / 取消 / Esc 都调 close），面板靠 on_apply(box) 拿结果。
    """

    def __init__(self, parent, fetch, frames: int, index: int, on_apply, on_note=None,
                 fullest=None):
        self.fetch = fetch                  # fetch(i) -> (灰度帧, 参考 layout)
        self.frames = max(1, int(frames))
        self.on_apply = on_apply
        self.on_note = on_note or (lambda text, hold=3.0: None)
        self.fullest = fullest              # fullest(box) -> 帧号（-1 = 找不到）
        self.index = max(0, min(int(index), self.frames - 1))
        self.frame = None
        self.guide = None
        self.box = None
        self._drag_from = None
        self._bg = None
        self._photo = {}
        self.scale = 1.0

        top = tk.Toplevel(parent)
        self.top = top
        top.title('框选费用条（参照图）')
        top.configure(bg=UI_BG)
        top.attributes('-topmost', True)
        tk.Label(top, bg=UI_BG, fg=UI_FG, font=(FONT_UI, 10), justify='left', anchor='w',
                 text='拖一个框把费用条整个圈住：左右两端要贴住**条槽的两头**（高矮随便）。\n'
                      '橙色 = 按画面尺寸推测的条槽位置（只是参考，30 格 = 30 个逻辑帧）；'
                      '框完会自动把左端吸附到白块起点。').pack(fill='x', padx=10, pady=(8, 4))

        mid = tk.Frame(top, bg=UI_BG)
        mid.pack(fill='both', expand=True, padx=10)
        self.cv = tk.Canvas(mid, bg=SUNKEN, highlightthickness=0, cursor='crosshair',
                            width=640, height=400)
        self.cv.pack(side='left')
        side = tk.Frame(mid, bg=UI_BG)
        side.pack(side='left', padx=(8, 0), anchor='n')
        self.cv_zoom = tk.Canvas(side, bg=SUNKEN, highlightthickness=0, width=560, height=190)
        self.cv_zoom.pack(anchor='w')
        self.lb_read = tk.Label(side, text='', bg=UI_BG, fg=UI_FG, font=(FONT_MONO, 10),
                                justify='left', anchor='w')
        self.lb_read.pack(fill='x', pady=(6, 0))
        self.lb_hint = tk.Label(side, text='', bg=UI_BG, fg=WARN, font=(FONT_UI, 9),
                                justify='left', anchor='w', wraplength=560)
        self.lb_hint.pack(fill='x', pady=(4, 0))

        frow = tk.Frame(top, bg=UI_BG)
        frow.pack(fill='x', padx=10, pady=(8, 0))
        if self.frames > 1:
            tk.Label(frow, text='参照帧', bg=UI_BG, fg=UI_DIM,
                     font=(FONT_UI, 9)).pack(side='left')
            self.sk = tk.Scale(frow, from_=0, to=self.frames - 1, orient='horizontal',
                               showvalue=0, bg=UI_BG, fg=UI_FG, troughcolor=SUNKEN, bd=0,
                               highlightthickness=0, sliderrelief='flat', resolution=1,
                               length=360, activebackground=RULE, font=(FONT_UI, 8))
            self.sk.set(self.index)
            self.sk.pack(side='left', padx=6)
            self.sk.bind('<ButtonRelease-1>', self._seek)
            self._btn(frow, '◀ 1 帧', lambda: self.load(self.index - 1)).pack(side='left', padx=2)
            self._btn(frow, '1 帧 ▶', lambda: self.load(self.index + 1)).pack(side='left', padx=2)
            if self.fullest is not None:
                self._btn(frow, '跳到最满的一帧', self._jump_fullest).pack(side='left', padx=6)
            self.lb_pos = tk.Label(frow, text='', bg=UI_BG, fg=UI_DIM, font=(FONT_UI, 9))
            self.lb_pos.pack(side='left', padx=6)

        nrow = tk.Frame(top, bg=UI_BG)
        nrow.pack(fill='x', padx=10, pady=(4, 0))
        for text, cmd in (('左端 −1', self._left_out), ('左端 +1', self._left_in),
                          ('右端 −1', self._right_in), ('右端 +1', self._right_out)):
            self._btn(nrow, text, cmd).pack(side='left', padx=2)
        self._btn(nrow, '吸附左端', self._snap).pack(side='left', padx=8)
        for text, cmd in (('整框 ↑', self._up_1), ('整框 ↓', self._down_1),
                          ('整框 ←', self._left_1), ('整框 →', self._right_1)):
            self._btn(nrow, text, cmd).pack(side='left', padx=2)

        brow = tk.Frame(top, bg=UI_BG)
        brow.pack(fill='x', padx=10, pady=(6, 10))
        self.btn_ok = self._btn(brow, '用这个框（重新抽流，几十秒）', self._apply)
        self.btn_ok.pack(side='left')
        self._btn(brow, '取消', self.close).pack(side='left', padx=6)

        self.cv.bind('<Button-1>', self._down)
        self.cv.bind('<B1-Motion>', self._drag)
        self.cv.bind('<ButtonRelease-1>', self._up)
        top.bind('<Escape>', lambda _e: self.close())
        top.protocol('WM_DELETE_WINDOW', self.close)
        self.load(self.index)

    # ---- 小部件 ----
    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=CARD_HI, fg=UI_FG, bd=0,
                         relief='flat', font=(FONT_UI, 9), padx=6, pady=2,
                         activebackground=RULE, activeforeground=UI_BG,
                         highlightthickness=0)

    # ---- 载入一帧参照图 ----
    def load(self, i):
        i = max(0, min(int(i), self.frames - 1))
        try:
            frame, guide = self.fetch(i)
        except Exception as exc:                        # noqa: BLE001
            self.on_note('取参照帧失败：%s' % exc)
            return
        self.index = i
        self.frame = frame
        self.guide = guide
        h, w = frame.shape[:2]
        max_w = max(420, int(self.top.winfo_screenwidth() * 0.55))
        max_h = max(280, int(self.top.winfo_screenheight() * 0.55))
        self.scale = min(1.0, max_w / float(w), max_h / float(h))
        img = boxpick.guide_image(frame, guide)
        if self.scale < 1.0:
            img = img.resize((max(1, int(w * self.scale)), max(1, int(h * self.scale))),
                             Image.BILINEAR)
        self._bg = ImageTk.PhotoImage(img)
        self.cv.configure(width=img.width, height=img.height)
        self.cv.delete('all')
        self.cv.create_image(0, 0, anchor='nw', image=self._bg)
        if self.box is None:
            # 从预测位置起步：对上了就直接用，没对上就在它附近拖 —— 总比从零开始强
            self.box = Rect(int(guide.bar.x), int(guide.bar.y) - 4, int(guide.bar.w), 25)
        if hasattr(self, 'lb_pos'):
            self.sk.set(self.index)
            self.lb_pos.configure(text='第 %d / %d 帧' % (self.index, self.frames - 1))
        self.redraw()

    def _seek(self, _event=None):
        self.load(self.sk.get())

    def _jump_fullest(self):
        if self.fullest is None or self.box is None:
            return
        try:
            i = self.fullest(self.box)
        except Exception as exc:                        # noqa: BLE001
            self.on_note('扫最满那一帧失败：%s' % exc)
            return
        if i < 0:
            self.lb_hint.configure(text='这一帧里没扫到白块（费用条可能一直没动）')
            return
        self.load(i)
        self.lb_hint.configure(text='已跳到费用条最满的一帧（第 %d 帧）：'
                                    '现在白块的右端就是条槽右端，把右端拖到那儿'
                               % self.index)

    # ---- 画 ----
    def _to_img(self, x, y):
        return int(x / self.scale), int(y / self.scale)

    def redraw(self):
        self.cv.delete('box')
        if self.box is None:
            return
        b = self.box
        x0, y0 = int(b.x * self.scale), int(b.y * self.scale)
        x1, y1 = int((b.x + b.w) * self.scale), int((b.y + b.h) * self.scale)
        self.cv.create_rectangle(x0, y0, x1, y1, outline='#ffffff', width=2, tags='box')
        self.cv.create_line(x0, y0, x0, y1, fill='#ff4646', width=2, tags='box')
        self.cv.create_line(x1, y0, x1, y1, fill='#46e66e', width=2, tags='box')
        h, w = self.frame.shape[:2]
        ok = b.w >= 20 and b.x >= 0 and b.h >= 3 and b.y >= 0 and b.x + b.w <= w and b.y + b.h <= h
        self.lb_read.configure(
            text='框 x=%d y=%d  宽 %d 高 %d\n'
                 '条带行 %d..%d ｜ 每逻辑帧 %.2f px（宽 / 30）%s'
                 % (b.x, b.y, b.w, b.h, b.y + BAR_BAND_TOP, b.y + BAR_BAND_BOTTOM - 1,
                    b.w / 30.0, '' if ok else '\n⚠ 框跑到画面外 / 太窄了'))
        self.btn_ok.configure(state='normal' if ok else 'disabled',
                              bg=CARD_HI if ok else CARD, fg=UI_FG if ok else FAINT)
        band = (b.y + BAR_BAND_TOP, b.y + BAR_BAND_BOTTOM - 1)
        img = boxpick.magnify(self.frame, b, panel=(556, 186), layout=self.guide, band=band)
        self._photo['zoom'] = ImageTk.PhotoImage(img)
        self.cv_zoom.delete('all')
        self.cv_zoom.create_image(0, 0, anchor='nw', image=self._photo['zoom'])

    # ---- 拖框 ----
    def _down(self, event):
        ix, iy = self._to_img(event.x, event.y)
        self._drag_from = (ix, iy)

    def _drag(self, event):
        if self._drag_from is None:
            return
        ix, iy = self._to_img(event.x, event.y)
        h, w = self.frame.shape[:2]
        x0, x1 = sorted((max(0, min(self._drag_from[0], w - 1)), max(0, min(ix, w - 1))))
        y0, y1 = sorted((max(0, min(self._drag_from[1], h - 1)), max(0, min(iy, h - 1))))
        self.box = Rect(x0, y0, max(2, x1 - x0), max(2, y1 - y0))
        self.redraw()

    def _up(self, event):
        if self._drag_from is None:
            return
        self._drag_from = None
        self._snap(auto=True)
        self.redraw()

    # ---- 微调 / 吸附 ----
    #
    # 为什么要有像素级微调：参照图按窗口大小缩过（2560 宽的录屏放进 1280 的窗口只有 0.5 倍），
    # 鼠标拖出来天生差 1~2 个画面像素，而「一费 30 帧」的相位对 1px 都敏感（6px = 1 帧）。
    # 所以拖完再让用户按着这几个键抠，抠的时候放大镜里看得见红 / 绿线在白块的哪一侧。

    def _edge(self, dx: int, dw: int):
        """动一条边：左端动 = x 和宽一起变；右端动 = 只变宽。"""
        b = self.box
        h, w = self.frame.shape[:2]
        x, ww = b.x + dx, b.w + dw
        if x < 0 or ww < 6 or x + ww > w or b.y + b.h > h:
            return
        self.box = Rect(x, b.y, ww, b.h)
        self.redraw()

    def _move(self, dx: int, dy: int):
        b = self.box
        h, w = self.frame.shape[:2]
        x = max(0, min(b.x + dx, w - b.w))
        y = max(0, min(b.y + dy, h - b.h))
        self.box = Rect(x, y, b.w, b.h)
        self.redraw()

    def _left_out(self):
        self._edge(-1, 1)

    def _left_in(self):
        self._edge(1, -1)

    def _right_in(self):
        self._edge(0, -1)

    def _right_out(self):
        self._edge(0, 1)

    def _left_1(self):
        self._move(-1, 0)

    def _right_1(self):
        self._move(1, 0)

    def _up_1(self):
        self._move(0, -1)

    def _down_1(self):
        self._move(0, 1)

    def _snap(self):
        """吸附左端：把左边缘钉到白块起点（画面上自带的尺子，见 boxpick.snap_box）。"""
        if self.box is None:
            return
        box, _shift, why = boxpick.snap_box(
            self.frame, self.box,
            (self.box.y + BAR_BAND_TOP, self.box.y + BAR_BAND_BOTTOM - 1))
        self.box = box
        self.lb_hint.configure(text=why)
        self.redraw()

    # ---- 收尾 ----
    def _apply(self):
        if self.box is None:
            return
        box = self.box
        self.close()
        self.on_apply(box)

    def close(self):
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class MonitorApp:
    # 面板两种状态各自的宽度：收起 = 小面板（大数字 + 一行读数 + 一行按钮），
    # 展开 = 打轴器 / 设置 / 诊断（那三张 ROI 预览图本身就要 704px）。
    COLLAPSED_W = 560
    EXPANDED_W = 724

    def __init__(self, monitor: LiveMonitor, *, calib_path: str = 'layout_calib.json',
                 settings_path: str = 'cost_rate.json', poll_ms: int = 8,
                 timeline_path: str = None, timeline_text: str = None,
                 templates=None):
        # poll_ms 是**面板重绘**的周期，不是采样周期（采样在 live.py 的线程里独立跑）。
        # 游戏 1 倍速下每秒走 30 个逻辑帧，面板必须显著快过 30 Hz，数字才会每拍只
        # 变化 0 或 1；慢了就会一次跳 2 帧，看着像「漏帧」（而帧操一次只推 1 帧，
        # 看着又是对的 —— 用户最早的反馈正是「一帧一帧过是对的、1 倍速下像在漏帧」）。
        #
        # 16 这个旧值是错的，因为 Windows 的定时器粒度是 **15.6 ms**：tkinter 的
        # after(n) 实际落在 ceil(n / 15.6) x 15.6 上。n=16 比 15.6 大一丁点，于是被抬到
        # 31.2 ms —— 实测面板 32.8 Hz，刚好压在 33.3 ms（1 倍速下一逻辑帧的显示时长）
        # 上，还是偶尔跳 2 帧。降到 8 之后实测周期 15.55 ms（64 Hz），翻了一倍。
        # 关键规律：**只要 n 跨过 15.6，周期就翻倍**，所以别往回调大。
        #
        # 实测（--demo-axis，真实 Tk 主循环，6 秒，取每次 _tick 的挂钟间隔中位数）：
        #     n=4  -> 15.53 ms (64.4 Hz)      n=8  -> 15.55 ms (64.3 Hz)
        #     n=16 -> 30.53 ms (32.8 Hz)      n=33 -> 46.65 ms (21.4 Hz)
        # 再往上就是被「整窗口重绘约 14.7 ms」卡住的上限，调 n 已经没用。
        # CPU 代价很小：n=16 约占一个核的 5%，n=8 约 13%。
        self.monitor = monitor
        # 实时那条链路整个留着：读窗口时把它停掉，点「切回实时」原样接回来
        # （LiveMonitor.start 会重新起线程，窗口由它自己每拍重新找，不用重建对象）。
        self.live_monitor = monitor
        self._src_kind = 'live'                 # 'live' / 'window'
        # ---- 读窗口（window.py 那条链路）----
        self._window_win = None                 # 选中的目标窗口（win32.WindowInfo）
        self._window_src = None                 # 校准好的 WindowSource（关掉要 close）
        self._window_busy = False               # 后台在校准窗口
        self._window_stage = ('align', -1.0, '')
        self._window_result = None              # (src 或 None, 异常或 None, 窗口, 提示)
        self._window_started = False            # 点过「开始计数」没有（没点就不起采样线程）
        self._pinned_hwnd = None                # 被我们钉到最上层的窗口（切走时还回去）
        self._pin_at = 0.0                      # 上次重新钉置顶的时刻（见 _keep_pin）
        self._full_ticks = 0                    # 满费格式还要多印几拍（见 _tick_body）
        self._pin_noted = ''                    # 置顶相关的提示只报一次（别每 0.7s 刷一遍）
        self._window_hint_base = ''             # 窗口那一行的固定部分（动态部分见 _refresh_window_hint）
        self._picker = None                     # 参照图取景器（BoxPicker，框选费用条用）
        # 倍速模板（read_speed 认 1X / 2X 数字笔画用）。run_monitor 会把它传进来；
        # 没传就自己从 templates/ 读一份（面板单独当库用时也能起来）。
        if templates is None:
            from .cli import DEFAULT_TEMPLATES, load_templates
            templates = load_templates(DEFAULT_TEMPLATES)
        self.templates = list(templates)
        self.calib_path = calib_path
        # 回费速率 + 局内负费两个设置共用一个文件（改档时写回这里）
        # 负费档从监视器上取（--negative 已经解析过），面板只负责显示和改
        self.settings_path = settings_path
        self.negative_mode = parse_negative(getattr(monitor, 'negative_mode', 'auto'))
        self.poll_ms = poll_ms
        self.deltas = {k: [0, 0, 0, 0] for k in ROI_ORDER}
        self.calib_scale = None
        self.target = 'bar'
        self._tick_times = []      # 面板重绘的实测速率（见 _tick_body）
        self.overlay = None
        self._photos = {}
        # 打轴器：prompter 只管「当前帧号 -> 该看哪条轴」，这里只管画
        self.prompter = Teleprompter()
        self._timeline_path = None      # 当前轴来自哪个 md 文件；None = 来自面板输入框
        self._prom_flash_until = 0.0
        # 采样线程的存活看门狗（见 _tick_body）：盯 stats.ticks 这个心跳。
        self._last_ticks = -1
        self._stall_since = 0.0
        # 刻度表的几何和配色。布局数学全在 akframe.scale 里（tools/check_scale.py
        # 能脱离界面把它验一遍），这里只负责照着自己画。
        self.scale_geom = ScaleGeom(width=420, height=280, ruler_w=50,
                                    px_per_frame=SCALE_ZOOM_DEFAULT)
        self.scale_theme = Theme()
        self._prom_state = None     # (当前帧, 全部轴, 已触发几条)，换缩放时重画要用
        # 刻度表本体是**一张 PIL 图**（akframe.scale_render），下面这两个是它贴到
        # Canvas 上的那一个图元和那一份 PhotoImage —— 必须留引用，否则会被回收成空白。
        self._scale_item = None
        self._scale_photo = None
        self._note_hold = 0.0           # 提示行的保留截止时刻（见 _set_note）
        self._load_calib()

        self.root = tk.Tk()
        self.root.title('明日方舟 全局帧数计数器')
        self.root.configure(bg=UI_BG)
        self.root.attributes('-topmost', True)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # 默认**收起**（小面板）。真正的尺寸等 _build_ui() 之后按内容需求算（见 _refit）。
        self._more = False
        # 用户想放的位置（不是实际位置）：展开 / 收起时窗口高矮差三四倍，直接按实际
        # 位置改尺寸会把它顶出屏幕，所以位置统一由 _place() 从这一份算（见 _place）。
        self._user_pos = (24, max(10, (sh - 260) // 2))
        self.root.geometry('%dx%d+%d+%d' % (self.COLLAPSED_W, 150,
                                            self._user_pos[0], self._user_pos[1]))
        self.root.protocol('WM_DELETE_WINDOW', self.quit)

        self._build_ui()
        self._bind_keys()
        # 建完界面才知道内容到底要多宽多高（换台机器 / 换字体缩放就不一样），这里按实际
        # 需求夹一次：窄了右边按钮会被裁（换窗口 / 退出），矮了最下面几行会被裁
        # （回费速率 / 提示 / 帮助）。写死 724 的那版在 150% 缩放的屏幕上就是裁着的 ——
        # tk 的点数会跟着系统 DPI 放大，同样的字号在物理像素上要宽 1.5 倍。
        self._win_w, self._win_h = self.COLLAPSED_W, 150
        self._refit()
        self._fit_at = 0.0
        # timeline_text 是直接把轴喂进面板输入框（演示模式用：轴不来自文件，
        # 但仍然能在面板里改、能按 Ctrl+Enter 重解析）。
        if timeline_text:
            self._set_axis_text(timeline_text)
            self.apply_axis_text()
        elif timeline_path:
            self.load_timeline(timeline_path)
        # 把键盘焦点拿回主窗口：否则 Tk 可能把焦点给输入框，快捷键（C/S/T…）就失效了
        self.root.after(300, self.root.focus_set)
        self.root.after(80, self._tick)

    # ---------------- 界面 ----------------

    def _label(self, parent, text='', size=10, color=UI_FG, bold=False, bg=UI_BG, **kw):
        font = (FONT_UI, size, 'bold' if bold else 'normal')
        return tk.Label(parent, text=text, bg=bg, fg=color, font=font, **kw)

    def _btn(self, parent, text, cmd, hover=True, **kw):
        """面板上的按钮统一走这里 —— 免得每个按钮各自写一遍颜色，改风格改不干净。

        hover=False 给「有选中态」的按钮用（比如刻度缩放那组）：悬停改底色会盖掉
        选中态（白底深字），Leave 时还会把选中态一起抹掉。
        """
        kw.setdefault('padx', 8)
        kw.setdefault('pady', 3)
        b = tk.Button(parent, text=text, command=cmd, font=(FONT_UI, 9),
                      bg=CARD_HI, fg=UI_FG, activebackground=RULE, activeforeground=UI_BG,
                      relief='flat', highlightthickness=0, cursor='hand2', **kw)
        if hover:
            b.bind('<Enter>', lambda _e: b.configure(bg='#34343a'))
            b.bind('<Leave>', lambda _e: b.configure(bg=CARD_HI))
        return b

    def _sep(self, parent):
        """两个小节之间那条 1px 淡分隔线。"""
        tk.Frame(parent, height=1, bg=RULE_DIM).pack(fill='x', padx=10, pady=(7, 0))

    # ---------------- 展开 / 收起 ----------------

    def toggle_more(self):
        """展开 / 收起。收起态就是一个小面板：大数字 + 一行读数 + 一行按钮。"""
        self._more = not self._more
        if self._more:
            self.fr_more.pack(fill='both', expand=True)
        else:
            self.fr_more.pack_forget()
        self.btn_more.configure(text='▾ 收起' if self._more else '▸ 打轴器 / 设置')
        self._refit()

    def _minimize(self):
        """最小化（任务栏）。再点一下任务栏图标就回来。"""
        try:
            self.root.iconify()
        except tk.TclError:
            pass

    def _drag_start(self, event):
        """按住标题栏拖动窗口（面板是无边框 HUD，系统标题栏被去掉了）。"""
        self._drag_at = (event.x_root - self.root.winfo_x(),
                         event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        at = getattr(self, '_drag_at', None)
        if at is None:
            return
        self._user_pos = (event.x_root - at[0], event.y_root - at[1])
        self._place(self._win_w, self._win_h)

    def _refit(self):
        """按当前展开状态把窗口尺寸重新夹一次（收起 = 小的，展开 = 大的）。

        为什么不写死尺寸：收起态只有 4 行，撑到 1000 高就是一大片空白；展开态的行数
        还会随提示文字换行变化（换台机器、换系统字体缩放又不一样）。所以宽度按状态给
        基准，高度按**内容实际需要**算。
        """
        base = self.EXPANDED_W if self._more else self.COLLAPSED_W
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 顺序很重要：**先**把会换行的那些行按基准宽度限住，再量内容要多宽。
        # 反过来的话，上一拍留下的宽 wraplength 会让一行长提示（「采样正常，这一拍
        # 没读到画面（未开始）」这种）把收起态从 560px 顶到 800px（实测踩过）。
        wrap = max(360, base - 30)
        self._set_wraps(wrap)
        try:
            self.root.update_idletasks()
            need_w = self.root.winfo_reqwidth()
        except Exception:                    # noqa: BLE001  还在建 / 已经销毁
            return
        w = min(max(base, need_w), max(420, sw - 40))
        self._set_wraps(max(360, w - 30))
        try:
            self.root.update_idletasks()
            need_h = self.root.winfo_reqheight()
        except Exception:                    # noqa: BLE001
            return
        h = min(max(need_h, 120), max(240, sh - 80))
        self._win_w, self._win_h = w, h
        self._place(w, h)

    def _set_wraps(self, wrap: int) -> None:
        """按可用宽度给会换行的那些行设换行宽度。

        右列那两行（逻辑帧 / 游戏内时间）平时只有几个字，但它们**不在**下面这一组里 ——
        单独给一个 420 的上限：大数字卡片本身就占掉 240px，再宽收起态就不「小」了。
        """
        for lb in (self.lb_cycle, self.lb_perf, self.lb_warn, self.lb_note,
                   self.lb_window_hint, self.lb_help):
            lb.configure(wraplength=wrap)
        for lb in (self.lb_frames_hint, self.lb_time):
            lb.configure(wraplength=max(240, min(wrap, 420)))
        # 三件 UI 的读数摘要（各占一行）：自然宽度最大的是倍速那行 406px，限到 520
        # 留足余地 —— 不限的话最长的组合能把**收起态**顶到 800px 宽（实测）。
        for lb in (self.lb_bar, self.lb_pause, self.lb_speed):
            lb.configure(wraplength=min(wrap, 520))

    def _place(self, w, h):
        """把窗口摆到屏幕上：尺寸一变（展开 / 收起）就按「想放的位置」重算一次。

        为什么必须有这一步：面板默认落在屏幕竖直中间（+24+670，本机 2560x1600），
        而展开区一出来内容就要 1300+ px —— 直接照原位置改尺寸，底边会掉到屏幕外，
        用户看到的是「展开了但下面半截看不见」。这里把左上角按屏幕夹一次：
        越界就往上 / 往左收，收到头就贴边（留 8px 边）。收起时又自动回到原来的位置。
        """
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 窗口左上角到**客户区**左上角还差一条边框 / 标题栏（本机实测 45px）：
        # 不把这一条算进去，贴底那次还是会露出小半行。
        off = max(0, self.root.winfo_rooty() - self.root.winfo_y())
        ux, uy = self._user_pos
        # 贴底还要避开任务栏：Tk 报的屏幕高度把任务栏也算进去了（实测展开态底边落在
        # 1592、任务栏从 1552 起，最下面那行帮助就被压住了）。多显示器时先看窗口在不在
        # 主屏上，不在就不扣（SPI_GETWORKAREA 只管主屏）。
        ax, ay, aw, ah = work_area()
        bottom = ay + ah if (aw > 0 and ah > 0 and ax <= ux + w // 2 <= ax + aw) else sh
        x = min(max(8, ux), max(8, sw - w - 8))
        y = min(max(8, uy), max(8, bottom - h - off - 8))
        self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))

    # ---------------- 输入源 / 主按钮 ----------------

    def _src_menu(self):
        """「输入：…」下拉：实时 / 读取窗口。"""
        m = tk.Menu(self.root, tearoff=0, bg=CARD, fg=UI_FG, activebackground=RULE,
                    activeforeground=UI_BG, relief='flat', font=(FONT_UI, 9))
        m.add_command(label='实时（游戏窗口）', command=self.back_to_live)
        m.add_command(label='读取一个窗口…', command=self.read_window)
        try:
            m.tk_popup(self.btn_src.winfo_rootx(),
                       self.btn_src.winfo_rooty() + self.btn_src.winfo_height())
        finally:
            m.grab_release()

    def _primary_action(self):
        """常驻行那个主按钮：按当前输入源做「最该做的那件事」。"""
        if self._src_kind == 'window':
            self.toggle_window_count()
        else:
            self.recalibrate()

    def _sync_ctrl_row(self):
        """主按钮 / 输入源按钮上的字，跟着「输入源 + 当前状态」走。

        只在这两个控件上写字，所以每拍调用一次也不心疼（内容没变就不 configure）。
        """
        kind = self._src_kind
        want_src = {'live': '输入：实时 ▾', 'window': '输入：窗口 ▾'}[kind]
        if self.btn_src.cget('text') != want_src:
            self.btn_src.configure(text=want_src)
        if kind == 'window':
            want = '停止计数' if self.monitor.alive else '开始计数'
        else:
            want = '重新校准'
        if self.btn_start.cget('text') != want:
            self.btn_start.configure(text=want)

    def _fit_window(self, now):
        """内容长高了就把窗口放高一点，别把最下面的帮助行切掉。

        为什么需要：窗口尺寸是 __init__ 里照**占位文本**算出来定死的；运行时那些会换行的
        行（性能行 / 告警行 / 费用周期）字一长就会多占一行，而窗口高度不会跟着变 ——
        最下面的帮助行于是被切在窗口外（实测：性能行多换了一行，帮助行只剩半行）。
        这里每 0.5 秒核一次「内容到底需要多高」，不够就长高，上限是屏幕高度。
        只长不缩：缩会在文本忽长忽短时来回抖。
        """
        if now - self._fit_at < 0.5:
            return
        self._fit_at = now
        try:
            need = self.root.winfo_reqheight()
        except Exception:                    # noqa: BLE001  窗口正在销毁
            return
        if need <= self._win_h + 2:
            return
        self._win_h = min(need, max(640, self.root.winfo_screenheight() - 20))
        self._place(self._win_w, self._win_h)

    def _draw_badge(self):
        """左上角那个白色菱形徽标（游戏里是「◆C」= 费用，这里写「◆帧」）。"""
        c, s, m = self.cv_badge, 44, 3
        c.delete('all')
        c.create_polygon(s / 2, m, s - m, s / 2, s / 2, s - m, m, s / 2, fill=RULE)
        # 里面放拉丁字母而不是汉字：tkinter 的 Canvas 文字**不做抗锯齿**，12pt 的
        # 汉字在 44px 的菱形里会糊成一团（实测放大看过）。游戏里那个「C」也是字母。
        c.create_text(s / 2, s / 2 + 1, text='F', fill=CARD, font=(FONT_NUM, 19))

    def _sync_rule(self):
        """大数字那一格的几何：白线宽度 + 这一格**预留**多宽。

        两条都只跟**整数位数**走，而且只拿「8」去量（数字里最宽的字形），绝不跟
        「这一拍印的是 123 还是 888.88」走。为什么必须这样：满费期间大数字带两位小数
        （`elapsed_f`），而满费那些拍走的通道会在「挂钟」和「倍速未知→冻结」之间来回
        切 —— 只要跟着文本宽度走，8ms 一拍就是「小数一出现整个界面抽搐一下」。

        修之前（数字格的宽度跟着这一拍的文本走）实测：

            文本        数字格  白线   卡片宽  右列 x   面板 reqw
            123          79     71     208     212      400
            123.45      150    142     228     232      420
            -> 小数一出现：白线 71 -> 142、右边三行整体右移 20px、面板跟着长 20px

        预留按「整数位个 8 + '.88'」算（能把这一位数下任何文本都装下），白线按
        「整数位个 8」算。修之后实测：

            文本        数字格  白线   卡片宽  右列 x   面板 reqw
            0           115     56     180     184      400
            123         175     82     240     244      432
            123.45      175     82     240     244      432
            888.88      175     82     240     244      432
            1234.56     205    112     270     274      462
            12345.67    235    142     300     304      492

        —— 同一位数内**没有任何东西会动**，连 123 -> 888 这种换字形都不会（1 号数字
        只要 18px、8 号要 30px，按实际文本量的话白线会在 30px 里来回跳）；位数真涨
        一位（99 -> 100）时白线和格子一起变一次，那是应该变的：游戏里那条压条本来
        就是跟着费用位数变长的。面板最宽 492 < 窗口 560，不会顶破。

        怎么做到像素级精确：Label 的 `width=` 单位是**字符**，Tk 拿字体平均宽去乘
        （本机 40pt Bahnschrift 平均宽 30px），于是 162px 的需求会被抬到 186px（多出
        24px 的死白），位数少时更亏。所以这里把 Label 的 width 钉成 1 字符、用
        sticky='we' 撑满格子，真正决定宽度的是 `grid_columnconfigure(1, minsize=...)`。
        注意 minsize 管的是**整格** = 控件 + 右侧 NUM_RIGHT_PAD（本机实测），所以预留
        里要把那 13px 加回去，否则最后一位数字会被切掉。
        """
        try:
            f = tkfont.Font(font=self.lb_frames.cget('font'))
            n = len(str(self.lb_frames.cget('text')).split('.')[0]) or 1
            widest = '8' * n                    # 最宽的数字字形：按它预留就不会切字
            self.fr_rule.configure(width=max(56, f.measure(widest) - 8))
            self.card.grid_columnconfigure(
                1, minsize=f.measure(widest + '.88') + NUM_RIGHT_PAD)
        except Exception:                    # noqa: BLE001  还没建出来/已销毁：忽略
            pass

    def _build_ui(self):
        pad = dict(padx=10, pady=4)

        # ---- 标题栏：整条能拖着走；右边三个键 = 展开 / 最小化 / 关闭 ----
        # 面板分成两块：**常驻区**（大数字 + 一行读数 + 一行按钮）和**展开区**（打轴器 /
        # 设置 / 诊断）。收起态就是一个小面板 —— 原先一上来铺 9 行按钮加三张预览图，
        # 用户的原话是「太臃肿了，完全用不了」。
        self.head = tk.Frame(self.root, bg=UI_BG)
        self.head.pack(fill='x')
        self.lb_title = self._label(self.head, '◆ 明日方舟 全局帧数计数器', 10, UI_DIM)
        self.lb_title.pack(side='left', padx=(12, 0), pady=(7, 5))
        self.btn_close = self._btn(self.head, '×', self.quit, padx=10, pady=1)
        self.btn_close.pack(side='right', padx=(2, 9), pady=(5, 5))
        self.btn_min = self._btn(self.head, '−', self._minimize, padx=10, pady=1)
        self.btn_min.pack(side='right', padx=2, pady=(5, 5))
        self.btn_more = self._btn(self.head, '▸ 打轴器 / 设置', self.toggle_more, padx=8, pady=1)
        self.btn_more.pack(side='right', padx=2, pady=(5, 5))
        for w in (self.head, self.lb_title):
            w.bind('<Button-1>', self._drag_start)
            w.bind('<B1-Motion>', self._drag_move)
        self._sep(self.root)

        # ---- 常驻①：大数字那一块，照着游戏里的费用显示做 ----
        # 实测那张 HUD 截图的构成：深炭灰面板上「白色菱形 + 大号白数字」，数字下面压一条
        # 粗白线。量到的比例是：面板 205x94、数字高 50（占面板高 53%）、
        # 白线 6px 厚 / 80px 宽（只占面板宽 39%）、贴着左下角。这里照抄这三样。
        top = tk.Frame(self.root, bg=UI_BG)
        top.pack(fill='x', **pad)
        card = tk.Frame(top, bg=CARD)
        card.pack(side='left')
        # 留着给 _sync_rule 用：大数字那一列按「整数位 + '.88'」预留固定宽度，小数出现 /
        # 消失时谁都不挪窝（实测、算式见 _sync_rule 的注释）。
        self.card = card
        self.cv_badge = tk.Canvas(card, width=44, height=44, bg=CARD, highlightthickness=0)
        self.cv_badge.grid(row=0, column=0, rowspan=2, padx=(11, 10), pady=(12, 0), sticky='n')
        self._draw_badge()
        # width=1 是故意的：这个 Label **不参与**「本行要多宽」的竞争，宽度由
        # _sync_rule 里的列 minsize 定死（像素级精确），再用 sticky='we' 撑满那一格。
        # 直接让 Label 按文本自适应的话，满费时印出两位小数就会把整张卡撑宽（实测
        # 208 -> 228px，右边三行跟着右移），收起小数又缩回去 —— 8ms 一拍地在抖。
        self.lb_frames = tk.Label(card, text='0', bg=CARD, fg=UI_FG,
                                  font=(FONT_NUM, 40), anchor='w', width=1)
        self.lb_frames.grid(row=0, column=1, padx=(0, NUM_RIGHT_PAD), pady=(6, 0),
                            sticky='we')
        self.fr_rule = tk.Frame(card, height=5, bg=RULE)
        self.fr_rule.grid(row=1, column=1, padx=(0, NUM_RIGHT_PAD), pady=(2, 9),
                          sticky='w')
        self._sync_rule()

        right = tk.Frame(top, bg=UI_BG)
        right.pack(side='left', padx=4, pady=6)
        # 两行都 anchor='w'（左对齐）：tk 默认 anchor='center'，文字比分到的宽度长时
        # 会**两头一起切**（实测切出来第一个字就没了）。左对齐至少从头开始读。
        # 原来这里还有第三行「状态 …」：文案太长（满费那条 330px、右列只有 292px）总是被
        # 窗口右边切掉，用户 2026-10-09 说「状态栏可以删掉」，于是整行去掉 —— 它的内容
        # 分两类，都没有丢：跑动细节在下面「费用条」那行的「通道=」里，真出事的告警在
        # 「告警：」那行（采样已停会转红写在那里，见 _tick_body 末尾）。
        self.lb_frames_hint = self._label(right, '逻辑帧（1 倍速 30 帧/秒）', 9, UI_DIM,
                                          anchor='w')
        self.lb_frames_hint.pack(anchor='w')
        self.lb_time = self._label(right, '游戏内时间 00:00.00', 13, anchor='w')
        self.lb_time.pack(anchor='w')

        # ---- 常驻②：三件 UI 的读数摘要 ----
        info = tk.Frame(self.root, bg=UI_BG)
        info.pack(fill='x', **pad)
        self.lb_bar = self._label(info, '费用条 —', 10)
        self.lb_bar.grid(row=0, column=0, columnspan=4, sticky='w')
        # 三行各占一行（不并排）：并排的话两段（355 + 406px）会把**收起态**顶到
        # 797px 宽，限宽又会折成两截读不懂 —— 一行一段最省事，代价只是多 33px 高。
        self.lb_pause = self._label(info, '按钮 —', 10)
        self.lb_pause.grid(row=1, column=0, columnspan=4, sticky='w')
        self.lb_speed = self._label(info, '倍速 —', 10)
        self.lb_speed.grid(row=2, column=0, columnspan=4, sticky='w')
        # 告警行常驻（平时空的）：采样停了 / 校准没把握 这类事必须一眼看得见
        self.lb_warn = self._label(info, '', 9, WARN, anchor='w', justify='left')
        self.lb_warn.grid(row=3, column=0, columnspan=4, sticky='w')

        # ---- 常驻③：控制键 ----
        # 主按钮按**输入源**变：实时 = 重新校准，窗口 = 开始 / 停止计数。
        # 只留这一个「最该做的那件事」，别的（框选、重解、保存…）都在展开区。
        ctrl = tk.Frame(self.root, bg=UI_BG)
        ctrl.pack(fill='x', **pad)
        self.btn_src = self._btn(ctrl, '输入：实时 ▾', self._src_menu, padx=7)
        self.btn_src.pack(side='left', padx=(0, 6))
        self.btn_start = self._btn(ctrl, '重新校准', self._primary_action)
        self.btn_start.pack(side='left', padx=2)
        self.btn_manual = self._btn(ctrl, '暂停计数 (P)', self.toggle_manual_pause)
        self.btn_manual.pack(side='left', padx=2)
        self._btn(ctrl, '清零', self.clear_count).pack(side='left', padx=2)

        # 读窗口那块：校准好之后 **等用户点「开始计数」** 才起采样线程 ——
        # 用户要的就是这个顺序：打开录屏置顶 -> 读这个窗口校准 -> 点开始。
        # 平时不 pack（用到才出现），所以直接挂在 root 上，不再套一层 holder。
        self.fr_window = tk.Frame(self.root, bg=UI_BG)
        wrow = tk.Frame(self.fr_window, bg=UI_BG)
        wrow.pack(fill='x')
        self._btn(wrow, '框选费用条', self.box_bar).pack(side='left', padx=(0, 3))
        self.btn_pin = self._btn(wrow, '置顶中', self.toggle_pin, hover=False)
        self.btn_pin.pack(side='left', padx=3)
        self.lb_window_hint = self._label(self.fr_window, '', 9, FAINT, anchor='w',
                                          justify='left')
        self.lb_window_hint.pack(fill='x', pady=(3, 0))

        # ---- 常驻④：提示行（面板用来说话的那一行）----
        self.lb_note = self._label(self.root, '', 10, UI_DIM, anchor='w', justify='left',
                                   height=2)
        self.lb_note.pack(fill='x', padx=10, pady=(4, 6))

        # ================= 展开区（默认收起，按 ▸ 才出来）=================
        self.fr_more = tk.Frame(self.root, bg=UI_BG)
        self._sep(self.fr_more)

        # ---- 打轴器（提词器）：导入 md 轴文件后，到帧就显示那一条 ----
        prom = tk.Frame(self.fr_more, bg=UI_BG)
        prom.pack(fill='x', **pad)
        head = tk.Frame(prom, bg=UI_BG)
        head.pack(fill='x')
        self.lb_prom_title = self._label(head, '打轴器 — 未导入（按 T 或点右边按钮选 md 轴文件）',
                                         10, UI_DIM)
        self.lb_prom_title.pack(side='left')
        for text, cmd in (('重载', self.reload_timeline), ('导入轴 (T)', self.import_timeline)):
            self._btn(head, text, cmd).pack(side='right', padx=3)
        # 刻度表：按帧等比例摆在一条竖直刻度上，指针（红线）固定不动，帧数上涨时
        # 整条刻度往下滚 —— 所以轴点是从上方压下来、穿过指针、再变暗沉下去。
        self.cv_scale = tk.Canvas(prom, width=self.scale_geom.width,
                                  height=self.scale_geom.height,
                                  bg=self.scale_theme.bg, highlightthickness=0)
        self.cv_scale.pack(anchor='w', pady=(6, 2))
        self._draw_scale_hint()

        zrow = tk.Frame(prom, bg=UI_BG)
        zrow.pack(fill='x', pady=(2, 0))
        self._label(zrow, '刻度缩放', 9, UI_DIM).pack(side='left')
        self.zoom_btns = {}
        for ppf, text in SCALE_ZOOMS:
            b = self._btn(zrow, text, lambda v=ppf: self.set_scale_zoom(v),
                          hover=False, padx=7, pady=2)
            b.pack(side='left', padx=3)
            self.zoom_btns[ppf] = b
        self._mark_zoom()
        # 下一条 + 倒计时秒数。秒数要按当前倍速折算（2 倍速下 30 帧只有 0.5 秒，
        # 子弹时间是恒定的 1/5），这一条 Canvas 里放不下，仍旧用 label 印。
        self.lb_cue_next = self._label(prom, '', 11, ACCENT, True, anchor='w')
        self.lb_cue_next.pack(fill='x')

        # ---- 轴文本输入：不想开文件就直接在这里写/粘，格式和 md 一样 ----
        editor = tk.Frame(self.fr_more, bg=UI_BG)
        editor.pack(fill='both', expand=True, **pad)
        ehead = tk.Frame(editor, bg=UI_BG)
        ehead.pack(fill='x')
        self._label(ehead, '轴文本（帧号开头，格式同 md；Ctrl+Enter 应用）',
                    10, UI_DIM).pack(side='left')
        for text, cmd in (('清空输入框', self.clear_axis_text),
                          ('保存为 md', self.save_axis_text),
                          ('应用到提词器', self.apply_axis_text)):
            self._btn(ehead, text, cmd).pack(side='right', padx=3)
        # width 只是给窗口定「初始需要多宽」用的（Text 是 expand=True，实际宽度跟着窗口
        # 走）。不写的话 Tk 默认按 80 字符算，光是它就能把窗口需求宽度顶到 1000+。
        self.txt_axis = tk.Text(editor, height=7, width=40, bg=SUNKEN, fg=UI_FG, relief='flat',
                                insertbackground=UI_FG, selectbackground=CARD_HI,
                                font=(FONT_MONO, 10), wrap='word', undo=True)
        self.txt_axis.pack(fill='both', expand=True, pady=(4, 0))
        self.txt_axis.insert('1.0', AXIS_HINT)
        # 在这两个键上返回 'break'，事件就不会继续冒泡到主窗口的快捷键
        self.txt_axis.bind('<Escape>', self._axis_escape)
        self.txt_axis.bind('<Control-Return>', self._axis_apply_key)

        # ---- 回费速率（减费 tag）：手动档，改完立刻生效并写回设置文件 ----
        # 为什么不自动认：认错档会让绝对帧号整段漂移（差一档就是几百帧），而打轴器
        # 要的正是绝对帧号。档位取值 = 「一费几个逻辑帧」，和倍速、画面帧率都无关。
        rate_row = tk.Frame(self.fr_more, bg=UI_BG)
        rate_row.pack(fill='x', **pad)
        self._label(rate_row, '回费速率', 10).pack(side='left')
        cur = (self.monitor.counter.frames_per_cost if self.monitor.counter is not None
               else DEFAULT_FRAMES_PER_COST)
        self.rate_var = tk.StringVar(value=_rate_label(cur))
        self._rate_labels = {}
        for v, desc in COST_RATE_PRESETS:
            self._rate_labels['%.0f  %s' % (v, desc)] = v
        items = list(self._rate_labels) + [RATE_CUSTOM_LABEL]
        self.rate_menu = tk.OptionMenu(rate_row, self.rate_var, *items,
                                       command=self._on_rate_pick)
        self.rate_menu.config(bg=CARD_HI, fg=UI_FG, activebackground=CARD_HI,
                              activeforeground=UI_FG, relief='flat', highlightthickness=0,
                              font=(FONT_UI, 9), width=22, anchor='w',
                              padx=4, pady=2)
        # 下拉里的高亮 = 白底深字（同上：游戏 HUD 的选中一律反色）
        self.rate_menu['menu'].config(bg=CARD, fg=UI_FG, activebackground=RULE,
                                      activeforeground=UI_BG, relief='flat',
                                      font=(FONT_UI, 9))
        self.rate_menu.pack(side='left', padx=(6, 6))
        self._label(rate_row, '自定义', 9, UI_DIM).pack(side='left')
        self.en_rate = tk.Entry(rate_row, width=5, bg=SUNKEN, fg=UI_FG, relief='flat',
                                insertbackground=UI_FG, justify='center',
                                font=(FONT_MONO, 10))
        self.en_rate.pack(side='left', padx=(4, 4))
        self.en_rate.bind('<Return>', lambda _e: self.apply_rate_entry())
        self._btn(rate_row, '应用', self.apply_rate_entry).pack(side='left', padx=3)

        # ---- 局内负费（费用条上出现减号）：一费 30 -> 60 逻辑帧 ----
        # 默认「自动」= 按画面判（费用数字左边那条纯白横杠，见 features.read_sign）。
        # 认不出来时能强制开 / 关，把计数救回来 —— 那个扫描框是照抄前人的
        # （geometry.sign_rect），还没拿真录像钉过，所以下面「费用周期」那行会摆一个
        # 「减号 run/thr」：run 是框里实测最长的纯白横杠，离 thr 差多少一眼就看得出
        # （正常画面 run 应当明显小于 thr，见 docs 第 31 节）。
        neg_row = tk.Frame(self.fr_more, bg=UI_BG)
        neg_row.pack(fill='x', **pad)
        self._label(neg_row, '局内负费', 10).pack(side='left')
        self.neg_var = tk.StringVar(value=NEGATIVE_LABELS[self.negative_mode])
        self.neg_menu = tk.OptionMenu(
            neg_row, self.neg_var, *[NEGATIVE_LABELS[m] for m in NEGATIVE_MODES],
            command=self._on_negative_pick)
        self.neg_menu.config(bg=CARD_HI, fg=UI_FG, activebackground=CARD_HI,
                             activeforeground=UI_FG, relief='flat', highlightthickness=0,
                             font=(FONT_UI, 9), width=26, anchor='w', padx=4, pady=2)
        self.neg_menu['menu'].config(bg=CARD, fg=UI_FG, activebackground=RULE,
                                     activeforeground=UI_BG, relief='flat',
                                     font=(FONT_UI, 9))
        self.neg_menu.pack(side='left', padx=(6, 6))
        self._label(neg_row, '负费时每逻辑帧只攒一半', 9, UI_DIM).pack(side='left')

        # ---- 诊断：费用周期 / 性能 / 当前输入，以及三个 ROI 的实时预览 ----
        # 这三张预览是**调 ROI 用的**（红框 = 特征真正在取样的那条带），平时不用看，
        # 所以跟着展开区一起收起来。
        diag = tk.Frame(self.fr_more, bg=UI_BG)
        diag.pack(fill='x', **pad)
        self.lb_cycle = self._label(diag, '费用周期 —', 10, anchor='w', justify='left')
        self.lb_cycle.grid(row=0, column=0, sticky='w')
        self.lb_perf = self._label(diag, '性能 —', 9, UI_DIM, anchor='w', justify='left')
        self.lb_perf.grid(row=1, column=0, sticky='w', pady=(4, 0))
        self.lb_source = self._label(diag, '', 9, UI_DIM, anchor='w', justify='left')
        self.lb_source.grid(row=2, column=0, sticky='w', pady=(2, 0))

        prev = tk.Frame(self.fr_more, bg=UI_BG)
        prev.pack(fill='x', **pad)
        self.cv_bar = tk.Canvas(prev, width=704, height=46, bg=SUNKEN, highlightthickness=0)
        self.cv_bar.pack(fill='x')

        # ---- 展开区最下面那行：不常用但必要时得有 ----
        btns = tk.Frame(self.fr_more, bg=UI_BG)
        btns.pack(fill='x', **pad)
        for text, cmd in (('保存校准', self.save_calib),
                          ('恢复默认', self.reset_calib),
                          ('ROI 框 (K)', self.toggle_overlay),
                          ('换窗口', self.pick_window)):
            self._btn(btns, text, cmd).pack(side='left', padx=3)
        self.lb_help = self._label(
            self.fr_more,
            'Tab 切 ROI ｜ 方向键移动 / Shift 缩放 ｜ K ROI 框 ｜ S 保存 ｜ R 默认 ｜ C 清零 ｜ '
            'L 记录 CSV ｜ P 暂停计数 ｜ T 导入轴 ｜ F5 重新校准 ｜ 输入框里可直接写轴（Ctrl+Enter 应用）',
            9, FAINT, wraplength=694, justify='left')
        self.lb_help.pack(fill='x', padx=10, pady=(4, 10))

        if self._more:                       # 默认收起：展开区先不 pack
            self.fr_more.pack(fill='both', expand=True)

    def _guard(self, handler):
        """给快捷键加一层「输入框保护」。

        Tk 的按键会沿 bindtags 依次送给 控件 -> 控件类 -> 顶层窗口；Text 插入字符时
        不会返回 'break'，所以不拦一下的话，在轴输入框里打字会顺手触发主窗口的快捷键
        （打 c 清零、打 t 弹文件框、打 s 存校准……）。
        """
        def wrapped(event):
            if isinstance(self.root.focus_get(), (tk.Text, tk.Entry)):
                return None
            return handler(event)
        return wrapped

    def _bind_keys(self):
        r = self.root
        for seq, fn in (('<Tab>', self._cycle_target),
                        ('<Escape>', self.quit),
                        ('k', self.toggle_overlay), ('K', self.toggle_overlay),
                        ('s', self.save_calib), ('S', self.save_calib),
                        ('r', self.reset_calib), ('R', self.reset_calib),
                        ('c', self.clear_count), ('C', self.clear_count),
                        ('l', self.toggle_csv), ('L', self.toggle_csv),
                        ('p', self.toggle_manual_pause), ('P', self.toggle_manual_pause),
                        ('<F5>', self.recalibrate),
                        ('t', self.import_timeline), ('T', self.import_timeline)):
            r.bind(seq, self._guard(lambda _e, f=fn: f()))
        for seq in ('<Left>', '<Right>', '<Up>', '<Down>',
                    '<Shift-Left>', '<Shift-Right>', '<Shift-Up>', '<Shift-Down>'):
            r.bind(seq, self._guard(self._nudge))

    # ---------------- 校准 ----------------

    def _load_calib(self):
        deltas, scale = load_calibration(self.calib_path)
        if deltas:
            # 只认现在还在的 ROI：老校准文件里可能留着已经删掉的通道
            self.deltas = {k: list(v) for k, v in deltas.items() if k in ROI_ORDER}
            for k in ROI_ORDER:
                self.deltas.setdefault(k, [0, 0, 0, 0])
            self.calib_scale = scale

    def _nudge(self, event):
        shift = bool(event.state & 0x1)
        name = self.target
        d = self.deltas[name]
        step = 1
        if event.keysym == 'Left':
            d[2 if shift else 0] -= step
        elif event.keysym == 'Right':
            d[2 if shift else 0] += step
        elif event.keysym == 'Up':
            d[3 if shift else 0] -= step
        elif event.keysym == 'Down':
            d[3 if shift else 0] += step
        self._apply_deltas()

    def _roi_names(self):
        '''当前 layout 里真有的 ROI 名字。'''
        lay = self.monitor.layout
        if lay is None:
            return ROI_ORDER
        return tuple(n for n in ROI_ORDER if getattr(lay, n, None) is not None)

    def _cycle_target(self):
        order = self._roi_names()
        if self.target not in order:
            self.target = order[0]
        else:
            self.target = order[(order.index(self.target) + 1) % len(order)]
        self._set_note('当前校准目标：%s' % self.target)

    def _apply_deltas(self):
        base = self._base_layout()
        if base is None:
            return
        try:
            self.monitor.set_layout(apply_deltas(base, self.deltas, self.calib_scale))
        except ValueError as exc:
            self._set_note('ROI 越界，已忽略这次调整：%s' % exc)

    def _base_layout(self):
        """方向键的 delta 加在谁身上。

        实时 = 按当前客户区算的预测（layout_for + 注册表里的 UI 缩放）；
        读窗口 = 监视器手里那份**刚校准出来**的布局（不是可能在面板上挪过的当前布局，
        否则挪一下再挪回去会越叠越远）。窗口那条链路不能用 layout_for —— 它的 ROI 在
        window_calib.json 里，按分辨率表算出来的位置是错的。
        """
        if self._src_kind == 'window':
            fn = getattr(self.monitor, 'base_layout', None)
            try:
                return fn() if callable(fn) else None
            except (ValueError, AttributeError):
                return None
        win = self.monitor.window
        if win is None:
            return None
        try:
            return layout_for(win.client[2], win.client[3], arknights_ui_scaler())
        except ValueError:
            return None

    def save_calib(self):
        if self._src_kind == 'window':
            src = self.monitor.src
            what = '窗口'
            try:
                path = src.save_current(layout=self.monitor.layout, source='面板校准',
                                        note='面板上挪过 / 确认过的 ROI')
            except OSError as exc:
                self._set_note('%s校准存盘失败：%s' % (what, exc))
                return
            self._set_note('已把当前三个 ROI 存进 %s（%dx%d 的%s下次直接套用）'
                           % (os.path.basename(path), src.size[0], src.size[1], what))
            return
        base = self._base_layout()
        if base is None:
            self._set_note('还没找到窗口，无法保存')
            return
        data = {'client': [base.width, base.height], 'scale': base.scale,
                'deltas': self.deltas,
                'abs': self.monitor.layout.to_dict() if self.monitor.layout else base.to_dict()}
        with open(self.calib_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        self._set_note('已保存校准到 %s' % os.path.abspath(self.calib_path))

    def reset_calib(self):
        self.deltas = {'bar': [0, 0, 0, 0], 'pause': [0, 0, 0, 0], 'speed': [0, 0, 0, 0]}
        self.calib_scale = None
        self._apply_deltas()
        self._set_note('已恢复默认布局')

    # ---------------- ROI 叠加框 ----------------

    def toggle_overlay(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
            self._set_note('已关闭 ROI 框')
            return
        if self.monitor.layout is None or self.monitor.window is None:
            self._set_note('还没找到窗口，无法显示 ROI 框')
            return
        x, y, w, h = virtual_screen_bounds()
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        top.geometry('%dx%d+%d+%d' % (w, h, x, y))
        top.configure(bg='#010101')
        try:
            top.attributes('-transparentcolor', '#010101')
        except tk.TclError:
            pass
        cv = tk.Canvas(top, bg='#010101', highlightthickness=0)
        cv.pack(fill='both', expand=True)
        self.overlay = top
        self.overlay_canvas = cv
        top.update_idletasks()
        # 关键：Tk 的 -transparentcolor 是设在**包装窗口**（wm_frame）上的，winfo_id()
        # 拿到的是里面的子窗口。如果对子窗口设 WS_EX_LAYERED，子窗口会变成「不带透明色
        # 的」不透明分层窗口，直接把整屏盖黑（本机实测踩过）。所以必须作用在 wrapper 上。
        try:
            wrapper = int(top.wm_frame(), 16)
        except (ValueError, TypeError):
            wrapper = top.winfo_id()
        set_click_through(wrapper)
        self._draw_overlay()
        self._set_note('ROI 框已显示（鼠标可以穿透，不影响游戏）')

    def _draw_overlay(self):
        if self.overlay is None:
            return
        cv = self.overlay_canvas
        cv.delete('all')
        lay, win = self.monitor.layout, self.monitor.window
        if lay is None or win is None:
            return
        ox, oy = win.client[0], win.client[1]
        for name in self._roi_names():
            r = getattr(lay, name, None)
            if r is None:
                continue
            color = ROI_COLORS[name] if name != self.target else '#ffffff'
            cv.create_rectangle(ox + r.x - 3, oy + r.y - 3, ox + r.x2 + 3, oy + r.y2 + 3,
                                outline=color, width=3)
            cv.create_text(ox + r.x, oy + r.y - 12, text='%s %dx%d' % (name, r.w, r.h),
                           fill=color, anchor='w', font=('Consolas', 12, 'bold'))

    # ---------------- 动作 ----------------

    def clear_count(self):
        """清零：数字立刻归 0，不管采样有没有在跑。

        为什么还要自己按一次 0：`monitor.reset()` 之后，面板是靠下一拍采样读出的读数
        来刷新大数字的 —— 而「读窗口校准完还没点开始计数 / 已经点了停止计数 / 录像暂停」
        这三种情况根本没有下一拍，数字就一直挂着清零前的值（用户报的「清零要等开始计数
        才生效、暂停期间没用」）。计数器那边其实已经归零了，这里只是把面板的显示跟上。
        """
        self.monitor.reset()
        self._show_zero()
        self._set_note('已清零')

    def _show_zero(self):
        """立刻把大数字 / 时间按回 0（不等下一拍采样）。"""
        self.lb_frames.configure(text='0')
        self._sync_rule()
        self.lb_time.configure(text='游戏内时间 00:00.00')

    def toggle_manual_pause(self):
        """手动「暂停计数」：数字按住不动，方便对着游戏/录像逐段核对。

        和游戏自己的暂停无关 —— 那条路一直按费用条算。这里只是让计数器不再往
        total 上加：读数、预览照常刷新，只是数字不动。再按一次恢复（恢复时重新起锚，
        按住的那一段既不会事后补进来、数字也不会跳）。符号画面变了的话是因为这个
        开关只管计数，不管特征提取。
        """
        on = self.monitor.toggle_manual_pause()
        self.btn_manual.configure(text='继续计数 (P)' if on else '暂停计数 (P)',
                                  fg=WARN if on else UI_FG)
        self._set_note('已暂停计数（手动）：数字按住不动，再按 P 继续'
                       if on else '已收开关：继续计数（重新起锚）')

    def _on_rate_pick(self, label):
        """下拉框选中某一项。选「自定义」不切档，只把光标丢进输入框。"""
        if label == RATE_CUSTOM_LABEL:
            self.en_rate.focus_set()
            self.en_rate.select_range(0, 'end')
            self._set_note('在输入框里填「一费几个逻辑帧」（例如 45），再点「应用」')
            # 下拉框回显真实值，免得看上去像已经切过去了
            self.rate_var.set(_rate_label(self._current_rate()))
            return
        v = self._rate_labels.get(label)
        if v is not None:
            self.apply_cost_rate(v)

    def _current_rate(self) -> float:
        return (self.monitor.counter.frames_per_cost if self.monitor.counter is not None
                else DEFAULT_FRAMES_PER_COST)

    def apply_rate_entry(self):
        """「应用」按钮 / 输入框回车：把输入框里的数当成一费逻辑帧数切档。"""
        text = self.en_rate.get().strip()
        try:
            v = float(text)
        except ValueError:
            self._set_note('「一费几个逻辑帧」要填数字，例如 60（回费速度 -50%% 就是 60）：%r'
                           % text)
            return
        if not (v > 0):
            self._set_note('「一费几个逻辑帧」要填正数，例如 60')
            return
        self.apply_cost_rate(v)

    def _on_negative_pick(self, label):
        self.apply_negative(_NEG_BY_LABEL.get(label, 'auto'))

    def apply_negative(self, mode):
        """切「局内负费」档：立刻生效（相位不断）+ 写回设置文件。

        换档不重新起锚：负费的进场 / 离场在游戏里就是「累加器接着走、只换速度」，
        重新起锚反而会在那一帧丢掉 / 多记一帧（见 Counter._boundary_correction）。
        """
        mode = self.monitor.set_negative_mode(mode)
        self.negative_mode = mode
        self.neg_var.set(NEGATIVE_LABELS[mode])
        note = '局内负费 = %s' % NEGATIVE_LABELS[mode]
        try:
            save_negative_mode(self.settings_path, mode)
            self._set_note('%s，已存到 %s' % (note, os.path.abspath(self.settings_path)))
        except OSError as exc:
            self._set_note('%s，但存盘失败：%s' % (note, exc))

    def apply_cost_rate(self, value):
        """切到某个回费速率：立刻生效（已数的像素帧按比例换算）+ 写回设置文件。"""
        v = clamp_frames_per_cost(value)
        self.monitor.set_cost_rate(v)
        self.rate_var.set(_rate_label(v))
        self.en_rate.delete(0, 'end')
        note = '回费速率 = %s' % format_frames_per_cost(v)
        try:
            save_cost_rate(self.settings_path, v)
            self._set_note('%s，已存到 %s' % (note, os.path.abspath(self.settings_path)))
        except OSError as exc:
            self._set_note('%s，但存盘失败：%s' % (note, exc))

    def toggle_csv(self):
        if self.monitor._csv_path:
            path = self.monitor._csv_path
            self.monitor.set_csv_path(None)
            self._set_note('已停止记录：%s' % path)
        else:
            path = os.path.join('out', 'live_%s.csv' % time.strftime('%m%d_%H%M%S'))
            self.monitor.set_csv_path(path)
            self._set_note('开始记录到 %s' % os.path.abspath(path))

    def pick_window(self):
        """「换窗口」：换实时链路读的那个窗口（不改帧来源）。

        窗口模式下「换窗口」= 换一个目标重新校准 —— 那套 ROI 是跟着「进程名 + 客户区
        尺寸」走的，把实时的分辨率表直接套到播放器窗口上必然读错。
        """
        if self._src_kind == 'window':
            self.read_window()
            return
        self._window_dialog('换窗口（实时链路读这个窗口）', self._adopt_live_window)

    def _adopt_live_window(self, win):
        self.monitor.exe, self.monitor.title, self.monitor.pid = win.exe, None, win.pid
        self.monitor.window = win
        self.monitor.set_layout(
            layout_for(win.client[2], win.client[3], arknights_ui_scaler()), keep_elapsed=True)
        self._apply_deltas()
        self._set_note('已切换到 %s' % win)

    def recalibrate(self):
        """重新校准：把「从屏幕哪块读、当前相位对到哪一帧」重新对一遍。

        按钮和 F5 都走这里，真正的活在 LiveMonitor.recalibrate 里：重找窗口、
        按当前客户区重算 ROI、采样线程死了就拉起来。**已经数出来的帧数不动** ——
        它的语义是「重新对准」，不是「清零」（清零有单独那个按钮）。

        什么时候需要它：游戏窗口被拖走/改了大小、换了分辨率，或者采样线程因为某次
        异常停了 —— 那时状态行会红着告诉你（见 _tick_body 的看门狗）。
        """
        if self._src_kind == 'window':
            # 读窗口这条链路：重新抓一遍（自动对齐 + 量条槽）。校准表里有这个
            # (进程名, 客户区尺寸) 的那份就直接套用，几毫秒；没有才跑网格搜索。
            # 数出来的帧数保留（_adopt_window 里把 Counter 接过来），本来在数就接着数。
            note = '窗口模式：重新校准（自动对齐 + 量条槽，几秒钟，帧数保留）'
            self._set_note(note, hold=4.0)
            self.start_window_calib(self._window_win, note=note)
            return note
        note = self.monitor.recalibrate()
        # 让看门狗从这一刻重新计时：否则刚重开就会被「之前停了 8 秒」立刻再报一次。
        self._last_ticks = self.monitor.stats.ticks
        self._stall_since = 0.0
        self._set_note(note, hold=4.0)
        return note

    def back_to_live(self):
        """切回游戏窗口。原来那条实时链路整个留着，重新起线程就行。

        切走时把「我们钉上去的置顶」还回去 —— 否则播放器会一直盖在所有窗口最上面。
        """
        if self._src_kind == 'live':
            self._set_note('现在就是实时输入')
            return
        try:
            self.monitor.stop()
        except Exception:                         # noqa: BLE001
            pass
        if self._window_src is not None:
            try:
                self._window_src.close()
            except Exception:                     # noqa: BLE001
                pass
            self._window_src = None
        self._release_pin()
        self._window_win = None
        self._window_started = False
        self.fr_window.pack_forget()
        self.monitor = self.live_monitor
        self._src_kind = 'live'
        self._load_calib()                        # 实时的 ROI 微调在 layout_calib.json 里
        self._apply_deltas()
        self.monitor.start(reset_clock=False)
        self.lb_source.configure(text='实时（游戏窗口）', fg=UI_DIM)
        self._set_note('已切回实时输入')

    # ---------------- 读窗口 ----------------
    #
    # 第 3 条需求「多样接口」的第二条路：读**屏幕上正在显示的一个窗口**（游戏，或者
    # 播放器里正在放的一段录屏）。和 video.py 那套基类只差「帧从哪来」这一环，
    # 所以采样 / 计数 / 打轴 / CSV 一行都不用改 —— 换成 window.WindowMonitor 就行。
    #
    # 用户要的顺序：打开录屏 → 置顶 → 点「读取窗口…」选它 → 自动校准（也可以
    # 「框选费用条」手动圈）→ **点「开始计数」**才起采样线程。

    def _window_dialog(self, title: str, on_pick):
        """选窗口的对话框。只列客户区 >= 320x240 的（再小的就不是游戏画面了）。"""
        from .win32 import list_windows
        wins = [w for w in list_windows() if w.client[2] >= 320 and w.client[3] >= 240]
        top = tk.Toplevel(self.root)
        top.title(title)
        top.configure(bg=UI_BG)
        top.attributes('-topmost', True)
        top.geometry('760x420+200+150')
        lb = tk.Listbox(top, font=(FONT_MONO, 10), bg=SUNKEN, fg=UI_FG,
                        selectbackground=RULE, selectforeground=UI_BG, activestyle='none')
        lb.pack(fill='both', expand=True, padx=8, pady=8)
        for w in wins:
            lb.insert('end', str(w))
        if not wins:
            lb.insert('end', '（没有找到可见的窗口）')

        def choose(_e=None):
            sel = lb.curselection()
            if not sel or not wins:
                return
            win = wins[sel[0]]
            top.destroy()
            on_pick(win)

        lb.bind('<Double-Button-1>', choose)
        self._btn(top, '选这个窗口', choose, font=(FONT_UI, 10)).pack(pady=(0, 8))

    def read_window(self):
        """读一个窗口：选窗口 -> 后台校准 -> 等用户点「开始计数」。"""
        if self._window_busy:
            self._set_note('有校准在跑，等它弄完再点')
            return
        self._window_dialog('读取窗口（校准完点「开始计数」）', self.start_window_calib)

    def start_window_calib(self, win, box=None, force: bool = False, note: str = ''):
        """在后台线程里校准一个窗口。

        box 是用户框选的费用条（**客户区坐标**），给了就走「手动校准」那条路。
        进度写在 _window_stage 里，主线程在 _poll_window 里读（后台线程不碰 Tk）。
        """
        if self._window_busy:
            self._set_note('有校准在跑，等它弄完再点')
            return
        self._window_busy = True
        self._window_stage = ('align', -1.0, '准备…')
        self._window_result = None
        # 目标窗口不能被盖住（盖住的那块抓到的是盖它的那个窗口）—— 「录屏置顶」就是这一步。
        # 钉不上（窗口已关 / 系统不给）也不拦着，只在提示里说一声。
        pinned = False
        try:
            pinned = set_topmost(win.hwnd, True)
        except Exception:                      # noqa: BLE001
            pinned = False
        if pinned:
            self._pinned_hwnd = win.hwnd
            # 钉完目标顺手抬面板（同 _keep_pin 里的顺序）：不抬的话目标压在面板上面，
            # 用户看到的是「校准进度刷不出来」。
            self._raise_panel()
        self._sync_pin_btn()
        self._set_note('%s\n正在校准 %s（%s）…'
                       % (note or '正在校准窗口：自动对齐 + 量条槽，几秒钟',
                          win.exe, '已置顶' if pinned else '没能置顶，别让它被盖住'),
                       hold=0.0)
        threading.Thread(target=self._window_worker, name='akframe-window', daemon=True,
                         args=(win, box, force)).start()

    def _window_worker(self, win, box, force: bool):
        """后台线程：算完把结果放进 _window_result，主线程下一拍取走。"""
        try:
            src = WindowSource(win, templates=self.templates,
                               progress=self._window_progress, use_profile=not force)
            note = ''
            if box is not None:
                # 手动校准：按框钉住费用条 -> 搜右上角两个按钮 -> 记进校准表
                self._window_progress('buttons', -1.0, '按框选钉住费用条…')
                _layout, note = src.manual_layout(box)
        except BaseException as exc:           # noqa: BLE001  连崩溃也要报给界面看
            self._window_result = (None, exc, win, '')
        else:
            self._window_result = (src, None, win, note)

    def _window_progress(self, stage, frac, note=''):
        self._window_stage = (stage, frac, note or '')

    def _poll_window(self) -> bool:
        """主线程每拍看一眼窗口校准那道工序。返回 True = 这一拍只画进度。"""
        if self._window_result is not None:
            src, exc, win, note = self._window_result
            self._window_result = None
            self._window_busy = False
            if exc is not None:
                self._set_note('读窗口失败：%s: %s' % (type(exc).__name__, exc),
                               hold=0.0, color=ERR)
            else:
                self._adopt_window(src, win, note)
            return False
        if not self._window_busy:
            return False
        stage, frac, note = self._window_stage
        pct = '' if frac < 0 else ' %.0f%%' % (frac * 100.0)
        self.lb_perf.configure(text='%s%s  %s' % (WINDOW_STAGES.get(stage, stage), pct,
                                                 note or '…'), fg=UI_DIM)
        self._set_note(note or '正在校准窗口…', hold=0.0)
        # 看门狗在这段时间不能计时：否则校准一完就会被「之前停了 8 秒」立刻报一次
        self._last_ticks = self.monitor.stats.ticks
        self._stall_since = 0.0
        return True

    def _adopt_window(self, src: WindowSource, win, note: str = ''):
        """校准好了：把面板切到这条窗口链路。**不自动起线程** —— 等用户点「开始计数」。

        只有「重新校准同一个窗口」才把数出来的帧数接过来（和 live.recalibrate 一个语义：
        重新对准，不是清零）。第一次读某个窗口时是一份全新的计数。
        """
        same = self._window_win is not None and self._window_win.hwnd == win.hwnd
        was_running = bool(same and self.monitor.alive)
        old = self.monitor
        csv_path = getattr(old, '_csv_path', None)
        rate = getattr(old, 'cost_rate', DEFAULT_FRAMES_PER_COST)
        neg = parse_negative(getattr(old, 'negative_mode', 'auto'))
        paused = bool(getattr(old, 'manual_pause', False))
        old_counter = old.counter if was_running else None
        try:
            old.stop()
        except Exception:                      # noqa: BLE001
            pass
        # 旧的帧来源要放掉。重校准同一个窗口时 self._window_src 就是 old.src，别关两遍。
        if not same and self._window_src is not None:
            try:
                self._window_src.close()
            except Exception:                  # noqa: BLE001
                pass
        if isinstance(old, WindowMonitor):
            try:
                old.src.close()
            except Exception:                  # noqa: BLE001
                pass
        elif not same:
            self.live_monitor = old             # 实时那条链路留着，等「切回实时」

        mon = WindowMonitor(win, source=src, layout=src.auto_layout,
                            templates=self.templates, csv_path=csv_path, cost_rate=rate,
                            negative=neg)
        mon.set_manual_pause(paused)            # 「暂停计数」是跨来源的开关，别被切没了
        if old_counter is not None:
            # 重新校准不该把数出来的帧数清掉；只搬 elapsed 会被下一拍覆盖，见 Counter.adopt
            mon.counter.adopt(old_counter)
            mon.counter.resync()
            # 时间基准也要接着走：新监视器是刚建的，_t0 比旧的大，不接过去的话
            # 计数器会看到一个**倒退**的时间戳（Counter.step 里 dt<=0 只能兑成 1/fps，
            # 白白丢一点时间）。start(reset_clock=False) 正是为这个语义准备的（见它的注释）。
            mon._t0 = old._t0
        self._window_src = src
        self._window_win = win
        self._src_kind = 'window'
        self.monitor = mon
        # 窗口的 ROI 是跟着「进程名 + 客户区尺寸」走的一份独立校准表（window_calib.json），
        # 和实时那套人工微调（layout_calib.json）不是一回事，切过来时清零
        self.deltas = {k: [0, 0, 0, 0] for k in ROI_ORDER}
        self.calib_scale = None
        self.fr_window.pack(fill='x')
        self.lb_source.configure(
            text='窗口 %s ｜ 客户区 %dx%d ｜ %s'
                 % (win.exe, win.client[2], win.client[3],
                    '手动框选' if src.manual else '自动校准'), fg=UI_FG)
        hint = [note or src.describe()]
        if not src.verified and not src.manual:
            hint.append('⚠ 自动校准没把握 → 点「框选费用条」手动圈一下费用条')
        if was_running:
            mon.start(reset_clock=False)        # 本来在数就接着数（时间基准不能倒退）
            hint.append('已接着数（帧数保留）')
        else:
            hint.append('点「开始计数」开始数')
        hint.extend(src.notes[-2:])
        self._window_hint_base = '\n'.join(hint)
        self._pin_noted = ''
        self._refresh_window_hint()
        self._window_started = mon.alive
        self._update_start_btn()
        self._sync_pin_btn()
        self._last_ticks = mon.stats.ticks
        self._stall_since = 0.0
        self._set_note('窗口已校准：%s' % hint[0], hold=4.0)

    def toggle_window_count(self):
        """「开始计数」/「停止计数」：窗口模式下采样线程由用户点这一下才起。"""
        if self._src_kind != 'window':
            self._set_note('「开始计数」是窗口模式的按钮（实时模式一直在数）')
            return
        mon = self.monitor
        if mon.alive:
            mon.stop()
            self._set_note('已停止计数：数出来的帧数保留，再点「开始计数」接着数')
        else:
            mon.start()
            self._set_note('开始计数')
        self._window_started = mon.alive
        self._last_ticks = mon.stats.ticks
        self._stall_since = 0.0
        self._update_start_btn()

    def _update_start_btn(self):
        """主按钮上的字（开始计数 / 停止计数 …）—— 统一由 _sync_ctrl_row 算。"""
        self._sync_ctrl_row()

    def _sync_pin_btn(self):
        on = bool(self._pinned_hwnd and self._window_win
                  and self._pinned_hwnd == self._window_win.hwnd)
        self.btn_pin.configure(text='置顶中' if on else '未置顶', fg=UI_FG if on else UI_DIM)

    def _release_pin(self):
        """把我们钉上去的置顶还回去（窗口已经关了就拉倒）。"""
        if not self._pinned_hwnd:
            return
        try:
            set_topmost(self._pinned_hwnd, False)
        except Exception:                      # noqa: BLE001
            pass
        self._pinned_hwnd = None

    def toggle_pin(self):
        """把目标窗口钉到最上层 / 放开。读窗口时被盖住就等于读不到。"""
        if self._src_kind != 'window' or self._window_win is None:
            self._set_note('先点「读取窗口…」选一个窗口')
            return
        on = self._pinned_hwnd != self._window_win.hwnd
        try:
            set_topmost(self._window_win.hwnd, on)
        except Exception as exc:               # noqa: BLE001
            self._set_note('置顶失败：%s' % exc)
            return
        self._pinned_hwnd = self._window_win.hwnd if on else None
        if on:
            self._raise_panel()                # 手动钉的时候也立刻把面板抬到它上面
        self._pin_noted = ''
        self._pin_at = 0.0                     # 刚钉过：让 _keep_pin 下一拍立刻复核一次
        self._sync_pin_btn()
        self._refresh_window_hint()
        self._set_note('已把目标窗口钉到最上层（被盖住就读不到画面）' if on
                       else '已放开置顶')

    # ---------- 置顶（读窗口那条链路）----------

    def _keep_pin(self, now):
        """把目标窗口的置顶**重新钉一遍**（每 PIN_KEEP_S 秒一次）。

        为什么钉一次不够：这条链路是抓**屏幕上那块像素**的，目标窗口被压住一角，压住的
        那块读到的就是别人的画面（费用条在右下角、按钮在右上角，压住等于整条链路瞎了）。
        而「置顶」这个状态并不牢靠，实测两种都会掉：

        1. 播放器切全屏 / 换下一段视频会**重建窗口**，新窗口不带 WS_EX_TOPMOST；
        2. 我们自己那个面板也是置顶的 —— 两个置顶窗口里，最后被激活的在上面。用户点一下
           面板（比如点「开始计数」），面板就压到播放器上面了。

        重新调用一次 SetWindowPos(HWND_TOPMOST) 同时解决两件事：把窗口重新标成置顶，
        并把它抬到置顶那一层的最上面（不动大小、不抢焦点，见 win32.set_topmost）。

        代价是：抬目标的同时也把**我们自己的面板**压下去了（面板也是置顶的，同一层里
        最后抬起来的在最上面）。实测 Desktop 2026.10.09 20.13.34（8.5 秒）里，面板每次
        只露 0.7 秒就被盖回去 —— 用户的原话「置顶还是有问题」。所以钉完目标紧接着调
        _raise_panel() 把面板再抬回来：目标在最上层（不被别的窗口盖住），面板在目标之上
        （用户看得见）。面板要是压住了某个 ROI，_roi_hits() 会当面告警。
        """
        if now - self._pin_at < PIN_KEEP_S:
            return
        self._pin_at = now
        hwnd, win = self._pinned_hwnd, self._window_win
        if hwnd is None or win is None:
            return
        if not is_window(hwnd):
            # 窗口没了（被关掉 / 播放器重建了它）。不自动换一个 —— 换窗口要重新校准、
            # 帧号也得从头数，那是用户的决定；这里只把话说明白。
            self._pinned_hwnd = None
            self._pin_noted = ''
            self._sync_pin_btn()
            self._note_pin('⚠ 目标窗口不见了（被关掉 / 播放器换了一个窗口）——'
                           '点「换窗口」重新选一个，再点「开始计数」')
            return
        try:
            ok = set_topmost(hwnd, True)
        except Exception:                      # noqa: BLE001
            ok = False
        if ok:
            self._raise_panel()
            return
        self._note_pin('⚠ 系统不让给这个窗口置顶（它可能以管理员身份在跑）—— '
                       '请手动把播放器设成「总在最前」，否则被盖住的那块就读不到画面')

    def _raise_panel(self):
        """把面板抬到目标窗口上面（同一置顶层里，最后抬上来的在最上面）。

        必须跟在 set_topmost(目标, True) 后面（见 _keep_pin）。抬不起来不算故障：这条
        链路唯一的硬要求是「目标窗口别被别的窗口盖住」，面板压不上去只影响用户看不看得
        见数字，不该因此把提示行刷红。
        """
        try:
            hwnd = top_level(self.root.winfo_id())
            if hwnd:
                raise_window(hwnd)
        except Exception:                      # noqa: BLE001
            pass

    def _note_pin(self, text):
        """置顶相关的提示只弹一次：_keep_pin 每 0.7 秒跑一遍，每遍都写会把提示行刷爆。"""
        if text == self._pin_noted:
            return
        self._pin_noted = text
        self._set_note(text, hold=0.0)
        self._refresh_window_hint()

    def _own_dialog_open(self) -> bool:
        """我们自己有没有开着对话框 / 取景器。

        开着的时候**别去抢置顶**（尤其是「框选费用条」那个取景器 —— 它就是要盖在
        目标窗口上面让用户对着参照图圈的）。
        """
        for w in self.root.winfo_children():
            if isinstance(w, tk.Toplevel):
                try:
                    if w.winfo_exists() and w.winfo_ismapped():
                        return True
                except tk.TclError:
                    continue
        return False

    def _roi_hits(self) -> list:
        """面板压住了哪几个**被读的 ROI**（费用条 / 暂停键 / 倍速键）。

        判据为什么不看整个客户区：读窗口的典型用法就是拿播放器放全屏录屏，客户区覆盖
        整块屏幕 —— 那时「面板和客户区相交」永远成立，天天报警等于没报警（实测
        Desktop 2026.10.09 20.13.34：媒体播放器客户区 2560x1539，面板怎么摆都在里面）。
        真正会把读数弄错的是**面板压住了 ROI 自己**：那块像素抓到的是面板的灰色，计数
        就卡在那个帧号上，而用户完全看不出为什么（冷启那段「莫名卡死」就是这么来的）。
        返回给人看的中文名，空列表 = 没压住任何 ROI。
        """
        win = self._window_win
        if win is None or self._src_kind != 'window':
            return []
        lay = getattr(self.monitor, 'layout', None)
        if lay is None:
            return []
        cx, cy = win.client[0], win.client[1]
        px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
        pw, ph = self.root.winfo_width(), self.root.winfo_height()
        hits = []
        for name, label in (('bar', '费用条'), ('pause', '暂停键'), ('speed', '倍速键'),
                            ('sign', '减号框')):
            r = getattr(lay, name, None)
            if r is None:
                continue
            rx, ry = cx + r.x, cy + r.y
            if px < rx + r.w and rx < px + pw and py < ry + r.h and ry < py + ph:
                hits.append(label)
        return hits

    def _refresh_window_hint(self):
        """重画窗口那一行：固定的那几句 + 置顶 / 遮挡这两条动态提示。"""
        if self._src_kind != 'window':
            return
        parts = [self._window_hint_base] if self._window_hint_base else []
        if self._window_win is not None:
            parts.append('置顶：%s（点右边那个键切换）'
                         % ('已钉在最上层、面板压在它上面'
                            if self._pinned_hwnd == self._window_win.hwnd else '未置顶'))
        hits = self._roi_hits()
        if hits:
            parts.append('⚠ 面板压住了「%s」—— 把它挪开，压住的那块读到的是面板自己'
                         % '」「'.join(hits))
        text = '\n'.join(p for p in parts if p)
        if self.lb_window_hint.cget('text') != text:
            self.lb_window_hint.configure(text=text, fg=WARN if hits else FAINT)

    # ---------- 框选费用条 ----------

    def box_bar(self):
        """框选费用条（读窗口那条链路）：拿**参照图**让用户圈，不是蒙着屏幕凭手感拖。

        为什么换成参照图：窗口里的画面是缩放过、还可能带黑边的（实测 VLC 里
        2560x1600 缩到 0.4777 倍），屏幕上差两个像素就是好几个逻辑帧的相位误差 ——
        用户的原话是「手动截取不准确，需要参照物」。参照图上的刻度尺 + 放大镜 +
        吸附左端把这一层误差压到 0，用的就是 BoxPicker 那套参照图取景器。
        """
        if self._src_kind != 'window' or self._window_win is None:
            self._set_note('先「读取窗口…」选一个窗口，再框选费用条')
            return
        if self._picker is not None:
            return
        win = self._window_win
        size = (int(win.client[2]), int(win.client[3]))

        def fetch(_i):
            # 参照图 = 这个窗口客户区的**当前**一帧；刻度尺那层的预测位置只是参考
            return (grab_window_client(win, Rect(0, 0, size[0], size[1])),
                    build_window_layout(win.exe, size[0], size[1]))

        self._picker = BoxPicker(self.root, fetch, 1, 0, on_apply=self._apply_window_box,
                                 on_note=self._set_note)
        self._set_note('框选费用条：对着参照图把条槽圈住（左右两端贴住条的两头），'
                       '框完点「用这个框」', hold=0.0)

    def _apply_window_box(self, box):
        """用户框完了（客户区坐标）：按这个框重算三个 ROI，并记进窗口校准表。"""
        self._picker = None
        win = self._window_win
        if win is None:
            return
        self._set_note('按框选的费用条重算 ROI：x=%d y=%d 宽 %d（几秒钟）'
                       % (box.x, box.y, box.w), hold=0.0)
        self.start_window_calib(win, box=box,
                                note='按框选校准（框了 %dx%d）' % (box.w, box.h))

    # ---------------- 打轴器 ----------------

    def import_timeline(self):
        """选一个 md 轴文件（默认从项目里的 maps/ 开始找）。"""
        path = filedialog.askopenfilename(
            parent=self.root, title='选择轴文件（Markdown）', initialdir=self._maps_dir(),
            filetypes=[('Markdown 轴文件', '*.md'), ('文本文件', '*.txt'),
                       ('所有文件', '*.*')])
        if path:
            self.load_timeline(path)

    def load_timeline(self, path: str):
        try:
            with open(path, encoding='utf-8-sig') as fh:      # utf-8-sig 顺带吃掉 BOM
                text = fh.read()
        except OSError as exc:
            self._set_note('读不了这个轴文件：%s' % exc)
            return
        self._timeline_path = os.path.abspath(path)
        self._set_axis_text(text)       # 顺手填进输入框，接着就能在面板里改
        tl = parse_lines(text.splitlines(), source=self._timeline_path)
        if not tl.cues:
            self._set_note('这个文件里没解析出任何一条轴，格式见 maps/example.md')
            return
        self._use_timeline(tl)
        note = '已导入 %s：%d 条轴' % (tl.name, len(tl))
        if tl.warnings:
            note += '  ｜  ' + tl.warnings[0]
        self._set_note(note)

    def reload_timeline(self):
        """重新解析当前轴：来自文件就重读文件，来自面板就重新解析输入框。"""
        if self._timeline_path:
            self.load_timeline(self._timeline_path)
            return
        if self.prompter.timeline is not None:
            self.apply_axis_text()
            return
        self._set_note('还没有轴：按 T 导入 md，或直接在下面的输入框里写')

    def apply_axis_text(self):
        """把输入框里的文本解析成轴，换掉当前提词用的轴。"""
        tl = parse_lines(self.txt_axis.get('1.0', 'end').splitlines(), source='(面板输入)')
        if not tl.cues:
            self._set_note('这段文本里没解析出轴。格式是「帧号 + 说明」，例如：120 部署银灰')
            return
        self._timeline_path = None      # 轴现在来自面板，不再是某个文件
        self._use_timeline(tl)
        note = '已应用面板里的 %d 条轴' % len(tl)
        if tl.warnings:
            note += '  ｜  ' + tl.warnings[0]
        self._set_note(note)

    def save_axis_text(self):
        """把输入框里的文本存成 md 文件（存完仍以面板内容为准）。"""
        text = self.txt_axis.get('1.0', 'end')
        if not text.strip():
            self._set_note('输入框是空的，没东西可存')
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='把轴保存成 md 文件', defaultextension='.md',
            initialdir=self._maps_dir(), initialfile='my_axis.md',
            filetypes=[('Markdown 轴文件', '*.md'), ('文本文件', '*.txt')])
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text.rstrip('\n') + '\n')
        self._set_note('已保存到 %s（当前轴仍按面板内容走，改完点「应用到提词器」）' % path)

    def clear_axis_text(self):
        """只清空输入框；已经在跑的提词轴不动。"""
        self.txt_axis.delete('1.0', 'end')
        self._set_note('输入框已清空（当前提词轴不变；换轴请点「应用到提词器」）')

    def _maps_dir(self) -> str:
        d = os.path.join(os.getcwd(), 'maps')
        return d if os.path.isdir(d) else os.getcwd()

    def _set_axis_text(self, text: str) -> None:
        self.txt_axis.delete('1.0', 'end')
        self.txt_axis.insert('1.0', text)

    def _use_timeline(self, tl) -> None:
        """装上一份新轴（不管是 md 来的还是面板文本来的）。"""
        self.prompter.set_timeline(tl)
        self.lb_prom_title.configure(text='打轴器 — %s ｜ 0/%d' % (tl.name, len(tl)))
        self._prom_state = None
        self._draw_scale_hint()
        self.lb_cue_next.configure(text='等计数器走到第一条：%d 条轴已就位' % len(tl))

    def _axis_escape(self, _event):
        """在输入框里按 Esc 只把焦点交回主窗口，不直接退出程序。"""
        self.root.focus_set()
        return 'break'

    def _axis_apply_key(self, _event):
        self.apply_axis_text()
        return 'break'

    # ---------------- 刻度表 ----------------

    def _draw_scale_hint(self, text='导入一份轴文件（按 T），这里会显示按帧排布的刻度表'):
        cv = self.cv_scale
        g = self.scale_geom
        cv.delete('all')
        # 图元被删了，引用要一起清掉 —— 否则 _draw_scale 以为还能复用那个图元，
        # 往一个已经不存在的 id 上贴图会直接抛 TclError。
        self._scale_item = None
        self._scale_photo = None
        cv.create_text(g.width / 2, g.height / 2, text=text, width=g.width - 30,
                       font=(FONT_UI, 10), fill=self.scale_theme.faint)

    def _mark_zoom(self):
        cur = self.scale_geom.px_per_frame
        for ppf, btn in self.zoom_btns.items():
            on = abs(ppf - cur) < 1e-9
            # 选中 = 白底深字（反色）。游戏 HUD 里没有第二种强调色，选中就靠反色。
            btn.configure(bg=RULE if on else CARD_HI, fg=UI_BG if on else UI_FG)

    def set_scale_zoom(self, ppf):
        """换刻度缩放：1 个逻辑帧画几个像素。轴点挤在一起就调小，隔得远就调大。"""
        self.scale_geom.px_per_frame = float(ppf)
        self._mark_zoom()
        if self._prom_state is not None:
            frame_f, cues, done = self._prom_state
            self._draw_scale(build_scale(frame_f, cues, done, self.scale_geom))
        self._set_note('刻度缩放 1 帧 = %.1f px（指针往上能看到 %.0f 帧）'
                       % (ppf, self.scale_geom.frames_up()))

    def _draw_scale(self, lay, flash=False):
        """把一张 Layout 贴到 Canvas 上：整张图用 PIL 画好，只留**一个**图元。

        为什么不用 Canvas 的基本图元一格格画（刻度线 / 数字 / 标签 / 高亮约 95 个）：
        那样每帧要 delete('all') 再重建 95 个 item，记账开销全在 Tk 那边。
        改成贴一张 420x280 的图之后实测 `_draw_scale` 整体 **2.68 ms**（PIL 画 1.19 +
        转 PhotoImage 0.24 + 贴 0.00），Canvas 上只剩一个 item。Canvas 坐标和物理像素是
        1:1（实测 reqwidth 420 -> winfo_width 424，差的 4 是 2px 边框），不会糊。

        别把这一条的收益想大了：面板真正的速率上限是「整窗口重绘约 14.7 ms」，跟刻度表
        贴不贴图关系不大；真正的瓶颈是定时器粒度，见 docs 14.2 的实测表。

        附带好处：离屏预览 tools/preview_scale.py 调的是同一个渲染器，
        「预览图」就是这里真正画出来的东西。
        """
        self._scale_photo = ImageTk.PhotoImage(
            render_scale(lay, self.scale_theme, flash=flash))
        cv = self.cv_scale
        if self._scale_item is None:
            cv.delete('all')
            self._scale_item = cv.create_image(0, 0, anchor='nw')
        cv.itemconfigure(self._scale_item, image=self._scale_photo)

    def _update_prompter(self, res):
        if self.prompter.timeline is None or res is None:
            return
        p = self.prompter.update(res.elapsed)
        tl = self.prompter.timeline
        self.lb_prom_title.configure(text='打轴器 — %s ｜ %d/%d' % (tl.name, p.done, p.total))

        if p.newly:
            self._prom_flash_until = time.time() + 1.4
        flash = time.time() < self._prom_flash_until
        if len(p.newly) > 1:
            self._set_note('这一拍一下跨过 %d 条轴' % len(p.newly), hold=2.0)

        # 刻度用带小数的帧号：满费走挂钟时那才是精确值，刻度也能一格一格地滑，而不是
        # 整帧整帧地跳。轴逻辑那边照旧用整数（和 CSV 的绝对帧号是同一个）。
        frame_f = res.elapsed_f if res.mode.startswith('clock') else float(res.elapsed)
        self._prom_state = (frame_f, tl.cues, p.done)
        self._draw_scale(build_scale(frame_f, tl.cues, p.done, self.scale_geom), flash)

        # 倒计时秒数按当前倍速折算：2 倍速下 30 帧只要 0.5 秒。
        # 子弹时间是**绝对**的 1/5（恒 6 帧/秒），与倍速设置无关 —— 所以这里要直接
        # 用 0.2，不能再乘 res.m（2X 下乘出来是 0.4 = 12 帧/秒，快了一倍）。
        # 实测见 docs 3.1。
        rate = 0.2 if res.bullet else (res.m if res.m else 1.0)
        if p.next_cue is None:
            self.lb_cue_next.configure(text='轴已走完（共 %d 条）' % p.total, fg=UI_DIM)
        else:
            secs = p.next_in / max(1e-6, 30.0 * rate)
            self.lb_cue_next.configure(
                text='下一条 %s ｜ 还差 %d 帧（≈ %.1f 秒）'
                     % (clip(one_line(p.next_cue.text), 40), p.next_in, secs),
                fg=ACCENT)

    def quit(self):
        try:
            self.monitor.stop()
            if self._window_src is not None:
                self._window_src.close()
                self._window_src = None
            self._release_pin()
        finally:
            self.root.destroy()

    # ---------------- 每帧刷新 ----------------

    def _set_note(self, text: str, hold: float = 3.0, color=None):
        """更新底部提示行。

        hold > 0 时这条提示会顶住 _tick 的自动提示几秒 —— 否则面板每 16ms 就把提示行
        刷一遍，用户刚点的操作（改回费速率、保存校准）连一眼都看不见。
        color 留给「必须看一眼」的那几条用（读窗口失败这类），默认是淡灰。
        """
        self.lb_note.configure(text=text, fg=color or UI_DIM)
        self._note_hold = time.perf_counter() + hold

    def _tick(self):
        """包一层：面板刷新出错绝不能让界面**冻死**。

        冻住的界面看起来就是「数字不动了」，用户会以为在漏帧/卡住 —— 而真实原因
        只是某一行抛了个异常（实测踩过：ROI 预览拿到 None，_tick 崩掉之后
        root.after 不再排下一次，整个面板就停在那儿，只有控制台里有 traceback）。
        所以这里兜住异常：印到控制台、把错误写进提示行，然后照常排下一次刷新。
        """
        try:
            self._tick_body()
        except Exception as exc:                     # noqa: BLE001
            traceback.print_exc()
            self._set_note('面板刷新出错：%s（细节在控制台）' % exc, hold=0.0)
        finally:
            self.root.after(self.poll_ms, self._tick)

    def _tick_body(self):
        # 面板自己的重绘速率，用来核对「显示跟不跟得上游戏」：
        # 1 倍速下游戏每秒走 30 个逻辑帧，面板必须显著高于 30 Hz 才可能每拍只跳 1 帧。
        # 实测过 60ms 的旧值（16.7 Hz）会每拍跳 2 帧，看着就像在漏帧。
        now = time.perf_counter()
        self._fit_window(now)
        self._sync_ctrl_row()
        # 读窗口那条链路：目标窗口必须一直在最上面（见 _keep_pin）。
        # 我们自己的对话框开着时不抢 —— 取景器就是要盖在目标窗口上让用户对着圈的。
        if not self._own_dialog_open():
            self._keep_pin(now)
        self._refresh_window_hint()
        self._tick_times.append(now)
        if len(self._tick_times) > 30:
            self._tick_times.pop(0)
        ui_hz = 0.0
        if len(self._tick_times) >= 2:
            span = self._tick_times[-1] - self._tick_times[0]
            ui_hz = (len(self._tick_times) - 1) / span if span > 0 else 0.0
        # 后台正在校准窗口：这一拍只画进度（原来那条链路还在跑，预览照常刷新，
        # 面板是活的）。两个都要调用 —— 返回值的语义是「这一拍只画进度」。
        busy = self._poll_window()
        if busy:
            up = self.monitor.latest
            self._render_previews(up)
            return
        upd = self.monitor.latest
        st = self.monitor.stats
        # ---- 采样线程的存活看门狗 ----
        # 为什么必须有这条：采样跑在**另一个线程**里，它一旦死了，面板这边完全看不出来 ——
        # root.after 循环照常转（「面板 64 Hz」还在跳）、ROI 预览还挂着最后一帧，只有数字
        # 和「采样 N 次」一起冻住。用户看到的就是「莫名卡死」（2026-10-07 实测报上来）。
        # 所以盯 stats.ticks 这个心跳（每转一圈 +1，**读不到画面也会涨**）：
        #     ticks 不涨 -> 线程死了 / 卡住了，这是异常，必须吼出来；
        #     ticks 在涨 -> 只是这一拍没读到画面，那是另一回事，_unreadable 已经写在提示里。
        if st.ticks != self._last_ticks:
            self._last_ticks = st.ticks
            self._stall_since = 0.0
        elif self._stall_since == 0.0:
            self._stall_since = now
        stall_s = (now - self._stall_since) if self._stall_since else 0.0
        # 门限由监视器自己报（见 LiveMonitor.stall_warn_s）
        stall_thr = getattr(self.monitor, 'stall_warn_s', STALL_WARN_S)
        # 读窗口这条链路是「校准完等用户点开始」：没点的时候采样线程本来就该是停的，
        # 那不是故障，别按「采样已停」报警。
        idle = self._src_kind == 'window' and not self._window_started
        sampling_dead = (not idle) and ((not self.monitor.alive) or stall_s >= stall_thr)
        # 「最大间隔」的告警门限必须跟着当前倍速走：采样间隔一旦超过「一个逻辑帧在屏幕上
        # 停留的时长」就会整帧漏采。停留时长 = 1000 / (30 x 倍速)：1X -> 33.3ms、
        # 2X -> 16.7ms。门限写死 33.3ms 的话，2 倍速下明明在漏采也不会报警。
        # 倍速读不到时按 1X 算（保守，宁可多报也不漏报）。
        cur_m = 1.0
        if upd.result is not None and upd.result.m == upd.result.m and upd.result.m > 0:
            cur_m = float(upd.result.m)
        if upd.result is not None:
            res = upd.result
            # 满费期间大数字印两位小数：那是「已经过了几个逻辑帧」的精确值，不足一帧的
            # 余量也在里面（`elapsed_f`）；其余时间像素通道数出来的永远是整数，印整数。
            # 判据用**条的状态**，不是「这一拍走的哪条通道」—— 满费那些拍会在挂钟通道和
            # 「倍速未知 → 冻结」之间来回切，跟着通道走的话小数位会一拍一个样，看上去
            # 就是数字在 123 / 123.00 之间闪。
            if res.bar_state in FULL_COST_STATES:
                self._full_ticks = FULL_COST_HOLD_TICKS
            elif self._full_ticks > 0:
                self._full_ticks -= 1
            if self._full_ticks > 0:
                self.lb_frames.configure(text='%.2f' % res.elapsed_f)
            else:
                self.lb_frames.configure(text=str(res.elapsed))
            self._sync_rule()      # 白线 / 数字那一格：只跟整数位数走
            self.lb_time.configure(text='游戏内时间 %s' % _fmt_time(res.elapsed_f / 30.0))
            # 这里原来有一条十几行的「状态 …」文案链（运行中 / 已暂停 / 起锚 / 刚回费 /
            # 相位重锚 / 不在关卡里 …）。面板那一行 2026-10-09 按用户要求删掉了，文案的
            # 去处：跑动细节看下面「费用条」那行的「通道=」和「模式=」，真出事看「告警：」
            # 那行（res.warn 原样打在那里），采样线程死了看 _tick_body 末尾那条转红的。
            self.lb_bar.configure(
                text='费用条 fill=%d/%d  %s  通道=%s' % (res.fill, self.monitor.layout.bar_w,
                                                      res.bar_state, res.mode))
            # 按钮没画出来时**不能**印「已暂停」：那正是「不在关卡里」的样子（见 features）。
            self.lb_pause.configure(
                text='按钮 ratio=%.3f solid=%.3f  %s' % (
                    res.pause_ratio, res.pause_solid,
                    '未画出' if not res.pause_drawn else
                    ('运行中' if res.running else '已暂停')))
            # 子弹时间下倍速数字不在画面上（res.m 是 nan），别印成「倍速 nanX」。
            if res.bullet:
                speed_text = '子弹时间 6 帧/秒'
            elif res.m == res.m:
                speed_text = '倍速 %.0fX' % res.m
            else:
                speed_text = '倍速未画出（沿用上次）'
            # glyph = 「三个三角（>>>）」的形状分：子弹时间就是这么认出来的。
            # 认不出来时先看它离 GLYPH_NCC_MIN(0.55) 差多少（同「减号 run/thr」的路子）。
            self.lb_speed.configure(
                text='%s  conf=%s  digit=%.3f corr=%s glyph=%s' % (
                    speed_text, 'ok' if res.corr_2x == res.corr_2x else 'low',
                    res.digit_ratio,
                    '%.3f' % res.corr_2x if res.corr_2x == res.corr_2x else '—',
                    '%.3f' % res.glyph if res.glyph == res.glyph else '—'))
            self.lb_warn.configure(text=('告警：' + res.warn) if res.warn else '',
                                   fg=WARN)
            self._update_cycle(res)
        else:
            # 这一拍没读到画面（窗口不在 / 还没进关卡 / 被最小化）：把告警行清空。
            # **必须清**：采样线程刚被「重新校准」救活、但还没读到画面时，告警行上还挂着
            # 红色的「采样已停」，留着的话用户会以为没救活 —— 实测踩到
            # （tools/check_recover.py 第 5 组就是这个场景）。真死了的话，
            # 下面 sampling_dead 那段会把它盖回去。
            self.lb_warn.configure(text='', fg=WARN)
        self._update_prompter(upd.result)
        # 「最大间隔」是漏帧的直接指标：采样间隔一旦超过一个逻辑帧的显示时长，
        # 就有可能整帧没被采到（表现：某一拍一次 +2）。门限见上面的 cur_m。
        # 这一行的内容按**来源**分：实时看采样率 / 有没有漏采（门限跟着倍速走，1X 33.3ms、
        # 2X 16.7ms —— 写死 33.3 的话 2 倍速下明明在漏采也不报警）。
        # 文本和「要不要标黄」都由 monitor.perf_line 给，界面只负责显示。
        perf_text, jam = self.monitor.perf_line(ui_hz, cur_m)
        self.lb_perf.configure(text=perf_text, fg=WARN if jam else UI_DIM)
        win = self.monitor.window
        lay = self.monitor.layout
        if now < self._note_hold:
            pass                     # 刚弹过的提示还留着，先别盖
        elif win is not None and lay is not None:
            self._set_note('%s  %dx%d @(%d,%d) ｜ %s' % (
                win.exe, win.client[2], win.client[3], win.client[0], win.client[1],
                upd.note or 'ROI %s' % self.target), hold=0.0)
        elif upd.note:
            self._set_note(upd.note, hold=0.0)

        self._render_previews(upd)
        if self.overlay is not None:
            self._draw_overlay()
        if self._src_kind == 'window':
            self._update_start_btn()      # 线程被停了/死了，按钮要跟着回到「开始计数」

        # 放在最后：上面那一段会按「正常」把告警行和提示行刷一遍，这里再覆盖掉。
        if sampling_dead:
            # 「数字不动」和「游戏暂停」在面板上长得一模一样 —— 这一条就是把它们分开。
            # 采样停了就得直说采样停了，并给出下一步动作。
            # 原来写在「状态」那一行（2026-10-09 那一行删了），现在写「告警：」这一行：
            # 它是常驻区里唯一的告警位置，转红 + 写到「采样线程停了…」。
            why = st.last_error or '采样线程已退出'
            self.lb_warn.configure(
                text='采样已停 %.1fs（%s）→ 点「重新校准」或按 F5' % (stall_s, why),
                fg=ERR)
            self._set_note('采样线程停了：%s。按 F5 / 点「重新校准」重开，'
                           '已经数出来的帧数不会丢' % why, hold=0.0, color=ERR)

    def _update_cycle(self, res):
        """更新「费用周期」那一行：一费几帧 + 本费走到第几帧。

        一费几帧由 Counter.frames_per_cost 给出：普通关卡 30，带了减回费的 tag 时是
        60 / 120（自动认，见 cost_rate.py；也可以用 --cost-rate 手工指定）。
        这里的「一费 N 帧（中位）」是**实测**值，正常应和它一致；
        稳定不一致才说明相位模型有问题。
        """
        counter = self.monitor.counter
        per = float(counter.frames_per_cost) if counter is not None else LOGICAL_FPS
        into = int(round(res.phase * per))
        parts = []
        # 局内负费（费用条上出现减号）：这一拍按速度减半在算（一费 60 逻辑帧）。
        if getattr(res, 'negative', False):
            parts.append('局内负费（每逻辑帧只攒一半）')
        # 减号框的原始证据：run = 框里实测最长的纯白横杠，thr = 判定用的宽度阈值。
        # 自动档下这两个数就是「框对不对」唯一的凭据（认不出来时先看差多少，
        # 比猜阈值省事；见 docs 第 31 节）。
        if self.negative_mode == 'auto' and getattr(res, 'sign_thr', 0):
            parts.append('减号 %d/%d' % (res.sign_run, res.sign_thr))
        # 回费速率（减费 tag）：手工设置的档位，或「自动识别」认准后自动切的档
        est = getattr(self.monitor, 'rate_estimate', None)
        auto_ok = est is not None and est.frames_per_cost
        if self.rate_var.get() == AUTO_RATE_LABEL and self.monitor.rate_locked:
            # 认准了：下拉框也跟着切过去，免得显示和实际不一致
            self.rate_var.set(_rate_label(per))
        if counter is not None and abs(per - LOGICAL_FPS) > 1e-9:
            parts.append('回费速率 %.0f 帧/费（%s）'
                         % (per, '自动认定' if auto_ok else '设置档'))
        elif auto_ok:
            parts.append('回费速率 %.0f 帧/费（自动认定）' % est.frames_per_cost)
        n_cyc, med, lo, hi = counter.cycle_stats() if counter is not None else (0, 0, 0, 0)
        if n_cyc:
            parts.append('一费 %d 帧（中位）' % med)
            parts.append('上次 %d 帧' % counter.cycle_lengths[-1])
            if lo != hi:
                parts.append('范围 %d..%d' % (lo, hi))
            parts.append('共 %d 个' % n_cyc)
        else:
            parts.append('还没走完一个完整周期')
        parts.append('本费 %d/%d 帧' % (into, int(round(per))))
        self.lb_cycle.configure(text='费用周期 ' + ' ｜ '.join(parts))

    def _render_previews(self, upd):
        """只画**费用条**那一条预览（红框 = 特征真正取样的那条带）。

        原来还有暂停键 / 倍速键两张 348x170 的大图，已经删掉：那两块靠 lb_pause /
        lb_speed 那两行数字（ratio / solid / conf）本来就够定位，白占 174px 高度 ——
        用户的原话是「不要的功能全部删掉，太臃肿了」。
        判空是必须的：LiveUpdate 只保证「读到画面时」各字段有值，缺一路不该把整个
        _tick 打挂 —— _tick 一崩界面就冻住，报错还只在控制台里（踩过）。
        """
        if upd.bar_img is not None:
            self._show(self.cv_bar, upd.bar_img, mark_rows=(5, 12))

    def _show(self, canvas, arr, mark_rows=None, mark_cols=None):
        """把 ROI 灰度图放大画进固定大小的画布（按比例缩放并居中，不拉伸）。

        红线 = 特征提取真正使用的那条带（费用条是行 5..12；暂停键是形状判别条带），
        蓝线 = 倍速数字的横向范围。ROI 偏了的话，一眼就能从预览上看出来。
        """
        h, w = arr.shape
        box_w = int(canvas['width'])
        box_h = int(canvas['height'])
        k = max(1, min(box_w // w, box_h // h))
        ox = (box_w - w * k) // 2
        oy = (box_h - h * k) // 2
        img = Image.fromarray(arr, mode='L').convert('RGB').resize((w * k, h * k), Image.NEAREST)
        d = ImageDraw.Draw(img)
        if mark_rows:
            r0, r1 = mark_rows
            d.rectangle([0, r0 * k, w * k - 1, (r1 + 1) * k - 1], outline=(230, 60, 60))
        if mark_cols:
            c0, c1 = mark_cols
            d.rectangle([c0 * k, 0, (c1 + 1) * k - 1, h * k - 1], outline=(60, 160, 255))
        photo = ImageTk.PhotoImage(img)
        self._photos[canvas] = photo
        canvas.delete('all')
        canvas.create_image(ox, oy, anchor='nw', image=photo)

    def run(self):
        self.root.mainloop()
