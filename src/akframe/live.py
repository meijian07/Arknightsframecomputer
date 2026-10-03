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
from dataclasses import dataclass, field

import numpy as np

from .features import read_bar, read_pause, read_speed
from .geometry import Layout, layout_for, union_rect
from .state import CSV_HEADER, Counter, FrameResult
from .win32 import ScreenGrabber, WindowInfo, client_rect, find_window, is_minimized, to_gray


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


class LiveMonitor:
    """实时监视一个游戏窗口，维护全局逻辑帧计数。

    线程模型：run() 在后台线程里跑采样循环，latest / stats 随时可读（GIL 下赋值是原子的，
    只做「读最新值」的界面不需要加锁）。
    """

    def __init__(self, layout: Layout = None, *, exe: str = 'Arknights.exe',
                 title: str = None, pid: int = None, rate: float = 60.0,
                 templates=(), csv_path: str = None, build_layout=None):
        self.exe = exe
        self.title = title
        self.pid = pid
        self.rate = float(rate)
        self.templates = list(templates)
        self.build_layout = build_layout or layout_for
        self.window: WindowInfo | None = None
        self.layout: Layout | None = None
        self.counter: Counter | None = None
        self.latest: LiveUpdate = LiveUpdate(0.0, None, None, None, None, 0.0, '未开始', False)
        self.stats = LiveStats()
        self.rows: list = []
        self.keep_rows = False     # True 时把每帧结果留在内存里（导出用）
        self._csv_path = csv_path
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.perf_counter()
        self._idx = 0
        self._hz_window = []
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
        self.layout = layout
        self._union = union_rect([layout.bar, layout.pause, layout.speed])
        self.counter = Counter(layout.bar_w, fps=max(1.0, self.rate or 60.0))
        self.counter.elapsed = elapsed
        self.counter.resync()
        # 外接矩形变了，之前按尺寸缓存的 DIB 不再需要（ScreenGrabber 自己按尺寸复用）
        self._grabber = None

    def reset(self) -> None:
        """清零计数（不清空已写出的 CSV）。"""
        self._t0 = time.perf_counter()
        self._idx = 0
        self._hz_window.clear()
        self.stats.unreadable_s = 0.0
        self.stats.resyncs = 0
        if self.layout:
            self.set_layout(self.layout, keep_elapsed=False)

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
        t = time.perf_counter() - self._t0

        def cut(r) -> np.ndarray:
            oy, ox = r.y - u.y, r.x - u.x
            return full[oy:oy + r.h, ox:ox + r.w]

        bar_img = to_gray(cut(lay.bar))
        pause_img = to_gray(cut(lay.pause))
        speed_img = to_gray(cut(lay.speed))

        bar = read_bar(bar_img, lay)
        pause = read_pause(pause_img, lay)
        speed = read_speed(speed_img, lay, self.templates)
        res = self.counter.step(self._idx, t, bar, pause, speed)

        note = ''
        if not pause.running:
            note = '已暂停（未在计数）'
        elif res.mode == 'clock':
            note = '满费/条不可读，按挂钟估算'

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
