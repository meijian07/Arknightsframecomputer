#!/usr/bin/env python
"""假游戏窗口：把离线缓存里的真实 ROI 画面按时间重放到屏幕上。

它是实时链路的测试夹具 —— 没有它就只能靠人工开游戏来验证，很难自动化。
画面内容是真视频抽出来的原始灰度 ROI，按 layout_for(W, H) 的位置贴到一个无边框窗口里，
然后以视频的原始帧率播放。

注意：它验证的是「抓屏 -> 特征 -> 计数」这条链路的正确性；因为读写双方用的是同一套
layout_for，它**验证不了 geometry.py 里的尺寸常数对不对** —— 那件事只能靠校准工具 +
真实游戏来定。这个边界必须说清楚，不然容易产生虚假的安心感。

用法：
    python tools/fake_screen.py --size 1280x800 --start 20 --duration 20
它会打印自己的 pid 和 source-index 日志路径，之后用：
    python run_monitor.py --pid <pid> --no-ui --csv out/live_test.csv
"""

import argparse
import json
import os
import sys
import time
import tkinter as tk

import numpy as np
from PIL import Image, ImageTk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.geometry import Layout, layout_for          # noqa: E402
from akframe.win32 import enable_dpi_awareness           # noqa: E402

ROI_NAMES = ('bar', 'pause', 'speed')


class FakeGame:
    def __init__(self, cache: str, size: tuple, start: float, rate: float,
                 duration: float, title: str, pos: tuple, log_path: str, fps: float = None):
        with open(os.path.join(cache, 'meta.json'), encoding='utf-8') as fh:
            meta = json.load(fh)
        self.fps = float(fps or meta['fps'])
        self.frames = int(meta['frames'])
        self.src = Layout.from_dict(meta['layout'])
        self.dst = layout_for(size[0], size[1])
        self.start = float(start)
        self.rate = float(rate)
        self.duration = float(duration)
        self.files = {n: open(os.path.join(cache, n + '.raw'), 'rb') for n in ROI_NAMES}
        self.log = open(log_path, 'w', encoding='utf-8')

        self.root = tk.Tk()
        self.root.title(title)
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.geometry('%dx%d+%d+%d' % (size[0], size[1], pos[0], pos[1]))
        self.canvas = tk.Canvas(self.root, width=size[0], height=size[1],
                                bg='#101318', highlightthickness=0)
        self.canvas.pack()
        self.photos = {}
        self.t0 = time.perf_counter()
        self.last_log = 0.0
        self.cur_idx = -1
        print('fake game pid=%d  size=%dx%d  cache=%s' % (os.getpid(), size[0], size[1], cache))
        print('log=%s' % os.path.abspath(log_path))
        self.root.after(50, self.draw)

    def read_roi(self, name: str, idx: int) -> np.ndarray:
        r = getattr(self.src, name)
        f = self.files[name]
        f.seek(idx * r.pixels)
        buf = f.read(r.pixels)
        if len(buf) < r.pixels:
            return None
        return np.frombuffer(buf, dtype=np.uint8).reshape(r.h, r.w)

    def scaled(self, name: str, idx: int):
        arr = self.read_roi(name, idx)
        if arr is None:
            return None
        r = getattr(self.dst, name)
        if arr.shape != (r.h, r.w):
            arr = np.asarray(Image.fromarray(arr).resize((r.w, r.h), Image.NEAREST))
        return arr

    def draw(self):
        elapsed = time.perf_counter() - self.t0
        t_video = self.start + elapsed * self.rate
        idx = int(t_video * self.fps)
        if idx >= self.frames or (self.duration and elapsed >= self.duration):
            self.close()
            return
        if idx != self.cur_idx:
            self.cur_idx = idx
            for name in ROI_NAMES:
                arr = self.scaled(name, idx)
                if arr is None:
                    continue
                r = getattr(self.dst, name)
                photo = ImageTk.PhotoImage(Image.fromarray(arr, mode='L'))
                self.photos[name] = photo
                self.canvas.delete('roi_' + name)
                self.canvas.create_image(r.x, r.y, anchor='nw', image=photo, tags='roi_' + name)
        now = time.time()
        if now - self.last_log >= 0.025:
            self.last_log = now
            self.log.write('%.4f %d\n' % (now, idx))
            self.log.flush()
        self.root.after(5, self.draw)

    def close(self):
        try:
            self.log.write('%.4f %d\n' % (time.time(), self.cur_idx))
            self.log.close()
        except Exception:
            pass
        for f in self.files.values():
            f.close()
        self.root.destroy()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='fake_screen.py', description='假游戏窗口（实时链路测试用）')
    p.add_argument('--cache', default=None, help='抽流缓存目录')
    p.add_argument('--size', default='1280x800', help='模拟的窗口客户区大小')
    p.add_argument('--start', type=float, default=0.0, help='从视频第几秒开始播')
    p.add_argument('--rate', type=float, default=1.0, help='播放速度倍数（1 = 实时）')
    p.add_argument('--duration', type=float, default=20.0, help='播放多少秒后自动退出')
    p.add_argument('--title', default='ArknightsFake', help='窗口标题')
    p.add_argument('--pos', default='0,0', help='窗口左上角屏幕坐标')
    p.add_argument('--log', default=None, help='source-index 日志路径')
    args = p.parse_args(argv)

    cache = args.cache
    if cache is None:
        base = os.path.join(ROOT, 'out', 'cache')
        subs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))] if os.path.isdir(base) else []
        if not subs:
            print('找不到缓存目录，请先跑一次 run_reader.py 或用 --cache 指定')
            return 1
        cache = os.path.join(base, sorted(subs)[0])
    size = tuple(int(v) for v in args.size.lower().split('x'))
    pos = tuple(int(v) for v in args.pos.split(','))
    log = args.log or os.path.join(ROOT, 'out', 'fake_log.txt')

    enable_dpi_awareness()
    game = FakeGame(cache, size, args.start, args.rate, args.duration, args.title, pos, log)
    game.root.mainloop()
    print('fake game 退出，source index 日志：%s' % os.path.abspath(log))
    return 0


if __name__ == '__main__':
    sys.exit(main())
