"""三个 UI 元素的 ROI 计算。

坐标约定：x 向右、y 向下，矩形用 (x, y, w, h) 表示，覆盖 x..x+w-1 与 y..y+h-1。

所有尺寸常数都以「单位尺度 s = 1」（即 1920x1080 的窗口）表示。
目前的数值是把 2560x1600 的实测值除以 s = 1.3333 反推出来的，
**只在一个窗口尺寸上验证过**；换尺寸后必须重新实测校正，不要盲目相信。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

REF_W = 1920
REF_H = 1080

# ---- 单位尺度常数（s = 1 时的像素值）----
BAR_W_UNIT = 180.0                    # 费用条槽宽度（= 一个费用周期的填充行程）
BAR_CENTER_FROM_BOTTOM_UNIT = 268.0   # 条中心距窗口底边
BAR_ROI_H = 17                        # 条 ROI 高度（固定像素，容错 ±1px 足够）
BAR_BAND_TOP = 5                      # ROI 内取第 5..12 行做逐列中位数
BAR_BAND_BOTTOM = 13                  # （左闭右开）

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
ARROW_RATIO_MIN = 0.02       # 箭头带里亮点占比达到这个数，才算「画着箭头」（0.2 倍速）

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
        for name in ('bar', 'pause', 'speed'):
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
        }

    @staticmethod
    def from_dict(d: dict) -> 'Layout':
        return Layout(
            width=d['width'],
            height=d['height'],
            scale=d['scale'],
            bar=Rect.from_dict(d['bar']),
            pause=Rect.from_dict(d['pause']),
            speed=Rect.from_dict(d['speed']),
            bar_width_frac=float(d.get('bar_width_frac') or 0.0),
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


def layout_for(width: int, height: int, ui_scaler: float = UI_SCALER_DEFAULT) -> Layout:
    """按帧（窗口）尺寸推算三个 ROI。

    ui_scaler 是游戏设置里的「UI 缩放」（PC 端能从注册表读到，见 win32.arknights_ui_scaler）。
    默认 1.0，此时结果和以前逐像素一致。
    """
    s = ui_scale(width, height)
    edge = ui_edge_scale(ui_scaler)

    bar_frac = BAR_W_UNIT * s * edge          # 真实条槽宽（保留小数，合成模型要用）
    bar_w = int(round(bar_frac))
    bar_x = width - bar_w
    bar_cy = height - int(round(BAR_CENTER_FROM_BOTTOM_UNIT * s * edge))
    bar = Rect(bar_x, bar_cy - BAR_ROI_H // 2, bar_w, BAR_ROI_H)

    btn_w = int(round(BTN_W_UNIT * s))
    btn_h = int(round(BTN_H_UNIT * s))
    btn_y = int(round(BTN_TOP_UNIT * s * edge))
    pause = Rect(width - int(round(PAUSE_RIGHT_MARGIN_UNIT * s * edge)) - btn_w,
                 btn_y, btn_w, btn_h)
    speed = Rect(width - int(round(SPEED_RIGHT_MARGIN_UNIT * s * edge)) - btn_w,
                 btn_y, btn_w, btn_h)

    lay = Layout(width=width, height=height, scale=s, bar=bar, pause=pause, speed=speed,
                 bar_width_frac=bar_frac)
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
        x0 = max(0, r.x + dx)
        y0 = max(0, r.y + dy)
        x1 = min(width, r.x + dx + r.w)
        y1 = min(height, r.y + dy + r.h)
        return Rect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    out = replace(layout, width=width, height=height,
                              bar=shift(layout.bar), pause=shift(layout.pause),
                              speed=shift(layout.speed))
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
    # ROI 被人工改小时，真实条槽宽按同样比例跟着变（纯平移 dw=0 时不变）
    frac = base.bar_width_frac
    if frac > 0 and base.bar.w > 0:
        frac = frac * (bar.w / float(base.bar.w))
    lay = Layout(width=base.width, height=base.height, scale=base.scale,
                 bar=bar,
                 pause=adj(base.pause, deltas.get('pause', zero)),
                 speed=adj(base.speed, deltas.get('speed', zero)),
                 bar_width_frac=frac)
    lay.validate()
    return lay


def save_layout(path: str, layout: Layout) -> None:
    """把布局存成 JSON（绝对值），供调试和复现用。"""
    import json
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(layout.to_dict(), fh, ensure_ascii=False, indent=2)


def load_layout(path: str) -> Layout:
    import json
    with open(path, encoding='utf-8') as fh:
        return Layout.from_dict(json.load(fh))


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
