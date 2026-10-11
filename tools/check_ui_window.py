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


def pump(app, cond, seconds=60.0, label=''):
    """直接调 _tick_body 当主循环（不用真的进 mainloop）。"""
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
        app.lb_frames.configure(text='123')
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
        # 面板这条路走的是 ui.BoxPicker（参照图 + 橙色刻度尺 + 放大镜 + 吸附左端），
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
        ui.set_topmost = lambda hwnd, on=True: (calls.append((hwnd, on)), True)[1]
        ui.is_window = lambda hwnd: True
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
            # 句柄失效 = 窗口被关掉 / 播放器换了一个 —— 放开置顶并当面说清楚，
            # 不能闷着（闷着就是用户看到的「说好的置顶没了」）
            ui.is_window = lambda hwnd: False
            app._keep_pin(t_now + 10.0)
            check('目标窗口句柄失效：放开置顶 + 提示去「换窗口」',
                  app._pinned_hwnd is None and '换窗口' in app.lb_note.cget('text'),
                  'pinned=%s ｜ %s' % (app._pinned_hwnd, app.lb_note.cget('text')))
        finally:
            ui.set_topmost, ui.is_window = real_topmost, real_iswin
            ui.top_level, ui.raise_window = real_top, real_raise

        # ---- 5c. 面板压住**被读的 ROI**：那块抓到的是面板自己，必须当面告警 ----
        # 判据是三个 ROI，不是整个客户区 —— 全屏播放器的客户区覆盖整块屏幕，按客户区判
        # 会天天误报（实测 Desktop 2026.10.09 20.13.34：媒体播放器客户区 2560x1539）。
        # 面板的真实位置这里改不了（面板是 withdraw 的），所以直接替掉那四个取值函数。
        lay = app.monitor.layout
        cx, cy = app._window_win.client[0], app._window_win.client[1]
        rrx, rry = app.root.winfo_rootx, app.root.winfo_rooty
        rrw, rrh = app.root.winfo_width, app.root.winfo_height
        try:
            app.root.winfo_rootx = lambda: cx + lay.bar.x + 8      # 正好盖住费用条
            app.root.winfo_rooty = lambda: cy + lay.bar.y + 2
            app.root.winfo_width = lambda: 60
            app.root.winfo_height = lambda: 12
            app._refresh_window_hint()
            check('面板压住费用条时当面告警（点名是哪一个 ROI）',
                  '面板压住了「费用条」' in app.lb_window_hint.cget('text'),
                  app.lb_window_hint.cget('text').replace('\n', ' ｜ '))
            # 人在客户区里、但**没压住任何一个 ROI** —— 全屏播放器就是这个样子，
            # 这种时候不该报警（报警刷屏等于没报警）。
            app.root.winfo_rootx = lambda: cx + 5
            app.root.winfo_rooty = lambda: cy + 5
            app.root.winfo_width = lambda: 400
            app.root.winfo_height = lambda: 300
            app._refresh_window_hint()
            check('在客户区里但没压住 ROI：不误报',
                  '压住' not in app.lb_window_hint.cget('text'),
                  app.lb_window_hint.cget('text').replace('\n', ' ｜ '))
            app.root.winfo_rootx = lambda: cx + app._window_win.client[2] + 50
            app.root.winfo_rooty = lambda: cy
            app.root.winfo_width = lambda: 10
            app.root.winfo_height = lambda: 10
            app._refresh_window_hint()
            check('整个挪出客户区：也不报警',
                  '压住' not in app.lb_window_hint.cget('text'),
                  app.lb_window_hint.cget('text').replace('\n', ' ｜ '))
        finally:
            app.root.winfo_rootx, app.root.winfo_rooty = rrx, rry
            app.root.winfo_width, app.root.winfo_height = rrw, rrh
            app._refresh_window_hint()
        check('窗口那一行随时说得出置顶状态',
              '置顶' in app.lb_window_hint.cget('text'), app.lb_window_hint.cget('text'))

        # ---- 5d. 我们自己开着对话框时不去抢置顶 ----
        # 「框选费用条」那个取景器就是要盖在目标窗口上面让用户对着参照图圈的，
        # 这时候把目标窗口抬上来会把取景器盖住。
        top = tk.Toplevel(app.root)
        app.root.update()
        check('开着自己的对话框时不抢置顶', app._own_dialog_open())
        top.destroy()
        app.root.update()
        check('对话框关掉之后又能抢了', not app._own_dialog_open())

        # ---- 6. 切回实时 ----
        app.back_to_live()
        check('「切回实时」回到实时链路', app._src_kind == 'live', app._src_kind)
        check('切回去之后窗口控件收起来了', app.fr_window.winfo_manager() == '',
              '管理器=%r' % app.fr_window.winfo_manager())
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
