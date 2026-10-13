"""三个 UI 元素的 ROI 计算。

坐标约定：x 向右、y 向下，矩形用 (x, y, w, h) 表示，覆盖 x..x+w-1 与 y..y+h-1。

所有尺寸常数都以「单位尺度 s = 1」（即 1920x1080 的窗口）表示。
数值是把 2560x1600 的实测值除以 s = 1.3333 反推出来的。

信任来源分三档，layout_for() 按顺序取：
    1. **实测校准表**（`resolutions_measured.json`，tools/calibrate.py 量出来写进去的）
       —— 表里有的客户区尺寸就是实测值：s = 条槽宽 / 180，另外还可以带条槽的
       竖直锚点与右边缘距，见 layout_for。尺寸不完全一样、但 ui_scale 落在同一档上的
       客户区也会命中（见 measured_for）。
    2. VERIFIED_SIZES：早期手工标定过的尺寸（2560x1600、1152x720）。
    3. 其余尺寸按 ui_scale() 的两条规则**推**：宽高比 >= 16:9 用高度、否则用宽度。
       推得对不对没人验过 —— resolution_status() 会把这一点如实报出来，
       tools/calibrate.py 负责把「推的」变成「实测的」。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

from .paths import app_dir, frozen

REF_W = 1920
REF_H = 1080

# ---- 单位尺度常数（s = 1 时的像素值）----
BAR_W_UNIT = 180.0                    # 费用条槽宽度（= 一个费用周期的填充行程）
BAR_CENTER_FROM_BOTTOM_UNIT = 268.0   # 条中心距窗口底边
BAR_ROI_H = 17                        # 条 ROI 高度（固定像素，容错 ±1px 足够）
BAR_BAND_TOP = 5                      # ROI 内取第 5..12 行做逐列中位数
BAR_BAND_BOTTOM = 13                  # （左闭右开）

# ---- 局内负费（费用数字左边那个减号）的扫描框 ----
# 数值照抄前人 ArknightsCostBarRuler（crates/ruler-core/src/analysis/scanner/cost_bar.rs）。
# 他们是在 1280x720 上量出来的四对边距：右边距 70..42、底边距 208..201，单位是
# 「720p 下的像素」。乘 1.5 就是这里的 s = 1（1920x1080）单位值 —— 因为他们的
# scale = 1.5 x 本模块的 ui_scale()（两条规则的换算系数都是 1.5，见 ui_scale 的说明）。
# 框**不在面板上挪**（没有手动微调那一项）：它只跟客户区尺寸有关（费用数字永远贴在
# 右下角那个位置），所以由 layout_for 按客户区算出来。但它是个**一等 ROI**，画面里
# 有桌面 / 播放器黑边（客户区带原点）时必须跟着一起平移缩放 —— 见 sign_from_bar
# 与 docs 第 31 节：按画面右下角硬算，实测桌面录像里会整块偏 50px 以上，落到
# 「剩余可放置角色」那行文字上，36% 的帧被误读成负费。
SIGN_RIGHT_LEFT_UNIT = 105.0      # 框左边界距**客户区**右边（= 前人 70 x 1.5）
SIGN_RIGHT_RIGHT_UNIT = 63.0      # 框右边界距**客户区**右边（= 前人 42 x 1.5）
SIGN_FROM_BOTTOM_HIGH_UNIT = 312.0    # 框上边界距**客户区**底边（= 前人 208 x 1.5）
SIGN_FROM_BOTTOM_LOW_UNIT = 301.5     # 框下边界距**客户区**底边（= 前人 201 x 1.5）

BTN_W_UNIT = 150.0                    # 右上角按钮宽
BTN_H_UNIT = 120.0                    # 右上角按钮高
BTN_TOP_UNIT = 30.0                   # 按钮顶边距窗口上边
PAUSE_RIGHT_MARGIN_UNIT = 45.0        # 暂停键右边距
SPEED_RIGHT_MARGIN_UNIT = 210.0       # 倍速键右边距

# ---- 按钮内部条带在 ROI 中的占比（实测自 200x160 的 ROI）----
PAUSE_SHAPE_BAND = (38 / 160, 49 / 160)     # 形状判别条带：三角顶端 / 双竖条顶端
# 倍速数字（1X / 2X）。只取上半段：实测数字笔画在 ROI 行 20..68，而子弹时间的三条
# 细箭头在行 52..80，截到 50 就能把两者在垂直方向分开（否则箭头会冒充数字）。
SPEED_DIGIT_BAND = (18 / 160, 50 / 160)
SPEED_ARROW_BAND = (70 / 160, 116 / 160)    # 倍速箭头（> / >> / >>>），备用
SPEED_DIGIT_COLS = (0.30, 0.85)             # 数字的横向范围（占 ROI 宽度比例）

# ---- 判别阈值（都用「占比」而不是像素数，这样与分辨率无关）----
UI_SCALER_DEFAULT = 1.0      # 游戏设置里「UI 缩放」的默认档（1.0 = 不缩小）

RUN_PAUSE_RATIO_MIN = 0.15   # 形状特征占比 >= 此值判为「运行中」，否则「已暂停」
DIGIT_RATIO_MIN = 0.20       # 数字条带亮点占比 >= 此值判为「有倍速数字」
CORR_2X_MIN = 0.60           # 与 2X 模板的相关系数阈值
ARROW_MIN_CONTRAST = 25      # 箭头带最亮处要比中位数亮这么多，才算「真的画了东西」；
                             # HDR 录像里 UI 白只有 122、箭头带 p99 只有 91，绝对阈值全废
ARROW_REL = 0.60             # 「亮点」判据：median + ARROW_REL * (p99 - median)
ARROW_SOLID_MIN = 0.020      # 箭头还得是**实心块**：3x3 腐蚀一遍后剩下的占比。
                             # 为什么不能只看「亮点占比」：实测「根本没画箭头」的画面在
                             # 箭头带里也有 0.028~0.093 的亮点（关卡外的加载 / 选关界面 0.073~0.093，
                             # 桌面 / OBS 窗口 0.028~0.090）—— 细笔画一被相对阈值放大就过 0.02 了，
                             # 于是整段被读成子弹时间（满费挂钟按 6 帧/秒算，整段差 5 倍）。
                             # 实心块只有真箭头有：实测真箭头（22-20-20 共 1000 帧）
                             # p5=0.0310 / 中位 0.0317 / max 0.0642，非箭头 max=0.0113。
                             # 取 0.020：往下离噪声 1.8 倍，往上离真箭头 1.5 倍，两边都留够。
PAUSE_GLYPH_MIN = 0.020      # 暂停键字形占形状带的**实心**占比（3x3 腐蚀一遍后的）。
                             # 两个用处：① 判「这个键到底画出来没有」（features.read_pause）；
                             # ② 关卡判定用它回答「游戏 HUD 还在不在」（state.Counter）。
                             # 实测：关卡内 0.031~0.235（▶ 0.031~0.047，❚❚ 0.22）；
                             # 关卡外（桌面 / OBS 窗口 / 黑屏 / 切场景）0.000~0.0146。

# 暂停键形状占比带滞回：中间那条带里保持上一拍的状态。
# 实测三个录像：已暂停 0.078 / 运行中 0.245，中间带里只是按钮淡入淡出的过渡。
PAUSE_RUN_RATIO = 0.20       # 占比 >= 此值判「运行中」
PAUSE_STOP_RATIO = 0.11      # 占比 <= 此值判「已暂停」
RUN_PAUSE_RATIO_MIN = 0.15   # read_pause 里给界面看的原始判据（不带滞回）


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w - 1

    @property
    def y2(self) -> int:
        return self.y + self.h - 1

    @property
    def pixels(self) -> int:
        return self.w * self.h

    def crop_filter(self) -> str:
        """给 ffmpeg 用的裁剪表达式。

        注意：必须先 format=gray 再 crop。对 yuv420p 原始帧，色度是 2x2 下采样，
        crop 的 x/y/w/h 会被强制取偶（本机实测 h=17 被悄悄改成 16）。转成 gray
        后是单平面，奇数尺寸才不会被改。
        """
        return 'crop=%d:%d:%d:%d' % (self.w, self.h, self.x, self.y)

    def band_rows(self, top_frac: float, bottom_frac: float) -> tuple:
        """把 ROI 高度上的占比区间换算成行号闭区间。"""
        r0 = int(round(self.h * top_frac))
        r1 = max(r0, int(round(self.h * bottom_frac)) - 1)
        return r0, min(r1, self.h - 1)

    def cols_range(self, left_frac: float, right_frac: float) -> tuple:
        c0 = int(round(self.w * left_frac))
        c1 = max(c0, int(round(self.w * right_frac)) - 1)
        return c0, min(c1, self.w - 1)

    def to_dict(self) -> dict:
        return {'x': self.x, 'y': self.y, 'w': self.w, 'h': self.h}

    @staticmethod
    def from_dict(d: dict) -> 'Rect':
        return Rect(d['x'], d['y'], d['w'], d['h'])


def _clamp_box(left: int, top: int, right: int, bottom: int,
               width: int, height: int) -> Rect:
    """把四条边夹进画面，保证至少 1x1。所有 ROI 都必须这么收口：越界的框交给
    win32 / ffmpeg 会被**悄悄裁掉**，读数就对不上了（见 sign_rect 的注释）。"""
    left = min(max(0, left), max(0, width - 1))
    right = min(max(left + 1, right), width)
    top = min(max(0, top), max(0, height - 1))
    bottom = min(max(top + 1, bottom), height)
    return Rect(left, top, right - left, bottom - top)


def sign_rect(width: int, height: int, scale: float, edge: float = 1.0) -> Rect:
    """局内负费减号的扫描框（右下角，费用数字那一行）—— 锚在**客户区**右下角。

    edge 是游戏内「UI 缩放」系数（ui_edge_scale）：它只改 HUD 元素**离锚点的距离**，
    不改变元素本身大小，所以乘在四条边距上、不乘在框的宽高上 —— 和费用条、右上角
    那两个按钮同一个口径。

    这个函数只负责「客户区就是整幅画面」那条路（实时读游戏窗口）。画面里有别的东西
    （桌面录像、播放器窗口里那段被缩放的画面）时客户区带平移，必须用 reframe_layout
    或 layout_from_bar_box 给的那一份，别拿画面底边硬算 —— 实测桌面录像里两者差 50px
    以上，框会整块落到「剩余可放置角色」那一行文字上（那是纯白横杠，会被读成负费）。
    """
    u = float(scale) * float(edge)
    return _clamp_box(int(round(width - SIGN_RIGHT_LEFT_UNIT * u)),
                      int(round(height - SIGN_FROM_BOTTOM_HIGH_UNIT * u)),
                      int(round(width - SIGN_RIGHT_RIGHT_UNIT * u)),
                      int(round(height - SIGN_FROM_BOTTOM_LOW_UNIT * u)),
                      width, height)


def sign_from_bar(bar: Rect, scale: float, edge: float = 1.0,
                  width: int = 0, height: int = 0) -> Rect:
    """从**费用条 ROI** 反推减号框（锚在条槽右端 + 条中心）。

    为什么还要有这一份：减号框和费用条是同一块 HUD 上的两个元素，四条边距锚的都是
    客户区的右边 / 底边（前人 1280x720 的实测值 x1.5 换成这里的单位）：

        条槽右端距客户区右边      1u   -> 客户区右边 = 条槽右端 + 1u
        减号框   距客户区右边  105u..63u
        条中心   距客户区底边    268u   -> 客户区底边 = 条中心 + 268u
        减号框   距客户区底边 312u..301.5u （左闭右开）

    只要费用条 ROI 是对的，条在哪减号框就跟到哪 —— 这两种场合非用它不可：
      * 画面被放大 / 裁剪过（bar_only_layout 只拿到条框，客户区尺寸推不出来）；
      * 读一份**整体平移过**的布局（桌面录像 / 播放器窗口），按画面底边算会偏。
    框一律夹进 (width, height)（给 0 = 不夹）。
    """
    u = float(scale) * float(edge)
    slot_right = float(bar.x + bar.w)
    bar_cy = float(bar.y) + float(bar.h) / 2.0
    left = int(round(slot_right - (SIGN_RIGHT_LEFT_UNIT - 1.0) * u))
    right = int(round(slot_right - (SIGN_RIGHT_RIGHT_UNIT - 1.0) * u))
    top = int(round(bar_cy - (SIGN_FROM_BOTTOM_HIGH_UNIT - BAR_CENTER_FROM_BOTTOM_UNIT) * u))
    bottom = int(round(bar_cy - (SIGN_FROM_BOTTOM_LOW_UNIT - BAR_CENTER_FROM_BOTTOM_UNIT) * u))
    if width > 0 and height > 0:
        return _clamp_box(left, top, right, bottom, width, height)
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    scale: float
    bar: Rect
    pause: Rect
    speed: Rect
    # 真实条槽宽（亚像素、未被裁）。0 表示未知，按 bar.w 处理（兼容旧缓存/旧校准文件）。
    bar_width_frac: float = 0.0
    # 画面里到底有没有右上角那两个按钮。截屏 / 放大 / 裁剪过的录像只截到右下角一块时
    # 设 False —— 这时暂停 / 倍速是「画面上没这个东西」，不是「读数为 0」，两者不能混。
    buttons_visible: bool = True
    # 局内负费减号的扫描框（见 features.read_sign）。**是个一等 ROI，不是派生量**：
    # 画面里客户区带平移时必须跟着一起平移（reframe_layout / layout_from_bar_box），
    # 按画面右下角硬算的话，桌面录像 / 播放器窗口里会整块偏 50px 以上。给 None 时按
    # 「客户区 = 整幅画面」兜底（实时读游戏窗口那条路就是这样）。
    sign: 'Rect' = None

    def __post_init__(self):
        """sign 留空（None）时按「客户区 = 整幅画面」补一个。

        为什么用 None 兜底、而不是做成 property：property 每次读都按当前 width /
        height 重算，于是 reframe_layout / layout_from_bar_box 平移过的框会被丢掉
        —— 而那正是减号框最需要跟着走的时候。做成字段之后，replace() 会原样带过去
        （dataclasses.replace 复制每个字段，__post_init__ 见到非 None 就不动它）。
        """
        if self.sign is None:
            object.__setattr__(self, 'sign',
                               sign_rect(self.width, self.height, self.scale))

    @property
    def bar_w(self) -> int:
        """条槽宽度 = 一个费用周期的填充行程（像素）。"""
        return self.bar.w

    @property
    def bar_width_f(self) -> float:
        """真实条槽宽（亚像素、未被裁）。

        合成模型（synthesis.py）必须用它算条长：bar.w 是取过整、还可能被屏幕裁掉的
        ROI 宽度，用它当条长会让每个周期差几帧（实时链路实测过「一费只记 28 帧」）。
        bar_width_frac 为 0（旧缓存文件）时回退到 bar.w。
        """
        return self.bar_width_frac if self.bar_width_frac > 0 else float(self.bar.w)

    @property
    def model_bar_w(self) -> int:
        """合成模型里的条槽宽（整数版）。ROI 被裁时它比 bar.w 大。"""
        return int(round(self.bar_width_f))

    @property
    def px_per_frame(self) -> float:
        """一个逻辑帧对应多少像素。核心常数：一个费用周期恒为 30 逻辑帧。

        用真实条宽（bar_width_f）而不是 ROI 宽：ROI 被屏幕裁掉时 bar.w 会小几像素，
        拿它当分母会把「每逻辑帧几像素」算小，报出来的数就不对了。
        """
        return self.bar_width_f / 30.0

    @property
    def bar_band(self) -> tuple:
        return (BAR_BAND_TOP, BAR_BAND_BOTTOM - 1)

    def validate(self) -> None:
        """ROI 不能越界。

        ffmpeg 的 crop 在 x+w 超出帧宽时会静默左移 x（本机踩过：2200 被改成 2160），
        越界不会报错、只会让数据对不上，所以这里必须硬断言。
        """
        for name in ('bar', 'pause', 'speed', 'sign'):
            r = getattr(self, name)
            if r.x < 0 or r.y < 0 or r.x2 >= self.width or r.y2 >= self.height:
                raise ValueError(
                    'ROI 越界：%s = (x=%d, y=%d, w=%d, h=%d)，帧尺寸 %dx%d'
                    % (name, r.x, r.y, r.w, r.h, self.width, self.height))

    def to_dict(self) -> dict:
        return {
            'width': self.width,
            'height': self.height,
            'scale': self.scale,
            'bar': self.bar.to_dict(),
            'pause': self.pause.to_dict(),
            'speed': self.speed.to_dict(),
            'bar_width_frac': self.bar_width_frac,
            'buttons_visible': self.buttons_visible,
            'sign': self.sign.to_dict(),
        }

    @staticmethod
    def from_dict(d: dict) -> 'Layout':
        """读一份布局。sign 缺失（旧校准文件 / 旧缓存）时按**费用条**反推。

        为什么不退回「按画面右下角算」：那份 dict 里的 bar 可能已经被平移过
        （录像 / 播放器窗口），客户区底边在哪只有条自己知道 —— 按条反推至少和条
        是自洽的（见 sign_from_bar），实测比按画面底边算准 50px 以上。
        """
        bar = Rect.from_dict(d['bar'])
        scale = float(d['scale'])
        sign = None
        raw = d.get('sign')
        if isinstance(raw, dict):
            try:
                sign = Rect.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                sign = None
        if sign is None:
            sign = sign_from_bar(bar, scale)
        return Layout(
            width=d['width'],
            height=d['height'],
            scale=scale,
            bar=bar,
            pause=Rect.from_dict(d['pause']),
            speed=Rect.from_dict(d['speed']),
            bar_width_frac=float(d.get('bar_width_frac') or 0.0),
            buttons_visible=bool(d.get('buttons_visible', True)),
            sign=sign,
        )


def ui_scale(width: int, height: int) -> float:
    """UI 缩放系数。宽高比 >= 16:9 时高度是限制边，否则宽度是限制边。"""
    if width <= 0 or height <= 0:
        raise ValueError('帧尺寸非法：%dx%d' % (width, height))
    if width / height >= 16 / 9:
        return height / REF_H
    return width / REF_W


def ui_edge_scale(ui_scaler: float) -> float:
    """游戏内「UI 缩放」对「离右边/下边多远」的系数。

    游戏设置里的 UI 缩放（uiScaler，0..1）只改 HUD 元素**离锚点的距离**，不改元素本身
    大小 —— 所以它只能乘在「距右/距底」上，不能乘在长宽上（乘了就把条本身也缩放错了）。
    系数取自前人项目 ArknightsCostBarRuler（roi.rs）在同款引擎上标出来的结论：
        edge_scale = 0.9 + 0.1 * uiScaler
    uiScaler = 1.0（默认档）时等于 1.0，即完全不影响下面的坐标。
    """
    if ui_scaler != ui_scaler:            # nan
        ui_scaler = UI_SCALER_DEFAULT
    if ui_scaler < 0.0:
        ui_scaler = 0.0
    elif ui_scaler > 1.0:
        ui_scaler = 1.0
    return 0.9 + 0.1 * ui_scaler


# ---- 实测校准表（换分辨率的入口）----
#
# tools/calibrate.py 拿一段录像量出条槽的真实几何之后写到这里。layout_for() 优先用它：
# 表里有的尺寸就是**实测值**，没有的才退回 ui_scale() 的推算值。
#
# 一条记录长这样（只有 scale 是必填的，其余是从同一次测量里顺手算出来的）：
#     "2560x1600": {
#         "scale": 1.3323,          # 实测条槽宽 / 180 / 记录时的 UI 缩放系数
#         "edge": 1.0,              # 记录时的 UI 缩放系数（游戏设置里的「UI 缩放」）
#         "bar_cy_unit": 268.0,     # 条槽中心距客户区底边 / (scale * edge)，可省
#         "right_margin_unit": 0.0, # 条槽右边缘距客户区右边 / (scale * edge)，可省
#         "bar_w": 239.8,           # 实测条槽宽（人看的，不参与计算）
#         "source": "录像 2026-10-04 15-54-49.mp4",
#         "date": "2026-10-06",
#     }
def measured_path() -> str:
    """实测校准表的路径（读和写都用它）。

    优先级：
        1. AKFRAME_RESOLUTIONS 环境变量指定的文件；
        2. **程序目录旁边**的同名文件（打包成 exe 后 = exe 旁边，用户自己量的结果放这儿）；
        3. 随代码发的那份（src/akframe/resolutions_measured.json）。

    为什么要第 2 条：打包成 exe 之后第 3 条那份在 PyInstaller 的解包目录里 —— 只读、
    每次启动都可能换个临时目录，而且实测数据本来是**跟着机器/分辨率走**的，应该能放在
    看得见摸得着的地方。
    """
    env = os.environ.get('AKFRAME_RESOLUTIONS')
    if env:
        return env
    if frozen():
        local = os.path.join(app_dir(), 'resolutions_measured.json')
        if os.path.isfile(local):
            return local
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'resolutions_measured.json')


_measured_cache = None


def measured_layouts() -> dict:
    """读实测校准表。读不到（没这个文件 / 坏了）就返回空表，不影响其它功能。"""
    global _measured_cache
    if _measured_cache is None:
        path = measured_path()
        data = {}
        try:
            with open(path, encoding='utf-8') as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items()
                        if isinstance(v, dict) and float(v.get('scale') or 0) > 0}
        except (OSError, ValueError, TypeError):
            data = {}
        _measured_cache = data
    return _measured_cache


MEASURED_MATCH_TOL = 0.005   # 「同一档缩放」的容差：ui_scale 相对差在这个范围内就算一档


def measured_for(width: int, height: int):
    """按客户区尺寸查实测表，返回 (记录, 'exact' / 'scale')；没有返回 (None, None)。

    先找尺寸完全一样的；找不到再找**同一档缩放**的 —— 表里的锚点（距底 / 距右）本来
    就是以 scale 为单位存的，ui_scale 相同的客户区直接用没问题。实测 2558x1440 和
    2558x1494 的 ui_scale 都是 1.3323（取宽度那一条），量其中一个就够用两个。
    """
    table = measured_layouts()
    key = '%dx%d' % (int(width), int(height))
    if key in table:
        return table[key], 'exact'
    want = ui_scale(width, height)
    best = None
    for k, rec in table.items():
        s = float(rec.get('scale') or 0)
        if s <= 0:
            continue
        d = abs(s / want - 1.0)
        if d <= MEASURED_MATCH_TOL and (best is None or d < best[0]):
            best = (d, rec)
    if best is None:
        return None, None
    return best[1], 'scale'


def reload_measured() -> dict:
    """丢掉缓存重读一次（工具写完之后调，省得重启进程）。"""
    global _measured_cache
    _measured_cache = None
    return measured_layouts()


def save_measured(key: str, rec: dict, path: str = None):
    """把一个客户区尺寸的实测结果写进校准表（合并，不覆盖别人的条目）。"""
    path = path or measured_path()
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[key] = rec
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    os.replace(tmp, path)
    return reload_measured()


def layout_for(width: int, height: int, ui_scaler: float = UI_SCALER_DEFAULT) -> Layout:
    """按帧（窗口）尺寸给出三个 ROI。

    ui_scaler 是游戏设置里的「UI 缩放」（PC 端能从注册表读到，见 win32.arknights_ui_scaler）。
    默认 1.0。实测校准表里有这个客户区尺寸时，用实测的 scale / 锚点，不再按规则推。
    """
    s = ui_scale(width, height)
    edge = ui_edge_scale(ui_scaler)
    rec, _how = measured_for(width, height)
    cy_unit = BAR_CENTER_FROM_BOTTOM_UNIT
    right_unit = 0.0
    if rec:
        s = float(rec['scale'])
        # 记录时的 UI 缩放系数可能和现在不同（记录时 edge 折进 s 里了），
        # 按比例换算回来：只有「距边」的那些量跟着它变。
        edge = edge / (float(rec.get('edge') or 1.0) or 1.0)
        cy_unit = float(rec.get('bar_cy_unit') or BAR_CENTER_FROM_BOTTOM_UNIT)
        right_unit = float(rec.get('right_margin_unit') or 0.0)

    bar_frac = BAR_W_UNIT * s * edge          # 真实条槽宽（保留小数，合成模型要用）
    bar_w = int(round(bar_frac))
    bar_x = width - int(round(right_unit * s * edge)) - bar_w
    bar_cy = height - int(round(cy_unit * s * edge))
    bar = Rect(bar_x, bar_cy - BAR_ROI_H // 2, bar_w, BAR_ROI_H)

    btn_w = int(round(BTN_W_UNIT * s))
    btn_h = int(round(BTN_H_UNIT * s))
    btn_y = int(round(BTN_TOP_UNIT * s * edge))
    pause = Rect(width - int(round(PAUSE_RIGHT_MARGIN_UNIT * s * edge)) - btn_w,
                 btn_y, btn_w, btn_h)
    speed = Rect(width - int(round(SPEED_RIGHT_MARGIN_UNIT * s * edge)) - btn_w,
                 btn_y, btn_w, btn_h)

    # sign 锚的是**客户区**右下角（这条路 width/height 就是客户区），所以直接算；
    # 画面里带桌面 / 窗口边框时由 reframe_layout 或 layout_from_bar_box 平移过去。
    lay = Layout(width=width, height=height, scale=s, bar=bar, pause=pause, speed=speed,
                 bar_width_frac=bar_frac, sign=sign_rect(width, height, s, edge))
    lay.validate()
    return lay


def layout_from_bar_box(client_size, box: Rect, ui_scaler: float = None) -> tuple:
    """用户框选的费用条 -> (客户区, 原点, Layout)。「手动框选」那条路共用这一份。

    为什么窗口这条路必须有手动：播放器窗口里画面是缩放过、还可能带黑边的，客户区尺寸和
    原点都推不出来（实测：2560x1600 的录屏放进 1378x844 的 VLC 窗口，画面缩到 0.4777 倍，
    游戏客户区落在窗口里 (85,50) 的 1218x688）。而用户一眼就能指出费用条在哪 —— 让他框
    一下，比在任何搜索空间里瞎猜都靠谱。

    只有一条硬关系需要用户给准：**条槽宽 = 180 * s * edge**（s = 显示出来的客户区的缩放）。
    所以框一定要贴住费用条的**两端**（框宽 = 条槽宽）；高度随便（只取中心线，ROI 固定
    17 行照旧）。反推出 s 之后客户区 = 1920s x 1080s，三个 ROI 照 geometry 的规则推，
    原点由框的位置定死。

    ui_scaler 是游戏设置里的「UI 缩放」：窗口里读不到注册表（那不是本机在跑的游戏），
    默认 1.0。
    """
    w = int(client_size[0])
    h = int(client_size[1])
    box = Rect(int(box.x), int(box.y), max(2, int(box.w)), max(2, int(box.h)))
    scaler = 1.0 if ui_scaler is None else float(ui_scaler)
    if box.x < 0 or box.y < 0 or box.x + box.w > w or box.y + box.h > h:
        raise ValueError('框跑到窗口外面了：%s（窗口 %dx%d）' % (box, w, h))
    edge = ui_edge_scale(scaler)
    # 条槽右端被窗口切掉时，框量到的只是「看得见的那一段」—— 真宽按框宽算
    # （和录像链路量到被裁的条槽时同一个取舍）。
    s = box.w / (BAR_W_UNIT * edge)
    cw = max(320, int(round(REF_W * s)))
    ch = max(240, int(round(REF_H * s)))
    base = layout_for(cw, ch, scaler)
    dx = box.x - base.bar.x
    dy = (box.y + box.h // 2) - (base.bar.y + base.bar.h // 2)
    layout = reframe_layout(base, dx, dy, w, h)
    # 费用条 ROI 直接按框钉死（1px 都不差）：左边缘用框的、宽用框宽、高照旧 17 行。
    bar_w = min(box.w, w - box.x)
    roi_y = max(0, min(box.y + box.h // 2 - BAR_ROI_H // 2, h - BAR_ROI_H))
    bar_roi = Rect(box.x, roi_y, bar_w, BAR_ROI_H)
    # 减号框按**条**反推（sign_from_bar），不用上面 reframe_layout 平移过来的那一份：
    # 那一份的横向位置锚在「客户区右边」，而录像里唯一实测到的只有这条框 —— 客户区的
    # 宽（1920s）和原点都是从框宽 / 条槽右端距边**推**出来的，带着校准表的误差。
    # 实测 2560x1600 的桌面负费录像（client 2560x1440 @(0,54)、条槽 240px）里那一份
    # 偏右 9px（= 减号宽度的 20%），框只剩 33px 压在减号上（阈值 28）；按条推是 41px。
    layout = replace(layout, bar=bar_roi, scale=s, bar_width_frac=float(box.w),
                     sign=sign_from_bar(bar_roi, s, edge, w, h))
    layout.validate()
    return (cw, ch), (int(dx), int(dy)), layout



def bar_only_layout(width: int, height: int, bar: Rect) -> Layout:
    """只有费用条、没有右上角按钮的布局（画面被放大 / 裁剪时用）。

    什么时候需要它：录像只截到屏幕右下角一块 —— 实测 2026-10-04 18-19-44.mp4 是整屏
    被放大 1.75 倍的裁剪画面（条槽 419px 而正常是 240px、条高 15px 而正常 8px），
    客户区原点、客户区尺寸都推不出来。但费用条本身完整地在画面里，而它一个就够用：
    相位读数全部来自条，「30 逻辑帧 = 一个费用周期」这个换算不需要屏幕几何。

    所以这里的取舍是**明确的**：
        bar        用调用方量出来的条槽；ROI 上下取 BAR_ROI_H 行，让 read_bar 固定的
                   判别条带（ROI 第 5..12 行）压在条身上；
        pause/speed 留 1x1 占位 + buttons_visible=False，读的时候直接返回「读不到」。
    代价写在 cli 的提示里：满费（条变灰底、没有相位）期间按暂停键，挂钟会多走。
    """
    roi_y = bar.y + bar.h // 2 - BAR_ROI_H // 2
    roi_y = max(0, min(int(roi_y), int(height) - BAR_ROI_H))
    placeholder = Rect(0, 0, 1, 1)
    # 减号框按**条**反推（客户区尺寸 / 原点都推不出来，只有条是实测的）：
    # 框在条中心往上 45u、条槽右端往左 104u..62u，见 sign_from_bar。
    lay = Layout(width=int(width), height=int(height), scale=bar.w / BAR_W_UNIT,
                 bar=Rect(int(bar.x), roi_y, int(bar.w), BAR_ROI_H),
                 pause=placeholder, speed=placeholder,
                 bar_width_frac=float(bar.w), buttons_visible=False,
                 sign=sign_from_bar(bar, bar.w / BAR_W_UNIT, 1.0,
                                    int(width), int(height)))
    lay.validate()
    return lay


# ---- 实时链路用到的补充工具：外接矩形、校准偏移、存档 ----

def reframe_layout(layout: Layout, dx: int, dy: int, width: int, height: int) -> Layout:
    """把三个 ROI 整体平移 (dx, dy)，并把画面尺寸换成 width x height，越界处裁掉。

    用途：录屏里含桌面或窗口边框时，客户区原点不在视频原点 ——
    先按客户区尺寸用 layout_for() 算出 ROI，再整体平移到视频坐标（dx, dy 就是客户区
    原点在视频里的位置），最后按视频尺寸裁掉被录屏截掉的部分。

    裁过之后 bar.w 会比真实条槽窄，这正是 Counter(visible_w=...) 存在的理由：
    合成模型按真实宽度算，画出来的宽度截到看得见的那一段。
    """
    def shift(r: Rect) -> Rect:
        # 必须夹得干干净净：客户区原点可能整个落在画面外（播放器窗口里那段画面又小又有
        # 黑边），那时某个 ROI 会一个像素都落不进来。留一个越界的空框比留一个画内的空框
        # 糟得多 —— validate() 会直接拦下（这才是对的），而 win32 / ffmpeg 拿到越界框
        # 只会悄悄裁掉。左 / 上先夹到 width-1 / height-1，再算右 / 下，保证永远 x2 < width。
        x0 = min(max(0, r.x + dx), max(0, width - 1))
        y0 = min(max(0, r.y + dy), max(0, height - 1))
        x1 = min(width, max(x0, r.x + dx + r.w))
        y1 = min(height, max(y0, r.y + dy + r.h))
        return Rect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    out = replace(layout, width=width, height=height,
                              bar=shift(layout.bar), pause=shift(layout.pause),
                              speed=shift(layout.speed), sign=shift(layout.sign))
    out.validate()
    return out


def union_rect(rects) -> Rect:
    """若干 ROI 的外接矩形。实时抓屏时一次抓它，再在内存里切出各个 ROI。"""
    x0 = min(r.x for r in rects)
    y0 = min(r.y for r in rects)
    x1 = max(r.x2 for r in rects)
    y1 = max(r.y2 for r in rects)
    return Rect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def apply_deltas(base: Layout, deltas: dict, src_scale: float | None = None) -> Layout:
    """在默认布局上叠加人工校准的偏移量。

    deltas = {'bar': (dx, dy, dw, dh), 'pause': ..., 'speed': ...}，单位是设备像素。
    src_scale 是记录这些偏移时的 scale：换窗口尺寸后按比例换算，这样校准结果
    在大窗口上量、小窗口上依然大致可用（UI 是按比例缩放的）。

    dw/dh 允许为负（缩小 ROI），最小保留 4 像素。
    """
    k = 1.0 if not src_scale or src_scale <= 0 else base.scale / src_scale

    def adj(r: Rect, d) -> Rect:
        dx, dy, dw, dh = [int(round(v * k)) for v in d]
        return Rect(r.x + dx, r.y + dy, max(4, r.w + dw), max(4, r.h + dh))

    zero = (0, 0, 0, 0)
    bar = adj(base.bar, deltas.get('bar', zero))
    # 面板上按方向键把费用条挪几像素时，减号框跟着挪同样的量 —— 两个元素在同一块 HUD 上，
    # 条挪了框不挪就等于把「框对准」这件事又交回给用户。
    dxb, dyb = [int(round(v * k)) for v in deltas.get('bar', zero)][:2]
    sign = _clamp_box(base.sign.x + dxb, base.sign.y + dyb,
                      base.sign.x + dxb + base.sign.w, base.sign.y + dyb + base.sign.h,
                      base.width, base.height)
    # ROI 被人工改小时，真实条槽宽按同样比例跟着变（纯平移 dw=0 时不变）
    frac = base.bar_width_frac
    if frac > 0 and base.bar.w > 0:
        frac = frac * (bar.w / float(base.bar.w))
    lay = Layout(width=base.width, height=base.height, scale=base.scale,
                 bar=bar,
                 pause=adj(base.pause, deltas.get('pause', zero)),
                 speed=adj(base.speed, deltas.get('speed', zero)),
                 bar_width_frac=frac, sign=sign)
    lay.validate()
    return lay


def load_calibration(path: str):
    """读前端保存的校准文件，返回 (deltas, 记录时的 scale)。

    deltas = {'bar': (dx, dy, dw, dh), ...}，单位是记录时的设备像素；换窗口尺寸后
    由 apply_deltas 按 scale 比例换算。文件不存在或损坏返回 (None, None)。
    """
    import json
    import os
    if not path or not os.path.isfile(path):
        return (None, None)
    try:
        with open(path, encoding='utf-8') as fh:
            d = json.load(fh)
        deltas = {k: tuple(v) for k, v in d['deltas'].items()}
        scale = float(d.get('scale') or 0) or None
        return (deltas, scale)
    except (OSError, ValueError, KeyError, TypeError):
        return (None, None)


# ---- 常见分辨率表 ----
#
# layout_for() 对任意尺寸都能算出一套 ROI，但「算得出」不等于「算得对」：
# 单位常数只在 2560x1600 上实测标定过，其它尺寸都是按 ui_scale() 的规则推的。
# 这里把「哪些尺寸可以直接信、哪些必须先实测校准」写进代码，配合 resolution_status()
# 让前端和 CLI 能明确提示，而不是静默给出错误读数。
#
# verified  在真实游戏窗口上实测标定过
# safe      16:9：ui_scale() 的两条规则给出同一个 s，纯粹等比缩放，可以放心用
# risky     非 16:9：缩放/锚定规则是推的，换机器前必须先实测

VERIFIED_SIZES = ((2560, 1600), (1152, 720))

COMMON_RESOLUTIONS = (
    (1280, 720, '16:9'),
    (1366, 768, '16:9'),
    (1600, 900, '16:9'),
    (1920, 1080, '16:9'),
    (2560, 1440, '16:9'),
    (3840, 2160, '16:9'),
    (2560, 1600, '16:10'),
    (1920, 1200, '16:10'),
    (1680, 1050, '16:10'),
    (2560, 1080, '21:9'),
    (3440, 1440, '21:9'),
)

ASPECT_TOL = 0.01                 # 宽高比容差：和 16:9 的差在这个范围内就算纯等比

# 低于这个客户区尺寸就别指望了：条槽只剩 90px、每逻辑帧 3px，
# 再小的话条边缘 1px 的抖动就等于 1/3 个逻辑帧。
MIN_CLIENT_W = 960
MIN_CLIENT_H = 540


def resolution_status(width: int, height: int) -> tuple:
    """这个客户区尺寸的布局可不可信。返回 (status, 给人看的说明)。"""
    rec, how = measured_for(width, height)
    if rec:
        src = rec.get('source') or 'tools/calibrate.py'
        return ('measured', '实测校准表：条槽 %.1fpx（%s%s）'
                % (float(rec.get('bar_w') or 0), src,
                   '，同缩放档' if how == 'scale' else ''))
    if (width, height) in VERIFIED_SIZES:
        return ('verified', '实测标定过')
    ar = width / height
    if abs(ar - REF_W / REF_H) <= ASPECT_TOL:
        return ('safe', '16:9 等比缩放，两条规则一致')
    if abs(ar - 16 / 10) <= ASPECT_TOL:
        return ('likely', '16:10 的「按宽度缩放」已被 2560x1600 实测确定')
    if ar < REF_W / REF_H:
        return ('risky', '比 16:9 高且不是 16:10：按宽度缩放是推的')
    return ('risky', '比 16:9 宽：超宽屏的缩放/锚定规则还没验证过')
