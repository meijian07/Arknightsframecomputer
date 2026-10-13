"""简易测试前端：实时帧数显示 + 三个 ROI 的实时预览 + ROI 校准工具。

为什么要有预览和校准：geometry.py 里的尺寸常数目前只在 2560x1600 这一个窗口尺寸上
实测过。换个窗口大小 / 换个平台（目标是「多样接口」），ROI 可能偏几个像素，而偏几像素
就足以让读数全错。所以前端的第一职责不是好看，而是**让你能一眼看出 ROI 有没有对准**：

    预览图上的红线 = 特征提取实际使用的那条带
    屏幕上加粗的方框（K 键）= 当前 ROI 位置，直接叠在游戏画面上

**没有任何键盘快捷键**（用户 2026-10-12：「把快捷键删掉！！！游戏里有快捷键会冲突」）。

计数器是**常驻在游戏旁边**的：按一下空格想暂停游戏，游戏收到「暂停」、面板也收到
「开始计数」，两边打架。所以 2026-10-12 把一个键都不剩地删了 —— 以前绑过的有
空格 / P / C / T / Ctrl+R / Ctrl+Enter / F5 / ↑↓ / Ctrl+Q / Esc，以及校准模式的
Tab / 方向键 / Shift+方向键 / S / R / K。

每个动作都在界面上有按钮，功能一个没少：
    开始 / 暂停 / 继续 = 主按钮三态        暂停计数        清零 + 停
    手动校准（二级窗口里：目标 / ←↑↓→ / 步长 / ROI 框 / 保存校准 / 恢复默认 / 重新选窗口）
    打轴器（导入轴… / 重载 / ◀ 回看 / 现在 ▶）
    展开 / 收起 = 标题栏那个 ▸ / ▾        重新校准 = 「校准」那行的键        退出 = 右上角 ×

回费效率（减费 tag）：默认 30 帧/费（1 秒 1 费）。点了「回费速度 -50% / -75%」的 tag
时，在**收起态**面板「回费效率」那一行选 60 / 120，或直接填数字后点「应用」——
改完立刻生效，并写回设置文件 cost_rate.json，下次启动自动带上。

打轴器（提词器）：按 T（或展开区右上角「导入轴…」）打开一个**独立小窗**——里面既能
把 md 文件拖进去，也能直接写（点「应用到提词器」）。计数器走到对应帧号时，刻度表上那条
就滚到指针位置，同时把说明放大显示出来。轴文件格式见 timeline.py 顶部注释和
maps/example.md。

读窗口（第 3 条需求「多样接口」）：**不解码文件**，直接读屏幕上正在显示的那个窗口 ——
把录屏用播放器打开、置顶，点「读取窗口…」选它 → 自动校准（没把握就点「框选费用条」
对着参照图手动圈）→ **点「开始计数」** 才起采样线程。游戏窗口本身也走这条路，所以
「识别游戏」和「识别视频」是同一套代码，只是 ROI 从 window_calib.json 来
（键 = 进程名 + 客户区尺寸）—— 见 window.py。
读窗口时目标窗口会被自动钉在最上层，我们自己的窗（面板 / 取景器 / 对话框）则每 0.7 秒
被复核一遍：目标压到我们头上就全部抬回来（置顶是一层，同一层里最后抬起来的在最上面）
—— 见 _keep_pin / _raise_if_covered / _raise_ours 和 docs 第 28、44 节。
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
from .geometry import (BAR_BAND_BOTTOM, BAR_BAND_TOP, PAUSE_SHAPE_BAND, Rect,
                       SPEED_DIGIT_BAND, SPEED_DIGIT_COLS, apply_deltas,
                       layout_for, layout_from_bar_box, load_calibration)
from .live import LiveMonitor
from .settings import (COST_RATE_PRESETS, DEFAULT_FRAMES_PER_COST,
                       clamp_frames_per_cost, format_frames_per_cost,
                       parse_negative, save_cost_rate)
from .scale import ScaleGeom, Theme, build as build_scale
from .scale_render import render as render_scale
from .state import LOGICAL_FPS
from .timeline import Teleprompter, parse_lines
from .win32 import (ScreenGrabber, arknights_ui_scaler, client_rect, is_above,
                    is_topmost, is_window, raise_window, set_click_through,
                    set_topmost, to_gray, top_level, virtual_screen_bounds,
                    work_area)

# 「把 md 文件拖进【导入 / 编辑轴】窗口」这一条要 tkinterdnd2（**可选**依赖）：
#     pip install tkinterdnd2
# 没装的话轴窗口照常能用（点「选择 md 文件…」/ 直接写），只是不吃拖放 —— 所以
# 这里绝不能硬 import，否则没装的人连程序都起不来。
try:                                             # pragma: no cover
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except Exception:                                # noqa: BLE001
    DND_FILES = None
    TkinterDnD = None
    _HAS_DND = False


def make_root():
    """建 Tk 根窗口。装了 tkinterdnd2 就用它那个 —— 只有它建出来的窗口才能当拖放目标。"""
    if _HAS_DND:
        return TkinterDnD.Tk()
    return tk.Tk()
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
             '// 写完点右边的「应用到提词器」\n')

# 读窗口那条链路的阶段名（window.WindowSource：没有探测和抽流，多了一步自检）
WINDOW_STAGES = {'align': '自动对齐费用条', 'measure': '量费用条槽',
                 'buttons': '搜右上角两个按钮', 'verify': '自检：费用条在不在动',
                 'ready': '就绪'}

# 「读窗口」的目标窗口每隔多少秒**复核**一次置顶（见 MonitorApp._keep_pin）。
# 0.7s 是「够快」和「别每拍都去问系统」之间的折中。复核不等于重钉：标记还在就一个系统
# 调用都不发 —— 无条件重钉每 0.7 秒会把面板盖一下，用户看到的就是「窗口会闪」。
PIN_KEEP_S = 0.7
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

# 三个 ROI 的中文名（手动校准窗口里当按钮用）。
ROI_LABELS = {'bar': '费用条', 'pause': '暂停键', 'speed': '倍速键'}

# 刻度表的**可视跨度**（2026-10-11 按用户要求固定）：整条刻度刚好盖住 150 帧
# —— 1 倍速下 = 5 秒 = 5 费，指针钉在正中，上下各 75 帧。
# 以前是 0.5x / 1x / 1.5x / 3x 四档手动缩放（还有 [ ] 两个键）。用户要的是固定值，
# 所以现在 px_per_frame 由刻度区高度反算：height / SCALE_SPAN_FRAMES。
SCALE_SPAN_FRAMES = 150.0
SCALE_HEIGHT = 280            # 刻度区高（px）

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
    """手动框选三个 ROI 的**取景器**：摆一帧画面当参照物，让用户对着它框。

    为什么不再用实时那条全屏半透明遮罩（MonitorApp.box_bar）：那是「蒙住屏幕、凭手感
    拖」的 —— 2560x1600 的画面缩到屏幕上，鼠标差 2 个像素就是 3~4 个逻辑帧的相位差。
    用户的原话是「手动截取不准确，需要参照物」，所以这里给三样：

        * 刻度尺：费用条的设计是「条槽宽 = 30 个逻辑帧」，参照图上把预测的条槽画成
          30 等分的橙尺 —— 对的是刻度，不是空气；
        * 放大镜：框那一块放大，四条彩线就是框的四条边（红 / 绿 = 左右，
          品红 = 上下），白块的左端压没压在红线上、白条在上下两条品红线之间
          居没居中，一眼就能看出来；能按住拖动（看框外面）、滚轮局部放大、
          双击复位；
        * 最满的一帧：「跳到最满的一帧」让用户拿到白块右端 = 条槽右端的那
          一帧。（原来还有一个「吸附左端」按钮，用户 2026-10-11：「不要搞吸附功能了，
          实测下来一次没用过」—— 按钮和 boxpick.snap_box 一起删了）。

    2026-10-11 第二轮（用户：「暂停键倍速键也会截取不到，需要手动校准；最好加一个
    放大镜，放大部分画面方便对齐」）：

        * 目标从「只有费用条」扩到三个（费用条 / 暂停键 / 倍速键）。自动搜按钮失败时
          那两路以前**没有手工补救的路**（_no_buttons 一关就永远读不到），现在能自己钉；
        * 按钮**只能挪不能缩**：read_pause / read_speed 的判别条带是按 ROI 高度的**占比**
          取的（见 geometry.PAUSE_SHAPE_BAND / SPEED_DIGIT_BAND），尺寸一变条带就落到别
          的地方去，所以按钮框固定用 layout_for 算出的标称尺寸，拖动只改位置；
        * 两个按钮**联动**：游戏里它们都锚在客户区右上角、间距是常量，所以拖一个另一个
          用同一个位移跟着走（分别夹边界会让相对位置散开 = 等于把其中一个挪错）；
        * 放大镜跟着当前目标走：费用条看条带那几行，按钮看它的判别条带（同一条红线）。

    对象用完即弃（应用 / 取消 / Esc 都调 close），面板靠 on_apply(boxes) 拿结果 ——
    boxes 是 {目标名: Rect}，**用户没动过的按钮框不回传**，好让后端继续去自动搜。

    2026-10-11 第三轮（用户：「手动校准界面右边的放大图要可以自己移动，不然很难对准」）：
    右边那块放大图能按住拖动 —— 视窗可以从框里挪出去看周围，而框的边线照旧画着
    （见 boxpick.magnify 的 pan）。同一轮把「暂停键对齐看哪儿」写进了顶部那几行说明。
    2026-10-11 第四轮（用户：「费用条难以准确框选，加一个可移动的局部放大功能，
    查看上下左右是否对齐」）：放大图里**框的四条边一起画**（原来只有左右两条）——
    只标左右的话，上下对没对上只能猜；顺手修掉 pan=0 时右边那条画不出来（原来画在
    sx(x + w) 上，正好等于图宽，被越界判掉了）。

    2026-10-12 第五轮（用户：「框选调整成创建一个框去移动，在右侧的放大区调整框进行
    微调」＋「手动框选暂停键参考不明确，最好加一个暂停键的虚影方便对齐」）：

        * 左边那张图只负责**建框 / 挪框**（费用条拖一下画一个新框；按钮按住拖，那一对
          一起走）；对齐全挪进右边那块放大图；
        * 放大图里：**按住框的身子拖 = 挪框**（原来这里是平移视窗）、**按住某一条边拖
          = 只改那一条边**、Shift+拖 / 中键拖 = 挪视窗（看框外面）、滚轮 = 局部放大、
          双击 = 复位；
        * 原来那两行「±1 像素」微调按钮（左端 / 右端 / 整框 / 整块挪）**全删了** ——
          用户要的就是「在右侧放大区调整」，那一排按钮既占地方、又和放大图抢同一件事；
        * 暂停键多了两块**淡青色虚影**（boxpick.pause_ghost = 双竖条该长的地方）：
          它们跟着框走，和画面里真的 ❚❚ 重合就算对上了 —— 这就是「参考不明确」的答案。

    2026-10-12 第六轮（用户：「放大做的什么鬼，先在左侧原画面创建一个框，就像 Windows 的
    截图一样可以拖动框放大缩小移动，右侧为框内放大图，放大图下方可以微调px；暂停键无法
    手动框选」）：把上一轮那套「在放大图里拖框」整个撤掉，分工改成截图工具的样子 ——

        * 左边那张图：拖框里面 = 挪框、拖手柄 = 拉边 / 拉角、框外面拖 = 拉新框
          （框在画布上太薄时，那两条边上的**中点**手柄不拿出来 —— 四角照旧，见
          _live_handles：画的这一份和 _hit 认的这一份必须是同一个函数，画出来却抓不住
          比不画更糟）；
        * 右边放大图：只看（左键拖 = 挪视窗、滚轮 = 局部放大、双击 = 复位）；
        * 放大图下面加回两行 px 微调（左 / 右 / 上 / 下 ±1、整框 ←→↑↓）——
          和第 46 轮删掉的那两排不是一回事：那两排是**唯一**的微调入口（拖框还没做），
          现在拖框在左边、微调在放大图底下，各管一段；
        * 按钮那一对现在**可以拉大拉小**了（原来写死标称尺寸、只能挪）——
          判别条带是按框高的**占比**算的，所以「虚影和字形重合」这件事本身就是把框
          拉对尺寸的过程；右上角那一对依旧联动（拉一个另一个同样大小、同样的四个边差）；
        * 「暂停键无法手动框选」的真因：layout 说「画面里没有右上角按钮」时，这里原来把
          pause / speed 两个目标**直接收掉**（1x1 占位框不拿出来骗人），于是点了「暂停键」
          什么都不发生。现在照旧给框，起点按条槽宽推一份。

    2026-10-12 第七轮（用户：「暂停键是斜着的，虚影对不上」）：虚影从**正矩形**改成
    **平行四边形**（`boxpick.pause_ghost` 现在返回四个点的多边形，斜率
    `PAUSE_GHOST_SHEAR = 0.19`）。游戏画出来的 ❚❚ 本来就是斜切过的，正矩形不管怎么
    挪都有一半压在字形外面 —— 这正是用户看到的「对不上」。
    """

    # 放大图画布的内尺寸：画布控件是 560x190，留 2px 边框，真正画图的是 556x186。
    ZOOM_PANEL = (556, 186)

    def __init__(self, parent, fetch, frames: int, index: int, on_apply, on_note=None,
                 on_close=None):
        self.fetch = fetch                  # fetch(i) -> (灰度帧, 参考 layout)
        self.on_close = on_close            # 收尾回调（应用 / 取消 / Esc 都会走）
        self._closed = False
        self.frames = max(1, int(frames))
        self.on_apply = on_apply
        self.on_note = on_note or (lambda text, hold=3.0: None)
        self.index = max(0, min(int(index), self.frames - 1))
        self.frame = None
        self.guide = None
        self.boxes = {}                     # {目标名: Rect}（客户区坐标）
        self.target = ROI_ORDER[0]          # 当前在框哪一个
        self.dirty = set()                  # 用户真拖过的目标（没动过的不回传）
        self._grab = None                   # 左图按住：('new' / 'inside' / 'handle',
                                            #   哪条边, 按下那一刻的 (x, y), 框, 全部框)
        self._bg = None
        self._photo = {}
        self.scale = 1.0
        self.pan = [0.0, 0.0]               # 放大图相对框的偏移（见 boxpick.magnify）
        self.zoom = 1.0                     # 局部放大倍数（1 = 窗口就是整个框）
        self._zpan_from = None              # 按住放大图：挪视窗的起点
        self._zbase = None                  # 放大倍数的基准窗口（见 _zwin_base）

        top = tk.Toplevel(parent)
        self.top = top
        top.title('框选 ROI（参照图）')
        top.configure(bg=UI_BG)
        top.attributes('-topmost', True)
        tk.Label(top, bg=UI_BG, fg=UI_FG, font=(FONT_UI, 10), justify='left', anchor='w',
                 text='三步：① 先选目标 ② 在**左边**那张图上圈住那一块 —— 和 Windows 截图'
                      '一样：拖框里面 = 挪框，拖四个角 / 四条边 = 放大缩小，\n'
                      '在框外面拖 = 重新拉一个框。③ 剩下那 1~2 个像素在**右边**放大图里看'
                      '（左键拖 = 挪视窗看框外面，滚轮 / ctrl+滚轮 = 放大缩小，''双击 = 复位），\n'
                      '微调用放大图下面那两行按钮：左 / 右 / 上 / 下 各 ±1 个像素、'
                      '整框 ←→↑↓（一次 1px）。\n'
                      '四条彩线就是框的四条边（红 / 绿 = 左右，品红 = 上下），它们跟着框走，'
                      '所以「框相对条 / 按钮偏了多少」四个方向一起看得见。\n'
                      '橙色 = 按画面尺寸推测的位置（只是参考）。\n'
                      '费用条：左右两条边贴住**条槽的两头**，上下让白条居中'
                      '（读的只是框正中间那条带，框高矮不影响读数）。\n'
                      '暂停键 / 倍速键：两个框联动（一样大、一起挪），可以随便拉大拉小。\n'
                      '那两块**淡青色虚影**就是字形该长的地方 —— 把虚影和画面里真的 ❚❚ '
                      '叠到一起就对上了。虚影是**斜的**：游戏把这两个竖条画成平行四边形，'
                      '虚影照同一条斜率画，所以能叠得上；\n'
                      '虚影比字形大 = 框拉大了，比字形小 = 框拉小了。\n'
                      '暂停键怎么对齐：读的是框顶部那条窄带（放大图上淡蓝的那一条，'
                      '约占框高的 24%~30%）—— 运行中的字形是双竖条 ❚❚，\n'
                      '蓝带的上沿要正好压住竖条的最上边（暂停时是单三角 ▶，只蹭到那个尖）。'
                      '带子整个落在字形上面 = 框挪高了；切进字形太深 = 挪低了。\n'
                      '对完点一下游戏里的暂停键，面板「暂停键」那行应该在 运行中 / 已暂停 '
                      '之间跟着翻。'
                 ).pack(fill='x', padx=10, pady=(8, 4))

        trow = tk.Frame(top, bg=UI_BG)
        trow.pack(fill='x', padx=10)
        tk.Label(trow, text='目标', bg=UI_BG, fg=UI_DIM, font=(FONT_UI, 9)).pack(side='left')
        self.tgt_btns = {}
        for name in ROI_ORDER:
            b = self._btn(trow, ROI_LABELS[name], None)
            b.configure(command=lambda n=name: self.set_target(n), padx=10)
            b.pack(side='left', padx=3)
            self.tgt_btns[name] = b

        mid = tk.Frame(top, bg=UI_BG)
        mid.pack(fill='both', expand=True, padx=10, pady=(6, 0))
        self.cv = tk.Canvas(mid, bg=SUNKEN, highlightthickness=0, cursor='crosshair',
                            width=640, height=400)
        self.cv.pack(side='left')
        side = tk.Frame(mid, bg=UI_BG)
        side.pack(side='left', padx=(8, 0), anchor='n')
        self.cv_zoom = tk.Canvas(side, bg=SUNKEN, highlightthickness=0, width=560, height=190)
        self.cv_zoom.pack(anchor='w')
        # 放大图下面那两行：px 级微调（用户 2026-10-12：「放大图下方可以微调px」）。
        nrow = tk.Frame(side, bg=UI_BG)
        nrow.pack(fill='x', pady=(6, 0))
        for label, kind, step in (('左 −1', 'l', -1), ('左 +1', 'l', 1),
                                  ('右 −1', 'r', -1), ('右 +1', 'r', 1),
                                  ('上 −1', 't', -1), ('上 +1', 't', 1),
                                  ('下 −1', 'b', -1), ('下 +1', 'b', 1)):
            self._btn(nrow, label,
                      lambda k=kind, s=step: self.nudge_edge(k, s)).pack(side='left', padx=1)
        mrow = tk.Frame(side, bg=UI_BG)
        mrow.pack(fill='x', pady=(2, 0))
        tk.Label(mrow, text='整框', bg=UI_BG, fg=UI_DIM,
                 font=(FONT_UI, 9)).pack(side='left')
        for label, dx, dy in (('←', -1, 0), ('→', 1, 0), ('↑', 0, -1), ('↓', 0, 1)):
            self._btn(mrow, label,
                      lambda a=dx, b=dy: self.nudge_move(a, b)).pack(side='left', padx=2)
        tk.Label(mrow, text='一次 1 个像素', bg=UI_BG, fg=FAINT,
                 font=(FONT_UI, 9)).pack(side='left', padx=6)
        self.lb_read = tk.Label(side, text='', bg=UI_BG, fg=UI_FG, font=(FONT_MONO, 10),
                                justify='left', anchor='w')
        self.lb_read.pack(fill='x', pady=(6, 0))

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
            self.lb_pos = tk.Label(frow, text='', bg=UI_BG, fg=UI_DIM, font=(FONT_UI, 9))
            self.lb_pos.pack(side='left', padx=6)

        # （这里原来有三排「±1 像素」微调按钮：2026-10-12 上午按用户要求删掉，改成在放大图
        #   里拖框；当天晚上用户说「放大做的什么鬼……放大图下方可以微调px」，于是它们回来
        #   了 —— 但搬到了**放大图下面**那一块，见 self.cv_zoom 后面那两行。）
        brow = tk.Frame(top, bg=UI_BG)
        brow.pack(fill='x', padx=10, pady=(6, 10))
        self.btn_ok = self._btn(brow, '用这些框（按框钉住 ROI）', self._apply)
        self.btn_ok.pack(side='left')
        self._btn(brow, '取消', self.close).pack(side='left', padx=6)

        self.cv.bind('<Button-1>', self._down)
        self.cv.bind('<B1-Motion>', self._drag)
        self.cv.bind('<ButtonRelease-1>', self._up)
        # 放大图：左键拖 = 挪视窗（看框外面）；滚轮 = 局部放大；双击 = 复位。
        self.cv_zoom.configure(cursor='fleur')
        self.cv_zoom.bind('<Button-1>', self._zdown)
        self.cv_zoom.bind('<B1-Motion>', self._zdrag)
        self.cv_zoom.bind('<ButtonRelease-1>', self._zup)
        self.cv_zoom.bind('<Leave>', self._zup)
        # Windows 上按住 Ctrl / Shift 滚会发带修饰键的那个序列，三个都绑到同一个处理上。
        for seq in ('<MouseWheel>', '<Control-MouseWheel>', '<Shift-MouseWheel>'):
            self.cv_zoom.bind(seq, self._wheel)
        self.cv_zoom.bind('<Double-Button-1>', self._zfit)
        # （原来这里绑着 <Escape> 关窗口 —— 2026-10-12 按用户要求删了，关窗口点「取消」
        #   或右上角的 ×。）
        top.protocol('WM_DELETE_WINDOW', self.close)

        self.load(self.index)

    # ---- 小部件 ----
    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=CARD_HI, fg=UI_FG, bd=0,
                         relief='flat', font=(FONT_UI, 9), padx=6, pady=2,
                         activebackground=RULE, activeforeground=UI_BG,
                         highlightthickness=0)

    # ---- 当前目标的框 ----
    @property
    def box(self):
        """当前目标的框 —— 微调 / 放大镜全部作用在它上面。"""
        return self.boxes.get(self.target)

    def set_target(self, name: str) -> None:
        """换一个要框的目标（三个 ROI 的名字见 ROI_ORDER）。"""
        if name not in ROI_ORDER or name not in self.boxes:
            return
        self.target = name
        self.pan = [0.0, 0.0]               # 换目标 = 换一个视窗（别带着上一个的平移）
        self.zoom = 1.0
        self._zbase = None                  # 换目标 = 重新取基准（这个目标的框）
        self._sync_target_btns()
        self.redraw()

    def _sync_target_btns(self) -> None:
        """当前目标那个键反色（白底深字），其余是普通态。"""
        for name, b in self.tgt_btns.items():
            if name == self.target:
                b.configure(bg=RULE, fg=UI_BG)
            else:
                b.configure(bg=CARD_HI, fg=UI_FG)


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
        if not self.boxes:
            # 三个框都从预测位置起步：对上了就直接用，没对上就在它附近拖 —— 总比从零开始强
            self.boxes = {
                'bar': Rect(int(guide.bar.x), int(guide.bar.y) - 4, int(guide.bar.w), 25),
                'pause': guide.pause,
                'speed': guide.speed,
            }
            if not guide.buttons_visible:
                # 这份 layout 认定「画面里没有右上角按钮」（校准表里就是关着的，或者画面
                # 被裁过）：可用户**正是**为这种情况才要手动框。以前这里把两个目标直接
                # 收掉，于是「暂停键」那个键点了没反应（用户 2026-10-12：「暂停键无法
                # 手动框选」）。现在照旧给他框，起点按**条槽宽**推一份。
                pb, sb = self._guess_buttons(guide.bar)
                if pb is not None:
                    self.boxes['pause'], self.boxes['speed'] = pb, sb

            self.pan = [0.0, 0.0]
            self.zoom = 1.0
            self._zbase = None              # 第一次铺框 = 基准就是这几个预测框
        if self.target not in self.boxes:
            self.target = 'bar'
        if hasattr(self, 'lb_pos'):
            self.sk.set(self.index)
            self.lb_pos.configure(text='第 %d / %d 帧' % (self.index, self.frames - 1))
        self._sync_target_btns()
        self.redraw()

    def _seek(self, _event=None):
        self.load(self.sk.get())

    def _guess_buttons(self, bar):
        """读不到右上角按钮时，按**条槽宽**推一份按钮框当起点。

        推出来的尺寸和位置都是猜的（正是自动那套没成功才走到这儿），所以只是**起点** ——
        用户自己拉着虚影去对字形（见 _draw_zoom 的 ghost）。
        """
        try:
            _client, _off, lay = layout_from_bar_box(self._frame_size(), bar)
        except Exception:                       # noqa: BLE001
            return (None, None)
        return (lay.pause, lay.speed)


    # ---- 画 ----
    def _to_img(self, x, y):
        return int(x / self.scale), int(y / self.scale)

    def _band_rows(self, r):
        """这个目标真正被读取的那几行（画面绝对行号）—— 放大镜上的红线画在它们上面。"""
        if self.target == 'bar':
            # 读的时候条 ROI 是**框正中间**那 17 行（见 geometry.layout_from_bar_box），
            # 所以这里必须按同一口径算 —— 蓝线画在别的地方会把用户带偏。
            a, b = boxpick.band_rows_of(r)
            return (r.y + a, r.y + b)
        frac = PAUSE_SHAPE_BAND if self.target == 'pause' else SPEED_DIGIT_BAND
        a, b = r.band_rows(*frac)
        return (r.y + a, r.y + b)

    def redraw(self):
        self.cv.delete('box')
        if self.frame is None or not self.boxes:
            return
        for name in ROI_ORDER:
            r = self.boxes.get(name)
            if r is None:
                continue
            on = name == self.target
            x0, y0 = int(r.x * self.scale), int(r.y * self.scale)
            x1, y1 = int((r.x + r.w) * self.scale), int((r.y + r.h) * self.scale)
            # 当前目标：白框 + 红 / 绿两条边 + 八个手柄（拖它就能放大缩小，见 _hit）；
            # 另一个按钮画成灰虚线，让人看见「联动的那一个现在在哪儿」。
            style = {'outline': '#ffffff', 'width': 2}
            if not on:
                style = {'outline': '#9a9a9a', 'width': 1, 'dash': (4, 3)}
            self.cv.create_rectangle(x0, y0, x1, y1, tags='box', **style)
            if on:
                self.cv.create_line(x0, y0, x0, y1, fill='#ff4646', width=2, tags='box')
                self.cv.create_line(x1, y0, x1, y1, fill='#46e66e', width=2, tags='box')
                for hx, hy in self._live_handles(r).values():
                    self.cv.create_rectangle(hx - self.HANDLE_PX, hy - self.HANDLE_PX,
                                             hx + self.HANDLE_PX, hy + self.HANDLE_PX,
                                             outline='#ffffff', tags='box')
        self.lb_read.configure(text=self._read_text())
        ok = self._valid()
        self.btn_ok.configure(state='normal' if ok else 'disabled',
                              bg=CARD_HI if ok else CARD, fg=UI_FG if ok else FAINT)
        self._draw_zoom()


    def _read_text(self):
        r = self.box
        if r is None:
            return ''
        head = '%s  x=%d y=%d  宽 %d 高 %d' % (ROI_LABELS.get(self.target, self.target),
                                              r.x, r.y, r.w, r.h)
        if self.target == 'bar':
            return (head + '\n条带行 %d..%d ｜ 每逻辑帧 %.2f px（宽 / 30）'
                    % (r.y + BAR_BAND_TOP, r.y + BAR_BAND_BOTTOM - 1, r.w / 30.0))
        a, b = self._band_rows(r)
        return (head + '\n取样条带 第 %d..%d 行（ROI 内，读取只用这几行）'
                % (a - r.y, b - r.y))

    def _valid(self):
        r = self.box
        if self.frame is None or r is None:
            return False
        h, w = self.frame.shape[:2]
        if r.x < 0 or r.y < 0 or r.x + r.w > w or r.y + r.h > h:
            return False
        return r.w >= 20 if self.target == 'bar' else r.w >= 8

    def _draw_zoom(self):
        r = self.box
        if r is None:
            return
        # 暂停键：淡青虚影（双竖条该长的地方）+ 条带整条填一层淡色（用户 2026-10-12：
        # 「暂停键参考不明确」—— 两条 1px 的蓝线要回答「字的顶边压在线的哪一侧」太容易
        # 看错，一条带的上沿就没这个歧义）；费用条不需要，那两条线本来就压在看得见的
        # 白块上。
        pause = self.target == 'pause'
        img = boxpick.magnify(self.frame, r, panel=self.ZOOM_PANEL,
                              layout=self.guide if self.target == 'bar' else None,
                              band=self._band_rows(r), pan=self.pan, zoom=self.zoom,
                              ghost=boxpick.pause_ghost(r) if pause else (),
                              band_fill=pause, base=self._zwin_base())
        self._photo['zoom'] = ImageTk.PhotoImage(img)
        self.cv_zoom.delete('all')
        self.cv_zoom.create_image(0, 0, anchor='nw', image=self._photo['zoom'])

    # ---- 放大图的倍数基准（用户 2026-10-13：「不要跟随框会很乱」）----
    def _zwin_base(self):
        """放大倍数的**基准窗口**（画面像素）：打开取景器 / 重画框那一刻的框大小。

        为什么不用「当前的框」：框是拿来对准的，拖手柄 / 微调 / 挪块都在改它。倍数要是
        跟着框算（`窗口 = 框 / zoom`），改框的同时整张放大图跟着缩放 —— 眼睛刚对上的那条
        线又跑了，手感就是「很乱」。基准定死之后，改框只动四条彩线的位置，倍数不动。

        什么时候重取（`self._zbase = None`）：换目标、第一次铺框、拉新框、双击复位 ——
        都正是「按这个框重新对准」的那几刻。
        """
        if self._zbase is None:
            r = self.box
            if r is None:
                return None
            self._zbase = [float(r.w), float(r.h)]
        return self._zbase

    def _zoom_lo(self):
        """现在能缩到多小：面板装得下为止（1 画面像素 = 1 屏像素，见 boxpick.zoom_floor）。"""
        base = self._zwin_base()
        return boxpick.zoom_floor(base, self.ZOOM_PANEL) if base else self.ZOOM_MIN

    # ---- 放大图：只看 + 挪视窗 / 滚轮局部放大 / 双击复位 ----
    #
    # 用户 2026-10-12：「放大做的什么鬼」—— 上一轮把「拖框」放进了放大图（拖身子挪框、
    # 拖边改边），结果在放大图里随便一拖，框就跑到别处去了。现在分工摆清楚：
    #     左边那张图   = 建框 / 挪框 / 拉边（Windows 截图那套）
    #     右边放大图   = 只看；左键拖 = 挪视窗（看框外面）、滚轮 = 局部放大、双击 = 复位
    #     放大图下面两行 = 一次 1 个像素的微调
    def _zdown(self, event):
        if self.box is None:
            return
        self._zpan_from = (event.x, event.y, self.pan[0], self.pan[1])

    def _zdrag(self, event):
        self._pan_drag(event)

    def _zup(self, _event=None):
        self._zpan_from = None

    def _pan_drag(self, event):
        """挪视窗：画面跟着手走（鼠标往右拖，那块画面也往右走 = 视窗往左）。"""
        r = self.box
        if self._zpan_from is None or r is None:
            return
        ox, oy, px, py = self._zpan_from
        kx, ky = boxpick.zoom_scale(r, self.ZOOM_PANEL, self.zoom, self._zwin_base())
        self.pan = [px - (event.x - ox) / float(kx), py - (event.y - oy) / float(ky)]
        self._pan_clamp()
        self._draw_zoom()


    # ---- 放大图：滚轮 = 局部放大（用户：「做局部放大！类似 ctrl+滚轮 的还是框选放大都行」）----
    ZOOM_STEP = 1.25                        # 一格滚轮的倍数
    ZOOM_MIN = boxpick.ZOOM_MIN             # 老口径的下限；实际下限看 _zoom_lo
    ZOOM_MAX = boxpick.ZOOM_MAX

    def _wheel(self, event):
        """滚轮一格 = 放大 / 缩小一档（ctrl+滚轮 / shift+滚轮 走同一条路，见上面的 bind）。

        往上滚 = 放大、往下滚 = 缩小，缩到 `_zoom_lo()` 为止（面板装得下 = 1:1，见
        boxpick.zoom_floor）；缩放中心 = 鼠标底下那一点（见 _zoom_at）。
        """
        if getattr(event, 'delta', 0) == 0:
            return
        step = self.ZOOM_STEP if event.delta > 0 else 1.0 / self.ZOOM_STEP
        self._zoom_at(step, event.x, event.y)

    def _zoom_at(self, factor, cx, cy):
        """缩放，并且**盯着鼠标底下那一点**缩（用户 2026-10-13：「放大鼠标单击的地方」）。

        为什么要盯住鼠标：放大图里差几个像素就是「框偏了」，用户是对着「白条左端」那个
        具体位置放大的 —— 放大之后它还在鼠标底下，就不用重新找、也不用再拖一次。

        **光标必须压在图上**：放大图（＝视窗 x 倍数）比画布小，右边 / 下面空着几十到
        一百多像素。在空处滚轮时，光标底下根本没有画面点 —— 拿那个「点」当锚点，算出来的
        视窗整个往框的左上角跑（画面上看就是「跟着框走、很乱」，用户同一句里报的正是这个）。
        所以先把 (cx, cy) 夹进**图**里：在空处滚 = 锚在离光标最近的那条图边，图不会乱走。
        """
        r = self.box
        if r is None or self.frame is None:
            return
        size = self._frame_size()
        base = self._zwin_base()
        vx, vy, vw, vh, kx, ky = boxpick.zoom_view(r, self.ZOOM_PANEL, self.pan,
                                                   self.zoom, size, base)
        cx = min(max(int(round(cx)), 0), max(0, int(vw * kx) - 1))    # 夹进图里
        cy = min(max(int(round(cy)), 0), max(0, int(vh * ky) - 1))
        fx, fy = vx + cx / float(kx), vy + cy / float(ky)    # 光标底下那个画面点
        self.zoom = boxpick.clamp_zoom(self.zoom * factor, base, self.ZOOM_PANEL)
        _vx, _vy, vw2, vh2, kx2, ky2 = boxpick.zoom_view(r, self.ZOOM_PANEL, self.pan,
                                                        self.zoom, size, base)
        # 缩完图可能变窄（视窗小了），那一列放不下就退到图里最近的一列 —— 锚点照样在
        # 图上，只是不再正好压在光标底下（差得最多就是空出来的那几列）。
        ax = min(cx, max(0, int(vw2 * kx2) - 1))
        ay = min(cy, max(0, int(vh2 * ky2) - 1))
        self.pan = [fx - ax / float(kx2) - int(r.x), fy - ay / float(ky2) - int(r.y)]
        self._pan_clamp()
        self.redraw()

    def _zfit(self, _event=None):
        """双击 = 回到「就是框里」（缩放和平移一起复位，倍数基准也跟着当前的框重取）。"""
        self.zoom = 1.0
        self.pan = [0.0, 0.0]
        self._zbase = None
        self.redraw()

    def _pan_clamp(self):
        """把 pan 夹进「视窗还在画面里」的范围。

        为什么不留给 magnify 夹：那里夹的是**画的时候**用的 vx/vy，pan 自己会越走越远 ——
        拖出去一大截再往回拖，要先「空拖」同样距离才动，手感像卡住。

        视窗多大必须跟放大镜**完全一致**（zoom > 1 时视窗比框小），所以这里把 pan 交给
        `boxpick.zoom_view`（它负责夹）再把夹完的结果读回来 —— 各算一份上下界最容易在
        缩放之后对不上。
        """
        r = self.box
        if r is None or self.frame is None:
            self.pan = [0.0, 0.0]
            return
        vx, vy = boxpick.zoom_view(r, self.ZOOM_PANEL, self.pan, self.zoom,
                                   self._frame_size(), self._zwin_base())[:2]
        self.pan = [vx - int(r.x), vy - int(r.y)]

    def _pan_carry(self, was):
        """框自己挪了：pan 反向补同样多，让放大图**看的是同一块画面**。

        不加这一下，边看放大图边按「← 1px」微调，画面会跟着跳（1 画面 px = k 像素的
        放大图）—— 那正是「很难对准」的来源。
        """
        r = self.box
        if was is None or r is None:
            return
        self.pan[0] -= r.x - was.x
        self.pan[1] -= r.y - was.y
        self._pan_clamp()

    # ---- 左边那张图：建框 / 挪框 / 拉边（Windows 截图那套） ----
    #
    # 用户 2026-10-12：「先在左侧原画面创建一个框，就像 Windows 的截图一样可以拖动框放大
    # 缩小移动」。所以这里只做截图工具那三件事：
    #     按在**手柄**上 -> 拉那一条边 / 那个角（八个手柄）
    #     按在框**里面** -> 整块挪
    #     按在框**外面** -> 拉一个新框（橡皮筋）
    # 位移一律**相对按下那一刻**算（见 boxpick.resize_box）：逐拍累加会攒下每一步的取整
    # 误差，手抖一下再抖回来框就回不到原位 —— 而这是「对 1 个像素」的活儿（6px = 1 帧）。
    #
    # 中点手柄什么时候不认：框在某个方向上太薄（费用条那个 25px 高的框缩到画布上只有
    # 9px）时，那条边上的中点会挤在角手柄上 —— 那一对中点就不认。**四角永远认**：Windows
    # 截图那套里四个角是最主要的手势，而角手柄落在两个方向的两条线上，不会跟谁抢。
    HANDLE_PX = 3                   # 手柄方块的半边长（画布像素）
    EDGE_PX = 3                     # 压在边线上（没正好压住方块）也算抓住了那条边

    def _handle_pts(self, r):
        """当前目标那个框的八个手柄在画布上的位置（四角 + 四边中点）。"""
        x0, y0 = int(r.x * self.scale), int(r.y * self.scale)
        x1, y1 = int((r.x + r.w) * self.scale), int((r.y + r.h) * self.scale)
        mx, my = (x0 + x1) // 2, (y0 + y1) // 2
        return {'tl': (x0, y0), 't': (mx, y0), 'tr': (x1, y0), 'r': (x1, my),
                'br': (x1, y1), 'b': (mx, y1), 'bl': (x0, y1), 'l': (x0, my)}

    def _live_handles(self, r):
        """这个框上**既画得出来、又按得住**的那几个手柄（四角 + 认得出的中点）。

        为什么画的这一份和 _hit 认的这一份必须是一个函数：画出来却抓不住比不画更糟 ——
        用户按住那个白方块往外拖，框一动不动，又回到「放大做的什么鬼」。
        """
        x0, y0 = int(r.x * self.scale), int(r.y * self.scale)
        x1, y1 = int((r.x + r.w) * self.scale), int((r.y + r.h) * self.scale)
        tall = (y1 - y0) > 4 * self.EDGE_PX
        wide = (x1 - x0) > 4 * self.EDGE_PX
        out = {}
        for kind, pt in self._handle_pts(r).items():
            if kind in ('t', 'b') and not tall:
                continue
            if kind in ('l', 'r') and not wide:
                continue
            out[kind] = pt
        return out

    def _hit(self, x, y):
        """左键按在画布上的哪儿 -> ('handle', 哪条边) / ('inside', None) / ('new', None)。"""
        r = self.box
        if r is None:
            return ('new', None)
        x0, y0 = int(r.x * self.scale), int(r.y * self.scale)
        x1, y1 = int((r.x + r.w) * self.scale), int((r.y + r.h) * self.scale)
        tall = (y1 - y0) > 4 * self.EDGE_PX
        wide = (x1 - x0) > 4 * self.EDGE_PX
        for kind, (hx, hy) in self._live_handles(r).items():
            if abs(x - hx) <= self.HANDLE_PX and abs(y - hy) <= self.HANDLE_PX:
                return ('handle', kind)
        if not (x0 - self.EDGE_PX <= x <= x1 + self.EDGE_PX
                and y0 - self.EDGE_PX <= y <= y1 + self.EDGE_PX):
            return ('new', None)
        near_t = tall and abs(y - y0) <= self.EDGE_PX
        near_b = tall and abs(y - y1) <= self.EDGE_PX
        near_l = wide and abs(x - x0) <= self.EDGE_PX
        near_r = wide and abs(x - x1) <= self.EDGE_PX
        if near_t or near_b or near_l or near_r:
            kind = (('t' if near_t else 'b') if (near_t or near_b) else '')
            kind += (('l' if near_l else 'r') if (near_l or near_r) else '')
            return ('handle', kind)
        return ('inside', None)

    def _min_w(self) -> int:
        """这个目标允许的最小宽（费用条的框要够宽，按钮小得多）。"""
        return self.MIN_W if self.target == 'bar' else self.BTN_MIN

    def _min_h(self) -> int:
        return self.MIN_H if self.target == 'bar' else self.BTN_MIN

    def _down(self, event):
        what, kind = self._hit(event.x, event.y)
        self._grab = (what, kind, self._to_img(event.x, event.y), self.box,
                      dict(self.boxes))
        if what == 'new':
            self.pan = [0.0, 0.0]           # 拉新框 = 重新对准，视窗回到「就是框里」
            self.zoom = 1.0
            self._zbase = None              # 框换了，倍数基准跟着新框重取

    def _drag(self, event):
        if self._grab is None:
            return
        what, kind, start, was, was_all = self._grab
        if was is None:
            return
        ix, iy = self._to_img(event.x, event.y)
        ox, oy = start
        if what == 'new':
            h, w = self.frame.shape[:2]
            x0, x1 = sorted((max(0, min(ox, w - 1)), max(0, min(ix, w - 1))))
            y0, y1 = sorted((max(0, min(oy, h - 1)), max(0, min(iy, h - 1))))
            new = Rect(x0, y0, max(2, x1 - x0), max(2, y1 - y0))
        else:
            new = boxpick.resize_box(was, 'move' if what == 'inside' else kind,
                                     ix - ox, iy - oy, self._frame_size(),
                                     self._min_w(), self._min_h())
        if new == was:
            return
        self._set_boxes(was_all, new)
        self._pan_carry(was)
        self.redraw()

    def _up(self, _event=None):
        self._grab = None

    def _set_boxes(self, was_all, new):
        """把新框写回去；右上角那一对**联动**（拉边 / 挪块都搬同样的四个边差）。"""
        names = ('pause', 'speed') if self.target in ('pause', 'speed') else (self.target,)
        self.boxes = boxpick.mirror_resize(was_all, self.target, new, self._frame_size(),
                                           names=names, min_w=self._min_w(),
                                           min_h=self._min_h())
        self.dirty.update(n for n in names if n in self.boxes)

    # ---- 放大图下面那两行：px 级微调 ----
    # 用户 2026-10-12：「放大图下方可以微调px」。走的和拖框**同一套**几何
    # （boxpick.resize_box），一次一拍 1 个像素 —— 对付的就是「红线和白块左端差 1~2px」。
    def nudge_edge(self, kind: str, step: int):
        """只动一条边：'l' / 'r' 动横的，'t' / 'b' 动竖的。"""
        self._nudge(kind, step if kind in ('l', 'r') else 0,
                    step if kind in ('t', 'b') else 0)

    def nudge_move(self, dx: int, dy: int):
        """整块挪 1 个像素。"""
        self._nudge('move', dx, dy)

    def _nudge(self, kind: str, dx: int, dy: int):
        was = self.box
        if was is None:
            return
        new = boxpick.resize_box(was, kind, dx, dy, self._frame_size(),
                                 self._min_w(), self._min_h())
        if new == was:
            return
        self._set_boxes(dict(self.boxes), new)
        self._pan_carry(was)        # 视窗反向补同样多 —— 放大图不跟着跳（第 44 轮的规矩）
        self.redraw()

    def _frame_size(self):
        h, w = self.frame.shape[:2]
        return (w, h)

    # 框能拉到多小。为什么非要有像素级微调（而不是光靠拖）：参照图按窗口大小缩过
    # （2560 宽的录屏放进 1280 的窗口只有 0.5 倍），鼠标拖出来天生差 1~2 个画面像素，
    # 而「一费 30 帧」的相位对 1px 都敏感（6px = 1 帧）—— 所以拖完还要能在放大图下面
    # 一个像素一个像素地抠，抠的时候红 / 绿线在白块的哪一侧看得清清楚楚。
    MIN_W = 20                      # 费用条的框：条槽 30 帧、每帧 6px，20px 以下没意义
    MIN_H = 3
    BTN_MIN = 8                     # 按钮小得多，能拉到的下限也小

    # ---- 收尾 ----
    def _apply(self):
        if not self.boxes:
            return
        # 费用条那一根**总是**交回去（它就是取景器的正题：位置以参照图上的框为准，
        # 哪怕用户一个像素都没动）；按钮只在用户真拖过时才交 —— 没动过说明「预测的位置
        # 看着还行」，那就让后端照旧按模板去搜（搜出来的和预测的往往不是一回事，
        # 不该拿一份没人验过的预测把它盖掉）。
        boxes = {n: r for n, r in self.boxes.items() if n == 'bar' or n in self.dirty}
        box = boxes.get('bar')
        self.close()
        self.on_apply(box if len(boxes) == 1 else boxes)

    def close(self):
        """关掉取景器（应用 / 取消 / Esc / 右上角 × 都走这里）。

        为什么要有 on_close 回调：这个对象是「用完即弃」的，而**取消**这条路不经过
        on_apply —— 面板那边的 self._picker 就永远不是 None 了，于是「手动校准」以后
        点了什么都不发生（用户报的「手动校准点取消后有时会无法再次手动校准」）。
        所以收尾统一走 on_close，让面板把那个引用清掉。
        幂等：应用那条路是先 close() 再 on_apply()，不能回调两次。
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        if self.on_close is not None:
            self.on_close()


class MonitorApp:
    # 面板宽度（**收起和展开是同一个宽度**，2026-10-11 按用户要求）。
    # 用户原话：「展开是向上展开，展开后的宽度不变」—— 所以展开只长高、
    # 不长宽，且窗口的**下边缘钉在原地**往上顶（见 _place）。展开区里所有会宽
    # 的东西都得收进这个宽度：刻度表画布 420、ROI 预览画布按可用宽
    # 实时算（_fit_preview_canvas）、长句子全给 wraplength（_set_wraps）。
    # 470 是照看板批注从 560 收下来的：要「尽量和游戏里显示费用的
    # 那个框差不多大」。
    COLLAPSED_W = 470

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
        # 计数该不该在跑（看门狗靠它区分「用户主动停的」和「线程死了」）。
        # 2026-10-11 初值改成 False：面板起来是**待命**态（主按钮写「开始」）。
        # 以前 run_monitor 一进来就 start()，而这里又是 True，于是
        # 主按钮一上来就是「暂停」——「开始」那个态根本没机会出现
        # （用户原话「现在根本没有开始键」）。
        # 现在两条链路一样：点了才开始（读窗口那条本来就是）。
        self._counting_started = False
        self._pinned_hwnd = None                # 被我们钉到最上层的窗口（切走时还回去）
        self._pin_at = 0.0                      # 上次重新钉置顶的时刻（见 _keep_pin）
        self._full_ticks = 0                    # 满费格式还要多印几拍（见 _tick_body）
        self._pin_noted = ''                    # 置顶相关的提示只报一次（别每 0.7s 刷一遍）
        self._roi_warn = ''                     # 当前那条「面板压住 ROI」告警（见 _refresh_roi_warn）
        self._picker = None                     # 参照图取景器（BoxPicker，框选费用条用）
        # 倍速模板（read_speed 认 1X / 2X 数字笔画用）。run_monitor 会把它传进来；
        # 没传就自己从 templates/ 读一份（面板单独当库用时也能起来）。
        if templates is None:
            from .cli import DEFAULT_TEMPLATES, load_templates
            templates = load_templates(DEFAULT_TEMPLATES)
        self.templates = list(templates)
        self.calib_path = calib_path
        # 回费速率写回这个文件（改档时）；局内负费已改成**一直开着**（见负费那段注释）
        # ——不再写回去，也不再读。negative_mode 只是为了读图时打印「减号 run/thr」那条证据。
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
        # px_per_frame 由「固定跨度」反算：150 帧正好铺满 280px 高
        # （2026-10-11 起固定，没有缩放档了）。
        self.scale_geom = ScaleGeom(width=420, height=SCALE_HEIGHT, ruler_w=50,
                                    px_per_frame=SCALE_HEIGHT / SCALE_SPAN_FRAMES)
        self.scale_theme = Theme()
        self._prom_state = None     # (当前帧, 全部轴, 已触发几条)，换缩放时重画要用
        self._axis_win = None       # 「导入 / 编辑轴」那个独立窗口（用到才建）
        self.txt_axis = None        # 那个窗口里的 Text；窗口没开就是 None
        self._axis_text = AXIS_HINT  # 轴文本缓冲区：窗口关着时以它为准
        # 刻度表本体是**一张 PIL 图**（akframe.scale_render），下面这两个是它贴到
        # Canvas 上的那一个图元和那一份 PhotoImage —— 必须留引用，否则会被回收成空白。
        self._scale_item = None
        self._scale_photo = None
        # 每个 Label 上一次写进去的 (文本, 颜色)。见 _set_text —— 面板每 8ms 刷一拍，
        # 内容没变还去 configure 的话光这一项就吃掉十几毫秒（实测 150% DPI 下一次 Tk
        # configure 平均 0.8ms），事件循环被塞满，拖窗口就一顿一顿的。
        self._memo = {}
        self._note_hold = 0.0           # 提示行的保留截止时刻（见 _set_note）
        self._warn_at = 0.0             # 上次「告警行收起来 + 重算窗口」的时刻（见 _set_warn）
        self._rule_need = None          # 上一拍大数字那一格的预留宽（见 _sync_rule）
        self._load_calib()

        self.root = make_root()
        self.root.title('明日方舟 全局帧数计数器')
        self.root.configure(bg=UI_BG)
        self.root.attributes('-topmost', True)
        sh = self.root.winfo_screenheight()
        # 默认**收起**（小面板）。真正的尺寸等 _build_ui() 之后按内容需求算（见 _refit）。
        self._more = False
        self._calib = False         # ROI 校准模式：进去才吃 Tab / 方向键 / S / R / K
        self._prom_shift = 0.0      # 打轴器回看：大于 0 = 往回看了这么多帧
        self._dragging = False      # 正在拖窗口（拖动期间 _tick_body 跳过重活）
        # 手动校准的二级窗口 + 它自己的状态（见 open_calib_dialog）
        self._calib_win = None
        self.calib_step = 1         # 微调步长（px）
        self.cv_bar = None          # ROI 预览画布：只在那个窗口开着时才有
        # 用户想放的位置（不是实际位置）：展开 / 收起时窗口高矮差三四倍，直接按实际
        # 位置改尺寸会把它顶出屏幕，所以位置统一由 _place() 从这一份算（见 _place）。
        #
        # 2026-10-11 把它从中线挪到**屏幕左下**：用户要「展开是向上展开」，而展开区
        # 本体就有 1172px 高（本机 150% DPI 实测）—— 摆在竖直中间时上面只剩 670px，
        # 根本不够往上长，_place 只能退成「贴着屏幕底边」（观感还是往下展开）。
        # 摆到左下之后实测：收起 470x364 @y=1111（客户区下边缘 1520）、展开 470x1172
        # @y=303（客户区下边缘还是 1520）—— 这才是纯正的**下边缘钉住、往上长**。
        # sh - 470 里的 470：收起态窗口高 364 + 标题栏 45 + 余量 ≈ 460，取整 470，
        # 想让收起态的客户区下边缘落在任务栏上方一点点。注意它只是个**上界**：本机
        # 工作区 1528、任务栏 72px 高，_place 里那条 `bottom - h - off - 8` 会先把
        # y 夹到 1111（< 1130），所以真正定位置的是那个夹子，这里只是别让它先跑偏。
        self._user_pos = (24, max(10, sh - 470))
        # 展开时窗口下边缘钉在这个位置（收起态摆好之后算出来，见 _place）
        self._anchor_bottom = 0
        # 上一次 geometry 请求的位置（见 _sync_pos）：拿它和窗口**真实**位置比，
        # 差出来那一段就是「窗口被别人挪走了」的那一段位移。
        self._asked_pos = None
        self.root.geometry('%dx%d+%d+%d' % (self.COLLAPSED_W, 150,
                                            self._user_pos[0], self._user_pos[1]))
        self.root.protocol('WM_DELETE_WINDOW', self.quit)

        self._build_ui()
        # 建完界面才知道内容到底要多宽多高（换台机器 / 换字体缩放就不一样），这里按实际
        # 需求夹一次：窄了右边按钮会被裁（换窗口 / 退出），矮了最下面几行会被裁
        # （回费速率 / 提示 / 帮助）。写死 724 的那版在 150% 缩放的屏幕上就是裁着的 ——
        # tk 的点数会跟着系统 DPI 放大，同样的字号在物理像素上要宽 1.5 倍。
        self._win_w, self._win_h = self.COLLAPSED_W, 150
        self._refit()
        self._fit_at = 0.0
        # 面板起来是待命态（上面 _counting_started 那段）：这一句告诉用户该点哪里。
        if not self._counting_started:
            self._set_note('点「开始」起采样（再点 = 暂停）', hold=8.0)
        # timeline_text 是直接把轴喂进面板输入框（演示模式用：轴不来自文件，
        # 但仍然能在面板里改、能点「应用到提词器」重解析）。
        if timeline_text:
            self._set_axis_text(timeline_text)
            self.apply_axis_text()
        elif timeline_path:
            self.load_timeline(timeline_path)
        # 焦点拿回主窗口（没有快捷键了，但输入框里留着光标会一直闪、也容易被误敲）
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
        """展开 / 收起。收起态就是一个小面板：大数字 + 时间 + 回费效率 + 一行控制键。"""
        self._more = not self._more
        if self._more:
            self._pack_more()
        else:
            self.fr_more_top.pack_forget()
            self.fr_more.pack_forget()
        self.btn_more.configure(text='▾' if self._more else '▸')
        self._refit()

    def _pack_more(self):
        """把展开区铺出来：**打轴器整块插到大数字上面**。

        用户 2026-10-11：「展开是向上展开，打轴器在计数表的上方」。pack 默认只会
        往末尾追加，想让打轴器跑到常驻区上面，只有 before=self.fr_nums 一条路。
        手动校准那一行（fr_more）照旧追加在末尾 —— 它在展开区的最下面。
        """
        self.fr_more_top.pack(fill='x', before=self.fr_nums)

    def _minimize(self):
        """最小化（任务栏）。再点一下任务栏图标就回来。"""
        try:
            self.root.iconify()
        except tk.TclError:
            pass

    def _drag_start(self, event):
        """按住标题栏拖动窗口。

        记两样东西：鼠标按下的屏幕坐标，以及**按下那一刻**窗口在哪。之后每一拍都用
        「按下那一刻的位置 + 鼠标位移」算新位置 —— 不每拍重新读 winfo_x()/winfo_y()
        再反推（那样每一拍的取整误差都会进到算出来的位置里）。
        """
        self._drag_at = (event.x_root, event.y_root)
        self._drag_win = (self.root.winfo_x(), self.root.winfo_y())
        self._dragging = True

    def _drag_move(self, event):
        """拖动中：鼠标走到哪，窗口就跟到哪（展开态也一样）。

        **展开态必须先把锚点跟着挪**：_place 的展开分支只认 _anchor_bottom（「下边缘
        钉住、往上长」那条），不看 _user_pos[1]。不挪锚点的话，展开态竖直方向**根本
        拖不动**（实测拖 dy=-30，窗口一动没动），而这一下攒进 _user_pos 的偏移会在
        收起那一刻突然冒出来 —— 面板往下一跳，用户看到的就是「位置漂移」（视频 1）。
        """
        at = getattr(self, '_drag_at', None)
        if at is None:
            return
        self._user_pos = (self._drag_win[0] + (event.x_root - at[0]),
                          self._drag_win[1] + (event.y_root - at[1]))
        off = max(0, self.root.winfo_rooty() - self.root.winfo_y())
        self._anchor_bottom = self._user_pos[1] + self._win_h + off
        self._place(self._win_w, self._win_h)

    def _drag_end(self, event=None):
        """松手：结束「拖动中」状态，并按内容重新核一次尺寸 / 夹一次位置。

        拖动期间 _tick_body 是**跳过重活**的（见那里的注释），松手这一下补回来。
        """
        if not getattr(self, '_dragging', False):
            return
        self._dragging = False
        self._refit()

    def _refit(self):
        """按当前展开状态把窗口尺寸重新夹一次（收起 = 小的，展开 = 大的）。

        为什么不写死尺寸：收起态只有 4 行，撑到 1000 高就是一大片空白；展开态的行数
        还会随提示文字换行变化（换台机器、换系统字体缩放又不一样）。所以宽度按状态给
        基准，高度按**内容实际需要**算。
        """
        # 展开也是一样的宽度（只长高）—— 展开区里宽的东西都得先收进 base。
        base = self.COLLAPSED_W
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 顺序很重要：**先**把会换行的那些行按基准宽度限住，再量内容要多宽。
        # 反过来的话，上一拍留下的宽 wraplength 会让一行长提示（「采样正常，这一拍
        # 没读到画面（未开始）」这种）把收起态从 470px 顶到 800px（实测踩过）。
        wrap = max(360, base - 30)
        self._set_wraps(wrap)
        try:
            self.root.update_idletasks()
            need_w = self.root.winfo_reqwidth()
        except Exception:                    # noqa: BLE001  还在建 / 已经销毁
            return
        w = min(max(base, need_w), max(420, sw - 40))
        # 用 base 而不是算出来的 w：画布宽一旦跟着 w 走就自激（w -> 画布 -> reqw -> w，
        # 实测直接顶到 609）。
        self._fit_preview_canvas(base)
        self._set_wraps(max(360, w - 30))
        try:
            self.root.update_idletasks()
            need_h = self.root.winfo_reqheight()
        except Exception:                    # noqa: BLE001
            return
        h = min(max(need_h, 120), max(240, sh - 80))
        self._win_w, self._win_h = w, h
        self._place(w, h)

    def _fit_preview_canvas(self, win_w: int) -> None:
        """把 ROI 预览画布调到「当前窗口装得下」的宽度。

        为什么不能写死 704：2026-10-11 起展开态的宽度和收起态一样（用户：
        「展开后的宽度不变」），而 704 会把整块面板顶宽 250px。
        _show() 本来就是按画布尺寸等比缩放居中的，所以这里只改画布宽 ——
        缩放倍数会自己变小（本机实测 k 从 2 掉到 1，费用条 ROI 240px 宽）。
        """
        if self.cv_bar is None:          # 手动校准窗口没开 -> 没有预览画布
            return
        # 24 = 左右各 10 的 padx + 4px 画布自己的边框
        self.cv_bar.configure(width=max(200, win_w - 24))

    def _set_wraps(self, wrap: int) -> None:
        """按可用宽度给会换行的那些行设换行宽度。

        右列那两行（逻辑帧 / 时间）平时只有几个字，但它们**不在**下面这一组里 ——
        单独给一个 420 的上限：大数字卡片本身就占掉 186~366px（跟着位数走），
        再宽收起态就不「小」了。
        """
        for lb in (self.lb_bar, self.lb_pause, self.lb_cycle, self.lb_perf,
                   self.lb_warn, self.lb_note,
                   # 展开区里这几行也会长：2026-10-11 起展开态宽度 = 收起态（同一个 base），
                   # 不给 wraplength 的话它们会按单行自然宽把整块面板顶宽（实测：轴文本
                   # 那行就要 509px，而 base 只有 470）。
                   self.lb_prom_title):
            lb.configure(wraplength=wrap)
        # 打轴器标题那一行右边还挂着两个按钮（重载 / 导入轴…），标题能用的宽度要
        # 扣掉它们 —— 不扣的话整行 453px，把展开态顶到 473（实测）。
        self.lb_prom_title.configure(wraplength=max(120, wrap - 185))
        for lb in (self.lb_frames_hint, self.lb_time):
            lb.configure(wraplength=max(240, min(wrap, 420)))

    def _place(self, w, h):
        """把窗口摆到屏幕上：尺寸一变（展开 / 收起）就按「想放的位置」重算一次。

        展开 / 收起：**同一个宽度**（用户要「展开后的宽度不变」，切的就是 COLLAPSED_W），
        高度按内容需求；位置靠这一份算：收起态摆好后把**客户区下边缘**记进
        _anchor_bottom，展开时 y = 下边缘 - h，下边缘不动、往上长（用户要的「向上展开」）。

        为什么要有「锚住下边缘」这一步：展开区一出来内容就 1172px 高（本机 150% DPI
        实测），照原位置直接改尺寸的话下面半截会掉到屏幕外、被任务栏压住。锚住下边缘
        就等价于「往上铺」——多出来的高度全在屏幕上方，正在看的那几行不动。
        上面真装不下（y < 8）就只贴屏幕顶。
        横向仍旧夹一次：越界就往左收，收到头贴边（留 8px）。
        """
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 窗口左上角到**客户区**左上角还差一条边框 / 标题栏（本机实测 45px）：
        # 不把这一条算进去，贴底那次还是会露出小半行。
        off = max(0, self.root.winfo_rooty() - self.root.winfo_y())
        # 先把位置缓存对齐到窗口**真实**位置（见 _sync_pos）—— 不对的话，窗口被别的
        # 方式挪走之后，任何一次布局重算都会照旧缓存把它拽回去（用户报的「点击按钮
        # 都有可能导致窗口重置位置」）。拖拽中不对：_drag_move 有自己的账。
        if not self._dragging:
            self._sync_pos(off)
        ux, uy = self._user_pos
        # 贴底还要避开任务栏：Tk 报的屏幕高度把任务栏也算进去了（实测展开态底边落在
        # 1592、任务栏从 1552 起，最下面那行帮助就被压住了）。多显示器时先看窗口在不在
        # 主屏上，不在就不扣（SPI_GETWORKAREA 只管主屏）。
        ax, ay, aw, ah = work_area()
        bottom = ay + ah if (aw > 0 and ah > 0 and ax <= ux + w // 2 <= ax + aw) else sh
        x = min(max(8, ux), max(8, sw - w - 8))
        if self._more and self._anchor_bottom > 0:
            # 展开：**下边缘钉住、往上长**（用户要的「向上展开」）。
            # 面板平时摆在屏幕中下部，展开区往上铺才不会被任务栏 / 屏幕底边切。
            y = self._anchor_bottom - h - off
        else:
            y = min(max(8, uy), max(8, bottom - h - off - 8))
            # 收起态的客户区下边缘 = 展开时的锚点
            self._anchor_bottom = y + h + off
        # 只夹这一次：**上面装不下就贴屏幕顶**（y = 8）—— 以前这里还多一条
        # 「退成贴着屏幕底边（bottom - h - off - 8）」的兜底，那条会把面板一脚踢到
        # 屏幕底部：实测收起在 y=100 时展开，整块面板从 100 瞬移到 738（638px），
        # 用户报的「展开收起都会导致面板漂移」就是它（收起能回原地，展开又跳一次）。
        # 贴顶只是「往上长不动了、剩下的往下铺」，看着是长大而不是瞬移。
        y = min(max(8, y), max(8, bottom - h - off - 8))
        self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))
        self._asked_pos = (x, y)

    def _sync_pos(self, off):
        """窗口被**别人**挪走时，把位置缓存按同一段位移平移（2026-10-11）。

        问题出在 `_user_pos` / `_anchor_bottom` 这两份缓存**只有拖面板自己那条标题栏**
        才会更新（head 那一条，见 _drag_move）。用系统标题栏拖、系统贴边 / 最大化、
        换分辨率、换显示器 —— 这些路都不经过它，缓存就停在原地；于是下一次布局重算
        （展开 / 收起、告警行收起来、内容长高触发的 _fit_window…）照着旧缓存算位置，
        就把窗口**拽回去**了。用户的说法：「点击按钮都有可能导致窗口重置位置（漂移）」。

        2026-10-11 复现（本机 1067 高的屏）：geometry 直接挪到 (700, 200) -> 点展开 ->
        跳到 (24, 249) —— x 回到缓存里的 24，y 也按缓存里那个下边缘重算了一遍。

        为什么是**平移**而不是「直接覆盖成实际位置」：缓存里存的是「用户想要的位置」，
        它允许超出屏幕（_place 会夹）。覆盖会把这个语义丢掉 —— 贴着屏幕上边展开时
        y 会被夹到 8，覆盖之后收起就回不到原来的 y 了（「收起 -> 展开 -> 收起」这条
        会开始漂）。平移只搬运「挪走的那一段」，两个语义都保住。

        两处判据是必要的，不是保守：
            ismapped —— __init__ 那一趟窗口还没上屏，winfo_x/y 是假值，照它平移会把
                        面板摆到 (0,0) 去；
            asked is None —— 同上，还没请求过位置时没有可比的对象。
        """
        try:
            if not self.root.winfo_ismapped():
                return
            live = (self.root.winfo_x(), self.root.winfo_y())
        except tk.TclError:                    # 窗口已经销毁
            return
        asked = self._asked_pos
        if asked is None:
            return
        dx, dy = live[0] - asked[0], live[1] - asked[1]
        if not (dx or dy):
            return
        ux, uy = self._user_pos
        self._user_pos = (ux + dx, uy + dy)
        self._anchor_bottom += dy

    # ---------------- 输入源 / 主按钮 ----------------

    def _src_menu(self):
        """「校准：…」下拉 —— 选「读哪儿」（2026-10-11 第五轮改名）。

        看板批注：「改成校准 / 实时为自动校准 / 视频为手动校准」。但用户这一轮发现
        「自动 / 手动」和那个「手动校准」键撞名了，所以按**读哪儿**命名：
            PC端 = 实时链路（游戏窗口本身，ROI 按分辨率的实测表推算）；
            窗口 = 读一个窗口（录屏 / 播放器 / 视频），画面被缩放过，得自己框费用条。
        """
        m = tk.Menu(self.root, tearoff=0, bg=CARD, fg=UI_FG, activebackground=RULE,
                    activeforeground=UI_BG, relief='flat', font=(FONT_UI, 9))
        m.add_command(label='PC端（实时游戏窗口）', command=self.back_to_live)
        m.add_command(label='窗口（读一个窗口…）', command=self.read_window)
        try:
            m.tk_popup(self.btn_src.winfo_rootx(),
                       self.btn_src.winfo_rooty() + self.btn_src.winfo_height())
        finally:
            m.grab_release()

    def _primary_action(self):
        """主按钮：开始 -> 暂停 -> 继续（2026-10-11 照看板「开始和暂停合并」）。

        三态，字由 _primary_label() 算：
            没在计数    「开始计数」：起采样线程（= 原来的「开始 / 停止计数」）
            在计数      「暂停」    ：数字按住不动（= 原来的「暂停计数 (P)」）
            手动暂停中  「继续」    ：接着数（重新起锚，停的那段不补）
        清零把三态一起拉回「开始」，见 clear_count。
        """
        if self.monitor.alive and self._counting_started:
            self.toggle_manual_pause()      # 计数中 / 暂停中都是这个开关在翻
        else:
            self.toggle_count()             # 没在数 -> 起采样线程

    def _manual_paused(self) -> bool:
        return bool(getattr(self.monitor, 'manual_pause', False))

    def _primary_label(self) -> str:
        """主按钮上该写哪几个字（三态，见 _primary_action）。

        文案只取两个字（开始 / 暂停 / 继续）：看板批注要「尽量缩小」，而这个按钮是
        控制行里最宽的一个 —— 实测「开始计数」比「开始」宽 36px，正好把整行顶出 470。
        2026-10-11 用户又说「快捷键操作去掉…不需要快捷键操作」：括号里的键名全删了，
        2026-10-12 键本身也删了（见模块顶部注释），所以这里只剩两个字。
        """
        if not (self.monitor.alive and self._counting_started):
            return '开始'
        return '继续' if self._manual_paused() else '暂停'

    def _sync_ctrl_row(self):
        """主按钮 / 输入源按钮上的字，跟着「输入源 + 当前状态」走。

        只在这两个控件上写字，所以每拍调用一次也不心疼（内容没变就不 configure）。
        """
        kind = self._src_kind
        want_src = {'live': '校准：PC端▾', 'window': '校准：窗口▾'}[kind]
        if self.btn_src.cget('text') != want_src:
            self.btn_src.configure(text=want_src)
        want = self._primary_label()
        if self.btn_start.cget('text') != want:
            self.btn_start.configure(text=want)
        # 回费效率那一行也在这儿刷：Counter 是**懒建**的（第一次 set_layout 才有），
        # 建好之前那一行只能显示 monitor.cost_rate，建好之后才等于 counter 的档位 ——
        # 两边不一致时用户会看着一个错的档位去判断帧数，见 _sync_rate_row 的说明。
        self._sync_rate_row()
    def _fit_window(self, now):
        """内容长高 / 长宽了就跟着把窗口放大，别把边上的东西切掉。

        为什么需要：窗口尺寸是 __init__ 里照**占位文本**算出来定死的，运行时内容还会长：
            高：性能行 / 告警行 / 费用周期这些会换行的行一长就多占一行 —— 最下面的
                帮助行被切在窗口外（实测：性能行多换了一行，帮助行只剩半行）。
            宽：大数字那一格是按**整数位数**预留的（见 _sync_rule），99 -> 100 那一下
                整张卡就宽 45px —— 而窗口宽是 _refit 那一刻算死的，不管的话右列和右边
                那几个按钮直接被窗口右边缘切掉（实测：跑到 407 帧时「时间 00:13.57」
                只剩「时间 00:13.」）。
        所以每 0.5 秒核一次「内容到底要多宽多高」，不够就长大，上限是屏幕尺寸。
        只长不缩：缩会在文本忽长忽短时来回抖（要缩请走 _refit —— 展开 / 收起 / 重校准
        那几条路都会重新按内容算一次）。
        """
        if now - self._fit_at < 0.5:
            return
        self._fit_at = now
        try:
            need_h = self.root.winfo_reqheight()
            need_w = self.root.winfo_reqwidth()
        except Exception:                    # noqa: BLE001  窗口正在销毁
            return
        # 判据不带「2px 容差」：这里差 2px 就是最右边那个字被切掉（实测 470 的窗口
        # 装 472 的内容，'CSV (L)' 的右半边和时间的最后两位一起没了）。长是单向的，
        # 偶尔被 1px 的抖动骗着多长 1px 无所谓，切掉一个字符才是真丢信息。
        grew = False
        if need_w > self._win_w:
            self._win_w = min(need_w, max(420, self.root.winfo_screenwidth() - 40))
            grew = True
        if need_h > self._win_h:
            self._win_h = min(need_h, max(640, self.root.winfo_screenheight() - 20))
            grew = True
        if grew:
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
        「这一拍印的是 123 还是 888.8」走。为什么必须这样：满费期间大数字带一位小数
        （`elapsed_f`），而满费那些拍走的通道会在「挂钟」和「倍速未知→冻结」之间来回
        切 —— 只要跟着文本宽度走，8ms 一拍就是「小数一出现整个界面抽搐一下」。

        修之前（数字格的宽度跟着这一拍的文本走）实测（那时候满费印的是两位小数）：

            文本        数字格  白线   卡片宽  右列 x   面板 reqw
            123          79     71     208     212      400
            123.45      150    142     228     232      420
            -> 小数一出现：白线 71 -> 142、右边三行整体右移 20px、面板跟着长 20px

        预留按「整数位个 8 + '.8'」算（满费只印一位小数，能把这一位数下任何文本都
        装下），白线按「整数位个 8」算。修之后实测（本机 150% 缩放、开了 DPI 感知，
        和面板真正跑起来时一致）：

            文本        数字格  白线   卡片宽  右列宽  面板 reqw
            0           121     56     186     168      466
            99          166     82     231     168      466
            123         211    127     276     168      472
            123.4       211    127     276     168      472
            888.8       211    127     276     168      472
            1234.5      256    172     321     168      517
            12345.6     301    217     366     168      562

        （数字格 = 这一格的 minsize = 量出来的 '8'x位数 + '.8' + NUM_RIGHT_PAD；
        卡片宽 = 数字格 + 65，65 是右边那个菱形徽标占的；右列 = 右边那两行「逻辑帧 /
        时间」共同要的宽，是个常数 168。面板 reqw 那块 2~3 位数时由控制行决定（446），
        4 位数起才由这一行决定 —— 那时 _fit_window 会把窗口一起放大，不会切。）

        —— 同一位数内**没有任何东西会动**，连 123 -> 888 这种换字形都不会（1 号数字
        只要 18px、8 号要 30px，按实际文本量的话白线会在 30px 里来回跳）；位数真涨
        一位（99 -> 100）时白线和格子一起变一次，那是应该变的：游戏里那条压条本来
        就是跟着费用位数变长的。

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
            need = f.measure(widest + '.8') + NUM_RIGHT_PAD
            if need != self._rule_need:
                # 位数涨一位：整张卡一下子宽 45px，这一下就得让 _fit_window 跟着
                # 放宽，别等它自己那 0.5s 的节拍 —— 实测不管的话最右边那几个字
                # 会被切半秒（1000 那一下）。
                self._rule_need = need
                self._fit_at = 0.0
            self.card.grid_columnconfigure(1, minsize=need)
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
        # 展开键只留一个正三角（看板批注「右上方有展开，x，-键」「展开图标变为正三角」）：
        # 原来写「▸ 打轴器 / 设置」，比另外两个键加起来还宽 —— 标题栏因此成了整块面板
        # 最宽的一行（525px），收起态缩不下去。
        self.btn_more = self._btn(self.head, '▸', self.toggle_more, padx=9, pady=1)
        self.btn_more.pack(side='right', padx=2, pady=(5, 5))
        for w in (self.head, self.lb_title):
            w.bind('<Button-1>', self._drag_start)
            w.bind('<B1-Motion>', self._drag_move)
            w.bind('<ButtonRelease-1>', self._drag_end)
        self._sep(self.root)

        # ---- 常驻①：大数字那一块，照着游戏里的费用显示做 ----
        # 实测那张 HUD 截图的构成：深炭灰面板上「白色菱形 + 大号白数字」，数字下面压一条
        # 粗白线。量到的比例是：面板 205x94、数字高 50（占面板高 53%）、
        # 白线 6px 厚 / 80px 宽（只占面板宽 39%）、贴着左下角。这里照抄这三样。
        self.fr_nums = tk.Frame(self.root, bg=UI_BG)
        self.fr_nums.pack(fill='x', **pad)
        card = tk.Frame(self.fr_nums, bg=CARD)
        card.pack(side='left')
        # 留着给 _sync_rule 用：大数字那一列按「整数位 + '.88'」预留固定宽度，小数出现 /
        # 消失时谁都不挪窝（实测、算式见 _sync_rule 的注释）。
        self.card = card
        self.cv_badge = tk.Canvas(card, width=44, height=44, bg=CARD, highlightthickness=0)
        self.cv_badge.grid(row=0, column=0, rowspan=2, padx=(11, 10), pady=(12, 0), sticky='n')
        self._draw_badge()
        # width=1 是故意的：这个 Label **不参与**「本行要多宽」的竞争，宽度由
        # _sync_rule 里的列 minsize 定死（像素级精确），再用 sticky='we' 撑满那一格。
        # 直接让 Label 按文本自适应的话，满费时多出那个小数就会把整张卡撑宽（实测
        # 208 -> 228px，右边三行跟着右移），收起小数又缩回去 —— 8ms 一拍地在抖。
        self.lb_frames = tk.Label(card, text='0', bg=CARD, fg=UI_FG,
                                  font=(FONT_NUM, 40), anchor='w', width=1)
        self.lb_frames.grid(row=0, column=1, padx=(0, NUM_RIGHT_PAD), pady=(6, 0),
                            sticky='we')
        self.fr_rule = tk.Frame(card, height=5, bg=RULE)
        self.fr_rule.grid(row=1, column=1, padx=(0, NUM_RIGHT_PAD), pady=(2, 9),
                          sticky='w')
        self._sync_rule()

        right = tk.Frame(self.fr_nums, bg=UI_BG)
        right.pack(side='left', padx=4, pady=6)
        # 两行都 anchor='w'（左对齐）：tk 默认 anchor='center'，文字比分到的宽度长时
        # 会**两头一起切**（实测切出来第一个字就没了）。左对齐至少从头开始读。
        # 原来这里还有第三行「状态 …」：文案太长（满费那条 330px、右列只有 292px）总是被
        # 窗口右边切掉，用户 2026-10-09 说「状态栏可以删掉」，于是整行去掉 —— 它的内容
        # 分两类，都没有丢：跑动细节在下面「费用条」那行的「通道=」里，真出事的告警在
        # 「告警：」那行（采样已停会转红写在那里，见 _tick_body 末尾）。
        # 这两行的文案**短**是量出来的：右列宽度 = 两行的最大者，而右列又是「大数字卡 +
        # 右列」这一行的宽度瓶颈（大数字卡片本身按位数要 186~366px）。原来那两句是
        # 218px / 240px，收起态 3 位数就要 550 宽；收成 135px / 162px 之后 3 位数是
        # 470 —— 而且一个字的信息都没少（'1X 30/秒' 和 '1 倍速 30 帧/秒' 是同一件事）。
        self.lb_frames_hint = self._label(right, '逻辑帧 1X 30/秒', 9, UI_DIM,
                                          anchor='w')
        self.lb_frames_hint.pack(anchor='w')
        self.lb_time = self._label(right, '时间 00:00.00', 13, anchor='w')
        self.lb_time.pack(anchor='w')

        # ---- 常驻②：回费效率（减费 tag）—— 2026-10-11 从展开区挪上来的 ----
        # 看板批注「回费效率设置放在这里」（箭头指进收起态面板），展开区那一行删掉。
        # 为什么不自动认：认错档会让绝对帧号整段漂移（差一档就是几百帧），而打轴器
        # 要的正是绝对帧号。档位取值 = 「一费几个逻辑帧」，和倍速、画面帧率都无关。
        rate_row = tk.Frame(self.root, bg=UI_BG)
        rate_row.pack(fill='x', **pad)
        self._label(rate_row, '回费效率', 10).pack(side='left')
        # 初值取 _current_rate()（= 真正在用的那一档），不是「默认 30」—— 见它的说明。
        self.rate_var = tk.StringVar(value=_rate_label(self._current_rate()))
        self._rate_labels = {}
        for v, desc in COST_RATE_PRESETS:
            self._rate_labels['%.0f  %s' % (v, desc)] = v
        items = list(self._rate_labels) + [RATE_CUSTOM_LABEL]
        self.rate_menu = tk.OptionMenu(rate_row, self.rate_var, *items,
                                       command=self._on_rate_pick)
        # width 必须**显式给**：不给（=0）时按钮宽度会跟着**当前值那串字**走
        # —— 实测值写「自定义（在右边输入）」时控件 244px、切成「30  正常」掉到 132px，
        # 也就是每切一档整块面板跟着左右一跳。给了之后宽度是个常数，切档不再跳。
        # 9 个字符 = 151px；最长的那条档位文案 '120  回费-75%' 只要 125px，留 26px 余量
        # 给别的机器上字号 / 字体略有出入的情况。
        self.rate_menu.config(bg=CARD_HI, fg=UI_FG, activebackground=CARD_HI,
                              activeforeground=UI_FG, relief='flat', highlightthickness=0,
                              font=(FONT_UI, 9), width=9, anchor='w', padx=4, pady=2)
        # 下拉里的高亮 = 白底深字（同上：游戏 HUD 的选中一律反色）
        self.rate_menu['menu'].config(bg=CARD, fg=UI_FG, activebackground=RULE,
                                      activeforeground=UI_BG, relief='flat',
                                      font=(FONT_UI, 9))
        self.rate_menu.pack(side='left', padx=(6, 6))
        self._label(rate_row, '自定义', 9, UI_DIM).pack(side='left')
        # width=4 而不是 5：三个数字（120）也放得下，整行因此窄 11px。
        self.en_rate = tk.Entry(rate_row, width=4, bg=SUNKEN, fg=UI_FG, relief='flat',
                                insertbackground=UI_FG, justify='center',
                                font=(FONT_MONO, 10))
        self.en_rate.pack(side='left', padx=(4, 4))
        # （原来这儿绑着 <Return>「回车就应用」——2026-10-12 按用户要求删了，只能点「应用」。）
        self._btn(rate_row, '应用', self.apply_rate_entry).pack(side='left', padx=3)

        # ---- 常驻③：告警行（平时空的）----
        # 看板批注「面板缩小…主体显示帧数和时间就行」：原来常驻的两行**诊断证据**
        # （费用条 / 暂停键 + 倍速 + conf/digit/glyph/ratio）搬进展开区的
        # 「诊断 / 预览」。只有告警行留在这儿 —— 采样停了 / 校准没把握，这类事必须
        # 一眼看得见。它平时是空的，**空着的时候整行收起来**：一个空 Label 也照占
        # 30px 字高（加上这一行的上下 8px，占收起态高度的 10%），见 _set_warn。
        info = tk.Frame(self.root, bg=UI_BG)
        info.pack(fill='x', **pad)
        self.fr_warn = info
        self.lb_warn = self._label(info, '', 9, WARN, anchor='w', justify='left')
        self.lb_warn.grid(row=0, column=0, columnspan=4, sticky='w')
        # 铺一次就收起来（真有告警时 _set_warn 会按 before=fr_ctrl 摆回来）。
        # 收的是**整条 Frame**而不是里面那个 Label：Tk 的 Frame 不会因为里面的
        # 控件被 grid_remove / pack_forget 就缩小自己的请求高度（实测：Label 拿掉
        # 前后 reqh 都是 23），只有把它自己从父容器里拿掉才真的腾出位置。
        info.pack_forget()

        # ---- 常驻④：控制键 ----
        # 看板批注：「改成校准」「开始和暂停合并」。这一轮按读哪儿命名：PC端 / 窗口。
        ctrl = tk.Frame(self.root, bg=UI_BG)
        ctrl.pack(fill='x', **pad)
        self.fr_ctrl = ctrl          # 告警行重新铺出来时的锚点（见 _set_warn）
        # 尖角前不留空格：'▾' 和空格一共 26px，控制行是收起态最宽的一行，去掉它
        # 整行才落进 470（实测 132 -> 121）。
        self.btn_src = self._btn(ctrl, '校准：PC端▾', self._src_menu, padx=7)
        self.btn_src.pack(side='left', padx=(0, 6))
        # 开始 / 暂停二合一：没在数 -> 开始；在数 -> 暂停；按了暂停 -> 继续。
        # 三态的字由 _sync_ctrl_row 每拍算，动作在 _primary_action。
        self.btn_start = self._btn(ctrl, '开始', self._primary_action)
        self.btn_start.pack(side='left', padx=2)
        self._btn(ctrl, '清零', self.clear_count).pack(side='left', padx=2)
        # （「CSV」那个按钮 2026-10-11 按用户要求删了 ——用户原话「csv 是什么，删掉」。
        #   层里那个逐帧记录能力（live.set_csv_path + run_monitor 的 --csv）留着：
        #   离线工具和自检还在用它，只是界面上再也不露这个开关。）

        # 控制行右边这两个键（2026-10-11 第四 / 五轮）。
        # 它们原来自己占一行（fr_window），下面还挂着一段多行「教程」（lb_window_hint）。
        # 用户第四轮的原话：「把读取窗口功能中的框选费用条替换手动校准，原有的手动校准
        # 删除；置顶中删除默认置顶；下方的教程删除，两种模式的窗口大小不变，不需要展开」；
        # 第五轮又加了一句「pc端也加入手动校准功能」。
        #   1. 「手动校准」**两种模式都有**，但两边不是同一件事（分派见 manual_calib）：
        #      PC端挪的是推算出来的 ROI（二级窗口），窗口是**对着参照图圈**条槽；
        #   2. 并进这一行（不用展开就够得着），而且两种模式**都铺**；
        #   3. 原来这儿还有个「置顶」键 —— 用户 2026-10-11 说「窗口模式置顶删除，
        #      默认置顶」，所以键没了，改成选完窗口**自动钉**（见 _pin_window）。
        #      敢默认钉是因为第 39 轮已经把「每 0.7 秒无条件重钉」（= 面板每 0.7 秒
        #      被盖一下，用户报的「窗口会闪」）改成「先复核再动手」（见 _keep_pin），
        #      钉上之后稳态一个系统调用都不发。
        self.btn_manual = self._btn(ctrl, '手动校准', self.manual_calib)
        self.btn_manual.pack(side='left', padx=3, before=self.btn_start)

        # ---- 常驻④：提示行（面板用来说话的那一行）----
        # 提示行：只在「刚点过什么」之后才有一句话，说完自己清空（见 _set_note）。
        # height=1：原来是 2 行高，于是这块地方永远空着两行 —— 用户要的「尽量缩小」。
        self.lb_note = self._label(self.root, '', 10, UI_DIM, anchor='w', justify='left',
                                   height=1)
        self.lb_note.pack(fill='x', padx=10, pady=(4, 6))

        # ================= 展开区（默认收起，点 ▸ 才出来）=================
        # 2026-10-11 拆成两块（用户：「展开是向上展开，打轴器在计数表的上方」）：
        #   fr_more_top —— 打轴器这一块（标题 + 刻度表）。它要在**大数字上面**，
        #                  所以展开时是插进常驻区、不是追加到末尾（见 _pack_more）。
        #   fr_more     —— 展开区最下面那一行（手动校准…），追加在末尾。
        self.fr_more_top = tk.Frame(self.root, bg=UI_BG)
        self._sep(self.fr_more_top)

        # ---- 打轴器（提词器）：导入 md 轴文件后，到帧就显示那一条 ----
        prom = tk.Frame(self.fr_more_top, bg=UI_BG)
        prom.pack(fill='x', **pad)
        head = tk.Frame(prom, bg=UI_BG)
        head.pack(fill='x')
        self.lb_prom_title = self._label(
            head, '打轴器 — 未导入（点右边「导入轴…」选 md / 直接写）', 10, UI_DIM)
        self.lb_prom_title.pack(side='left')
        # 「导入轴…」= 开那个独立小窗（看板批注第 6 条），不是直接弹文件选择框 ——
        # 窗口里既能拖 md 进来，也能直接写。
        # 「◀ 回看 / 现在 ▶」= 原来 ↑↓ 那两个键（2026-10-12 键删了，功能挪到按钮上）：
        # 回看只动「画哪一段」，不碰任何计数（见 _prom_scroll）。pack(side='right')
        # 是从右往左摆，所以这里写的顺序就是屏幕上从右到左的顺序。
        for text, cmd in (('重载', self.reload_timeline),
                          ('导入轴…', self.open_axis_dialog),
                          ('现在 ▶', lambda: self._prom_scroll(-99)),
                          ('◀ 回看', lambda: self._prom_scroll(1))):
            self._btn(head, text, cmd).pack(side='right', padx=3)
        # 刻度表：按帧等比例摆在一条竖直刻度上，指针（白线）钉在正中不动。
        # 帧数上涨 -> 整条刻度**向上滚**（走过去的轴往上、变暗）—— 第 34 节翻的方向。
        self.cv_scale = tk.Canvas(prom, width=self.scale_geom.width,
                                  height=self.scale_geom.height,
                                  bg=self.scale_theme.bg, highlightthickness=0)
        self.cv_scale.pack(anchor='w', pady=(6, 2))
        self._draw_scale_hint()

        # （「刻度缩放」那四个按钮 2026-10-11 删了：跨度固定 150 帧。）
        # 本来这里还有三行「状态描述」——「刻度表固定盖 150 帧（= 5 费，指针在正中）」
        # 「下一条 … 还差 N 帧」「轴文本 / 导入改在独立窗口里（按 T …）」。
        # 2026-10-11 按用户要求（「状态描述全部删掉」）一起删了：刻度表自己就把
        # 跨度、下一条、还差多少画出来了（指针 / 高亮行 / 指针下面的青柱），
        # 再拿三行灰字复述一遍纯属占地方。

        # （「回费速率」那一行 2026-10-11 按看板批注挪进收起态面板了，见 _build_ui
        #   常驻②那一块 —— 展开区不再重复。）

        # （「局内负费」那一行 2026-10-11 按用户要求删掉了：它**一直开着** ——
        #   费用数字左边出现减号就按一费 60 逻辑帧算（见 features.read_sign）。
        #   以前那个 auto / on / off 下拉框是给「认不出减号」时救计数用的，现在只保留命令行
        #   --negative（高级用法）；界面上不再有这个开关。
        #   「认得对不对」仍然看得到：下面「费用周期」那行里的「减号 run/thr」。

        # ---- 展开区最下面：手动校准（一个按钮，开二级窗口）----
        # 用户 2026-10-11：
        #     「诊断删掉，保存校准恢复默认删掉，改成手动校准，把手动校准的二级窗口图给我看」
        # 原来这里挤着 5 个控件、两行按钮 —— 诊断/预览（二级折叠）+ 保存校准 / 恢复默认 /
        # ROI 框 / 换窗口 + ROI 校准模式。全是「装好一次、换分辨率才再碰」的东西，
        # 现在一起收进 open_calib_dialog() 那个二级窗口，面板上只留这一个入口。
        # 这一块单独一个 Frame：展开时留在面板**最末尾**（默认 pack 就是追加），
        # 打轴器则在 fr_more_top 里、展开时插到大数字上面 —— 见 _pack_more。
        self.fr_more = tk.Frame(self.root, bg=UI_BG)

        # （那个「手动校准…」按钮 2026-10-11 按用户要求删了 —— 原话「原有的手动校准
        #   删除」。它开的是**自动模式**的 ROI 二级窗口（微调 delta / 存
        #   layout_calib.json），而读窗口那条链路的 ROI 走 window_calib.json，两边不是
        #   一回事，所以那个入口不该待在读窗口那一行、也不该再占一行。
        #   二级窗口本身没删：open_calib_dialog() 还在（自检第 8 组直接调它），要挂个
        #   入口回来只需在这儿摆一个按钮。）
        # 于是 fr_more 现在没有可见内容了（只剩不铺出来的 fr_diag）—— 见 _pack_more。

        # 下面这几行读数（费用条 / 暂停键 / 费用周期 / 性能 / 当前输入）**留着控件、
        # 不铺出来**：它们是查漏帧时唯一能看的原始证据，而用户要的是「面板上不要」。
        # 挂在 fr_diag 里 —— 这个 Frame 从头到尾**不 pack**，_tick_body 照旧往里写，
        # 界面上一格都不占。要真删得连同 _tick_body 里那十来处引用一起清，风险不值当。
        self.fr_diag = tk.Frame(self.fr_more, bg=UI_BG)
        diag = tk.Frame(self.fr_diag, bg=UI_BG)
        diag.pack(fill='x', **pad)
        # 这两行是从**常驻区**搬下来的（看板批注：主体只留帧数和时间）：
        #   费用条 fill/状态/通道/本费 —— 判断「这一拍读的是哪条通道」用
        #   暂停键 + 倍速 + 子弹 + conf/digit/glyph/ratio —— 判断「按钮认没认对」用
        self.lb_bar = self._label(diag, '费用条 —', 10, anchor='w', justify='left')
        self.lb_bar.grid(row=0, column=0, sticky='w')
        self.lb_pause = self._label(diag, '暂停键 —', 10, UI_DIM, anchor='w',
                                    justify='left')
        self.lb_pause.grid(row=1, column=0, sticky='w', pady=(2, 0))
        self.lb_cycle = self._label(diag, '费用周期 —', 10, anchor='w', justify='left')
        self.lb_cycle.grid(row=2, column=0, sticky='w', pady=(4, 0))
        self.lb_perf = self._label(diag, '性能 —', 9, UI_DIM, anchor='w', justify='left')
        self.lb_perf.grid(row=3, column=0, sticky='w', pady=(4, 0))
        self.lb_source = self._label(diag, '', 9, UI_DIM, anchor='w', justify='left')
        self.lb_source.grid(row=4, column=0, sticky='w', pady=(2, 0))

        # （2026-10-11 先删了「帮助行」那两行快捷键提示，2026-10-12 把键本身也删了 ——
        #   用户：「把快捷键删掉！！！游戏里有快捷键会冲突」。见 ui 顶部注释。）

        if self._more:                       # 默认收起：展开区先不 pack
            self._pack_more()

    def _redraw_scale(self, flash: bool = False):
        """按当前「回看偏移」重画刻度表（换缩放 / 翻条 / 换轴都走这里）。"""
        if self._prom_state is None:
            return
        frame_f, cues, done = self._prom_state
        shift = self._prom_shift
        self._draw_scale(build_scale(frame_f - shift, cues, done, self.scale_geom,
                                     now=frame_f), flash)

    def _prom_scroll(self, direction: int, step_frames: float = 30.0):
        """翻条（标题栏那两个「◀ 回看 / 现在 ▶」按钮）：把刻度表往回 / 往前拨 30 帧。

        回看时指针那行印「回看」，真正的「现在」由刻度表另画一条灰线标出来
        （见 scale_render.render）—— 只动「画哪一段」，**不碰任何计数**。
        """
        if self.prompter.timeline is None:
            self._set_note('先导入一份轴')
            return
        before = self._prom_shift
        self._prom_shift = max(0.0, min(600.0, before + direction * step_frames))
        self._redraw_scale()
        if abs(self._prom_shift - before) < 1e-9:
            self._set_note('已在最前面（回看 %d 帧）' % int(self._prom_shift))
        elif self._prom_shift == 0:
            self._set_note('回到「现在」')
        else:
            self._set_note('回看 %d 帧' % int(self._prom_shift))

    def _set_calib_mode(self, on: bool) -> None:
        """进出「微调 ROI」状态：进去就把屏幕上的 ROI 框亮出来，窗口一关就还原。

        2026-10-11 起它和「手动校准」那个二级窗口绑在一起（用户：「改成手动校准」）：
        窗口是唯一入口，一关就还原 —— 以前是一个常驻的「ROI 校准模式 ▸」开关。
        原来它管的是「哪几个键归我吃」，2026-10-12 键全删了，现在它只管两件事：
        亮 / 收屏幕上那个 ROI 框、给一句提示。
        """
        if self._calib == on:
            return
        self._calib = on
        if on:
            self._set_note('微调「%s」' % ROI_LABELS.get(self.target, self.target),
                           hold=6.0)
            if self.overlay is None:
                self.toggle_overlay()          # 进微调就把屏幕上的框亮出来
        else:
            if self.overlay is not None:
                self.toggle_overlay()
            self._set_note('已退出微调')
        if self._more:
            self._refit()

    # ---------------- 手动校准（二级窗口） ----------------

    def open_calib_dialog(self):
        """手动校准：选 ROI / 微调 / 看预览 / 保存 / 恢复默认 / 换窗口。

        为什么收进一个独立窗口（用户 2026-10-11：「改成手动校准，把手动校准的二级
        窗口图给我看」）：这几样都是「装好一次、换了分辨率才再碰」的东西，常驻在
        展开区里就是两行按钮白占地方。窗口是**无模式**的（不 grab）—— 对着游戏画面
        挪 ROI 的时候得能同时看见游戏。
        """
        if self._calib_win is not None:
            self._calib_win.deiconify()
            self._calib_win.lift()
            return
        top = tk.Toplevel(self.root)
        self._calib_win = top
        top.title('手动校准')
        top.configure(bg=UI_BG)
        top.attributes('-topmost', True)
        top.resizable(False, False)
        top.protocol('WM_DELETE_WINDOW', self.close_calib_dialog)

        head = tk.Frame(top, bg=UI_BG)
        head.pack(fill='x')
        self._label(head, '◆ 手动校准', 10, UI_DIM).pack(side='left', padx=(12, 0),
                                                       pady=(7, 5))
        self._btn(head, '×', self.close_calib_dialog, padx=10, pady=1).pack(
            side='right', padx=(2, 9), pady=(5, 5))
        self._sep(top)

        # ---- 目标：三个 ROI 谁在调（当前那个反色）----
        row = tk.Frame(top, bg=UI_BG)
        row.pack(fill='x', padx=10, pady=(9, 0))
        self._label(row, '目标', 10).pack(side='left', padx=(0, 6))
        self.calib_tgt_btns = {}
        for name in ROI_ORDER:
            b = self._btn(row, ROI_LABELS[name], None, hover=False)
            b.configure(command=lambda n=name: self.set_target(n))
            b.pack(side='left', padx=3)
            self.calib_tgt_btns[name] = b

        # ---- 预览：真正取特征的那条带用红框标出来 ----
        prev = tk.Frame(top, bg=UI_BG)
        prev.pack(fill='x', padx=10, pady=(9, 0))
        # 预览画布 = 当前目标那个 ROI 的**真实像素**，等比放大到画布装得下为止 ——
        # 这就是用户要的「放大镜」（原来只画费用条那一条，调暂停 / 倍速时是盲挪）。
        # 高 200 是照着按钮 ROI 那一档定的（实测 200x160）：费用条只有 17 行高，放进这个
        # 画布会被横向放大到占满宽度。
        self.cv_bar = tk.Canvas(prev, width=430, height=200, bg=SUNKEN,
                                highlightthickness=0)
        self.cv_bar.pack(fill='x')
        self.lb_calib_pos = self._label(prev, '', 9, UI_DIM, anchor='w', justify='left')
        self.lb_calib_pos.pack(fill='x', pady=(4, 0))

        # ---- 微调：四个方向键 + 步长 ----
        pad2 = tk.Frame(top, bg=UI_BG)
        pad2.pack(fill='x', padx=10, pady=(9, 0))
        self._label(pad2, '微调', 10).pack(side='left', padx=(0, 6))
        for text, dx, dy in (('←', -1, 0), ('↑', 0, -1), ('↓', 0, 1), ('→', 1, 0)):
            b = self._btn(pad2, text, None, padx=11, pady=1)
            b.configure(command=lambda a=dx, c=dy: self.nudge(a, c))
            b.pack(side='left', padx=2)
        self._label(pad2, '步长', 9, UI_DIM).pack(side='left', padx=(12, 4))
        self.calib_step_btns = {}
        for step in (1, 5, 10):
            b = self._btn(pad2, '%dpx' % step, None, hover=False, padx=6)
            b.configure(command=lambda v=step: self.set_step(v))
            b.pack(side='left', padx=2)
            self.calib_step_btns[step] = b

        # ---- 动作 ----
        acts = tk.Frame(top, bg=UI_BG)
        acts.pack(fill='x', padx=10, pady=(10, 8))
        for text, cmd in (('ROI 框', self.toggle_overlay),
                          ('保存校准', self.save_calib),
                          ('恢复默认', self.reset_calib),
                          ('重新选窗口…', self.pick_window)):
            self._btn(acts, text, cmd).pack(side='left', padx=3)

        self._set_calib_mode(True)
        self._sync_calib_buttons()
        self._sync_calib_pos()
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        # 摆在主面板右边；右边装不下就摆左边。竖直方向跟着主面板走。
        scr_w, scr_h = top.winfo_screenwidth(), top.winfo_screenheight()
        x = self.root.winfo_x() + self.root.winfo_width() + 14
        if x + w > scr_w - 8:
            x = max(8, self.root.winfo_x() - w - 14)
        y = min(max(8, self.root.winfo_y()), max(8, scr_h - h - 70))
        top.geometry('%dx%d+%d+%d' % (w, h, x, y))

    def close_calib_dialog(self):
        """关掉手动校准窗口：顺手退微调、收掉屏幕上的 ROI 框。"""
        if self._calib_win is None:
            return
        self._calib_win.destroy()
        self._calib_win = None
        self.cv_bar = None
        # 位置读数那个 Label 跟着窗口一起没了 —— 缓存也得扔掉，否则下次开窗口
        # 时文本没变，_set_text 会以为「不用写」，新 Label 就空着（实测过一次）。
        self._memo.pop('calib_pos', None)
        self.calib_tgt_btns = {}
        self.calib_step_btns = {}
        self._set_calib_mode(False)
        self._refit()

    def set_target(self, name: str) -> None:
        """切换正在微调的 ROI（= 原来 Tab 键那件事，改成按按钮）。"""
        if name not in ROI_ORDER:
            return
        self.target = name
        self._sync_calib_buttons()
        self._sync_calib_pos()

    def set_step(self, step: int) -> None:
        self.calib_step = int(step)
        self._sync_calib_buttons()

    def nudge(self, dx: int, dy: int) -> None:
        """微调一下当前 ROI：挪 (dx, dy) x 步长。"""
        d = self.deltas[self.target]
        d[0] += dx * self.calib_step
        d[1] += dy * self.calib_step
        self._apply_deltas()
        self._sync_calib_pos()

    def _sync_calib_buttons(self) -> None:
        """目标 / 步长按钮的选中态（白底深字 = 当前）。只在切换时调，不要每拍调。"""
        for name, b in getattr(self, 'calib_tgt_btns', {}).items():
            if name == self.target:
                b.configure(bg=RULE, fg=UI_BG)
            else:
                b.configure(bg=CARD_HI, fg=UI_FG)
        for step, b in getattr(self, 'calib_step_btns', {}).items():
            if step == self.calib_step:
                b.configure(bg=RULE, fg=UI_BG)
            else:
                b.configure(bg=CARD_HI, fg=UI_FG)

    def _sync_calib_pos(self) -> None:
        """把当前 ROI 的位置 / 大小刷到窗口上（每拍调，靠 _set_text 的缓存省开销）。"""
        if self._calib_win is None:
            return
        lay = self.monitor.layout
        roi = getattr(lay, self.target, None) if lay is not None else None
        if roi is None:
            self._set_text('calib_pos', self.lb_calib_pos, '还没读到画面，ROI 未知')
        else:
            self._set_text('calib_pos', self.lb_calib_pos,
                           '%s   x=%d  y=%d   %dx%d' % (ROI_LABELS[self.target],
                                                        roi.x, roi.y, roi.w, roi.h))

    # （_base_keys / _calib_keys / _bind_keys / CALIB_SEQS 这一整套 2026-10-12 删了。
    #   用户：「把快捷键删掉！！！游戏里有快捷键会冲突」—— 常驻在游戏旁边的计数器
    #   不该抢任何键。每个动作在界面上都有按钮，见模块顶部注释。）

    # ---------------- 校准 ----------------

    def _load_calib(self):
        deltas, scale = load_calibration(self.calib_path)
        if deltas:
            # 只认现在还在的 ROI：老校准文件里可能留着已经删掉的通道
            self.deltas = {k: list(v) for k, v in deltas.items() if k in ROI_ORDER}
            for k in ROI_ORDER:
                self.deltas.setdefault(k, [0, 0, 0, 0])
            self.calib_scale = scale

    def _roi_names(self):
        '''当前 layout 里真有的 ROI 名字。'''
        lay = self.monitor.layout
        if lay is None:
            return ROI_ORDER
        return tuple(n for n in ROI_ORDER if getattr(lay, n, None) is not None)

    def _apply_deltas(self):
        base = self._base_layout()
        if base is None:
            return
        try:
            self.monitor.set_layout(apply_deltas(base, self.deltas, self.calib_scale))
        except ValueError:
            self._set_note('ROI 越界（已忽略）')

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
                src.save_current(layout=self.monitor.layout, source='面板校准',
                                note='面板上挪过 / 确认过的 ROI')
            except OSError as exc:
                self._set_note('%s存盘失败：%s' % (what, exc))
                return
            self._set_note('已保存校准（%dx%d 的%s）'
                           % (src.size[0], src.size[1], what))
            return
        base = self._base_layout()
        if base is None:
            self._set_note('没找到窗口，不能保存')
            return
        data = {'client': [base.width, base.height], 'scale': base.scale,
                'deltas': self.deltas,
                'abs': self.monitor.layout.to_dict() if self.monitor.layout else base.to_dict()}
        with open(self.calib_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        self._set_note('已保存校准')

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
            self._set_note('没找到窗口')
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
        self._set_note('ROI 框已显示（鼠标可穿透）')

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
        """清零（C）：停采样 + 数字归零 + 主按钮回到「开始计数」。

        看板批注：「点清零后变成开始」。所以清零不只是把数字按回 0 —— 它还**停止
        计数**、并把手动暂停一起收掉，把那三态拉回第一态。打轴的流程就是
        「清零 -> 开始」。

        为什么还要自己把面板上的数字按回 0：`monitor.reset()` 之后，面板是靠下一拍
        采样读出的读数来刷新大数字的 —— 而「校准完还没点开始 / 已经点了停止 / 录像
        暂停」这几种情况根本没有下一拍，数字就挂着清零前的值（用户报的「清零要等
        开始计数才生效、暂停期间没用」）。计数器那边其实已经归零了，这里只是把面板的
        显示跟上。
        """
        self._reset_count('已清零')

    def _reset_count(self, note=None):
        """停采样 + 归零 + 主按钮回到「开始」（清零那个键和「切换模式」共用）。

        为什么要「切来源就清零」（用户 2026-10-11：「切换模式自动清零」）：
            1. 两个来源的帧号根本不是一个坐标系（PC端读游戏窗口、窗口读一个播放器 /
               录像），接在一起没有意义；
            2. 不清的话大数字会**挂着上一个来源数到的值**，看着像「已经数了 500 帧」，
               而其实这一拍什么都没数。
        语义和「清零」那个键完全一样（停止 + 归零 + 三态拉回第一态），只是提示语不同；
        note=None 表示「别动提示行」（调用方自己还要说别的话，见 _adopt_window）。
        """
        if self.monitor.alive:
            self.monitor.stop()
        self._counting_started = False
        self.monitor.set_manual_pause(False)
        self.monitor.reset()
        self._show_zero()
        self._sync_ctrl_row()
        # 刚停过：看门狗从这一刻重新计时，否则切完模式立刻报一次「采样已停」
        self._last_ticks = self.monitor.stats.ticks
        self._stall_since = 0.0
        if note is not None:
            self._set_note(note, hold=6.0)

    def _set_text(self, key: str, label, text: str, fg=None) -> None:
        """写一个 Label，但**内容没变就一个 Tk 调用都不发**。

        为什么要单独一个函数：面板每 8ms 刷一拍，一拍拍下去原本要发十几二十次
        configure。Tk 每一次 configure 都要过一次 Tcl 解释器再触发布局计算 ——
        实测（cProfile，本机 150% DPI）200 拍里 2321 次 configure 占了 1.95s，
        是整拍耗时里最大的一块。文本没变就跳过之后，这一块直接归零。

        key 由调用方自己起：**同一个 Label 的每一处写入都必须走这里**，否则缓存
        会和界面对不上（现在这些 Label 都只有一两处写入，见各自的调用点）。
        """
        want = (text, fg)
        if self._memo.get(key) == want:
            return
        self._memo[key] = want
        if fg is None:
            label.configure(text=text)
        else:
            label.configure(text=text, fg=fg)

    def _set_warn(self, text: str, color=WARN) -> None:
        """写告警行。**空文本就把这一行整个收起来**（看板批注要「尽量缩小」）。

        为什么非要收：这一行平时是空的，但一个空 Label 照样占着字高（实测 30px，
        加上这一行的上下 8px = 收起态高度的 10%）。真有告警（采样停了 / 校准没把握）
        时再放出来 —— 那种事必须一眼看得见，占一条是值得的。

        收起来要顺手把窗口也收回原高：`_fit_window` 只长不缩（怕文字忽长忽短时抖），
        不主动重算的话告警消失后会剩一条 38px 的空档。只在「有 -> 无」这一下重算，
        而且 0.25s 内最多一次 —— 告警在一秒里反复闪时不至于每 8ms 量一遍布局。
        """
        if text:
            self._set_text('warn', self.lb_warn, text, color)
            if not self.fr_warn.winfo_manager():
                # before=fr_ctrl：pack 是**追加**到顺序末尾的，不指定锚点的话
                # 这条告警会跑到控制行和提示行下面去。
                self.fr_warn.pack(fill='x', padx=10, pady=4, before=self.fr_ctrl)
            return
        self._set_text('warn', self.lb_warn, '', color)
        if self.fr_warn.winfo_manager():
            self.fr_warn.pack_forget()
            now = time.perf_counter()
            if now - self._warn_at >= 0.25:
                self._warn_at = now
                self._refit()

    def _show_zero(self):
        """立刻把大数字 / 时间按回 0（不等下一拍采样）。"""
        self._set_text('frames', self.lb_frames, '0')
        self._sync_rule()
        self._set_text('time', self.lb_time, '时间 00:00.00')

    def toggle_manual_pause(self):
        """手动「暂停计数」：数字按住不动，方便对着游戏/录像逐段核对。

        和游戏自己的暂停无关 —— 那条路一直按费用条算。这里只是让计数器不再往
        total 上加：读数、预览照常刷新，只是数字不动。再按一次恢复（恢复时重新起锚，
        按住的那一段既不会事后补进来、数字也不会跳）。符号画面变了的话是因为这个
        开关只管计数，不管特征提取。
        """
        on = self.monitor.toggle_manual_pause()
        self._sync_ctrl_row()          # 主按钮的字跟着三态走（开始 / 暂停 / 继续）
        self._set_note('已暂停' if on else '继续计数')

    def _on_rate_pick(self, label):
        """下拉框选中某一项。选「自定义」不切档，只把光标丢进输入框。"""
        if label == RATE_CUSTOM_LABEL:
            self.en_rate.focus_set()
            self.en_rate.select_range(0, 'end')
            self._set_note('填「一费几个逻辑帧」再点「应用」')
            # 下拉框回显真实值，免得看上去像已经切过去了
            self.rate_var.set(_rate_label(self._current_rate()))
            return
        v = self._rate_labels.get(label)
        if v is not None:
            self.apply_cost_rate(v)

    def _current_rate(self) -> float:
        """此刻**真正在用**的「一费几个逻辑帧」。

        窗口模式刚起来时 monitor.counter 还是 None（Counter 要等第一次 set_layout 才建），
        而那时真正决定档位的是 monitor.cost_rate（run_monitor 从 cost_rate.json 读出来的
        存盘值）。旧代码这里直接回落到默认的 30，于是存盘值是 39 时面板写着「30 正常」、
        实际却在按 39 帧/费数 —— 用户 2026-10-12 报的「1 费却是 36 帧」就是这么来的
        （实测那段录像：30 帧/费 记 29/29/28/32 帧，39 帧/费 记 37/38/36/42 帧）。
        """
        if self.monitor.counter is not None:
            return self.monitor.counter.frames_per_cost
        rate = getattr(self.monitor, 'cost_rate', None)
        try:
            v = float(rate)
        except (TypeError, ValueError):
            return DEFAULT_FRAMES_PER_COST
        return clamp_frames_per_cost(v) if v > 0 else DEFAULT_FRAMES_PER_COST

    def _sync_rate_row(self):
        """把「回费效率」那一行刷成 _current_rate()。填过一次就不动（每拍都会调）。"""
        want = _rate_label(self._current_rate())
        if self.rate_var.get() != want:
            self.rate_var.set(want)

    def apply_rate_entry(self):
        """「应用」按钮 / 输入框回车：把输入框里的数当成一费逻辑帧数切档。"""
        text = self.en_rate.get().strip()
        try:
            v = float(text)
        except ValueError:
            self._set_note('要填数字（例如 60，-50%% 回费就是 60）：%r' % text)
            return
        if not (v > 0):
            self._set_note('要填正数（例如 60）')
            return
        self.apply_cost_rate(v)

    def apply_cost_rate(self, value):
        """切到某个回费速率：立刻生效（已数的像素帧按比例换算）+ 写回设置文件。"""
        v = clamp_frames_per_cost(value)
        self.monitor.set_cost_rate(v)
        self.rate_var.set(_rate_label(v))
        self.en_rate.delete(0, 'end')
        note = '回费速率 = %s' % format_frames_per_cost(v)
        try:
            save_cost_rate(self.settings_path, v)
            self._set_note('%s（已存盘）' % note)
        except OSError:
            self._set_note('%s（存盘失败）' % note)

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

        只有「校准」那个按钮走这里，真正的活在 LiveMonitor.recalibrate 里：重找窗口、
        按当前客户区重算 ROI、采样线程死了就拉起来。**已经数出来的帧数不动** ——
        它的语义是「重新对准」，不是「清零」（清零有单独那个按钮）。

        什么时候需要它：游戏窗口被拖走/改了大小、换了分辨率，或者采样线程因为某次
        异常停了 —— 那时状态行会红着告诉你（见 _tick_body 的看门狗）。
        """
        if self._src_kind == 'window':
            # 读窗口这条链路：重新抓一遍（自动对齐 + 量条槽）。校准表里有这个
            # (进程名, 客户区尺寸) 的那份就直接套用，几毫秒；没有才跑网格搜索。
            # 数出来的帧数保留（_adopt_window 里把 Counter 接过来），本来在数就接着数。
            note = '窗口：重新校准（几秒钟，帧数保留）'
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
            self._set_note('已经是 PC端')
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
        self.monitor = self.live_monitor
        self._src_kind = 'live'
        self._load_calib()                        # 实时的 ROI 微调在 layout_calib.json 里
        self._apply_deltas()
        # 换来源 = 新计数（用户：「切换模式自动清零」）—— 切过去是**待命**态，
        # 点「开始」才数（和读窗口那条链路一个规矩，两边都一眼看得出没在数）。
        self._reset_count('已切到 PC端（已清零，点「开始」接着数）')
        self.lb_source.configure(text='PC端（实时游戏窗口）', fg=UI_DIM)

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
            self._set_note('正在校准，等它弄完')
            return
        self._window_dialog('读取窗口（校准完点「开始计数」）', self.start_window_calib)

    def start_window_calib(self, win, box=None, force: bool = False, note: str = ''):
        """在后台线程里校准一个窗口。

        box 是用户框选的费用条（**客户区坐标**），给了就走「手动校准」那条路。
        进度写在 _window_stage 里，主线程在 _poll_window 里读（后台线程不碰 Tk）。
        """
        if self._window_busy:
            self._set_note('正在校准，等它弄完')
            return
        self._window_busy = True
        self._window_stage = ('align', -1.0, '准备…')
        self._window_result = None
        # 目标窗口不能被盖住（盖住的那块抓到的是盖它的那个窗口）：选完窗口**就钉**
        # （用户 2026-10-11：「窗口模式置顶删除，默认置顶」）。校准本身也要看得见画面。
        self._pin_window(win)
        self._set_note(note or '正在校准…', hold=0.0)
        threading.Thread(target=self._window_worker, name='akframe-window', daemon=True,
                         args=(win, box, force)).start()

    def _window_worker(self, win, box, force: bool):
        """后台线程：算完把结果放进 _window_result，主线程下一拍取走。

        box 是「手动校准」交回来的东西：一个 Rect（只圈了费用条）或者
        {目标名: Rect}（按钮也是用户自己钉的，见 BoxPicker._apply）。给了它就**不做**
        自动对齐 / 量条槽 / 搜按钮 —— 用户已经把 ROI 指出来了（见 WindowSource）。
        """
        if isinstance(box, dict):
            bar_box, pause_box = box.get('bar'), box.get('pause')
            speed_box = box.get('speed')
        else:
            bar_box, pause_box, speed_box = box, None, None
        if bar_box is not None:
            self._window_progress('buttons', -1.0, '按框选钉住 ROI…')
        try:
            src = WindowSource(win, templates=self.templates,
                               progress=self._window_progress, use_profile=not force,
                               bar_box=bar_box, pause_box=pause_box, speed_box=speed_box)
            note = src.manual_note
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
                self._release_pin()        # 校准没成：把刚钉上去的置顶还回去
                self._set_note('读窗口失败：%s' % exc, hold=8.0, color=ERR)
            else:
                self._adopt_window(src, win, note)
            return False
        if not self._window_busy:
            return False
        stage, frac, note = self._window_stage
        pct = '' if frac < 0 else ' %.0f%%' % (frac * 100.0)
        self._set_text('perf', self.lb_perf,
                       '%s%s  %s' % (WINDOW_STAGES.get(stage, stage), pct,
                                                 note or '…'), fg=UI_DIM)
        self._set_note(note or '正在校准…', hold=0.0)
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
                            templates=self.templates, cost_rate=rate,
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
        if not same:
            # 换窗口 / 从 PC端 切过来 = 一份**全新**的计数（用户：「切换模式自动清零」）。
            # 重新校准同一个窗口则相反：帧数要接着数（见上面 adopt 那一段）。
            self._reset_count()
        self.lb_source.configure(
            text='手动校准：窗口 %s ｜ 客户区 %dx%d ｜ %s ｜ %s'
                 % (win.exe, win.client[2], win.client[3],
                    '自己框的' if src.manual else '自动试出来的',
                    ' ｜ '.join(src.notes[-2:])), fg=UI_FG)
        self._pin_noted = ''
        self._counting_started = mon.alive
        self._update_start_btn()
        self._last_ticks = mon.stats.ticks
        self._stall_since = 0.0
        if was_running:
            mon.start(reset_clock=False)     # 本来在数就接着数（时间基准不能倒退）
        # 校准的结论只用**提示行那一行**说（用户 2026-10-11：「下方的教程删除」）。
        # 细节（src.describe / src.notes）搬进「当前输入」那行读数（fr_diag，不铺出来），
        # 要排查时仍然看得到。
        if not src.verified and not src.manual:
            self._set_note('自动校准没把握（点「手动校准」）', hold=6.0)
        elif was_running:
            self._set_note('已校准（帧数接着数）', hold=4.0)
        else:
            self._set_note('已校准（%s），点「开始」'
                           % ('自己框的' if src.manual else '自动试出来的'), hold=4.0)

    def toggle_count(self):
        """「开始计数」/「停止计数」（主按钮）：采样线程由这一下控制。

        两条链路都走这里。实时链路原来没有「停」的地方（run_monitor 一进来就 start），
        2026-10-10 照用户确认的示意图打通：停下来是真的把采样线程停掉（省 CPU、
        也不再读屏幕），再点就重新起来。

        停止期间画面照样在动，所以**恢复时必须重新起锚**（resync）—— 否则那一段
        没读到的条会被当成「条走了这么多帧」一次性补进计数里，而用户刚说的是别数了。
        已经数出来的帧数一直保留（要归零请按 C）。
        """
        mon = self.monitor
        if mon.alive:
            mon.stop()
            self._set_note('已停止（帧数保留）')
        else:
            # reset_clock=True：停止这一段挂钟不算进来（t 从头来，dt<=0 只当一拍）。
            mon.start()
            if mon.counter is not None:
                mon.counter.resync()
            self._set_note('开始计数')
        self._counting_started = mon.alive
        self._last_ticks = mon.stats.ticks
        self._stall_since = 0.0
        self._update_start_btn()

    def _update_start_btn(self):
        """主按钮上的字（开始计数 / 停止计数 …）—— 统一由 _sync_ctrl_row 算。"""
        self._sync_ctrl_row()

    def manual_calib(self):
        """「手动校准」那个键：两种模式各有一套，按模式分派。

        PC端（实时游戏窗口）：ROI 是按分辨率的实测表**推算**出来的，人工那一步是挪
        格子（微调差值 -> layout_calib.json），所以开二级窗口，见 open_calib_dialog。
        窗口（录屏 / 播放器）：画面被缩放过（实测 VLC 里 2560x1600 缩到 0.4777 倍），
        推出来的框落不到条上，只能**对着参照图圈一遍**（box_bar -> window_calib.json）。

        两条路都有放大镜（用户 2026-10-11：「最好加一个放大镜，放大部分画面方便对齐」）：
        二级窗口里是**当前目标那个 ROI** 的原图放大（切目标就跟着换，见 _render_previews），
        取景器里是框内那一块放大（红线 = 特征真正取样的那几行）。
        两份校准表的键都不一样（分辨率 / 进程名 + 客户区尺寸），所以合不成一件事。
        """
        if self._src_kind == 'window':
            self.box_bar()
        else:
            self.open_calib_dialog()

    def _pin_window(self, win):
        """把目标窗口钉到最上层 —— **默认就这么干**（用户 2026-10-11：
        「窗口模式置顶删除，默认置顶」）。

        为什么敢默认钉：这条链路是抓**屏幕上那块像素**的，目标被盖住一角，压住的
        那块读到的就是别人的画面（费用条在右下角），等于整条链路瞎了。而第 39 轮
        已经把「每 0.7 秒无条件重钉」改成「先复核再动手」（见 _keep_pin）—— 钉上之后
        稳态一个系统调用都不发，所以不会再出现「窗口会闪」。

        钉失败（系统不让）不用在这儿报：_keep_pin 每 0.7 秒复核一次，失败它会说一句话。
        """
        hwnd = int(win.hwnd)
        self._pinned_hwnd = hwnd
        self._pin_at = 0.0                  # 下一拍立刻复核一次
        self._pin_noted = ''
        try:
            ok = set_topmost(hwnd, True)
        except Exception:                      # noqa: BLE001
            ok = False
        if ok:
            self._raise_ours()              # 抬目标会把它压到我们所有窗上面，得抢回来

    def _release_pin(self):
        """把我们钉上去的置顶还回去（窗口已经关了就拉倒）。"""
        if not self._pinned_hwnd:
            return
        try:
            set_topmost(self._pinned_hwnd, False)
        except Exception:                      # noqa: BLE001
            pass
        self._pinned_hwnd = None

    # ---------- 置顶（读窗口那条链路）----------

    def _keep_pin(self, now):
        """**复核**目标窗口的置顶（每 PIN_KEEP_S 秒一次）—— 只在真丢了标记时才动手。

        为什么钉一次不够：这条链路是抓**屏幕上那块像素**的，目标窗口被压住一角，压住的
        那块读到的就是别人的画面（费用条在右下角、按钮在右上角，压住等于整条链路瞎了）。
        而置顶这个状态并不牢靠：播放器切全屏 / 换下一段视频会**重建窗口**，新窗口不带
        WS_EX_TOPMOST 标记。

        2026-10-11 改法（用户：「在读取视频时窗口会闪解决一下」）：先问一句再动手。
        旧写法无条件 set_topmost(HWND_TOPMOST) —— 它除了打标记，还顺手把目标抬到置顶层
        最上面，于是必须紧接着把我们自己的窗全抢回来（_raise_ours）。这一抬一抢每 0.7 秒
        来一次，全屏播放器就会盖住面板一下再让开，用户看到的就是「窗口会闪」。

        现在只有三种情况，且只有第二种会发系统调用：
            标记还在   -> 什么都不做（稳态零闪烁）
            标记丢了   -> 补钉 + 抬我们的窗（这一下会闪，但确实需要，否则目标会被盖住）
            系统不让钉 -> 报一次，让用户自己在播放器里勾「总在最前」

        每拍都被 _tick_body 调（拖面板期间除外），**不**看我们自己的对话框开没开 ——
        那道门是第 39~43 轮的写法，会让置顶在里面开着的时候整段停摆，见 _raise_ours。
        """
        if now - self._pin_at < PIN_KEEP_S:
            return
        self._pin_at = now
        hwnd = self._pinned_hwnd
        if hwnd is None:
            return                          # 没在读窗口：不钉（见 _pin_window）
        # 判据里**不**看 self._window_win：第一次校准那一整段（自动那条路是几十秒）
        # _window_win 还是 None，而那时窗口早就钉上了 —— 按老的写法这段复核是停摆的，
        # 目标在这期间丢了标记就再没人钉（第 44 轮顺手补上）。
        if not is_window(hwnd):
            # 窗口没了（被关掉 / 播放器重建了它）。不自动换一个 —— 换窗口要重新校准、
            # 帧号也得从头数，那是用户的决定；这里只把话说明白。
            self._pinned_hwnd = None
            self._pin_noted = ''
            self._note_pin('目标窗口不见了，重新选一个（校准 → 窗口）')
            return
        try:
            if is_topmost(hwnd):
                # 标记还在：不用动目标（动它就闪）。但**我们自己可能被它压住了** —— 见
                # _raise_if_covered 里的实测：用户点一下播放器，它就跑到置顶层最上面。
                self._raise_if_covered(hwnd)
                return
        except Exception:                   # noqa: BLE001
            pass
        try:
            ok = set_topmost(hwnd, True)
        except Exception:                   # noqa: BLE001
            ok = False
        if ok:
            self._pin_noted = ''
            self._raise_ours()              # 抬目标会把它压到我们所有窗上面，得抢回来
            self._note_pin('置顶丢了，已重新钉上')
            return
        self._note_pin('系统不让置顶（手动勾播放器「总在最前」）')

    def _our_windows(self) -> list:
        """我们自己开着的顶层窗口：面板 + 所有还铺在屏幕上的 Toplevel。

        为什么两个用途共用一个列表（第 44 轮）：_own_dialog_open() 要知道「除了面板还有
        没有别的窗」，_raise_ours() 要挨个把它们抬起来。判据分开写就会账对不上 ——
        一边认为没开对话框、另一边却真有扇窗压着目标。

        Tk 的顶层窗有两块句柄（winfo_id() 给的是里面的 TkChild），要动 Z 序得先换成
        外面那个，见 win32.top_level。
        """
        out = [self.root]
        for w in self.root.winfo_children():
            if isinstance(w, tk.Toplevel):
                try:
                    if w.winfo_exists() and w.winfo_ismapped():
                        out.append(w)
                except tk.TclError:
                    continue
        return out

    def _own_dialog_open(self) -> bool:
        """我们自己有没有开着对话框 / 取景器（= 除了面板还有别的顶层窗）。"""
        return len(self._our_windows()) > 1

    def _raise_ours(self):
        """把我们自己的窗口全抬到目标窗口上面（同一置顶层里，最后抬上来的在最上面）。

        为什么不是只抬面板（第 44 轮改）：钉目标那一下（SetWindowPos 到 HWND_TOPMOST）
        除了打标记，还会把目标抬到**置顶层的最上面** —— 被它压下去的不只是面板，还有
        开着的对话框和取景器。第 39~43 轮的对策是「开着对话框就干脆别去钉」
        （_tick_body 里那道 _own_dialog_open 门），代价是那段时间置顶**整段停摆**：
        只要目标在这期间丢了标记（播放器自己重设窗口 / 换下一段视频 / 切全屏），就再也
        没人把它钉回去，而面板上什么都不会说 —— 用户报的「读取窗口无法保持置顶」就是它。

        现在门拆了，改成「钉完把自己人全抬回来」：既不丢置顶，取景器也照样盖在目标上面。
        抬不起来不算故障：这条链路唯一的硬要求是「目标窗口别被别的窗口盖住」，
        面板压不上去只影响用户看不看得见数字，不该因此把提示行刷红。
        """
        for w in self._our_windows():
            try:
                hwnd = top_level(w.winfo_id())
                if hwnd:
                    raise_window(hwnd)
            except Exception:                  # noqa: BLE001
                pass

    def _raise_if_covered(self, hwnd) -> bool:
        """目标跑到我们某个窗上面了吗？是就把我们自己抬回来（返回有没有抬）。

        为什么光看 WS_EX_TOPMOST 标记不够（第 44 轮的真 bug）：置顶是一**层**，同一层里
        最后被抬起来的在最上面 —— 用户点一下正在读的那个播放器，它就被系统抬到这一层的
        最上面，我们自己的面板 / 取景器当场被压到下面去，而标记还好好地挂着。实测真 VLC：

            钉完        VLC rank=17、面板 rank=13   （面板在上面）
            点一下 VLC   VLC rank=14、面板 rank=17   （面板被压下去）
            之后 189 拍  set_topmost 一次都没发      （标记还在，没人发现）

        所以每 PIN_KEEP_S 秒补一句 z 序的复核。抬的时候只动我们自己（raise_window），
        **不碰目标** —— 碰目标（SetWindowPos HWND_TOPMOST）就会闪一下，那正是第 39 轮
        用户报的「读视频时窗口会闪」。
        """
        try:
            for w in self._our_windows():
                h = top_level(w.winfo_id())
                if h and is_above(hwnd, h):
                    self._raise_ours()
                    return True
        except Exception:                      # noqa: BLE001
            pass
        return False

    def _note_pin(self, text):
        """置顶相关的提示只弹一次：_keep_pin 每 0.7 秒跑一遍，每遍都写会把提示行刷爆。"""
        if text == self._pin_noted:
            return
        self._pin_noted = text
        self._set_note(text, hold=6.0)

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

    def _refresh_roi_warn(self):
        """面板压住了 ROI 就报警（写进「告警：」那一行）。

        这一段原来写在窗口那一行的「教程」里（用户 2026-10-11：「下方的教程删除」）。
        压住 ROI 不是教程 —— 压住的那块像素读到的是**面板自己**，计数会卡在那个帧号上，
        而用户完全看不出为什么（冷启那段「莫名卡死」就是这么来的），所以搬进常驻区唯一
        的告警位（fr_warn：平时整行收起来，有事才铺出来）。

        为什么要记住上一次那条：这个函数**每拍**都会被调。不记的话「没有压住」那一支
        每拍都会去 _set_warn('')，而它只要看到 fr_warn 铺着就 pack_forget —— 采样停了
        的告警是每拍末尾重新铺的，两条一起就是「告警行每拍收一次铺一次」，看着就是闪。
        所以状态没变直接 return；真要收的时候也只收「我们自己那条」。
        """
        hits = self._roi_hits()
        text = ('⚠ 面板压住了「%s」—— 挪开它，压住的那块读到的是面板自己'
                % '」「'.join(hits)) if hits else ''
        if text == self._roi_warn:
            return                          # 状态没变：一个 Tk 调用都不发
        if text:
            self._roi_warn = text
            self._set_warn(text)
            return
        if self.lb_warn.cget('text') == self._roi_warn:
            self._set_warn('')              # 只收回我们这条；别的告警在说话就别碰
        self._roi_warn = ''

    # ---------- 框选费用条 ----------

    def box_bar(self):
        """框选 ROI（读窗口那条链路）：拿**参照图**让用户圈，不是蒙着屏幕凭手感拖。

        为什么换成参照图：窗口里的画面是缩放过、还可能带黑边的（实测 VLC 里
        2560x1600 缩到 0.4777 倍），屏幕上差两个像素就是好几个逻辑帧的相位误差 ——
        用户的原话是「手动截取不准确，需要参照物」。参照图上的刻度尺 + 放大镜（四条边 +
        平移 + 滚轮局部放大）把这一层误差压到 0，用的就是 BoxPicker 那套参照图取景器。

        2026-10-11 第二轮起三个 ROI 都能在这儿框（用户：「暂停键倍速键也会截取不到，
        需要手动校准；最好加一个放大镜，放大部分画面方便对齐」）：自动搜按钮搜不到时，
        暂停 / 倍速那两路只能靠用户自己指，放大镜就是给他对位置用的。
        """
        if self._src_kind != 'window' or self._window_win is None:
            self._set_note('先选一个窗口（校准 → 窗口）')
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
                                 on_note=self._set_note, on_close=self._picker_closed)
        self._set_note('对着参照图圈住 ROI', hold=6.0)

    def _picker_closed(self):
        """取景器关掉了（应用 / 取消 都算）——把引用清掉。

        不清的后果是「手动校准」这个键**再也点不动**（box_bar 开头那句
        `if self._picker is not None: return` 会一直命中），用户报的
        「手动校准点取消后有时会无法再次手动校准」就是它。
        """
        self._picker = None

    def _apply_window_box(self, box):
        """用户框完了（客户区坐标）：按这些框重算三个 ROI，并记进窗口校准表。

        取景器交回来的可能是**一个框**（只圈了费用条，按钮留给后端去自动搜），也可能是
        {目标名: Rect}（费用条 + 用户自己钉的按钮，见 BoxPicker._apply）。
        """
        self._picker = None
        win = self._window_win
        if win is None:
            return
        self._set_note('按这个框重算 ROI（几秒钟）', hold=0.0)
        what = '框了 %d 块' % len(box) if isinstance(box, dict) else '框了 %dx%d' % (box.w, box.h)
        self.start_window_calib(win, box=box, note='按框选校准（%s）' % what)

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
            self._set_note('没解析出轴（格式见 maps/example.md）')
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
        self._set_note('还没有轴（点「导入轴…」）')

    def open_axis_dialog(self):
        """「导入轴…（T）」：弹一个**独立小窗**（看板批注第 6 条）。

        看板原话：「展开后加一个导入按钮 / 点击后打开新窗口，新窗口实现拖入 md 文件
        和直接输入轴」。以前是一个常驻在展开区里的大文本框，把打轴器挤成半屏、而它
        大部分时间是空的；挪进窗口之后展开区第一屏就只剩打轴器。

        「拖进来」要 tkinterdnd2（可选依赖，见文件顶 _HAS_DND）：装了就能把 md 拖
        进文本框；没装就点「选择 md 文件…」，其余功能一模一样。
        """
        if self._axis_win is not None:                 # 已经开着：抬到前面就行
            self._axis_win.deiconify()
            self._axis_win.lift()
            self._axis_win.focus_force()
            return
        top = tk.Toplevel(self.root)
        self._axis_win = top
        top.title('导入 / 编辑轴')
        top.configure(bg=UI_BG)
        # 面板自己是置顶的，这个窗也得置顶，否则一开就被面板压住看不见。
        top.attributes('-topmost', True)
        top.geometry('+%d+%d' % (self.root.winfo_rootx() + 30,
                                 self.root.winfo_rooty() + 70))
        pad = dict(padx=10, pady=6)

        if _HAS_DND:
            hint = '把 md 轴文件拖进来，或在这里直接写（帧号开头，格式和 md 一样）'
        else:
            hint = ('点「选择 md 文件…」，或在这里直接写（帧号开头，格式和 md 一样）'
                    '　｜　想支持把文件拖进来：pip install tkinterdnd2')
        self._label(top, hint, 9, FAINT, anchor='w', justify='left').pack(fill='x', **pad)

        row = tk.Frame(top, bg=UI_BG)
        row.pack(fill='x', **pad)
        for text, cmd in (('选择 md 文件…', self.import_timeline),
                          ('清空', self.clear_axis_text),
                          ('保存为 md', self.save_axis_text),
                          ('应用到提词器', self.apply_axis_text)):
            self._btn(row, text, cmd).pack(side='left', padx=3)

        self.txt_axis = tk.Text(top, height=14, width=54, bg=SUNKEN, fg=UI_FG,
                                relief='flat', insertbackground=UI_FG,
                                selectbackground=CARD_HI, font=(FONT_MONO, 10),
                                wrap='word', undo=True)
        self.txt_axis.pack(fill='both', expand=True, **pad)
        self.txt_axis.insert('1.0', self._axis_text)
        # （Esc / Ctrl+Enter 两个键 2026-10-12 删了：用户要求「把快捷键删掉」，
        #   退出走窗口右上角的 ×，应用走下面那个「应用到提词器」按钮。）
        top.protocol('WM_DELETE_WINDOW', self._close_axis_dialog)
        if _HAS_DND:
            try:
                self.txt_axis.drop_target_register(DND_FILES)
                self.txt_axis.dnd_bind('<<Drop>>', self._axis_drop)
            except Exception:                          # noqa: BLE001  环境缺 DnD 就当没装
                pass
        self.txt_axis.focus_set()

    def _close_axis_dialog(self):
        """关窗口：把窗口里的文本收回缓冲区（关着的时候以缓冲区为准）。"""
        if self._axis_win is None:
            return
        if self.txt_axis is not None:
            self._axis_text = self.txt_axis.get('1.0', 'end')
        self.txt_axis = None
        self._axis_win.destroy()
        self._axis_win = None

    def _axis_read(self) -> str:
        """当前轴文本：窗口开着就取窗口里的，关着就取缓冲区。"""
        if self.txt_axis is not None:
            return self.txt_axis.get('1.0', 'end')
        return self._axis_text

    def _axis_drop(self, event):
        """把拖进来的文件当轴文件读（一次拖多个就取第一个）。"""
        try:
            files = self.txt_axis.tk.splitlist(event.data)
        except Exception:                              # noqa: BLE001
            files = []
        if files:
            self.load_timeline(files[0])
        return getattr(event, 'action', 'copy')

    def apply_axis_text(self):
        """把轴窗口里的文本解析成轴，换掉当前提词用的轴。"""
        tl = parse_lines(self._axis_read().splitlines(), source='(轴窗口)')
        if not tl.cues:
            self._set_note('没解析出轴（例如：120 部署银灰）')
            return
        self._timeline_path = None      # 轴现在来自面板，不再是某个文件
        self._use_timeline(tl)
        note = '已应用面板里的 %d 条轴' % len(tl)
        if tl.warnings:
            note += '  ｜  ' + tl.warnings[0]
        self._set_note(note)

    def save_axis_text(self):
        """把轴窗口里的文本存成 md 文件（存完仍以窗口内容为准）。"""
        text = self._axis_read()
        if not text.strip():
            self._set_note('轴文本是空的')
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='把轴保存成 md 文件', defaultextension='.md',
            initialdir=self._maps_dir(), initialfile='my_axis.md',
            filetypes=[('Markdown 轴文件', '*.md'), ('文本文件', '*.txt')])
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text.rstrip('\n') + '\n')
        self._set_note('已保存到 %s（点「应用到提词器」生效）' % path)

    def clear_axis_text(self):
        """只清空轴文本；已经在跑的提词轴不动。"""
        self._axis_text = ''
        if self.txt_axis is not None:
            self.txt_axis.delete('1.0', 'end')
        self._set_note('轴文本已清空')

    def _maps_dir(self) -> str:
        d = os.path.join(os.getcwd(), 'maps')
        return d if os.path.isdir(d) else os.getcwd()

    def _set_axis_text(self, text: str) -> None:
        """把文本记进缓冲区；轴窗口开着的话，顺手同步到窗口里。"""
        self._axis_text = text
        if self.txt_axis is not None:
            self.txt_axis.delete('1.0', 'end')
            self.txt_axis.insert('1.0', text)

    def _use_timeline(self, tl) -> None:
        """装上一份新轴（不管是 md 来的还是面板文本来的）。"""
        self.prompter.set_timeline(tl)
        self.lb_prom_title.configure(text='打轴器 — %s ｜ 0/%d' % (tl.name, len(tl)))
        self._prom_state = None
        self._draw_scale_hint()
        # （原来这里往「下一条 … 还差 N 帧」那一行写提示，2026-10-11 那行删了。）

    # ---------------- 刻度表 ----------------

    def _draw_scale_hint(self, text='导入一份轴文件，这里会显示按帧排布的刻度表'):
        cv = self.cv_scale
        g = self.scale_geom
        cv.delete('all')
        # 图元被删了，引用要一起清掉 —— 否则 _draw_scale 以为还能复用那个图元，
        # 往一个已经不存在的 id 上贴图会直接抛 TclError。
        self._scale_item = None
        self._scale_photo = None
        cv.create_text(g.width / 2, g.height / 2, text=text, width=g.width - 30,
                       font=(FONT_UI, 10), fill=self.scale_theme.faint)

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
        # 整帧整帧地跳。轴逻辑那边照旧用整数（和离线 CSV 里的绝对帧号是同一个）。
        frame_f = res.elapsed_f if res.mode.startswith('clock') else float(res.elapsed)
        self._prom_state = (frame_f, tl.cues, p.done)
        self._redraw_scale(flash)

        # （「下一条 … 还差 N 帧（≈ x 秒）」那一行 2026-10-11 按用户要求删了 ——
        #   「状态描述全部删掉」。这点信息刻度表上本来就画着：指针下面紧挨着的那条
        #   高亮行就是下一条，指针到它之间那条青柱的长度就是还差多少。）

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

    def _status_text(self, sampling_dead: bool = False) -> str:
        """底部那一行的**状态**（用户 2026-10-11：「下方的状态描述用一行简短的文字，
        比如正在校准。。。；正在计数。。。」）。

        为什么要它：那一行原来只在「刚点过什么」之后才有字，平时空着 —— 而真正
        **现在在干什么**（正在校准 / 正在计数 / 暂停 / 停了）反而没地方看。
        动作反馈说完（hold 到点）就让位给它；状态没变一个 Tk 调用都不发
        （_set_text 有记忆，见 _tick_body 里那一段）。

        sampling_dead 由调用方传进来：那是 _tick_body 的看门狗判出来的，语义是
        「在数，但采样线程不涨了」，这里只是换一句话说。
        """
        if self._window_busy or self._calib:
            return '正在校准…'
        if sampling_dead:
            return '采样已停'
        if not (self.monitor.alive and self._counting_started):
            return '未开始'
        return '已暂停' if self._manual_paused() else '正在计数…'

    def _set_note(self, text: str, hold: float = 3.0, color=None):
        """底部那一行：一句话反馈（用户点完一件事之后）。

        hold > 0：这句话留 `hold` 秒，然后让位给**状态**（见 _status_text）——
        不让位的话它会一直挂在那儿，看起来就像一行甩不掉的状态。
        hold = 0：一直留着，等下一个说话的人来盖。只留给两类：正在跑的工序进度
        （校准期间 _tick_body 提前 return，轮不到状态那一段）和面板自己出错的告警。
        color 留给「必须看一眼」的那几条用（读窗口失败这类），默认是淡灰。
        """
        self._set_text('note', self.lb_note, text, color or UI_DIM)
        self._note_hold = (time.perf_counter() + hold) if hold > 0 else 0.0

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
            self._set_note('面板出错：%s' % exc, hold=0.0)
        finally:
            self.root.after(self.poll_ms, self._tick)

    def _tick_body(self):
        # 面板自己的重绘速率，用来核对「显示跟不跟得上游戏」：
        # 1 倍速下游戏每秒走 30 个逻辑帧，面板必须显著高于 30 Hz 才可能每拍只跳 1 帧。
        # 实测过 60ms 的旧值（16.7 Hz）会每拍跳 2 帧，看着就像在漏帧。
        now = time.perf_counter()
        # 拖动窗口期间只做「轻活」：大数字 / 时间那几行照刷，但**跳过**刻度表重画、
        # ROI 预览、布局重算、置顶刷新。实测（cProfile，本机 150% DPI、展开态）一整拍
        # 16ms —— 其中刻度表的 PIL 重画 6.3ms、预览 1ms、十几次 Tk configure 约 10ms。
        # 不跳过的话事件循环被它塞满，鼠标移动消息只能排队，拖起来就是一顿一顿的
        # （用户报的「窗口拖拽卡顿」）。松手时 _drag_end 会补一次 _refit。
        dragging = self._dragging
        if not dragging:
            self._fit_window(now)
        self._sync_ctrl_row()
        # 读窗口那条链路：目标窗口必须一直在最上面（见 _keep_pin）。
        # 我们自己的对话框开着时**照样**复核（第 44 轮）：以前这里挂一道
        # _own_dialog_open() 的门，门一关置顶整段停摆，目标丢了标记就再也回不来。
        # 「别把取景器压下去」这件事改由 _raise_ours() 负责 —— 钉完把我们也全抬回来。
        if not dragging:
            self._keep_pin(now)
        if not dragging:
            self._refresh_roi_warn()
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
            if not dragging:
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
        # 「用户主动停的」不算故障 —— 实时链路现在也能停（见 toggle_count）。
        idle = not self._counting_started
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
                # 看板批注「帧数计算 2 位但显示一位小数节省空间」：内部仍然按
                # elapsed_f 的**两位**精度算，只是大数字印一位 —— 面板就窄了。
                self._set_text('frames', self.lb_frames, '%.1f' % res.elapsed_f)
            else:
                self._set_text('frames', self.lb_frames, str(res.elapsed))
            self._sync_rule()      # 白线 / 数字那一格：只跟整数位数走
            self._set_text('time', self.lb_time,
                           '时间 %s' % _fmt_time(res.elapsed_f / 30.0))
            # 这里原来有一条十几行的「状态 …」文案链（运行中 / 已暂停 / 起锚 / 刚回费 /
            # 相位重锚 / 不在关卡里 …）。面板那一行 2026-10-09 按用户要求删掉了，文案的
            # 去处：跑动细节看下面「费用条」那行的「通道=」和「模式=」，真出事看「告警：」
            # 那行（res.warn 原样打在那里），采样线程死了看 _tick_body 末尾那条转红的。
            # 读数行 1：费用条 + 通道 + **本费**（照示意图把「本费」从展开区提到常驻区）
            counter = self.monitor.counter
            per = float(counter.frames_per_cost) if counter is not None else LOGICAL_FPS
            into = int(round(res.phase * per))
            self._set_text(
                'bar', self.lb_bar,
                '费用条 fill=%d/%d  %s  通道=%s ｜ 本费 %d/%d 帧'
                % (res.fill, self.monitor.layout.bar_w, res.bar_state, res.mode,
                   into, int(round(per))))
            # 读数行 2：暂停键 + 倍速 + 子弹（原来的「按钮」「倍速」两行并到这里）。
            # 按钮没画出来时**不能**印「已暂停」：那正是「不在关卡里」的样子（见 features）。
            pause_text = ('未画出' if not res.pause_drawn
                          else ('运行中' if res.running else '已暂停'))
            # 子弹时间下倍速数字不在画面上（res.m 是 nan），别印成「倍速 nanX」。
            if res.bullet:
                speed_text = '子弹时间 6 帧/秒'
            elif res.m == res.m:
                speed_text = '倍速 %.0fX' % res.m
            else:
                speed_text = '倍速未画出（沿用上次）'
            # glyph = 「三个三角（>>>）」的形状分：子弹时间就是这么认出来的。
            # 认不出来时先看它离 GLYPH_NCC_MIN(0.55) 差多少（同「减号 run/thr」的路子）。
            # 并成两行之后这里不再印 corr（数字模板的相关系数）—— 它是 conf 的原始值，
            # 一行放不下三组数；要排查就展开「诊断 / 预览」看通道。
            self._set_text(
                'pause', self.lb_pause,
                '%s ｜ %s  conf=%s  digit=%.3f  glyph=%s ｜ ratio=%.3f'
                % (pause_text, speed_text,
                   'ok' if res.corr_2x == res.corr_2x else 'low', res.digit_ratio,
                   '%.3f' % res.glyph if res.glyph == res.glyph else '—',
                   res.pause_solid))
            self._set_warn('告警：' + res.warn if res.warn else '', WARN)
            if not dragging:
                self._update_cycle(res)
        else:
            # 这一拍没读到画面（窗口不在 / 还没进关卡 / 被最小化）：把告警行清空。
            # **必须清**：采样线程刚被「重新校准」救活、但还没读到画面时，告警行上还挂着
            # 红色的「采样已停」，留着的话用户会以为没救活 —— 实测踩到
            # （tools/check_recover.py 第 5 组就是这个场景）。真死了的话，
            # 下面 sampling_dead 那段会把它盖回去。
            self._set_warn('', WARN)
        if not dragging:
            self._update_prompter(upd.result)
        # 「最大间隔」是漏帧的直接指标：采样间隔一旦超过一个逻辑帧的显示时长，
        # 就有可能整帧没被采到（表现：某一拍一次 +2）。门限见上面的 cur_m。
        # 这一行的内容按**来源**分：实时看采样率 / 有没有漏采（门限跟着倍速走，1X 33.3ms、
        # 2X 16.7ms —— 写死 33.3 的话 2 倍速下明明在漏采也不报警）。
        # 文本和「要不要标黄」都由 monitor.perf_line 给，界面只负责显示。
        if not dragging:
            perf_text, jam = self.monitor.perf_line(ui_hz, cur_m)
            self._set_text('perf', self.lb_perf, perf_text, WARN if jam else UI_DIM)
        # （这里原来每拍往提示行刷一句**状态描述**：读窗口时是
        #   「Arknights.exe  2558x1494 @(0,0) ｜ 通道 pixel」，演示里是
        #   「演示：1.0X ｜ 通道 wrap」。2026-10-11 按用户要求删掉 ——
        #   「状态描述全部删掉」。提示行现在只在用户点完一件事之后说一句话，
        #   说完就自己清空（见 _set_note / 下面那一段）。
        #   要排查「这一拍走的哪条通道」，仍然看得到：那行读数在 fr_diag 里
        #   （不铺出来，但 _tick_body 一直在往里写）。）
        # 底部那一行 = **状态**（用户 2026-10-11：「下方的状态描述用一行简短的文字，
        # 比如正在校准。。。；正在计数。。。」）。动作反馈说完（hold 到点）自己让位。
        if self._note_hold and now >= self._note_hold:
            self._note_hold = 0.0
        if not self._note_hold:
            self._set_note(self._status_text(sampling_dead), hold=0.0)

        if not dragging:
            self._render_previews(upd)
            self._sync_calib_pos()
        if self.overlay is not None and not dragging:
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
            self._set_warn('采样已停 %.1fs（%s）→ 点「重新校准」'
                           % (stall_s, why), ERR)
            # 底部那一行**不再**跟着说一遍（用户要「一行简短的文字」）：那一行由
            # _status_text 写「采样已停」，带下一步动作的那条留在告警行里。

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
        self._set_text('cycle', self.lb_cycle, '费用周期 ' + ' ｜ '.join(parts))

    def _render_previews(self, upd):
        """只画**当前校准目标**那一个 ROI 的预览（红框 = 特征真正取样的那条带）。

        为什么跟着目标变（2026-10-11 用户：「暂停键倍速键也会截取不到，需要手动校准；
        最好加一个放大镜，放大部分画面方便对齐」）：以前只有费用条那一张预览，挪按钮的
        时候看不见 ROI 罩没罩住按钮，只能盲挪。现在切到哪个目标就放大显示哪一个 ——
        40 倍放大之后，按钮在不在框里、条带压在按钮的哪一段，一眼就能判。
        判空是必须的：LiveUpdate 只保证「读到画面时」各字段有值，缺一路不该把整个
        _tick 打挂 —— _tick 一崩界面就冻住，报错还只在控制台里（踩过）。
        """
        if self.cv_bar is None:
            return
        img = {'bar': upd.bar_img, 'pause': upd.pause_img,
               'speed': upd.speed_img}.get(self.target)
        if img is None:
            return
        rows, cols = self._preview_marks(img.shape)
        self._show(self.cv_bar, img, mark_rows=rows, mark_cols=cols)

    def _preview_marks(self, shape):
        """预览上那两条红线（和蓝线）画在哪儿 —— 就是特征**真正取值**的那几行 / 几列。

        条带全是按 ROI 尺寸的**占比**定义的（geometry.PAUSE_SHAPE_BAND 等），所以这里
        拿一个同尺寸的空 Rect 换算，和 features.read_pause / read_speed 用的是同一套数。
        """
        h, w = int(shape[0]), int(shape[1])
        if self.target == 'bar':
            return (BAR_BAND_TOP, BAR_BAND_BOTTOM - 1), None
        r = Rect(0, 0, w, h)
        if self.target == 'pause':
            return r.band_rows(*PAUSE_SHAPE_BAND), None
        return r.band_rows(*SPEED_DIGIT_BAND), r.cols_range(*SPEED_DIGIT_COLS)

    def _show(self, canvas, arr, mark_rows=None, mark_cols=None):
        """把 ROI 灰度图**放大**画进固定大小的画布（等比缩放居中，不拉伸）。

        缩放倍数不再是整数：以前 `box_w // w` 一取整就只剩 1 倍 —— 费用条 253px 宽的
        ROI 放进 430 的画布只画 253px，按钮 ROI（200x160）放进 64 高的画布会被**裁掉**。
        现在按「长宽都比得进去」取浮点倍数（上限 8 倍，别让 1x1 的占位框撑爆画布），
        再交给 PIL 最近邻缩放 —— 像素块看着是方的，正好用来对条带。
        红线 = 特征提取真正使用的那条带（费用条是行 5..12；暂停键是形状条带；倍速键是
        数字条带），蓝线 = 倍速数字的横向范围。ROI 偏了一眼就能从预览上看出来。
        """
        h, w = arr.shape
        box_w = int(canvas['width'])
        box_h = int(canvas['height'])
        k = min(8.0, box_w / float(w), box_h / float(h))
        nw, nh = max(1, int(w * k)), max(1, int(h * k))
        ox = (box_w - nw) // 2
        oy = (box_h - nh) // 2
        img = Image.fromarray(arr, mode='L').convert('RGB').resize((nw, nh), Image.NEAREST)
        d = ImageDraw.Draw(img)
        if mark_rows:
            r0, r1 = mark_rows
            d.rectangle([0, int(r0 * k), nw - 1, max(int(r1 * k), int((r1 + 1) * k) - 1)],
                        outline=(230, 60, 60))
        if mark_cols:
            c0, c1 = mark_cols
            d.rectangle([int(c0 * k), 0, max(int(c1 * k), int((c1 + 1) * k) - 1), nh - 1],
                        outline=(60, 160, 255))
        photo = ImageTk.PhotoImage(img)
        self._photos[canvas] = photo
        canvas.delete('all')
        canvas.create_image(ox, oy, anchor='nw', image=photo)

    def run(self):
        self.root.mainloop()
