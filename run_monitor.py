#!/usr/bin/env python
"""akframe 实时监视入口：在游戏窗口上跑全局帧数计数器，并打开简易测试前端。

用法：
    python run_monitor.py                  # 自动找 Arknights.exe 的窗口
    python run_monitor.py --list           # 列出所有可见窗口（不知道进程名时用）
    python run_monitor.py --pid 12345      # 指定窗口
    python run_monitor.py --no-ui          # 不开界面，在控制台打印读数
    python run_monitor.py --rate 0         # 不限速，用满 CPU 跑到最快（测上限用）
    python run_monitor.py --timeline maps/example.md   # 启动时直接载入打轴器的轴文件

界面里的键位见 ui.py 顶部注释（Tab/方向键校准 ROI，K 显示 ROI 框，S 保存，L 记录 CSV）。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

from akframe.cli import DEFAULT_TEMPLATES, load_templates          # noqa: E402
from akframe.geometry import apply_deltas, layout_for, load_calibration  # noqa: E402
from akframe.live import LiveMonitor                                # noqa: E402
from akframe.win32 import (arknights_ui_scaler, enable_dpi_awareness,  # noqa: E402
                           find_window, list_windows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='run_monitor.py',
                                description='明日方舟全局逻辑帧计数器 —— 实时监视（PC 窗口）')
    p.add_argument('--exe', default='Arknights.exe', help='游戏进程名（默认 Arknights.exe）')
    p.add_argument('--title', default=None, help='窗口标题里的关键字（可选，比进程名更精确）')
    p.add_argument('--pid', type=int, default=None, help='直接指定进程号')
    p.add_argument('--rate', type=float, default=60.0,
                   help='每秒采样次数（默认 60；0 = 不限速）')
    p.add_argument('--templates', default=DEFAULT_TEMPLATES, help='倍速模板目录')
    p.add_argument('--calib', default='layout_calib.json', help='ROI 校准文件')
    p.add_argument('--ui-scaler', type=float, default=None,
                   help='游戏内「UI 缩放」0..1（默认从注册表读；读不到按 1.0）')
    p.add_argument('--csv', default=None, help='把逐帧读数记录到这个 CSV')
    p.add_argument('--list', action='store_true', help='列出可见窗口后退出')
    p.add_argument('--no-ui', action='store_true', help='不开图形界面，控制台打印')
    p.add_argument('--duration', type=float, default=0.0, help='运行多少秒后自动退出（0=不限）')
    p.add_argument('--timeline', default=None,
                   help='打轴器：启动时直接导入的 md 轴文件（界面里也能按 T 换）')
    return p


def build_monitor(args) -> LiveMonitor:
    templates = load_templates(args.templates)
    deltas, calib_scale = load_calibration(args.calib)
    ui_scaler = args.ui_scaler if args.ui_scaler is not None else arknights_ui_scaler()

    def build_layout(w: int, h: int):
        # ROI 只和窗口尺寸 + 游戏内「UI 缩放」有关（后者只乘在距右/距底上）
        base = layout_for(w, h, ui_scaler)
        if deltas:
            return apply_deltas(base, deltas, calib_scale)
        return base

    win = find_window(exe=args.exe, title=args.title, pid=args.pid)
    layout = build_layout(*win.client_size) if win else None
    mon = LiveMonitor(layout, exe=args.exe, title=args.title, pid=args.pid,
                      rate=args.rate, templates=templates, csv_path=args.csv,
                      build_layout=build_layout)
    # 把刚找到的窗口交给监视器：不然 main() 里判 mon.window 永远是 None，
    # 明明找到了也会打印「找不到窗口」，白白吓人一跳
    mon.window = win
    mon.ui_scaler = ui_scaler          # 给 main() 打印用（不是 LiveMonitor 的必需字段）
    return mon


def run_console(mon: LiveMonitor, duration: float = 0.0) -> int:
    """无界面模式：每秒打印一行，用来在终端里确认读数对不对。"""
    mon.start()
    t0 = time.perf_counter()
    last_print = 0.0
    try:
        while True:
            time.sleep(0.1)
            now = time.perf_counter()
            if now - last_print >= 1.0:
                last_print = now
                upd = mon.latest
                st = mon.stats
                if upd.result is None:
                    print('[%6.1fs] %s | 采样 %.1fHz 抓屏 %.1fms' % (now - t0, upd.note or '未就绪',
                                                              st.hz, st.grab_ms))
                else:
                    r = upd.result
                    print('[%6.1fs] 帧 %-6d 时间 %s | 条 %-9s fill %3d/%-3d | %s | %.0fX%s '
                          '| 通道 %-5s | %.1fHz %.1fms%s'
                          % (now - t0, r.elapsed, _fmt_time(r.elapsed / 30.0), r.bar_state,
                             r.fill, mon.layout.bar_w, '运行中' if r.running else '已暂停',
                             r.m, ' 子弹' if r.bullet else '', r.mode, st.hz, st.grab_ms,
                             (' | ' + r.warn) if r.warn else ''))
            if duration and now - t0 >= duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
    st = mon.stats
    print()
    if mon.counter is None:
        print('没有找到窗口，全程未计数。')
        return 1
    print('累计逻辑帧: %d  (%.2f 秒游戏内 1 倍速时间)'
          % (mon.counter.elapsed, mon.counter.elapsed / 30.0))
    print('采样 %.0f 次，平均 %.1f Hz，抓屏 %.1f ms，读不到累计 %.2f s'
          % (st.samples, st.hz, st.grab_ms, st.unreadable_s))
    return 0


def _fmt_time(seconds: float) -> str:
    m = int(seconds // 60)
    return '%02d:%05.2f' % (m, seconds - m * 60)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    dpi = enable_dpi_awareness()          # 必须在建窗口/抓屏之前

    if args.list:
        for w in list_windows():
            print(w)
        return 0

    mon = build_monitor(args)
    if mon.window is None:
        print('找不到窗口（进程名 %s）。用 --list 看看有哪些窗口，或加 --pid。' % args.exe)
    else:
        print('目标窗口: %s' % mon.window)
        print('ROI     : bar=%s pause=%s speed=%s (scale=%.4f)'
              % (mon.layout.bar, mon.layout.pause, mon.layout.speed, mon.layout.scale))
        print('条槽真实宽: %.2f px，UI 缩放 %.2f' % (mon.layout.bar_width_f, mon.ui_scaler))
    print('DPI 感知: %s' % dpi)

    if args.no_ui:
        return run_console(mon, args.duration)

    from akframe.ui import MonitorApp
    app = MonitorApp(mon, calib_path=args.calib, timeline_path=args.timeline)
    mon.start()
    if args.duration:
        app.root.after(int(args.duration * 1000), app.quit)
    app.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
