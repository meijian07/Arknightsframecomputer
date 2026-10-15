"""打轴器的「温度计刻度表」：把轴点按帧等比例摆到一条竖直刻度上。

为什么单独一个模块：这一层是纯数学（帧号 -> 像素坐标），完全不碰 tkinter。
单独放就能在没有界面的情况下跑检查和渲染预览图（tools/check_scale.py、
tools/preview_scale.py）—— 而界面恰恰是最难自动验证的那部分。
界面那边只负责「照着这个布局画」。

坐标系：y 向下为正（和屏幕一致），刻度区左上角是 (0, 0)：

    y(f) = pointer_y + (f - frame) * px_per_frame

frame 往上涨 -> 同一个 f 的 y 变小 -> 整条刻度**向上滚**（提词器：走过去的往上走）。
「现在」那条线（pointer）钉在刻度区**正中**（pointer_ratio = 0.5）不动，于是：

    线的**上方** = 已经过去的轴（越往上越久远，颜色越暗）
    线的**下方** = 还没到的轴（紧贴着线的那一条就是「下一条」，也是唯一高亮的那条）

2026-10-11 之前是反的（pointer 在 70% 处、刻度往下滚）—— 用户在看板批注里指出：
「后一个操作高亮不是前一个；现在针一直居中，操作向上滚动」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .timeline import Cue

# 小刻度的候选间隔（帧）。这是明日方舟的语境：30 帧 = 1 费 = 1 倍速下 1 秒。
TICK_CANDIDATES = (1, 2, 5, 10, 15, 30)
MIN_TICK_PX = 4.0      # 相邻小刻度至少隔这么多像素，否则糊成一片
COST_FRAMES = 30       # 大刻度间隔：30 帧 = 1 费 = 1 秒


@dataclass
class Theme:
    """刻度表的配色。要换风格只动这里，画的地方不用改。

    这一套是照游戏内 HUD 抄的：**只有白和灰两档**，靠线的粗细和明暗分层，不引入第二种
    强调色。「现在」线和青柱（现在 -> 下一条）用纯白；「下一条」整行**反色**
    （白底深字）—— 就是游戏里到帧那一下 flash 的做法，靠反色而不是靠颜色抓眼睛。
    """

    bg: str = '#0b0b0e'
    ruler_bg: str = '#0d0d0f'
    stroke: str = '#2a2a2e'        # 刻度区四边那 1px 描边（2026-10-13，见 53 节）
    rail: str = '#3a3a3e'          # 刻度条右边缘那条竖线（2026-10-11 起不画了，留着不删）
    tick: str = '#4a4a4f'
    tick_major: str = '#8f8f95'
    num: str = '#9b9ba0'
    accent: str = '#ffffff'        # 「现在」线 / 青柱 / 下一条
    text: str = '#ffffff'
    dim: str = '#b9b9c0'
    faint: str = '#8f8f95'         # 已经过去的轴
    mark_next: str = '#ffffff'
    mark_future: str = '#b9b9c0'
    mark_past: str = '#8f8f95'
    next_fill: str = '#2a2a2d'     # 「下一条」那行的底色（还没走到时）
    connector: str = '#6a6a70'     # 标签被推开时连回真实刻度的细线


@dataclass
class ScaleGeom:
    """刻度区的几何。全是像素，和具体窗口无关。"""

    width: int = 300
    height: int = 250
    ruler_w: int = 46              # 左边刻度条的宽度
    pad_right: int = 6
    # 「现在 N」那个读数的右边距（2026-10-13 用户：「把打轴器现在的帧数往左移」）。
    # 单开一个字段而不是直接加大 pad_right：pad_right 还管着**轴帧号**那一列（右边那串
    # 「570」），动它会连帧号一起挪。帧号最多四位数（≈30px 宽）右对齐在 W-6，44 让读数
    # 右边缘落在它左边约 10px 处 —— 原来读数和帧号都贴 W-6，读数正好压在帧号上。
    now_pad_right: int = 44
    pointer_ratio: float = 0.50    # 「现在」线在刻度区的相对高度（0.5 = 正中）
    px_per_frame: float = 1.5      # 1 帧画多少像素（缩放档）
    # 2026-10-11 字号放大一档（见 scale_render.font_sizes），行高和最小间距跟着长：
    # 18/20 是按 11/12px 的字定的，13px 的字塞进去上下会顶到。
    label_h: int = 22              # 轴标签的高度
    label_gap: float = 22.0        # 相邻标签的最小间距（防重叠用）

    @property
    def pointer_y(self) -> float:
        return self.height * self.pointer_ratio

    def y_of(self, f, frame) -> float:
        return self.pointer_y + (f - frame) * self.px_per_frame

    def frames_up(self) -> float:
        """「现在」线**上方**能显示多少帧（过去那一半，帧号更小的方向）。"""
        return self.pointer_y / self.px_per_frame

    def frames_down(self) -> float:
        """「现在」线**下方**能显示多少帧（未来方向）。"""
        return (self.height - self.pointer_y) / self.px_per_frame


@dataclass
class Mark:
    """一条轴在刻度上的落点。

    tick_y 是它的**真实**位置（按帧等比例算出来的，不能动）；
    label_y 是标签**实际**画在哪（挨太近会被推开，用细线连回 tick_y）。
    """

    cue: Cue
    delta: float          # 相对当前帧；正 = 还没到，负 = 已经过了
    tick_y: float
    label_y: float
    state: str            # 'past' | 'next' | 'future'

    @property
    def spread(self) -> bool:
        return abs(self.label_y - self.tick_y) > 1.0


@dataclass
class Layout:
    geom: ScaleGeom
    frame: float
    pointer_y: float
    ticks: list = field(default_factory=list)    # [(帧号, y, 是不是大刻度)]
    marks: list = field(default_factory=list)    # [Mark]
    # （mercury「青柱」字段 2026-10-11 删了：用户要求去掉那根竖柱，见 scale_render）
    # （now 字段 2026-10-13 跟着「回看」那颗键一起删了 —— 那时 frame 是「视图帧」、
    #   now 才是真实帧，两者能不一样。现在指针永远指着 frame，不需要第二个帧号。）


def one_line(text: str) -> str:
    """轴说明里可能有换行；刻度表一行只画得下一条，换行折叠成 ·。"""
    return ' · '.join(x.strip() for x in (text or '').split('\n') if x.strip())


def clip(text: str, n: int) -> str:
    """按字符数截断（中文一个字算一个），超了用 … 结尾。"""
    s = one_line(text)
    return s if len(s) <= n else s[:n - 1] + '…'


def _tick_steps(px_per_frame: float):
    """小 / 大刻度的间隔（帧）。小刻度不能密到糊成一片。"""
    minor = TICK_CANDIDATES[-1]
    for cand in TICK_CANDIDATES:
        if cand * px_per_frame >= MIN_TICK_PX:
            minor = cand
            break
    else:
        minor = 60
    major = COST_FRAMES if minor <= COST_FRAMES else minor * 2
    return minor, major


def spread_labels(marks, geom: ScaleGeom) -> None:
    """标签防重叠，并把「现在」那一行整条留出来。

    「现在」线上要印当前帧号，所以它是一条**保留带**：上方的轴往上排、下方的轴往下排，
    谁都不许压上去。否则帧号刚好落在指针附近时（轴走到 1198.5、那条轴在 1198），
    标签会盖住帧号，两边都看不清。

    两侧都是「越靠近指针的越先占住真实位置」，被推开的代价落在更远的那些上面 ——
    因为近的那几条才是打轴时要看的。
    """
    py = geom.pointer_y
    # 保留带的半高：帧号读数压在指针线上（上下各约 5px），所以标签还得再退一点
    half = geom.label_h / 2.0 + 5.0
    above = [m for m in marks if m.tick_y < py]
    below = [m for m in marks if m.tick_y >= py]

    prev = py - half
    for m in sorted(above, key=lambda m: m.tick_y, reverse=True):
        y = min(m.tick_y, prev)
        m.label_y = y
        prev = y - geom.label_gap

    prev = py + half
    for m in sorted(below, key=lambda m: m.tick_y):
        y = max(m.tick_y, prev)
        m.label_y = y
        prev = y + geom.label_gap


def build(frame, cues, done, geom: ScaleGeom = None,
          recent_pad: float = 0.0, future_pad: float = 0.0) -> Layout:
    """把「当前帧 + 轴 + 已经触发了几条」摆成一张刻度表。

    frame : 当前帧号（刻度按它摆）。可以是小数 —— 满费时计数器给的就是小数
    cues  : 全部轴，按帧号升序（就是 Timeline.cues）
    done  : 已经触发了多少条（就是 Prompt.done）。cues[done] 就是「下一条」。
    """
    g = geom or ScaleGeom()
    out = Layout(geom=g, frame=frame, pointer_y=g.pointer_y)
    if not cues:
        return out

    n = len(cues)
    done_i = max(0, min(int(done), n))

    # ---- 刻度 ----
    minor, major = _tick_steps(g.px_per_frame)
    # 线的上面是过去（帧号更小），下面是未来（帧号更大）—— 见模块开头那张坐标图
    f_top = frame - g.frames_up() - minor
    f_bottom = frame + g.frames_down() + minor
    k = int(math.floor(f_top / minor))
    while k * minor <= f_bottom:
        f = k * minor
        if f >= 0:
            y = g.y_of(f, frame)
            if -1.0 <= y <= g.height + 1.0:
                out.ticks.append((f, y, f % major == 0))
        k += 1

    # ---- 轴点：只捡屏幕（加一点余量）里的，不要 O(全部轴) ----
    margin = g.label_h * 2
    picked = []
    i = done_i                      # 未来的：在指针下面，帧号越大越靠下
    while i < n:
        y = g.y_of(cues[i].frame, frame)
        if y > g.height + margin + future_pad:
            break
        if y >= -margin:
            picked.append((i, y))
        i += 1
    j = done_i - 1                  # 过去的：在指针上面，帧号越小越靠上
    while j >= 0:
        y = g.y_of(cues[j].frame, frame)
        if y < -margin - recent_pad:
            break
        if y <= g.height + margin:
            picked.append((j, y))
        j -= 1
    picked.sort(key=lambda t: t[1], reverse=True)

    for idx, y in picked:
        c = cues[idx]
        state = 'past' if idx < done_i else ('next' if idx == done_i else 'future')
        out.marks.append(Mark(cue=c, delta=c.frame - frame, tick_y=y, label_y=y,
                              state=state))
    spread_labels(out.marks, g)

    return out
