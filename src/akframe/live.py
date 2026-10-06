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
from dataclasses import dataclass

import numpy as np

from .features import read_bar, read_pause, read_speed
from .geometry import UI_SCALER_DEFAULT, Layout, layout_for, union_rect
from .cost_rate import RateEstimator
from .settings import DEFAULT_FRAMES_PER_COST
from .state import CSV_HEADER, Counter, FrameResult
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

    def __init__(self, layout: Layout = None, *, exe: str = 'Arknights.exe',
                 title: str = None, pid: int = None, rate: float = 120.0,
                 templates=(), csv_path: str = None, build_layout=None,
                 cost_rate=DEFAULT_FRAMES_PER_COST, ui_scaler: float = None):
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
        elapsed = self.counter.elapsed if (keep_elapsed and self.counter) else 0
        manual = _manual_cost_rate(self.cost_rate)
        self.layout = layout
        self._union = union_rect([layout.bar, layout.pause, layout.speed])
        self.counter = Counter(layout.model_bar_w, fps=max(1.0, self.rate or 60.0),
                               frames_per_cost=manual or DEFAULT_FRAMES_PER_COST,
                               bar_width_frac=layout.bar_width_f, visible_w=layout.bar.w)
        self.counter.elapsed = elapsed
        self.counter.resync()
        self.counter.set_manual_pause(self.manual_pause)
        self._apply_cost_rate()
        # 外接矩形变了，之前按尺寸缓存的 DIB 不再需要（ScreenGrabber 自己按尺寸复用）
        self._grabber = None

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

        bar = read_bar(bar_img, lay)
        pause = read_pause(pause_img, lay)
        speed = read_speed(speed_img, lay, self.templates)
        self._track_cost_rate(bar)
        res = self.counter.step(self._idx, t, bar, pause, speed)

        note = ''
        if res.mode == 'manual-pause':
            note = '计数已暂停（手动，数字按住不动）'
        elif res.mode == 'pause':
            note = '已暂停（不计数）'
        elif not res.running:
            # 帧操：采样这一瞬图标是「暂停」，但两次采样之间游戏跑过了、条动过了
            note = '暂停键=暂停，但费用条在动 —— 按费用条计数'
        elif res.mode.startswith('clock'):
            note = '满费中，按挂钟 × 倍速估算（小数 = 不足一帧的余量）'
        elif res.warn == 'speed-unknown':
            note = '满费中，但倍速键上没画数字 —— 冻结不猜'

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

    # ---------- 循环 ----------

    def run(self, stop: threading.Event = None) -> None:
        stop = stop or self._stop
        period = 1.0 / self.rate if self.rate > 0 else 0.0
        while not stop.is_set():
            t_start = time.perf_counter()
            self._sync_csv()
            self._sample()
            if period > 0:
                # 注意：Windows 上 threading.Event.wait 的时间粒度是 15.6ms（系统时钟节拍），
                # 用它配速只能跑到 ~33Hz。Python 3.11+ 的 time.sleep 走的是高精度等待定时器
                # （~1ms），所以这里用 sleep 循环、顺带检查 stop。
                deadline = t_start + period
                remain = deadline - time.perf_counter()
                while remain > 0 and not stop.is_set():
                    time.sleep(min(remain, 0.05))
                    remain = deadline - time.perf_counter()
        self._close_csv()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self.run, name='akframe-live', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

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
