#!/usr/bin/env python
"""面板「读取窗口」那条路的自检：选窗口 -> 校准 -> 点「开始计数」-> 真的在数。

为什么单独一个检查：后端（window.py）自己已经被 tools/check_window.py 验过了，但
「校准完**不**自动跑」「点开始才跑」「重新校准保留帧数」「框选的屏幕坐标 -> 客户区坐标」
这几条是**面板**的账，只有把整个 MonitorApp 建起来才验得到。

怎么做到不弹窗打扰人：root.withdraw() 把面板藏起来，帧源换成 tools/check_window.py
那份假屏幕（import 复用它的夹具），校准表也换成一个临时文件（别把用户的
window_calib.json 写花）。

用法：python tools/check_ui_window.py
"""

import atexit
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_window as CW                                          # noqa: E402

import tkinter as tk                                               # noqa: E402

import akframe.ui as ui                                            # noqa: E402
import akframe.window as W                                         # noqa: E402
from akframe.demo import DemoMonitor                               # noqa: E402
from akframe.geometry import Rect                                  # noqa: E402
from akframe.window import load_window_profiles, window_key        # noqa: E402

FAIL = []
FPS = 30.0


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def tmp_path(name):
    d = tempfile.mkdtemp(prefix='akframe_uiwin_')
    atexit.register(shutil.rmtree, d, ignore_errors=True)   # 跑完自己收尾
    return os.path.join(d, name)


def make_app(prof, settings):
    """建一个面板：初始输入用演示链路（合成费用条，不需要游戏窗口）。"""
    mon = DemoMonitor(CW.WIN_SIZE, speed=1.0, rate=60.0)
    app = ui.MonitorApp(mon, calib_path=tmp_path('layout_calib.json'),
                        settings_path=settings, poll_ms=8)
    app.root.withdraw()
    app.monitor.start()
    # 校准表：面板自己挑的是 data_path 里那份，检查时换成临时的
    real_src, real_mon = ui.WindowSource, ui.WindowMonitor

    class TmpSource(real_src):
        def __init__(self, win, **kw):
            kw['profile_path'] = prof
            real_src.__init__(self, win, **kw)

    class TmpMonitor(real_mon):
        def __init__(self, win, **kw):
            kw['profiles_path'] = prof
            real_mon.__init__(self, win, **kw)

    ui.WindowSource, ui.WindowMonitor = TmpSource, TmpMonitor
    return app, (real_src, real_mon), TmpSource, TmpMonitor


def pump(app, cond, seconds=240.0, label=''):
    """直接调 _tick_body 当主循环（不用真的进 mainloop）。

    预算 240s 是怎么来的：这个检查里两处等待都在等「搜右上角两个按钮」那道网格搜索 ——
    在 1378x844 的合成画面上它**自己**就要 30 秒纯 CPU（实测 31.1s；HEAD 那份 32.6s），
    而采样线程同时在抢 GIL，真跑下来是 50~100 秒。原来写 60s，一度量到 55.3s 勉强过 ——
    余量太小，机器一忙就红，而红出来的那几条（ROI 没重算 / 校准表没写）全是这一条的
    连锁反应，看不出真因（2026-10-11 就是这么红的）。
    """
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        app._tick_body()
        if cond():
            return True
        time.sleep(0.005)
    return False


def main() -> int:
    prof = tmp_path('window_calib.json')
    settings = tmp_path('cost_rate.json')
    truth = CW.scene_layout()
    screen = CW.FakeScreen(CW.WIN_SIZE, truth)
    CW.install(screen)
    # 面板里的 client_rect 是 import 时绑过去的，也要换成假的
    ui.client_rect = W.client_rect

    app, reals, TmpSource, TmpMonitor = make_app(prof, settings)
    try:
        win = CW.fake_window()
        # ---- 1. 校准（自动）----
        app.start_window_calib(win)
        ok = pump(app, lambda: app._src_kind == 'window')
        check('读窗口：校准完把面板切到窗口链路', ok,
              'src_kind=%s' % app._src_kind)
        mon = app.monitor
        check('读窗口：用的是 WindowMonitor（不是实时那条）',
              isinstance(mon, W.WindowMonitor), type(mon).__name__)
        lay = app._base_layout()
        check('读窗口：方向键的基准 = 校准出来那份（不是分辨率表的预测）',
              lay is not None and abs(lay.bar.x - truth.bar.x) <= 2
              and abs(lay.bar.w - truth.bar.w) <= 2,
              'bar.x %s（真值 %d）、条宽 %s（真值 %d）'
              % (None if lay is None else lay.bar.x, truth.bar.x,
                 None if lay is None else lay.bar.w, truth.bar.w))
        # ---- 2. 校准完**不**自动跑 ----
        check('校准完不自动起采样线程（等用户点开始）', not mon.alive)
        check('按钮这时候写的是「开始」',
              app.btn_start.cget('text') == '开始',
              app.btn_start.cget('text'))
        app._tick_body()
        app._tick_body()
        check('没点开始也不算「采样已停」（那是正常待命，不是故障）',
              '采样已停' not in app.lb_warn.cget('text'), app.lb_warn.cget('text'))

        # ---- 2b. 清零：**没在采样**时也得立刻归 0 ----
        # 面板大数字读的是 monitor.latest.result.elapsed，而那一份只在「这一拍真读到
        # 画面」时才刷新；校准完还没点开始计数时根本就没有这一拍。所以清零必须顺手把
        # 面板自己按回 0（用户报的「清零要等开始计数才生效」）。
        # 这里故意先写个假数字进去：不清零的话它就该一直挂在那儿。
        # 必须走 _set_text（面板写大数字的**唯一**入口）：它带内容缓存（memo），绕过它
        # 直接 configure 会让缓存和界面对不上 —— 清零那一次写入会被当成「内容没变」
        # 跳过去，大数字就一直挂着这个假数字（这条曾经这么红过）。
        app._set_text('frames', app.lb_frames, '123')
        app.clear_count()
        check('没在采样时清零：大数字立刻归 0（不等下一拍）',
              app.lb_frames.cget('text') == '0', app.lb_frames.cget('text'))
        check('没在采样时清零：时间行也归 0',
              app.lb_time.cget('text') == '时间 00:00.00', app.lb_time.cget('text'))

        # ---- 3. 点「开始计数」-> 真的在数，而且一费 = 30 帧 ----
        app.toggle_count()
        check('点开始之后采样线程起来了', mon.alive)
        check('按钮翻成「暂停」（开始 / 暂停 / 继续三态里的第二态）',
              app.btn_start.cget('text') == '暂停',
              app.btn_start.cget('text'))
        t0 = time.perf_counter()
        screen.t0 = t0
        mon._t0 = t0                       # 假屏幕和计数器的挂钟对齐
        # 先采一拍，再清零。为什么要这个顺序：LiveMonitor.reset -> Counter.mark_zero_start(fill)
        # 要知道「清零那一刻条画到第几帧」，fill 读不到（还没采过）就只能
        # 保守地一律不补 —— 那样「从几帧开始数」就取决于第一拍落在周期第几帧，
        # 总数就没有固定真值可比了（机器一忙，第一拍可能落在第 8 帧）。
        # 2026-10-11 起「清零」的语义是「停采样 + 归零 + 按钮回『开始』」
        # （看板批注「点清零后变成开始」），所以要接着数就得再点一次「开始计数」。
        # 这里照用户的真实操作顺序走一遍。
        mon._sample()
        app.clear_count()
        check('清零：采样顺手停了、按钮回到「开始」',
              (not mon.alive) and app.btn_start.cget('text') == '开始',
              '%s alive=%s' % (app.btn_start.cget('text'), mon.alive))
        e0 = mon.counter.elapsed
        check('清零之后立刻从 0 开始数', e0 == 0, '清零后读到 %s' % e0)
        app.toggle_count()
        check('清零后再点「开始计数」：采样线程又起来了', mon.alive)
        idx0 = screen.last_idx
        while time.perf_counter() - t0 < 3.6:
            mon._sample()
        cyc = list(mon.counter.cycle_lengths)
        check('窗口链路数出来的「一费」就是 30 帧', len(cyc) >= 2 and set(cyc) == {30},
              '%d 个周期：%s' % (len(cyc), cyc))
        elapsed = mon.counter.elapsed
        # 变量名别叫 truth：main() 里已经有一个 truth = 场景真值（Layout），
        # 同名会把它盖掉，后面 '框选用的真值条' 就会拿到一个 int。
        frames_seen = screen.last_idx - idx0
        check('总帧数 = 清零之后画面走过的逻辑帧', abs(elapsed - frames_seen) <= 3,
              '数到 %d 帧，假窗口走了 %d 帧（帧号 %d..%d）'
              % (elapsed, frames_seen, idx0, screen.last_idx))

        # ---- 4. 框选费用条：框（客户区坐标）-> 钉住 ROI ----
        # 面板这条路走的是 ui.BoxPicker（参照图 + 橙色刻度尺 + 放大镜：四条边 / 平移 / 滚轮局部放大），
        # 它交回来的框**已经是客户区坐标**了（见 BoxPicker._apply -> _to_img 的换算），
        # 所以「用户框完点了用这个框」那一下就是 _apply_window_box(box)。
        # 框本身 -> 客户区尺寸 / 原点 / 三个 ROI 的换算由 tools/check_boxpick.py 钉死，
        # 这里只管「面板拿到框之后有没有重算、有没有把帧数留住」。
        box = truth.bar
        x1 = box.x + 2
        x2 = box.x2 - 2                      # 宽度真取：ROI 的左边缘 = 框的左边缘
        y1 = max(0, box.y - 40)
        y2 = min(CW.WIN_SIZE[1], box.y + box.h + 40)   # 高度随便：ROI 只取中心线 17 行
        app._apply_window_box(Rect(x1, y1, x2 - x1, y2 - y1))
        check('框完之后取景器关掉了', app._picker is None)
        ok = pump(app, lambda: not app._window_busy and app.monitor is not mon)
        # 失败时把「那道工序现在什么状态 / 提示行说了什么」一起报出来：
        # 这条偶尔会在机器忙的时候红（校准线程更慢 / 报错），不带这两个信息看不出是谁的锅。
        check('框选之后重新算了一份 ROI', ok,
              'busy=%s ｜ 提示=%r' % (app._window_busy, app.lb_note.cget('text')))
        mon2 = app.monitor
        check('框选钉住的费用条 = 框的左边缘（1px 不差）',
              mon2.layout.bar.x == x1 and mon2.layout.bar.w == x2 - x1,
              'x=%d 宽 %d（框 x=%d 宽 %d）'
              % (mon2.layout.bar.x, mon2.layout.bar.w, x1, x2 - x1))
        check('框选（半路重校准）不把数出来的帧数清掉',
              mon2.counter.elapsed >= elapsed,
              '重校准前 %d -> 之后 %d' % (elapsed, mon2.counter.elapsed))
        check('刚才在数，重校准完自己接着数', mon2.alive)

        # ---- 4b. 手框按钮：用户指的位置直接钉上，**不再**跑那道几十秒的网格搜索 ----
        # 用户 2026-10-11：「暂停键倍速键也会截取不到，需要手动校准」。自动搜按钮只认
        # 「倍速数字和模板对得上」—— 画面里那段没画数字就搜不到，于是 _no_buttons 把暂停 /
        # 倍速整段关掉。手框就是给它兜底的：用户把框挪了几个像素 -> 就按他指的钉。
        pb = Rect(truth.pause.x + 3, truth.pause.y + 2, truth.pause.w, truth.pause.h)
        sb = Rect(truth.speed.x + 3, truth.speed.y + 2, truth.speed.w, truth.speed.h)
        t1 = time.perf_counter()
        app._apply_window_box({'bar': Rect(x1, y1, x2 - x1, y2 - y1), 'pause': pb, 'speed': sb})
        ok = pump(app, lambda: not app._window_busy and app.monitor is not mon2)
        dt = time.perf_counter() - t1
        mon3 = app.monitor
        check('手框按钮：重新算了一份 ROI', ok,
              'busy=%s ｜ 提示=%r' % (app._window_busy, app.lb_note.cget('text')))
        check('手框按钮：位置就是用户指的那个（1px 不差）',
              (mon3.layout.pause.x, mon3.layout.pause.y) == (pb.x, pb.y)
              and (mon3.layout.speed.x, mon3.layout.speed.y) == (sb.x, sb.y),
              '%s / %s' % (mon3.layout.pause, mon3.layout.speed))
        check('手框按钮：两条通道是开着的（不是 _no_buttons 关掉那份）',
              mon3.layout.buttons_visible and app._window_src.buttons_ok,
              'visible=%s ok=%s' % (mon3.layout.buttons_visible,
                                    app._window_src.buttons_ok))
        check('手框按钮：没再跑那道几十秒的按钮搜索', dt < 20.0, '耗时 %.1f 秒' % dt)
        rec2 = load_window_profiles(prof)[window_key(CW.EXE, CW.WIN_SIZE)]['layout']
        check('手框按钮：也记进了窗口校准表（下次开同尺寸窗口直接套用）',
              rec2['pause'] == mon3.layout.pause.to_dict()
              and rec2['speed'] == mon3.layout.speed.to_dict(), str(rec2['pause']))

        # ---- 4c. 手框按钮的**大小**：采纳用户指的，但夹在标称值的 0.55~1.8 倍 ----
        # 用户 2026-10-12：「放大做的什么鬼……右侧为框内放大图」「暂停键无法手动框选」。
        # 为什么允许改大小：判别条带是按 ROI 高度的**占比**取的（geometry.PAUSE_SHAPE_BAND /
        # SPEED_DIGIT_BAND），所以只要「字形在框里的相对位置」和标称一致读数就照样成立 ——
        # 而这件事正是取景器里那两块虚影要回答的（虚影和字形重合 = 框对上了，包括大小）。
        # 窗口里的画面被缩放过时（播放器实测 0.4777 倍）标称尺寸本身就不对，只搬位置根本
        # 框不住字形。为什么要夹：拉得太离谱条带就飘到字形外面去，读数是**悄悄**错的。
        # 这一段直接量 WindowSource._pin_buttons（面板那条路的中枢），不重复跑一遍
        # _apply_window_box 那几十秒的链路。
        src = app._window_src
        nominal = src.layout
        want = Rect(nominal.pause.x, nominal.pause.y,
                    int(round(nominal.pause.w * 1.2)), int(round(nominal.pause.h * 1.2)))
        lay, _note = src._pin_buttons(nominal, want, None)
        check('手框按钮：大小也用用户指的那个（1.2 倍 -> 就按 1.2 倍钉）',
              (lay.pause.w, lay.pause.h) == (want.w, want.h),
              '%s 期望 %s' % (lay.pause, want))
        check('手框按钮：只给一个框时，另一个按同一个位移、**一样大**跟过去',
              (lay.speed.w, lay.speed.h) == (want.w, want.h)
              and (lay.speed.x - lay.pause.x, lay.speed.y - lay.pause.y)
              == (nominal.speed.x - nominal.pause.x, nominal.speed.y - nominal.pause.y),
              '%s / %s' % (lay.pause, lay.speed))
        huge = Rect(nominal.pause.x, nominal.pause.y, nominal.pause.w * 5, nominal.pause.h * 5)
        lay2, _ = src._pin_buttons(nominal, huge, None)
        check('手框按钮：拉得离谱时夹在标称值的 1.8 倍（条带不飘出字形）',
              (lay2.pause.w, lay2.pause.h)
              == (int(round(nominal.pause.w * 1.8)), int(round(nominal.pause.h * 1.8))),
              '%s 标称 %s' % (lay2.pause, nominal.pause))
        tiny = Rect(nominal.pause.x, nominal.pause.y, 2, 2)
        lay3, _ = src._pin_buttons(nominal, tiny, None)
        check('手框按钮：缩得太小也夹住（0.55 倍下限）',
              (lay3.pause.w, lay3.pause.h)
              == (int(round(nominal.pause.w * 0.55)), int(round(nominal.pause.h * 0.55))),
              '%s' % (lay3.pause,))

        # ---- 5. 保存校准 -> 落到 window_calib.json ----
        app.save_calib()
        profs = load_window_profiles(prof)
        key = window_key(CW.EXE, CW.WIN_SIZE)
        got = profs.get(key, {}).get('layout', {}).get('bar', {})
        check('面板「保存校准」真的写进了窗口校准表', got.get('x') == x1,
              '%s -> bar=%s' % (key, got))
        check('校准表里记了客户区原点（下次开窗口拿它当预测起点）',
              bool(profs.get(key, {}).get('offset')))

        # ---- 5b. 置顶：每 0.7s 重新钉一遍 ----
        # 为什么要重钉：这条链路抓的是**屏幕上那块像素**。(1) 播放器切全屏 / 换下一段
        # 视频会重建窗口，新窗口没有 WS_EX_TOPMOST；(2) 我们自己那个面板也是置顶的，
        # 用户点一下面板（点「开始计数」就得点），面板就压到播放器上面了。
        # 真去调 SetWindowPos 没法在检查里验，所以把 win32 那两个函数换成记录用的假的。
        calls = []
        lifted = []
        real_topmost, real_iswin = ui.set_topmost, ui.is_window
        real_top, real_raise = ui.top_level, ui.raise_window
        real_above = ui.is_above
        real_topmost_flag = ui.is_topmost
        ui.set_topmost = lambda hwnd, on=True: (calls.append((hwnd, on)), True)[1]
        ui.is_window = lambda hwnd: True
        ui.is_above = lambda a, b: False           # 目标没压在我们上面
        ui.top_level = lambda hwnd: 4242           # 面板自己的顶层句柄（假的）
        ui.raise_window = lambda hwnd: (lifted.append(hwnd), True)[1]
        try:
            app._pinned_hwnd = win.hwnd
            t_now = time.perf_counter()
            app._pin_at = 0.0
            app._keep_pin(t_now)
            check('重新钉一次置顶', calls and calls[-1] == (win.hwnd, True), str(calls[-1:]))
            # 钉完目标必须**顺手把面板抬回来**：置顶是一层，最后抬起来的在最上面，
            # 只钉目标的话面板每 0.7 秒被盖回去一次（用户看到的「置顶还是有问题」）。
            check('钉完目标顺手把面板抬回来（不然面板每 0.7s 被盖一次）',
                  lifted == [4242], 'lifted=%s' % (lifted,))
            n = len(calls)
            app._keep_pin(t_now + 0.01)
            check('间隔没到就不重复调用（别每拍都去 SetWindowPos）',
                  len(calls) == n, '%d -> %d' % (n, len(calls)))
            # ---- 5b2. 标记还在 ≠ 我们没被压住（第 44 轮的真 bug）----
            # 置顶是**一层**，同一层里最后被抬起来的在最上面：用户点一下正在读的那个
            # 播放器，系统就把它抬到这一层最上面，我们的面板当场被压下去，而
            # WS_EX_TOPMOST 标记还好端端挂着（真 VLC 实测：VLC rank 17->14、面板 13->17，
            # 之后 189 拍一个 set_topmost 都没发）。所以标记还在那一支也要比一次 z 序。
            # 先让「标记还在」成立（上面那条走的是「补钉」支，这里要另一支）
            ui.is_topmost = lambda hwnd: True
            ui.is_above = lambda a, b: True
            lifted = []
            app._pin_at = 0.0
            app._keep_pin(t_now + 1.0)
            check('标记还在、但目标被抬到我们上面：不发系统调用，只把面板抬回来',
                  len(calls) == n and lifted == [4242],
                  'calls=%d lifted=%s' % (len(calls), lifted))
            ui.is_above = lambda a, b: False
            # 句柄失效 = 窗口被关掉 / 播放器换了一个 —— 放开置顶并当面说清楚，
            # 不能闷着（闷着就是用户看到的「说好的置顶没了」）
            ui.is_window = lambda hwnd: False
            app._keep_pin(t_now + 10.0)
            check('目标窗口句柄失效：放开置顶 + 提示重新选一个窗口',
                  app._pinned_hwnd is None and '重新选一个' in app.lb_note.cget('text'),
                  'pinned=%s ｜ %s' % (app._pinned_hwnd, app.lb_note.cget('text')))
        finally:
            ui.set_topmost, ui.is_window = real_topmost, real_iswin
            ui.top_level, ui.raise_window = real_top, real_raise
            ui.is_above = real_above
            ui.is_topmost = real_topmost_flag

        # ---- 5c. 面板压住**被读的 ROI**：那块抓到的是面板自己，必须当面告警 ----
        # 判据是三个 ROI，不是整个客户区 —— 全屏播放器的客户区覆盖整块屏幕，按客户区判
        # 会天天误报（实测 Desktop 2026.10.09 20.13.34：媒体播放器客户区 2560x1539）。
        # 面板的真实位置这里改不了（面板是 withdraw 的），所以直接替掉那四个取值函数。
        # 2026-10-11 起这条写进常驻的「告警：」那一行（lb_warn），不再是窗口那一行的
        # 「教程」（那一段用户让删了）—— 见 MonitorApp._refresh_roi_warn。
        lay = app.monitor.layout
        cx, cy = app._window_win.client[0], app._window_win.client[1]
        rrx, rry = app.root.winfo_rootx, app.root.winfo_rooty
        rrw, rrh = app.root.winfo_width, app.root.winfo_height
        try:
            app.root.winfo_rootx = lambda: cx + lay.bar.x + 8      # 正好盖住费用条
            app.root.winfo_rooty = lambda: cy + lay.bar.y + 2
            app.root.winfo_width = lambda: 60
            app.root.winfo_height = lambda: 12
            app._refresh_roi_warn()
            check('面板压住费用条时当面告警（点名是哪一个 ROI）',
                  '面板压住了「费用条」' in app.lb_warn.cget('text'),
                  app.lb_warn.cget('text').replace('\n', ' ｜ '))
            # 人在客户区里、但**没压住任何一个 ROI** —— 全屏播放器就是这个样子，
            # 这种时候不该报警（报警刷屏等于没报警）。
            app.root.winfo_rootx = lambda: cx + 5
            app.root.winfo_rooty = lambda: cy + 5
            app.root.winfo_width = lambda: 400
            app.root.winfo_height = lambda: 300
            app._refresh_roi_warn()
            check('在客户区里但没压住 ROI：不误报',
                  '压住' not in app.lb_warn.cget('text'),
                  app.lb_warn.cget('text').replace('\n', ' ｜ '))
            app.root.winfo_rootx = lambda: cx + app._window_win.client[2] + 50
            app.root.winfo_rooty = lambda: cy
            app.root.winfo_width = lambda: 10
            app.root.winfo_height = lambda: 10
            app._refresh_roi_warn()
            check('整个挪出客户区：也不报警',
                  '压住' not in app.lb_warn.cget('text'),
                  app.lb_warn.cget('text').replace('\n', ' ｜ '))
        finally:
            app.root.winfo_rootx, app.root.winfo_rooty = rrx, rry
            app.root.winfo_width, app.root.winfo_height = rrw, rrh
            app._refresh_roi_warn()
        check('没压住之后那条告警的状态位也清回去了（下次压住才会再报）',
              app._roi_warn == '', repr(app._roi_warn))

        # ---- 5d. 我们自己开着对话框时：置顶照样复核，而且把我们自己的窗全抬回来 ----
        # 「框选费用条」那个取景器就是要盖在目标窗口上面让用户对着参照图圈的，所以钉完
        # 目标必须把我们自己的窗再抬回来。第 39~43 轮是用「开着对话框就**别去钉**」实现
        # 这一点的（_tick_body 里那道 _own_dialog_open 门），代价是置顶在那段时间**整段
        # 停摆**：目标只要在这期间丢了标记（播放器自己重设窗口 / 换下一段视频 / 切全屏），
        # 就再也没有人把它钉回去，面板上还什么都不会说 —— 用户报的「读取窗口又无法保持
        # 置顶了」就是它（第 44 轮）。现在门拆了：照样复核，钉完 _raise_ours() 把我们也
        # 抬回来，取景器照样盖在目标上面。
        calls = []
        lifted2 = []
        real2 = (ui.set_topmost, ui.is_window, ui.is_topmost,
                 ui.top_level, ui.raise_window)
        ui.set_topmost = lambda hwnd, on=True: (calls.append((hwnd, on)), True)[1]
        ui.is_window = lambda hwnd: True
        ui.is_topmost = lambda hwnd: False         # 标记丢了（播放器刚重设过窗口）
        ui.top_level = lambda hwnd: hwnd           # 面板 / 对话框各自给回自己的句柄
        ui.raise_window = lambda hwnd: (lifted2.append(hwnd), True)[1]
        top = tk.Toplevel(app.root)
        app.root.update()
        try:
            check('开着自己的对话框时 _own_dialog_open() 为真', app._own_dialog_open())
            panel_h, dlg_h = app.root.winfo_id(), top.winfo_id()
            app._pinned_hwnd = win.hwnd
            app._pin_at = 0.0
            app._keep_pin(time.perf_counter())
            check('对话框开着也照样补钉（第 44 轮修的就是这条）',
                  calls == [(win.hwnd, True)], str(calls))
            check('钉完把面板和对话框都抬回来（取景器仍然盖在目标上面）',
                  lifted2 == [panel_h, dlg_h], str(lifted2))
        finally:
            top.destroy()
            app.root.update()
            (ui.set_topmost, ui.is_window, ui.is_topmost,
             ui.top_level, ui.raise_window) = real2
            app._pinned_hwnd = None
        check('对话框关掉之后又能抢了', not app._own_dialog_open())

        # ---- 6. 切回实时 ----
        app.back_to_live()
        check('「切回实时」回到实时链路', app._src_kind == 'live', app._src_kind)
        check('切回去之后「手动校准」还在（两种模式都在；「置顶」键 41 轮删了）',
              app.btn_manual.winfo_manager() != '' and not hasattr(app, 'btn_pin'),
              '管理器=%r' % (app.btn_manual.winfo_manager(),))
    finally:
        ui.WindowSource, ui.WindowMonitor = reals
        try:
            app.quit()
        except Exception:                  # noqa: BLE001
            pass

    print()
    if FAIL:
        print('%d 项没过：%s' % (len(FAIL), '；'.join(FAIL)))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
