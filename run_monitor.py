#!/usr/bin/env python
"""akframe 实时监视入口：在游戏窗口上跑全局帧数计数器，并打开简易测试前端。

用法：
    python run_monitor.py                  # 自动找 Arknights.exe 的窗口
    python run_monitor.py --list           # 列出所有可见窗口（不知道进程名时用）
    python run_monitor.py --pid 12345      # 指定窗口
    python run_monitor.py --no-ui          # 不开界面，在控制台打印读数
    python run_monitor.py --rate 0         # 不限速，用满 CPU 跑到最快（测上限用）
    python run_monitor.py --rate 60        # 省 CPU（半个核足够），代价是偶尔漏采一帧
    python run_monitor.py --timeline maps/example.md   # 启动时直接载入打轴器的轴文件
    python run_monitor.py --cost-rate 60    # 回费速度 -50% 的 tag：一费 60 逻辑帧
    python run_monitor.py --demo-axis      # 演示模式：不找游戏，用假费用条推着面板跑
    python run_monitor.py --demo-axis --demo-speed 4 --demo-pause 8
                                           # 4 倍速快进 + 每 8 秒暂停 1.2 秒

回费速率默认读设置文件 cost_rate.json（没有这个文件就按 30 = 1 秒 1 费），
也可以在这里用 --cost-rate 覆盖；界面上那行「回费速率」改完会写回设置文件。

界面里的键位见 ui.py 顶部注释（Tab/方向键校准 ROI，K 显示 ROI 框，S 保存，L 记录 CSV）。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

from akframe.cli import DEFAULT_TEMPLATES, load_templates          # noqa: E402
from akframe.demo import DEMO_AXIS_TEXT, DEMO_SIZE, DemoMonitor     # noqa: E402
from akframe.geometry import apply_deltas, layout_for, load_calibration  # noqa: E402
from akframe.live import LiveMonitor                                # noqa: E402
from akframe.paths import data_path                                 # noqa: E402
from akframe.settings import (DEFAULT_FRAMES_PER_COST, clamp_frames_per_cost,  # noqa: E402
                              format_frames_per_cost, load_cost_rate)
from akframe.win32 import (arknights_ui_scaler, enable_dpi_awareness,  # noqa: E402
                           find_window, list_windows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='run_monitor.py',
                                description='明日方舟全局逻辑帧计数器 —— 实时监视（PC 窗口）')
    p.add_argument('--exe', default='Arknights.exe', help='游戏进程名（默认 Arknights.exe）')
    p.add_argument('--title', default=None, help='窗口标题里的关键字（可选，比进程名更精确）')
    p.add_argument('--pid', type=int, default=None, help='直接指定进程号')
    p.add_argument('--rate', type=float, default=120.0,
                   help='每秒采样次数（默认 120；0 = 不限速）。'
                        '1 倍速下一个逻辑帧只有 33.3ms，采样必须远快于 30Hz 才不会整帧漏采；'
                        '实测本机一拍（抓屏+特征）约 6.7ms，上限约 150Hz。'
                        '机器吃不消时降到 60，代价是偶尔会漏采一帧（下一拍一次 +2）')
    p.add_argument('--templates', default=DEFAULT_TEMPLATES, help='倍速模板目录')
    p.add_argument('--calib', default=data_path('layout_calib.json'),
                   help='ROI 校准文件（默认和程序放一起）')
    p.add_argument('--ui-scaler', type=float, default=None,
                   help='游戏内「UI 缩放」0..1（默认从注册表读；读不到按 1.0）')
    p.add_argument('--cost-rate', default=None,
                   help='回费速率 = 一费几个逻辑帧。不填就读设置文件（--settings），'
                        # argparse 会对 help 文本做 % 格式化，裸的 % 必须写成 %%
                        '30 = 正常（1 秒 1 费）；60 = 回费速度 -50%%；120 = -75%%。'
                        'auto = 开自动识别（可选功能：从费用条一个周期里出现过多少种'
                        '宽度自己认，认错档会让绝对帧号整段漂移，所以默认不用）')
    p.add_argument('--settings', default=data_path('cost_rate.json'),
                   help='回费速率设置文件（默认和程序放一起；界面里改档会写回这里）')
    p.add_argument('--csv', default=None, help='把逐帧读数记录到这个 CSV')
    p.add_argument('--list', action='store_true', help='列出可见窗口后退出')
    p.add_argument('--no-ui', action='store_true', help='不开图形界面，控制台打印')
    p.add_argument('--duration', type=float, default=0.0, help='运行多少秒后自动退出（0=不限）')
    p.add_argument('--timeline', default=None,
                   help='打轴器：启动时直接导入的 md 轴文件（界面里也能按 T 换）')
    # ---- 演示模式：没有游戏画面也能把面板跑起来（刻度表预览 / 调界面用）----
    p.add_argument('--demo-axis', action='store_true',
                   help='演示模式：不找游戏窗口，用合成的费用条推着面板跑。'
                        '刻度表只在「计数器在动」时才会画，这一档就是给刻度表看的')
    p.add_argument('--demo-speed', type=float, default=1.0,
                   help='演示里每秒走几倍速的逻辑帧（默认 1 = 每秒 30 帧；'
                        '嫌轴来得太慢就调大，比如 4 就是 4 倍速快进）')
    p.add_argument('--demo-bullet', action='store_true', help='演示子弹时间（恒 6 帧/秒）')
    p.add_argument('--demo-max', action='store_true',
                   help='演示全程满费（逼状态机走挂钟通道，帧号会带小数）')
    p.add_argument('--demo-pause', type=float, default=0.0,
                   help='演示里每这么多秒暂停 1.2 秒（0 = 不暂停），用来看数字会不会停住')
    p.add_argument('--demo-res', default='%dx%d' % DEMO_SIZE,
                   help='演示用的客户区尺寸（默认 %dx%d，正好命中实测校准表）' % DEMO_SIZE)
    return p


def resolve_cost_rate(args):
    """命令行 --cost-rate 优先；没给就读设置文件。返回 (值, 报错信息)。

    值可以是数字（手工档，一费几个逻辑帧）或 'auto'（开自动识别）。
    """
    if args.cost_rate is None or str(args.cost_rate).strip() == '':
        return (load_cost_rate(args.settings), None)
    text = str(args.cost_rate).strip().lower()
    if text in ('auto', 'off', 'none'):
        return ('auto', None)
    try:
        v = float(text)
    except ValueError:
        return (None, '--cost-rate 要填 auto 或一个正数（一费几个逻辑帧，例如 60）：%r'
                % (args.cost_rate,))
    if not (v > 0):
        return (None, '--cost-rate 要填正数（一费几个逻辑帧，例如 60）')
    return (clamp_frames_per_cost(v), None)


def build_monitor(args, cost_rate=DEFAULT_FRAMES_PER_COST) -> LiveMonitor:
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
                      build_layout=build_layout, cost_rate=cost_rate,
                      ui_scaler=ui_scaler)
    # 把刚找到的窗口交给监视器：不然 main() 里判 mon.window 永远是 None，
    # 明明找到了也会打印「找不到窗口」，白白吓人一跳
    mon.window = win
    mon.ui_scaler = ui_scaler          # 给 main() 打印用（不是 LiveMonitor 的必需字段）
    return mon


def build_demo(args, cost_rate) -> DemoMonitor:
    """--demo-axis：不找游戏窗口，用合成输入把面板推起来。

    存在的理由：刻度表只在「计数器在动」时才画（ui._update_prompter 在 res 为 None 时
    直接 return），所以游戏不在关卡里的时候，刻度表区域永远只有一句提示文字 ——
    改刻度表就只能靠玩游戏时顺手瞄一眼。这一档把「画面」换成合成的费用条，
    面板、状态机、打轴器、刻度表全是真的（见 akframe/demo.py）。
    """
    text = str(args.demo_res).lower().replace('*', 'x')
    try:
        w, h = (int(v) for v in text.split('x')[:2])
    except ValueError:
        raise SystemExit('--demo-res 要写成 宽x高（比如 1920x1080），收到的是 %r'
                         % args.demo_res)
    return DemoMonitor((w, h), speed=args.demo_speed, bullet=args.demo_bullet,
                       maxed=args.demo_max, pause_every=args.demo_pause, rate=args.rate,
                       cost_rate=cost_rate, ui_scaler=args.ui_scaler)


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
                    est = mon.rate_estimate
                    fpc = (mon.counter.frames_per_cost if mon.counter is not None
                           else DEFAULT_FRAMES_PER_COST)
                    rate = ''
                    if abs(fpc - DEFAULT_FRAMES_PER_COST) > 1e-9:
                        rate = ' | 回费 %.0f帧/费(%s)' % (
                            fpc, '自动' if (est is not None and est.frames_per_cost) else '设置')
                    # 子弹时间下倍速数字不在画面上（r.m 是 nan），别印成「nanX」。
                    if r.bullet:
                        m_txt = '子弹6帧/s'      # 与倍速设置无关，见 docs 3.1
                    elif r.m == r.m:
                        m_txt = '%.0fX' % r.m
                    else:
                        m_txt = '倍速?'
                    print('[%6.1fs] 帧 %-6d 时间 %s | 条 %-9s fill %3d/%-3d | %s | %-11s '
                          '| 通道 %-5s | %.1fHz %.1fms%s%s'
                          % (now - t0, r.elapsed, _fmt_time(r.elapsed / 30.0), r.bar_state,
                             r.fill, mon.layout.bar_w, '运行中' if r.running else '已暂停',
                             m_txt, r.mode, st.hz, st.grab_ms,
                             (' | ' + r.warn) if r.warn else '', rate))
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

    cost_rate, why = resolve_cost_rate(args)
    if why:
        print(why)
        return 2

    mon = build_demo(args, cost_rate) if args.demo_axis else build_monitor(args, cost_rate)
    if args.demo_axis:
        print('演示模式：没有游戏输入，帧号由合成的费用条推出来（刻度表预览 / 调界面用）')
        print('假世界  : %s' % mon.describe())
        print('ROI     : bar=%s (scale=%.4f，条槽真实宽 %.2f px)'
              % (mon.layout.bar, mon.layout.scale, mon.layout.bar_width_f))
    elif mon.window is None:
        print('找不到窗口（进程名 %s）。用 --list 看看有哪些窗口，或加 --pid。' % args.exe)
    else:
        print('目标窗口: %s' % mon.window)
        print('ROI     : bar=%s pause=%s speed=%s (scale=%.4f)'
              % (mon.layout.bar, mon.layout.pause, mon.layout.speed, mon.layout.scale))
        print('条槽真实宽: %.2f px，UI 缩放 %.2f' % (mon.layout.bar_width_f, mon.ui_scaler))
    fpc = mon.counter.frames_per_cost if mon.counter else DEFAULT_FRAMES_PER_COST
    print('回费速率: %s%s' % (
        format_frames_per_cost(fpc),
        '（自动识别已开启，认准后自动切换）' if mon.rate_est is not None else
        '，改档在界面「回费速率」那行，会写回 %s' % args.settings))
    print('DPI 感知: %s' % dpi)

    if args.no_ui:
        return run_console(mon, args.duration)

    from akframe.ui import MonitorApp
    # 演示模式默认塞一份内置轴（除非用户明确给了 --timeline 文件）：刻度表要有轴才画得满，
    # 轴不来自文件也不影响，面板输入框里能直接改、能按 Ctrl+Enter 重解析。
    text = DEMO_AXIS_TEXT if (args.demo_axis and not args.timeline) else None
    app = MonitorApp(mon, calib_path=args.calib, settings_path=args.settings,
                     timeline_path=args.timeline, timeline_text=text)
    mon.start()
    if args.duration:
        app.root.after(int(args.duration * 1000), app.quit)
    app.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
