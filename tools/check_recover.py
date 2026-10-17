#!/usr/bin/env python
"""采样自愈自检：莫名卡死必须能被兜住、能被看见、能被一键救回来。

为什么单独一个检查：用户报了「有些时候会莫名卡死」（2026-10-07 实拍录像
Desktop 2026.10.07 - 13.56.18.01.mp4，21 秒）。从那段视频里量出来的是：

    * 面板一路显示「采样 116.2 Hz」，但「采样 656 次」从头到尾一次没变过；
    * 「面板 N Hz」在 48~76 之间正常跳动（root.after 那个循环还活着）；
    * 游戏那边在正常走（MAA 锁屏暂停，费用条在动）。

结论：**采样线程死了，而界面上没有任何一处看得出来** —— 用户看到的就是「莫名卡死」。

根因：live.LiveMonitor.run() 原来是裸循环，_sample() 抛一次异常，这个 daemon 线程
就静默退出了。更糟的是打包后用 pythonw.exe 跑根本没有控制台，traceback 无处可去。

检查什么（前 6 项都不需要真游戏窗口）：
    1. 采样循环抛异常不再杀线程：异常计数涨、心跳继续涨、线程还活着；
    2. 异常之后能自己恢复采样，不需要人来点；
    3. 异常会把窗口/抓屏器缓存丢掉（异常多半就是「窗口动了」这一类）；
    4. 心跳在读不到画面时照样涨（这是「区分暂停和死掉」的前提）；
    5. set_layout(keep_elapsed=True) 真的保住已数帧数 —— 这一条以前是假的，
       只搬了一天 elapsed，下一拍就被重算成 0（见 state.Counter.adopt）；
    6. recalibrate()：找不到窗口时不丢帧数；能把死掉的线程拉起来，而且
       **不重置时间基准 _t0**（重置会让挂钟通道拿到倒退的时间戳、算出负 dt）；
    7. 面板看门狗：心跳不涨 -> 状态行转红写「采样已停」（Tk 可用时才测）。
       注意 2026-10-11 起面板起来是**待命**态（主按钮写「开始」，见 ui._counting_started）：
       没点开始的时候采样线程本来就该是停的，心跳不涨**不算故障**、不许报警。

用法：python tools/check_recover.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.demo import DemoMonitor                      # noqa: E402
from akframe.geometry import layout_for                   # noqa: E402
from akframe.live import LiveMonitor                      # noqa: E402

# 用实测校准表覆盖的那台机器尺寸，免得测试结果跟着当前机器的分辨率变
LAY = layout_for(2558, 1494, 1.0)
NO_WIN = '(检查脚本：这个进程名不存在)'

FAILED = []
SKIPPED = []


def check(name, cond, detail=''):
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s   %s' % (name, detail))
        FAILED.append(name)


def skip(name, why):
    print('  skip %s   %s' % (name, why))
    SKIPPED.append(name)


class FakeClock:
    """假挂钟：推一下走一步。DemoMonitor 塞它进来就能脱离真实时间推演。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def step(self, dt):
        self.t += dt


class AliveThread:
    """假装线程还活着（看门狗只看 is_alive / join）。"""

    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass


class FlakyMonitor(LiveMonitor):
    """前 fail_times 次采样故意抛异常，之后正常返回。

    不需要真窗口：恢复之后走 _unreadable 那条早退分支就够了 —— 这里测的是
    「循环会不会被异常打死」，不是抓屏本身。
    """

    def __init__(self, fail_times, **kw):
        super().__init__(**kw)
        self.fail_times = int(fail_times)
        self.calls = 0

    def _sample(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError('故意炸的第 %d 拍' % self.calls)
        return self._unreadable(0.0, '检查脚本：没有窗口')


def wait_until(cond, timeout=3.0, tick=0.005):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(tick)
    return bool(cond())


def t_loop_survives():
    """1+2+3：异常不再杀线程、异常后自愈、缓存被丢掉。"""
    mon = FlakyMonitor(3, layout=LAY, exe=NO_WIN, rate=250.0)
    mon.window = object()        # 假装缓存了一个窗口句柄
    mon._grabber = object()      # 和一份 DIB
    try:
        mon.start()
        wait_until(lambda: mon.stats.errors >= 3 and mon.calls >= 5)
        check('抛 3 次异常之后采样线程还活着', mon.alive,
              'alive=%s ticks=%d errors=%d' % (mon.alive, mon.stats.ticks, mon.stats.errors))
        check('异常被逐次记进 stats', mon.stats.errors == 3,
              'errors=%d last=%r' % (mon.stats.errors, mon.stats.last_error))
        check('心跳一直在涨（异常也没让它停）', mon.stats.ticks >= 5,
              'ticks=%d calls=%d' % (mon.stats.ticks, mon.calls))
        check('异常之后自己恢复了采样', mon.calls >= 5 and mon.latest.note.startswith('检查脚本'),
              'note=%r' % mon.latest.note)
        check('异常把窗口 / 抓屏器缓存丢掉了', mon.window is None and mon._grabber is None,
              'window=%r grabber=%r' % (mon.window, mon._grabber))
    finally:
        mon.stop()


def t_heartbeat_when_unreadable():
    """4：读不到画面时心跳也必须涨。

    这一条是看门狗的前提：如果「找不到窗口」时心跳不涨，面板就会把「游戏没开 /
    窗口最小化」误报成「采样线程死了」，红字一响反而没人信。
    """
    mon = LiveMonitor(LAY, exe=NO_WIN, rate=250.0)
    try:
        mon.start()
        wait_until(lambda: mon.stats.ticks > 0)
        a = mon.stats.ticks
        time.sleep(0.2)
        b = mon.stats.ticks
        check('读不到画面时心跳照样涨', b > a, 'ticks %d -> %d' % (a, b))
        check('并且这时的读数标着「读不到」', mon.latest.readable is False,
              'readable=%s note=%r' % (mon.latest.readable, mon.latest.note))
    finally:
        mon.stop()


def t_set_layout_keeps_frames():
    """5：换布局保住帧数（adopt），keep_elapsed=False 仍然清零。"""
    clock = FakeClock()
    dm = DemoMonitor(rate=120.0, clock=clock)
    for _ in range(240):                     # 2 秒 = 60 个逻辑帧
        clock.step(1.0 / 120.0)
        dm._sample()
    before = dm.counter.elapsed
    dm.set_layout(dm.layout, keep_elapsed=True)
    clock.step(1.0 / 120.0)
    dm._sample()
    after = dm.counter.elapsed
    check('换布局不丢已数帧数（Counter.adopt）', before > 0 and after >= before,
          'before=%d after=%d' % (before, after))

    clock2 = FakeClock()
    dm2 = DemoMonitor(rate=120.0, clock=clock2)
    for _ in range(120):
        clock2.step(1.0 / 120.0)
        dm2._sample()
    had = dm2.counter.elapsed
    dm2.set_layout(dm2.layout, keep_elapsed=False)
    check('keep_elapsed=False 仍然是清零语义', had > 0 and dm2.counter.elapsed == 0,
          'had=%d now=%d' % (had, dm2.counter.elapsed))


def t_recalibrate():
    """6：重新校准 —— 找不到窗口不丢帧数；能把死线程拉起来且不动 _t0。"""
    clock = FakeClock()
    dm = DemoMonitor(rate=120.0, clock=clock)
    dm._thread = AliveThread()               # 别让它真起线程（会按真实挂钟补帧）
    for _ in range(240):
        clock.step(1.0 / 120.0)
        dm._sample()
    before = dm.counter.elapsed
    note = dm.recalibrate()
    check('recalibrate 找不到窗口时把话说清楚', '没找到窗口' in note, repr(note))
    check('recalibrate 不丢已数帧数', dm.counter.elapsed == before,
          'before=%d after=%d' % (before, dm.counter.elapsed))
    check('recalibrate 顺手清掉上一次的异常记录',
          dm.stats.errors == 0 and not dm.stats.last_error, 'errors=%d' % dm.stats.errors)

    mon = LiveMonitor(LAY, exe=NO_WIN, rate=250.0)
    mon._t0 = 12345.0                        # 钉一个确定的时间基准
    mon._thread = None                       # 假装采样线程已经死了
    try:
        note2 = mon.recalibrate()
        check('recalibrate 能把死掉的采样线程拉起来', mon.alive, repr(note2))
        check('重启时不动时间基准 _t0（否则挂钟会算出负 dt）', mon._t0 == 12345.0,
              '_t0=%r' % mon._t0)
        check('返回语里说明了「线程已重启」', '重启' in note2, repr(note2))
    finally:
        mon.stop()


def t_watchdog_ui():
    """7：面板看门狗把「采样死了」和「游戏暂停」分开。"""
    try:
        import tkinter as tk
        from akframe import ui as ui_mod
    except Exception as exc:                 # noqa: BLE001
        skip('面板看门狗', 'Tk 不可用：%s' % exc)
        return
    dm = DemoMonitor(rate=120.0, clock=FakeClock())
    try:
        app = ui_mod.MonitorApp(
            dm, calib_path=os.path.join(ROOT, 'layout_calib.json'),
            settings_path=os.path.join(os.environ.get('TEMP', '.'),
                                       'akframe_check_nope.json'))
    except tk.TclError as exc:
        skip('面板看门狗', '建不出窗口：%s' % exc)
        return
    try:
        app.root.withdraw()
        app._tick_body()
        check('看门狗每拍记下心跳', app._last_ticks == dm.stats.ticks,
              '_last_ticks=%d ticks=%d' % (app._last_ticks, dm.stats.ticks))

        # 待命态（用户还没点「开始」）：采样线程本来就该是停的 —— 心跳冻住 1 秒多
        # 也**不许**报「采样已停」。这是 2026-10-11「开始键」那次改动的配套语义：
        # 面板一起来就自动 start 的话，「开始」那一态根本没机会出现；既然改成待命，
        # 看门狗就得认这个待命态，否则一开面板就红着报警。
        dm._thread = None
        time.sleep(1.05)
        app._tick_body()
        check('待命态（没点开始）：心跳不涨也不报「采样已停」',
              not app._stall_logged, app._stall_logged)

        # 用户点了「开始」—— 从这一拍起看门狗才该管事。
        app._counting_started = True
        app._stall_since = 0.0

        dm._thread = AliveThread()            # 线程「活着」，但心跳从此冻住
        frozen = dm.stats.ticks
        app._tick_body()
        check('心跳一停下，看门狗就开始计时', app._stall_since != 0.0)
        time.sleep(1.05)                      # 超过 STALL_WARN_S = 1.0
        app._tick_body()
        check('心跳停 1 秒以上 -> 控制台报一次「采样已停」',
              app._stall_logged, app._stall_logged)
        check('看门狗没冤枉读数（心跳确实没涨）', dm.stats.ticks == frozen,
              'ticks=%d frozen=%d' % (dm.stats.ticks, frozen))

        dm._thread = None                     # 线程直接没了 -> 不用等一秒
        app._stall_logged = False             # 模拟上一次故障解除后重新进入
        app._tick_body()
        check('线程消失 -> 立刻报采样已停', app._stall_logged)

        dm._thread = AliveThread()
        app.recalibrate()
        check('重新校准后看门狗计时归零（不然刚重开又被「停了 8 秒」再报）',
              app._stall_since == 0.0, '_stall_since=%r' % app._stall_since)
        app._tick_body()
        check('重新校准后不再报采样已停', not app._stall_logged)
        check('界面里没有告警条了', not hasattr(app, 'lb_warn'))
    finally:
        try:
            app.root.destroy()
        except Exception:                     # noqa: BLE001
            pass


def main():
    print('采样自愈自检（莫名卡死 / 重新校准）')
    print()
    print('1. 采样循环兜异常')
    t_loop_survives()
    print('2. 心跳语义')
    t_heartbeat_when_unreadable()
    print('3. set_layout 保留帧数')
    t_set_layout_keeps_frames()
    print('4. 重新校准')
    t_recalibrate()
    print('5. 面板看门狗')
    t_watchdog_ui()
    print()
    if SKIPPED:
        print('跳过 %d 项：%s' % (len(SKIPPED), ', '.join(SKIPPED)))
    if FAILED:
        print('失败 %d 项：%s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
