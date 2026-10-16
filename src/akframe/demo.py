"""合成输入的假监视器：没有游戏画面也把面板跑起来（刻度表预览 / 调界面用）。

为什么需要这一层：刻度表画在 tkinter 的 Canvas 上，而 ui._update_prompter 在
「这一拍没读到画面」时直接 return —— 游戏不在关卡里（或者根本没开）时，刻度表就只剩
一句提示文字。于是「刻度表画得对不对」只能靠玩游戏时顺手瞄一眼，没法边改边看，
也没法自动验证。

这个类把 LiveMonitor 的接口原样实现一遍，只有「画面从哪来」被换掉：按挂钟推进一个
理想游戏，每走一个逻辑帧就画一根费用条，交给**真的** Counter 去数。所以面板上跑的是
全链路（假画面 -> 真 read_bar -> 真状态机 -> 真打轴器 -> 真刻度表），
只有最外面那层抓屏是假的。合成宽度用 synthesis.synthesized_width ——
和游戏渲染的是同一条曲线，tools/check_state_synth.py 已经证明它喂进状态机误差 0 帧。

假画面怎么画的：条 ROI 的 5..12 行（read_bar 取的那条带）按相位涂白，
其余列涂轨道灰；满费那档整条涂 146（实测满费底色就是 146）。
这条路和真录像是同一个结果：30 个相位里只有「宽度 0」那一帧读成 EMPTY，
而游戏里那一帧本来就没画东西，状态机对它的处理和真录像完全一致。

暂停键也走真的 read_pause（画成双竖条 / 单三角，实测占比 0.241 / 0.078，
和真录像是同一个量级）。只有倍速键是直接构造的：认 1X / 2X 要用 templates/ 里的模板
做互相关，合成的数字笔画和真模板对不上，而演示要验的是刻度表，不是再验一遍按钮识别。
"""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np

from .features import SpeedReading, read_bar, read_pause
from .geometry import (SPEED_DIGIT_BAND, SPEED_DIGIT_COLS,
                       UI_SCALER_DEFAULT, Layout, layout_for)
from .live import LiveMonitor, LiveUpdate
from .settings import DEFAULT_FRAMES_PER_COST
from .state import BULLET_FPS, LOGICAL_FPS
from .synthesis import synthesized_width

# 默认演示的客户区尺寸：就是实测校准表里的那台机器（2558x1494 会命中实测条槽几何）。
DEMO_SIZE = (2558, 1494)

TRACK_GREY = 55      # 轨道（没填充的那段）灰度，实测 55..68
MAX_GREY = 146       # 满费底色，实测精确等于 146（见 features.GREY_MAX）

# 卡顿时挂钟会一下子涨好几秒，不限量的话一次要喂几千拍（面板会假死）。
MAX_CATCHUP = 600

# 演示轴的文本。用 // 开头写注释（解析器整行跳过）—— 写成 # 开头的话，
# 「# 120 部署银灰」剥掉井号正好是一条合法轴，会被当成真轴吃掉。
DEMO_AXIS_TEXT = '''// 演示轴：每 45 帧一条，密度够让刻度表一直有事可做。
// 想换成自己的轴：直接改这个框，再点「应用到提词器」。
30 部署 桃金娘 朝右
75 技能 桃金娘
120 部署 银灰 在 A2
165 技能 银灰
210 部署 能天使 朝右
255 撤退 桃金娘
300 技能 能天使
345 部署 山
390 技能 山
435 撤退 银灰
480 部署 史尔特尔
525 技能 史尔特尔
570 撤离 史尔特尔
615 部署 塞雷娅
660 技能 塞雷娅
705 开 NEXT WAVE
'''


class DemoMonitor(LiveMonitor):
    """假输入监视器。接口和 LiveMonitor 完全一样，界面那边一行都不用改。

    speed        每秒走几倍速的逻辑帧 = 30 x speed。只影响**演示跑多快**，
                 不影响计数（像素通道本来就与倍速无关）。
    bullet       子弹时间：恒 6 帧/秒（= 30/5），和倍速设置无关，见 state.BULLET_FPS。
    maxed        全程满费：条整条涂灰底，逼状态机走挂钟通道（帧号会带小数）。
    pause_every  每这么多秒暂停 pause_len 秒（0 = 不暂停），用来看「暂停时数字停住」。
    clock        取时间的函数，默认 perf_counter。检查脚本塞一个假钟进来就能
                 脱离真实时间逐拍推演（见 tools/check_demo.py）。
    """

    def __init__(self, size=DEMO_SIZE, *, speed: float = 1.0, bullet: bool = False,
                 maxed: bool = False, pause_every: float = 0.0, pause_len: float = 1.2,
                 rate: float = 120.0, cost_rate=DEFAULT_FRAMES_PER_COST,
                 ui_scaler: float = None, clock=time.perf_counter):
        scaler = UI_SCALER_DEFAULT if ui_scaler is None else ui_scaler
        layout: Layout = layout_for(int(size[0]), int(size[1]), scaler)
        super().__init__(layout, exe='(演示：没有游戏窗口)', rate=rate,
                         cost_rate=cost_rate, ui_scaler=scaler)
        self.bullet = bool(bullet)
        self.maxed = bool(maxed)
        self.speed = float(speed)
        if self.bullet:
            # 子弹时间是**绝对**的 1/5（恒 6 帧/秒），把倍速设置整个顶掉
            self.speed = 1.0
            self.fps_wall = BULLET_FPS
        else:
            self.fps_wall = LOGICAL_FPS * self.speed
        if self.maxed:
            # 满费只能走挂钟通道，而挂钟要乘倍速读数 —— 演示里给一个确定的 1X，
            # 免得「演示帧数不对」和「倍速没读到」两件事混在一起看不清。
            self.speed, self.fps_wall = 1.0, LOGICAL_FPS
        self.pause_every = float(pause_every)
        self.pause_len = float(pause_len)
        self._clock = clock
        self._vt0 = None
        self._last_vt = 0.0
        self._vt = 0.0          # 虚拟墙钟（秒），一直涨
        self._vt_game = 0.0     # 其中的「游戏在跑」那部分（暂停段不涨）
        self._fed = 0           # 已经喂给 Counter 的逻辑帧数
        self._running = True    # 这一拍游戏是不是在跑（决定暂停键画成哪个形状）
        self._btn_cache = {}    # 暂停键 / 倍速键的合成图（只画一次，然后复用）

    # ---------- 对外说明 ----------

    def describe(self) -> str:
        """一行话描述这个假游戏在干什么（启动时打印用）。"""
        if self.bullet:
            what = '子弹时间（恒 %.0f 帧/秒）' % BULLET_FPS
        elif self.maxed:
            what = '全程满费（走挂钟通道，帧号带小数）'
        else:
            what = '%.1fX（每秒 %.0f 逻辑帧）' % (self.speed, self.fps_wall)
        if self.pause_every > 0:
            what += '，每 %.1fs 暂停 %.1fs' % (self.pause_every, self.pause_len)
        return what

    # ---------- 合成读数 ----------

    def _bar_image(self, fill: int) -> np.ndarray:
        """画一根费用条（read_bar 取的那条带 = 行 5..12）。"""
        roi = np.full((self.layout.bar.h, self.layout.bar.w), TRACK_GREY, dtype=np.int16)
        if self.maxed:
            roi[:, :] = MAX_GREY
        elif fill > 0:
            roi[5:13, :min(fill, self.layout.bar.w)] = 255
        return roi

    def _pause_image(self, running: bool) -> np.ndarray:
        """画暂停键 / 继续键：双竖条 = 运行中，单三角 = 已暂停。

        形状按实测的「顶部宽度」比例画：双竖条顶部是两条整宽矩形、单三角顶部很窄，
        所以两者在 PAUSE_SHAPE_BAND 那条带里的亮点占比差约 4 倍。
        """
        key = ('pause', running)
        if key not in self._btn_cache:
            w, h = self.layout.pause.w, self.layout.pause.h
            a = np.zeros((h, w), dtype=np.int16)
            if running:
                a[int(h * 0.22):int(h * 0.78), int(w * 0.34):int(w * 0.46)] = 255
                a[int(h * 0.22):int(h * 0.78), int(w * 0.54):int(w * 0.66)] = 255
            else:
                y0, y1 = int(h * 0.18), int(h * 0.82)
                cx = w * 0.5
                for r in range(y0, y1):
                    half = (r - y0) / float(y1 - y0) * (w * 0.26)
                    a[r, int(cx - half):int(cx + half)] = 255
            self._btn_cache[key] = a
        return self._btn_cache[key]

    def _speed_image(self, has_digit: bool) -> np.ndarray:
        """画倍速键的数字区（1X / 2X）。子弹时间那档画面上没有数字，所以是空的。"""
        key = ('speed', has_digit)
        if key not in self._btn_cache:
            w, h = self.layout.speed.w, self.layout.speed.h
            a = np.zeros((h, w), dtype=np.int16)
            if has_digit:
                r0, r1 = self.layout.speed.band_rows(*SPEED_DIGIT_BAND)
                c0, c1 = self.layout.speed.cols_range(*SPEED_DIGIT_COLS)
                seg = c1 - c0
                # 笔画只占带高的中间一半，占空比才接近真数字（约 0.3）
                y0, y1 = r0 + (r1 - r0) // 4, r1 - (r1 - r0) // 4
                a[y0:y1, c0 + seg * 5 // 100:c0 + seg * 35 // 100] = 255
                a[y0:y1, c0 + seg * 60 // 100:c0 + seg * 90 // 100] = 255
            self._btn_cache[key] = a
        return self._btn_cache[key]

    def _readings(self, fill: int):
        """这一拍的三份读数 + 三张图（图给面板的 ROI 预览用，和读数是同一份数据）。

        费用条和暂停键都走**真的**特征提取（read_bar / read_pause）；只有倍速键
        直接构造 —— 认 1X / 2X 要 templates/ 里的模板做互相关，合成的数字笔画和真模板
        对不上，而演示要验的是刻度表，不是再验一遍按钮识别。
        """
        bar_roi = self._bar_image(fill)
        pause_roi = self._pause_image(self._running)
        speed_roi = self._speed_image(not self.bullet)
        bar = read_bar(bar_roi, self.layout)
        pause = read_pause(pause_roi, self.layout)
        if self.bullet:
            # 子弹时间下倍速键上只有三条箭头、没有数字：m 读不到（nan），
            # 速率由 bullet 这一位给出（见 state.BULLET_FPS）
            speed = SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                                 m=float('nan'), bullet=True, conf='ok')
        else:
            speed = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True,
                                 m=self.speed if self.speed else 1.0, bullet=False,
                                 conf='ok')
        return bar_roi, pause_roi, speed_roi, bar, pause, speed

    def _phase_of(self, n: int) -> float:
        """第 n 个逻辑帧画在什么相位上（1/per .. per/per 循环）。

        回费那一帧相位是 1.0（条画满），下一帧才回到 1/per（宽 0）——
        和 tools/check_state_synth.py 用的是同一个约定，也是从实测录像里数出来的。
        """
        per = float(self.counter.frames_per_cost if self.counter else LOGICAL_FPS)
        return (((n - 1) % per) + 1) / per

    def _fill_of(self, n: int) -> int:
        return synthesized_width(self.layout.model_bar_w, self.layout.bar_width_f,
                                 self._phase_of(n))

    # ---------- 采样 ----------

    def _sample(self) -> LiveUpdate:
        now = self._clock()
        if self._vt0 is None:
            self._vt0 = self._last_vt = now
        dt = max(0.0, now - self._last_vt)
        self._last_vt = now
        self._vt += dt

        paused = bool(self.pause_every > 0.0
                      and (self._vt % self.pause_every) < self.pause_len)
        if not paused:
            self._vt_game += dt

        self.stats.samples += 1
        self._hz_window.append(self._vt)
        if len(self._hz_window) > 60:
            self._hz_window.pop(0)
        if len(self._hz_window) >= 2:
            span = self._hz_window[-1] - self._hz_window[0]
            self.stats.hz = (len(self._hz_window) - 1) / span if span > 0 else 0.0
        self.stats.gap_ms = dt * 1000.0
        self.stats.gap_max_ms = max(self.stats.gap_max_ms, self.stats.gap_ms)

        self._running = not paused
        if paused:
            # 暂停段**一帧都不喂**，只把面板上的状态改成「已暂停」：数字按住不动，
            # 这正是真实暂停的样子（游戏里条照常画着、就是不动）。
            # 恢复时**不重新起锚**：相位在暂停期间没有变，重锚等于白丢一帧。
            if self.latest.result is not None:
                self.latest = replace(
                    self.latest,
                    result=replace(self.latest.result, mode='pause', delta=0, warn='',
                                   running=False),
                    note='演示：暂停中（不计数）')
            return self.latest

        due = int(self._vt_game * self.fps_wall)
        steps = min(max(0, due - self._fed), MAX_CATCHUP)
        if steps <= 0:
            return self.latest
        first = self._fed + 1
        self._fed += steps

        self._running = True
        for n in range(first, self._fed + 1):
            self._idx += 1
            bar_roi, pause_roi, speed_roi, bar, pause, speed = self._readings(self._fill_of(n))
            res = self.counter.step(self._idx, n / self.fps_wall, bar, pause, speed)
            note = '演示：%s ｜ 通道 %s' % (
                '满费' if self.maxed else ('子弹时间' if self.bullet
                                           else '%.1fX' % self.speed), res.mode)
            self.latest = LiveUpdate(self._vt, res, bar_roi, pause_roi, speed_roi,
                                     0.0, note, True)
        return self.latest
