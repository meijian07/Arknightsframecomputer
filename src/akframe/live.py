"""实时链路：抓屏 -> 特征 -> 计数器。

和离线链路（capture.py + cli.py）共用 features / state / geometry，唯一区别是帧从
屏幕来。这样「读条逻辑」只有一份，离线验证过的结论可以直接用到实时上。

一个采样周期做四件事：
    1. 找到游戏窗口，取客户区矩形（窗口动了/缩放了要重新取）
    2. 一次 BitBlt 抓「三个 ROI 的外接矩形」
    3. 在内存里切出三个 ROI、转灰度、提特征
    4. 喂给 state.Counter 得到这一拍的逻辑帧增量

读不到画面时（窗口最小化、找不到、太小）**不猜测**：调 Counter.resync() 重新起锚，
并且把这段时间单独记在 stats.unreadable_s 里，让用户知道总帧数是不完整的。
"""

from __future__ import annotations

import csv
import os
import threading
import time
import traceback
from dataclasses import dataclass, replace

import numpy as np

from .features import read_bar, read_pause, read_sign, read_speed
from .geometry import UI_SCALER_DEFAULT, Layout, layout_for, union_rect
from .cost_rate import RateEstimator
from .settings import DEFAULT_FRAMES_PER_COST, negative_override, parse_negative
from .state import CSV_HEADER, LOGICAL_FPS, Counter, FrameResult
from .win32 import (ScreenGrabber, WindowInfo, arknights_ui_scaler, client_rect,
                    find_window, is_minimized, to_gray)


def _default_build_layout(w: int, h: int) -> Layout:
    """没显式传 build_layout 时的兜底：按注册表里的「UI 缩放」算 ROI。"""
    return layout_for(w, h, arknights_ui_scaler())


@dataclass
class LiveUpdate:
    """一次采样的结果快照，供界面显示。"""
    t: float                                  # 相对开始时刻的秒数
    result: FrameResult | None                # 读到画面时才有
    bar_img: np.ndarray | None                # 三个 ROI 的灰度图（界面预览用）
    pause_img: np.ndarray | None
    speed_img: np.ndarray | None
    grab_ms: float
    note: str
    readable: bool


@dataclass
class LiveStats:
    samples: int = 0
    hz: float = 0.0
    grab_ms: float = 0.0
    unreadable_s: float = 0.0
    resyncs: int = 0
    rows: int = 0
    # 相邻两次「抓屏那一刻」的间隔。1 倍速下一个逻辑帧只有 33.3ms，采样间隔一旦超过它，
    # 就有可能整帧没被采到 —— 表现是「某一帧不读、下一帧一次 +2」（用户报的
    # 「第 0 帧不读、第一帧补了两帧」）。所以这个数必须露出来，漏帧才有据可查。
    gap_ms: float = 0.0
    gap_max_ms: float = 0.0
    # 采样循环的心跳：每转一圈 +1，**不管这一拍读没读到画面**。界面靠它区分
    # 「游戏暂停所以数字不动」和「采样线程已经死了」——后者以前是看不出来的。
    ticks: int = 0
    # 循环里被兜住的异常（见 run）。有控制台时会打完整调用栈，这里留最近一条给界面，
    # 因为打包后用 pythonw.exe 跑**根本没有控制台**，不给界面看就彻底静默了。
    errors: int = 0
    last_error: str = ''
    last_error_t: float = 0.0


def _manual_cost_rate(spec):
    """把 cost_rate 的取值解析成「一费几个逻辑帧」；auto / 空 -> None（= 自动识别）。"""
    if spec is None:
        return None
    if isinstance(spec, (int, float)):
        return float(spec) if spec > 0 else None
    text = str(spec).strip().lower()
    if text in ('', 'auto', 'off', 'none'):
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    return v if v > 0 else None


class LiveMonitor:
    """实时监视一个游戏窗口，维护全局逻辑帧计数。

    线程模型：run() 在后台线程里跑采样循环，latest / stats 随时可读（GIL 下赋值是原子的，
    只做「读最新值」的界面不需要加锁）。
    """

    # 采样线程多久没有心跳就判「停了」（界面读这个值，见 ui.STALL_WARN_S 的说明）。
    # 实时链路 120Hz，1 秒足够；录像链路一拍可能喂很久（全速 / 大跨度倒带），
    # 覆盖成更大的值，否则「全速跑完整段」那一下会被当成采样线程死了。
    stall_warn_s = 1.0

    def __init__(self, layout: Layout = None, *, exe: str = 'Arknights.exe',
                 title: str = None, pid: int = None, rate: float = 120.0,
                 templates=(), csv_path: str = None, build_layout=None,
                 cost_rate=DEFAULT_FRAMES_PER_COST, ui_scaler: float = None,
                 negative: str = 'auto'):
        self.exe = exe
        self.title = title
        self.pid = pid
        self.rate = float(rate)
        self.templates = list(templates)
        self.build_layout = build_layout or _default_build_layout
        # None = 用注册表里的游戏 UI 缩放（和 _default_build_layout 同一个来源）
        if ui_scaler is None:
            try:
                ui_scaler = arknights_ui_scaler()
            except Exception:
                ui_scaler = UI_SCALER_DEFAULT
        self.ui_scaler = ui_scaler
        self.window: WindowInfo | None = None
        self.layout: Layout | None = None
        self.counter: Counter | None = None
        self.latest: LiveUpdate = LiveUpdate(0.0, None, None, None, None, 0.0, '未开始', False)
        self.stats = LiveStats()
        self.rows: list = []
        self.keep_rows = False     # True 时把每帧结果留在内存里（导出用）
        self.manual_pause = False  # 「暂停计数」开关（手动，给测试用）
        # 回费速率：默认为手工档「一费几个逻辑帧」（30 = 1 秒 1 费，60 = 回费 -50%，
        # 120 = -75%）。给 'auto' 才开自动识别（见 cost_rate.py），那是可选功能：
        # 认错档会让绝对帧号整段漂移，所以默认不认。
        self.cost_rate = cost_rate
        # 局内负费：'auto' 按画面判（费用数字左边的减号），'on' / 'off' 强制。
        # 默认 auto —— 这一档错了整段就是 2 倍偏差，所以手动档只当兜底（见 settings）。
        self.negative_mode = parse_negative(negative)
        self.rate_est = None       # 自动识别器（手工指定速率时是 None）
        self.rate_estimate = None  # 最近一次识别结果（界面显示用）
        self._rate_locked = False  # 已经认准过速率就不再改（防两段样本来回翻）
        self._csv_path = csv_path
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.perf_counter()
        self._idx = 0
        self._hz_window = []
        self._gap_window = []          # 最近 1 秒左右的采样间隔（毫秒）
        self._last_grab_t = None
        self._grabber = None
        self._union = None
        self._last_window_scan = 0.0
        self._unreadable_since = None
        for lay in ([layout] if layout is not None else []):
            self.set_layout(lay)

    # ---------- 布局 / 计数 ----------

    def set_layout(self, layout: Layout, keep_elapsed: bool = True) -> None:
        """换布局（窗口缩放、人工校准后）。会重建外接矩形和计数器。"""
        layout.validate()
        # 只留引用，最后一并 adopt 过去：**不能只搬 elapsed**，理由见 Counter.adopt
        old = self.counter if keep_elapsed else None
        manual = _manual_cost_rate(self.cost_rate)
        self.layout = layout
        # 减号扫描框也一起框进外接矩形：它落在「条到按钮」这条竖直带里，
        # 实测（1905x1085）外接矩形一点没变，等于白捡（见 geometry.sign_rect）。
        self._union = union_rect([layout.bar, layout.pause, layout.speed, layout.sign])
        self.counter = Counter(layout.model_bar_w, fps=max(1.0, self.rate or 60.0),
                               frames_per_cost=manual or DEFAULT_FRAMES_PER_COST,
                               bar_width_frac=layout.bar_width_f, visible_w=layout.bar.w)
        if old is not None:
            self.counter.adopt(old)
        self.counter.resync()
        self.counter.set_manual_pause(self.manual_pause)
        self.counter.set_negative_override(negative_override(self.negative_mode))
        self._apply_cost_rate()
        # 外接矩形变了，之前按尺寸缓存的 DIB 不再需要（ScreenGrabber 自己按尺寸复用）
        self._grabber = None

    def set_negative_mode(self, mode) -> str:
        """切「局内负费」手动档（auto / on / off）。返回归一之后的档名。

        换档**不重新起锚**（相位是连续的）：下一拍起按新速度走，那一拍会被当成
        负费转场帧做亚帧修正（见 Counter._boundary_correction）。
        """
        self.negative_mode = parse_negative(mode)
        if self.counter is not None:
            self.counter.set_negative_override(negative_override(self.negative_mode))
        return self.negative_mode

    def set_cost_rate(self, spec) -> float:
        """改回费速率：数字 = 手工档（一费几个逻辑帧）；'auto' = 开自动识别。

        已经数出来的像素帧会按新旧速率比例换算（见 Counter.set_frames_per_cost），
        所以中途改档不会让绝对帧号从头偏掉。返回此刻生效的「一费几个逻辑帧」。
        """
        self.cost_rate = spec
        self._apply_cost_rate()
        if self.counter is not None:
            return self.counter.frames_per_cost
        return _manual_cost_rate(spec) or DEFAULT_FRAMES_PER_COST

    @property
    def rate_locked(self) -> bool:
        """速率是不是已经定下来了（手工档恒为 True；auto 认准后变 True）。"""
        return bool(self._rate_locked)

    def _apply_cost_rate(self) -> None:
        """把 self.cost_rate 落到 Counter 上（换布局、改设置都走这里）。"""
        manual = _manual_cost_rate(self.cost_rate)
        if manual:
            # 手工档：没有可认的，直接换档
            if self.counter is not None:
                self.counter.set_frames_per_cost(manual)
            self.rate_est = None
            self._rate_locked = True
        else:
            # 'auto'：认出来之前先按 30 算（和没有减费 tag 的关卡一致），认准了再换
            if self.counter is not None:
                self.counter.set_frames_per_cost(DEFAULT_FRAMES_PER_COST)
            lay = self.layout
            self.rate_est = (None if lay is None else
                             RateEstimator(lay.model_bar_w, lay.bar_width_f, lay.bar.w))
            self._rate_locked = False
        self.rate_estimate = None

    def set_manual_pause(self, on: bool) -> None:
        """开关「暂停计数」：数字按住不动，方便对着游戏逐段核对。"""
        self.manual_pause = bool(on)
        if self.counter is not None:
            self.counter.set_manual_pause(self.manual_pause)

    def _track_cost_rate(self, bar) -> None:
        """顺手认回费速率：认准了就换 Counter 的 required（并把已数的帧按比例换算）。

        为什么认准了才换、而且只换一次：认错档位比不认更糟 —— 差一档会让绝对帧号
        整段漂移，打轴器要的正是绝对帧号。所以只在置信度 high 时换，换过就锁住
        （两段样本可能给出略有出入的估计，来回翻会毁掉帧号）。
        """
        if self.rate_est is None or self._rate_locked:
            return
        est = self.rate_est.feed(bar.fill, bar.state)
        self.rate_estimate = est
        if est.conf == 'high' and est.frames_per_cost:
            if abs(est.frames_per_cost - self.counter.frames_per_cost) > 1e-9:
                self.counter.set_frames_per_cost(est.frames_per_cost)
            self._rate_locked = True

    def toggle_manual_pause(self) -> bool:
        self.set_manual_pause(not self.manual_pause)
        return self.manual_pause

    def reset(self) -> None:
        """清零计数（不清空已写出的 CSV）。"""
        if self.rate_est is not None:
            self.rate_est.reset()
        self.rate_estimate = None
        if not _manual_cost_rate(self.cost_rate):
            self._rate_locked = False
        self._t0 = time.perf_counter()
        self._idx = 0
        self._hz_window.clear()
        self.stats.unreadable_s = 0.0
        self.stats.resyncs = 0
        if self.layout:
            self.set_layout(self.layout, keep_elapsed=False)
        if self.counter is not None:
            # 清零 = 「从这一刻起从 0 开始数」。下一次起锚只补清零**之后**才走的帧，
            # 所以要把「清零这一刻条画到第几帧」交给计数器：万一清零时条还是空的
            # （本费第 0 帧），清零之后走到「第一个像素」那 1 帧就必须记，
            # 否则整段帧号从此差 1（用户报的「第零帧到第一帧记不到」）。
            last = self.latest.result
            fill = int(last.fill) if last is not None else None
            self.counter.mark_zero_start(fill)
        self.rezero_latest()

    def rezero_latest(self) -> None:
        """把缓存读数（latest.result）里的帧号按回 0。

        为什么清零必须连这一份一起改：面板上那个大数字读的是 `latest.result.elapsed`，
        而 `latest` **只在「这一拍真读到画面」时才刷新**（video.py 暂停那一拍、窗口链路
        还没点开始计数、点了停止计数 —— 这三种情况下这一拍永远不来）。于是清零只改了
        计数器，面板还挂着清零前的数字，用户看到的就是「清零没用 / 要等开始计数才生效」。

        只动帧号（idx / delta / elapsed / elapsed_f），条宽 / 状态 / 模式照旧 ——
        那些是"这一刻画面长什么样"，与第几帧无关。
        """
        upd = self.latest
        if upd is None or upd.result is None:
            return
        res = upd.result
        if not (res.idx or res.delta or res.elapsed or res.elapsed_f):
            return
        self.latest = replace(upd, result=replace(res, idx=0, delta=0, elapsed=0,
                                                 elapsed_f=0.0))

    # ---------- 采样 ----------

    def _resolve_window(self) -> WindowInfo | None:
        w = self.window
        if w is not None:
            x, y, cw, ch = client_rect(w.hwnd)
            if cw > 0 and ch > 0:
                w = WindowInfo(w.hwnd, w.pid, w.exe, w.title, (x, y, cw, ch), is_minimized(w.hwnd))
                self.window = w
                return w
        now = time.perf_counter()
        if now - self._last_window_scan < 1.0:
            return None
        self._last_window_scan = now
        self.window = find_window(exe=self.exe, title=self.title, pid=self.pid)
        return self.window

    def _sample(self) -> LiveUpdate:
        self._idx += 1
        # 时间戳先算好：下面几条「读不到画面」的早退分支也要用它
        # （之前这里是未定义变量，窗口一最小化就 NameError，采样线程直接死掉）
        t = time.perf_counter() - self._t0

        win = self._resolve_window()
        if win is None:
            return self._unreadable(t, '找不到游戏窗口')
        if win.minimized:
            return self._unreadable(t, '窗口已最小化')
        x, y, w, h = win.client
        if w < 320 or h < 240:
            return self._unreadable(t, '窗口太小 (%dx%d)' % (w, h))

        if self.layout is None or (w, h) != (self.layout.width, self.layout.height):
            self.set_layout(self.build_layout(w, h))

        if self._grabber is None:
            self._grabber = ScreenGrabber()
        lay = self.layout
        u = self._union
        t_grab = time.perf_counter()
        full = self._grabber.grab_bgra(x + u.x, y + u.y, u.w, u.h)
        grab_ms = (time.perf_counter() - t_grab) * 1000.0
        # 时间戳取在抓屏之后：这一拍的像素内容就是这一刻左右从屏幕上取的
        now = time.perf_counter()
        t = now - self._t0
        if self._last_grab_t is not None:
            gap_ms = (now - self._last_grab_t) * 1000.0
            self.stats.gap_ms = gap_ms
            self._gap_window.append(gap_ms)
            if len(self._gap_window) > 120:
                self._gap_window.pop(0)
            self.stats.gap_max_ms = max(self._gap_window)
        self._last_grab_t = now

        def cut(r) -> np.ndarray:
            oy, ox = r.y - u.y, r.x - u.x
            return full[oy:oy + r.h, ox:ox + r.w]

        # 只把三个 ROI 转灰度，**不要**把整块外接矩形转一遍再切。
        # 为什么：三个 ROI 加起来 6.8万像素，外接矩形 53万像素（条在右下、按钮在右上，
        # 中间一大片全是空的）。实测 to_gray 整块 7.33 ms、三个 ROI 0.73 ms —— 差 6.6 ms，
        # 而这一步的省下直接决定采样率：本机实时链路原来 8.9+7.0+1.2 ≈ 18 ms/拍 = 54.6 Hz，
        # 改完 ≈ 11 ms/拍 = 90 Hz（用户要的「16ms 精度」是 60 Hz）。
        # 结果完全一致（同一像素同一公式，已验证逐像素相等）。
        bar_img = to_gray(cut(lay.bar))
        pause_img = to_gray(cut(lay.pause))
        speed_img = to_gray(cut(lay.speed))
        sign_img = to_gray(cut(lay.sign))

        bar = read_bar(bar_img, lay)
        pause = read_pause(pause_img, lay)
        speed = read_speed(speed_img, lay, self.templates)
        # 局内负费：费用数字左边的减号。负费期间每逻辑帧只攒一半（一费 30 -> 60 帧），
        # 不认出来这一段会被数成一半，见 features.read_sign / state._pixel。
        sign = read_sign(sign_img, lay)
        self._track_cost_rate(bar)
        res = self.counter.step(self._idx, t, bar, pause, speed, sign)

        note = ''
        if res.mode == 'manual-pause':
            note = '计数已暂停（手动，数字按住不动）'
        elif res.mode == 'pause':
            note = '已暂停（不计数）'
        elif not res.running:
            # 帧操：采样这一瞬图标是「暂停」，但两次采样之间游戏跑过了、条动过了
            note = '暂停键=暂停，但费用条在动 —— 按费用条计数'
        elif res.negative:
            note = '局内负费：每逻辑帧只攒一半，一费 60 逻辑帧'
        elif res.mode.startswith('clock'):
            note = '满费中，按挂钟 × 倍速估算（小数 = 不足一帧的余量）'
        elif res.warn == 'speed-unknown':
            note = '满费中，但倍速键上没有数字 —— 冻结不猜（从没读到过倍速数字）'
        elif res.warn == 'bullet-assumed':
            note = '满费中，倍速键整块空白 —— 按子弹时间 6 帧/秒估（拖干员）'

        self.stats.samples += 1
        self._hz_window.append(t)
        if len(self._hz_window) > 60:
            self._hz_window.pop(0)
        if len(self._hz_window) >= 2:
            span = self._hz_window[-1] - self._hz_window[0]
            self.stats.hz = (len(self._hz_window) - 1) / span if span > 0 else 0.0
        self.stats.grab_ms = grab_ms
        self._unreadable_since = None
        upd = LiveUpdate(t, res, bar_img, pause_img, speed_img, grab_ms, note, True)
        self.latest = upd
        row = res.as_row()
        if self.keep_rows:
            self.rows.append(row)
        self._write_row(row)
        return upd

    def _unreadable(self, t: float, note: str) -> LiveUpdate:
        now = time.perf_counter()
        if self._unreadable_since is None:
            self._unreadable_since = now
        else:
            self.stats.unreadable_s += now - self._unreadable_since
            self._unreadable_since = now
        if self.counter is not None:
            self.counter.resync()
        self.stats.resyncs += 1
        self.latest = LiveUpdate(t, None, None, None, None, 0.0, note, False)
        return self.latest

    def perf_line(self, ui_hz: float, cur_m: float = 1.0) -> tuple:
        """面板「性能」那一行的文本 + 要不要标黄。

        为什么放在这里而不是界面里：录像监视器（video.VideoMonitor）要把它换成
        「播放到哪一帧了」，界面只管显示、不用自己判类型。
        """
        st = self.stats
        # 「最大间隔」的告警门限必须跟着当前倍速走：采样间隔一旦超过「一个逻辑帧在
        # 屏幕上停留的时长」就会整帧漏采。停留时长 = 1000 / (30 x 倍速)：
        # 1X -> 33.3ms、2X -> 16.7ms。门限写死 33.3ms 的话，2 倍速下明明在漏采
        # 也不会报警。倍速读不到时按 1X 算（保守，宁可多报也不漏报）。
        jam_thr = 1000.0 / (LOGICAL_FPS * max(1.0, cur_m))
        jam = st.gap_max_ms >= jam_thr
        text = ('采样 %.1f Hz ｜ 最大间隔 %s%.1f ms%s ｜ 面板 %.0f Hz ｜ 抓屏 %.1f ms '
                '｜ 采样 %d 次 ｜ 心跳 %d ｜ 读不到累计 %.2f s'
                % (st.hz, '⚠ ' if jam else '', st.gap_max_ms,
                   '（超过 %.1f ms，可能漏采）' % jam_thr if jam else '',
                   ui_hz, st.grab_ms, st.samples, st.ticks, st.unreadable_s))
        return text, jam

    def _on_error(self, exc: BaseException, misses: int) -> None:
        """一次采样抛异常之后的收尾：记账 + 自愈 + 让界面看得见。

        为什么要丢掉 window / _grabber：采样里的异常绝大多数都是「窗口动了、被最小化、
        换了进程、分辨率变了」这一类，缓存下来的窗口句柄和 DIB 全都不作数了。全部丢掉，
        下一拍 _resolve_window 会重新找一次窗口 —— 多数情况下不用人管就自己好了。
        """
        self.stats.errors += 1
        self.stats.last_error = '%s: %s' % (type(exc).__name__, exc)
        self.stats.last_error_t = time.perf_counter() - self._t0
        if misses == 1:
            # 只在第一次失败时打完整栈：持续失败的话每拍打一遍会把控制台刷爆，
            # 而打包后本来就没有控制台，所以 stats.last_error 还要留给界面显示。
            traceback.print_exc()
        self.window = None
        self._grabber = None
        self._last_window_scan = 0.0
        if self.counter is not None:
            self.counter.resync()          # 相位不可信了，重新起锚，不猜
        self.stats.resyncs += 1
        self._unreadable_since = None
        t = self.latest.t if self.latest is not None else 0.0
        self.latest = LiveUpdate(t, None, None, None, None, 0.0,
                                 '采样异常（连续第 %d 次）：%s' % (misses, self.stats.last_error),
                                 False)

    # ---------- 循环 ----------

    def run(self, stop: threading.Event = None) -> None:
        """采样主循环。**一次异常都不许让它退出。**

        实测踩过这个坑（2026-10-07，用户报「有些时候会莫名卡死」）：原来这里没有兜异常，
        _sample() 只要抛一次（窗口被拖走/最小化时 BitBlt 失败、ROI 切出空数组、
        counter 里的断言……）这个 daemon 线程就**静默退出**了。而面板是另一个
        root.after 循环在跑，看起来一切正常 —— 「面板 64 Hz」还在跳、ROI 预览还挂着
        最后一帧，只有数字和「采样 N 次」一起冻住。于是就成了「莫名卡死」：界面上没有
        任何一处告诉你采样已经停了。

        更糟的是打包后用 pythonw.exe 跑**没有控制台**，traceback 连个地方都去不了。

        所以现在：每一拍都兜住异常、记进 stats、**丢掉窗口和抓屏器的缓存**（绝大多数
        异常都是「窗口动了 / 换了个进程」这一类，下一拍重新找一次就好了），然后继续。
        连续失败时稍微让出 CPU，别把核心烧在必然失败的循环上。
        """
        stop = stop or self._stop
        period = 1.0 / self.rate if self.rate > 0 else 0.0
        misses = 0
        while not stop.is_set():
            t_start = time.perf_counter()
            self.stats.ticks += 1
            try:
                self._sync_csv()
                self._sample()
                misses = 0
            except Exception as exc:                     # noqa: BLE001
                misses += 1
                self._on_error(exc, misses)
            if period > 0:
                # 注意：Windows 上 threading.Event.wait 的时间粒度是 15.6ms（系统时钟节拍），
                # 用它配速只能跑到 ~33Hz。Python 3.11+ 的 time.sleep 走的是高精度等待定时器
                # （~1ms），所以这里用 sleep 循环、顺带检查 stop。
                deadline = t_start + period
                remain = deadline - time.perf_counter()
                while remain > 0 and not stop.is_set():
                    time.sleep(min(remain, 0.05))
                    remain = deadline - time.perf_counter()
            elif misses:
                time.sleep(0.001)      # rate=0（不限速）时才需要，正常配速自带等待
        self._close_csv()

    def start(self, reset_clock: bool = True) -> None:
        """启动采样线程。

        reset_clock=False 时**保留原来的时间基准 _t0**：重新校准会把一个已经死掉的线程
        拉起来，那时候 t 已经走到几百秒了，重置 _t0 会让 t 倒退回 0 —— Counter 的挂钟
        通道拿到一个倒退的时间戳，会算出一个负的 dt。
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if reset_clock:
            self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self.run, name='akframe-live', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def alive(self) -> bool:
        """采样线程还活着吗。

        界面靠它 + stats.ticks 区分两件以前长得一模一样的事：「游戏暂停所以数字不动」
        和「采样线程已经死了」。分不开的话，用户看到的就只有「莫名卡死」。
        """
        th = self._thread
        return bool(th is not None and th.is_alive())

    def recalibrate(self) -> str:
        """重新校准：重找窗口 -> 按当前客户区重算 ROI -> 重启采样线程 -> 重新起锚。

        什么时候用：游戏窗口被拖走 / 改了大小、换了分辨率、切了全屏，或者采样线程因为
        某次异常停了（见 run 的注释）。全程**保留已经数出来的帧数** —— 只是把「从屏幕
        哪块读、当前相位对到哪一帧」重新对一遍；要归零请用清零那个按钮。

        返回一句给界面显示的结果。
        """
        before = self.counter.elapsed if self.counter is not None else 0
        # 1) 丢掉缓存，强制重新找窗口（理由同 _on_error）
        self.window = None
        self._grabber = None
        self._last_window_scan = 0.0
        win = self._resolve_window()
        if win is None:
            note = '重新校准：没找到窗口（%s）—— 采样线程会继续等它回来' % self.exe
        else:
            try:
                self.set_layout(self.build_layout(*win.client_size), keep_elapsed=True)
            except Exception as exc:                 # noqa: BLE001
                note = '重新校准：按 %dx%d 重算 ROI 失败（%s: %s）' % (
                    win.client[2], win.client[3], type(exc).__name__, exc)
            else:
                x, y, w, h = win.client
                note = ('重新校准完成：客户区 %dx%d @(%d,%d)，已数的 %d 帧不变'
                        % (w, h, x, y, before))
        if self.counter is not None:
            self.counter.resync()
        # 2) 线程死了就拉起来 —— 这是「卡死之后点一下就活」的关键那一下
        if not self.alive:
            self.start(reset_clock=False)
            note += '；采样线程已重启'
        # 3) 清掉上一次的异常记录，界面回到干净状态
        self.stats.errors = 0
        self.stats.last_error = ''
        self.stats.last_error_t = 0.0
        return note

    # ---------- CSV 记录 ----------

    def set_csv_path(self, path: str | None) -> None:
        self.rows = [] if self.keep_rows else self.rows
        self._csv_path = path

    def _sync_csv(self) -> None:
        want = self._csv_path
        cur = getattr(self, '_csv_fh', None)
        cur_path = getattr(self, '_csv_open_path', None)
        if want == cur_path:
            return
        if cur:
            cur.close()
            self._csv_fh = None
            self._csv_open_path = None
        if want:
            d = os.path.dirname(os.path.abspath(want))
            if d:
                os.makedirs(d, exist_ok=True)
            new_file = not os.path.isfile(want)
            fh = open(want, 'a', newline='', encoding='utf-8')
            if new_file:
                # 在线 CSV 比离线多一列 wall（绝对时间戳），方便和别的日志对齐
                csv.writer(fh).writerow(list(CSV_HEADER) + ['wall'])
            self._csv_fh = fh
            self._csv_open_path = want
            self._csv_writer = csv.writer(fh)

    def _write_row(self, row) -> None:
        fh = getattr(self, '_csv_fh', None)
        if fh is None:
            return
        self._csv_writer.writerow(list(row) + ['%.4f' % time.time()])
        self.stats.rows += 1
        if self.stats.rows % 60 == 0:
            fh.flush()

    def _close_csv(self) -> None:
        fh = getattr(self, '_csv_fh', None)
        if fh:
            fh.close()
            self._csv_fh = None
            self._csv_open_path = None

    def __del__(self):
        try:
            self._close_csv()
        except Exception:
            pass
