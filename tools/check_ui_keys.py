#!/usr/bin/env python
"""面板键位 / 折叠自检：2026-10-10 示意图那批 + 2026-10-11 看板批注那批。

两轮改的东西大半是**界面接线**：哪些键归谁、哪一块默认收起来、按钮写了几个字 ——
这类东西最怕「改完自己看着对、下一次动一下又接回去」，所以单独一个检查钉住。

检查什么（全部不需要真游戏窗口）：
    1. 大数字旁边那两行诊断读数（lb_bar / lb_pause）**搬进了「诊断 / 预览」**；
       收起态看得见的只有大数字 + 游戏内时间（看板批注「主体显示帧数和时间就行」），
       lb_speed 那个控件早就没了；另外把「收起态每一行都落进基准宽 470」与
       「空告警整行收起 / 有告警压回控制行上面」两条也钉住；
    2. 主按钮 / 空格 = **开始 -> 暂停 -> 继续**三态（看板批注「开始和暂停合并」）；
       起来时重新起锚；**用户主动暂停的**不算故障（看门狗不报「采样已停」）；
       清零（C）= 停采样 + 归零 + 把三态拉回「开始计数」；
    3. Esc 只收起展开区、不再退程序；退出改 Ctrl+Q；
    4. ROI 校准模式（2026-10-11 起跟「手动校准」二级窗口绑定）：Tab / 方向键 /
       Shift+方向键 / S / R / K 只在窗口开着时绑上，关窗口（或 Esc）一定解绑
       （以前是全局的 —— 打字、翻条、误按都会碰到 ROI）；
    5. 刻度跨度**固定 150 帧**（2026-10-11 删了 [ ] 缩放档：跨度常量算出来的
       px_per_frame 得对得上）；↑↓ 翻条（回看偏移、边界、没轴时的提示）；
    6. 「CSV」按钮、「刻度缩放」四个键、「局内负费」下拉**都删掉了**（用户看板批注）；
    7. 诊断那一层删掉了（2026-10-11）：按钮 / 折叠方法没了，fr_diag 一直不 pack；
    8. 刻度表的「回看」：build(now=...) 时多画一条「现在」灰线（画出来的图不一样）；
    9. 拖动标题栏：位移精确（收起 / 展开各一次 —— 展开态竖直拖不动 = _anchor_bottom
       没跟着挪），松手结束「拖动中」。

    10. A 方案（2026-10-11）：展开区拆两块 —— 打轴器（fr_more_top）展开时插到
        **大数字上面**，手动校准（fr_more）留在最末尾；收起 -> 展开 -> 收起
        y 回原处（没有新漂移）。

用法：python tools/check_ui_keys.py
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.scale import ScaleGeom, Theme, build                  # noqa: E402
from akframe.scale_render import render as render_scale            # noqa: E402
from akframe.timeline import Cue                                   # noqa: E402

TEMP = os.environ.get('TEMP') or os.environ.get('TMP') or '.'

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


class FakeClock(object):
    """假挂钟：推一下走一步。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def step(self, dt):
        self.t += dt


class AliveThread(object):
    """假装采样线程还活着（看门狗只看 is_alive / join）。"""

    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass


def kill_afters(app):
    """把还没跑的 after 回调取消掉。

    检查脚本不跑 mainloop，只手动调 _tick_body；但 _build_ui 之后仍然挂着一个
    `after(80, _tick)`。窗口销毁之后那个回调再跑，Tcl 会往 stderr 吐一串
    `invalid command name "..._tick"` —— 会淹掉真正要看的东西，所以先取消干净。
    """
    try:
        for aid in app.root.tk.call('after', 'info'):
            app.root.after_cancel(aid)
    except Exception:                    # noqa: BLE001  窗口已经没了就算了
        pass


def alive(app):
    try:
        return bool(app.root.winfo_exists())
    except Exception:                    # noqa: BLE001  TclError：窗口已经没了
        return False


def make_app():
    """建一个面板 + 演示监视器（假输入，不动真屏幕）。

    真实那条链路会去抓 Arknights.exe 的窗口，检查脚本一律用 DemoMonitor：
    它和 LiveMonitor 的接口完全一样（_sample / latest / stats / counter 都在）。
    """
    from akframe.demo import DemoMonitor
    from akframe import ui as ui_mod

    dm = DemoMonitor(rate=120.0, clock=FakeClock())
    app = ui_mod.MonitorApp(
        dm, calib_path=os.path.join(TEMP, 'akframe_check_ui_keys_calib.json'),
        settings_path=os.path.join(TEMP, 'akframe_check_ui_keys_rate.json'))
    app.root.withdraw()
    kill_afters(app)
    return app, dm


def pump(dm, n=30, dt=1.0 / 120.0):
    """推着假挂钟走 n 拍：DemoMonitor._sample 是按挂钟推进逻辑帧的（dt=0 什么都不喂）。"""
    for _ in range(n):
        dm._clock.step(dt)
        dm._sample()
    return dm.latest


def t_readouts(app, dm):
    """1：诊断读数搬进「诊断 / 预览」，收起态只剩大数字 + 时间。"""
    pump(dm)
    app._tick_body()
    check('lb_speed 那个控件已经没了', not hasattr(app, 'lb_speed'))
    # 看板批注：收起态「主体显示帧数和时间就行」—— 那两行诊断证据（费用条 / 暂停键）
    # 得挂在**默认收起**的 fr_diag 里。只验「在不在 fr_diag 底下」比数像素稳。
    check('两行读数搬进了「诊断 / 预览」（收起态看不见）',
          app.fr_diag.winfo_manager() == '' and app.lb_bar.master.master is app.fr_diag
          and app.lb_pause.master.master is app.fr_diag,
          '管理器=%r' % app.fr_diag.winfo_manager())
    bar = app.lb_bar.cget('text')
    check('行 1 = 费用条 + 通道 + 本费',
          bar.startswith('费用条') and '本费' in bar, bar)
    pause = app.lb_pause.cget('text')
    check('行 2 = 暂停键 + 倍速',
          '\uff5c' in pause and ('倍速' in pause or '子弹' in pause), pause)
    check('控制行里没有「CSV」按钮了（用户：csv 是什么，删掉）',
          not hasattr(app, 'btn_csv'))
    # 看板批注「尽量缩小，比如帧数计算 2 位但显示一位小数节省空间」——这一条把
    # 「缩得动」钉住：收起态每一行都得落进基准宽，否则 _refit 会按内容把面板放宽。
    app.root.update_idletasks()
    check('收起态每一行都落进基准宽 470',
          app.root.winfo_reqwidth() <= app.COLLAPSED_W,
          '内容要 %dpx，基准 %dpx' % (app.root.winfo_reqwidth(), app.COLLAPSED_W))
    # 空的告警行以前也占着 30px 字高，2026-10-11 起空着就整行收起来。
    app._set_warn('')
    check('空告警：整行收起来（空 Label 也占字高）',
          app.fr_warn.winfo_manager() == '', app.fr_warn.winfo_manager())
    app._set_warn('告警：测试')
    order = list(app.root.pack_slaves())          # pack 顺序 = 从上往下的顺序
    check('有告警：铺出来看得见，而且压在控制行上面',
          app.fr_warn.winfo_manager() == 'pack'
          and order.index(app.fr_warn) < order.index(app.fr_ctrl),
          '%s 顺序=%d < %d' % (app.fr_warn.winfo_manager(),
                              order.index(app.fr_warn), order.index(app.fr_ctrl)))
    app._set_warn('')


def t_toggle_count(app, dm):
    """2：主按钮 / 空格 = 开始 -> 暂停 -> 继续（三态）；清零把三态拉回开始。"""
    dm._thread = None
    app._sync_ctrl_row()
    check('没在采样时按钮写「开始」',
          app.btn_start.cget('text') == '开始', app.btn_start.cget('text'))

    starts = []
    resyncs = []

    def fake_start(reset_clock=True):
        starts.append(reset_clock)
        dm._thread = AliveThread()

    dm.start = fake_start                       # 不真起线程：演示监视器没有真屏幕
    orig_resync = dm.counter.resync

    def spy_resync(*a, **k):
        resyncs.append(1)
        return orig_resync(*a, **k)

    dm.counter.resync = spy_resync
    app._primary_action()
    check('空格 / 主按钮：真的起来了', dm.alive and app._counting_started)
    check('恢复时重新起锚（resync 一次，停止期间那段不补）', len(resyncs) == 1, resyncs)
    check('起来之后按钮翻成「暂停」',
          app.btn_start.cget('text') == '暂停', app.btn_start.cget('text'))

    # 第 2 态 -> 第 3 态：数字按住不动。采样线程**不**停 —— 还得接着读画面看条。
    app._primary_action()
    check('再按一下：数字按住不动（采样线程还在）', dm.manual_pause and dm.alive)
    check('按钮翻成「继续」',
          app.btn_start.cget('text') == '继续', app.btn_start.cget('text'))

    # 第 3 态 -> 第 2 态：接着数。
    app._primary_action()
    check('第三下：接着数', (not dm.manual_pause) and dm.alive)
    check('按钮翻回「暂停」',
          app.btn_start.cget('text') == '暂停', app.btn_start.cget('text'))
    app._tick_body()
    check('用户主动暂停的不算故障（不报「采样已停」）',
          '采样已停' not in app.lb_warn.cget('text'), app.lb_warn.cget('text'))

    # 用户 2026-10-11：「快捷键操作去掉（暂停（空格）改成暂停）不需要快捷键操作」。
    # 键**本身**还留着（见 _base_keys），但面板上一个键名都不许再出现。
    ctrl_btns = _btn_texts(app.fr_ctrl)
    check('控制行的按钮上不带键名了',
          '清零' in ctrl_btns and not any(('空格' in t or '(C)' in t) for t in ctrl_btns),
          ctrl_btns)

    # 清零（C）：看板批注「点清零后变成开始」—— 停采样 + 归零 + 回第 1 态。
    app.clear_count()
    check('清零：采样停了、按钮回到「开始」',
          (not dm.alive) and (not app._counting_started) and (not dm.manual_pause)
          and app.btn_start.cget('text') == '开始',
          '%s alive=%s started=%s' % (app.btn_start.cget('text'), dm.alive,
                                      app._counting_started))


def t_escape(app):
    """3：Esc 收起展开区，**不再退程序**。"""
    app.toggle_more()
    app._tick_body()
    app._on_escape()
    check('展开态按 Esc：收起展开区，窗口还在',
          (not app._more) and alive(app), '_more=%s' % app._more)
    app._on_escape()
    note = app.lb_note.cget('text')
    check('收起态再按 Esc：给一句提示（而且不再报键名），窗口还在',
          alive(app) and note and 'Ctrl+Q' not in note and 'Esc' not in note, note)
    check('Esc / Ctrl+Q 两个键都绑上了',
          bool(app.root.bind('<Escape>')) and bool(app.root.bind('<Control-q>')))


def t_ctrl_q():
    """3b：Ctrl+Q 真的退程序（单独开一个窗口 —— 这一下会把窗口销毁）。

    按键要走真事件（event_generate），所以这个窗口得先 deiconify + 拿到焦点：
    收起态、没有焦点的窗口收不到键事件。
    """
    import tkinter as tk
    app, dm = make_app()
    app.root.deiconify()
    app.root.update()
    app.root.focus_force()
    app.root.update()
    try:
        app.root.event_generate('<Control-q>', when='now')
        app.root.update()
    except tk.TclError:
        pass                             # destroy 之后再 update 会抛，正是「已经退了」
    still = alive(app)
    check('Ctrl+Q 真的退出（窗口没了）', not still, '窗口还在' if still else '')
    if still:
        kill_afters(app)
        try:
            app.root.destroy()
        except Exception:                # noqa: BLE001
            pass


def t_calib_mode(app):
    """4：ROI 校准模式的键门。"""
    import tkinter as tk
    from akframe import ui as ui_mod

    def bound(seq):
        return bool(app.root.bind(seq))

    check('平时 Tab 没被占（还给系统）', not bound('<Tab>'))
    check('平时 s / k / Shift+方向键 也没绑',
          not bound('s') and not bound('k') and not bound('<Shift-Up>'))
    try:
        app.open_calib_dialog()
    except tk.TclError as exc:
        skip('ROI 校准模式', '建不出二级窗口：%s' % exc)
        return
    check('开窗口 = 进模式：Tab / s / k / Shift+方向键 全绑上',
          app._calib and bound('<Tab>') and bound('s') and bound('k')
          and bound('<Shift-Up>'))
    check('目标按钮当前那个反色（白底深字）',
          app.calib_tgt_btns[app.target].cget('bg') == ui_mod.RULE,
          app.calib_tgt_btns[app.target].cget('bg'))
    ev = types.SimpleNamespace(keysym='Right', state=0)
    app._nudge(ev)
    check('模式里方向键真的挪 ROI', app.deltas[app.target][0] == 1, app.deltas)
    app.reset_calib()
    check('R 恢复默认把 delta 清零',
          app.deltas[app.target] == [0, 0, 0, 0], app.deltas)
    app.close_calib_dialog()
    check('关窗口 = 退模式：那几个键全解绑',
          (not app._calib) and not bound('<Tab>') and not bound('s')
          and not bound('k') and not bound('<Shift-Up>'))
    check('关窗口后预览画布收掉（cv_bar = None）', app.cv_bar is None)


def t_scale_and_scroll(app):
    """5：刻度固定 150 帧（没有缩放档了）+ ↑↓ 翻条。"""
    from akframe import ui as ui_mod

    # 看板批注「刻度缩放固定为 150 帧」：跨度是常量，缩放那一套（[ ] 键、四个按钮、
    # set_scale_zoom / _zoom_step）2026-10-11 全删了 —— 这里正着反着都钉一遍。
    want = ui_mod.SCALE_HEIGHT / ui_mod.SCALE_SPAN_FRAMES
    check('刻度跨度固定 150 帧（px_per_frame 由常量反算）',
          abs(app.scale_geom.px_per_frame - want) < 1e-9
          and abs(ui_mod.SCALE_SPAN_FRAMES - 150.0) < 1e-9,
          '%s vs %s' % (app.scale_geom.px_per_frame, want))
    check('缩放那一套真删干净了（[ ] 没绑、方法没有）',
          not app.root.bind('bracketleft') and not app.root.bind('bracketright')
          and not hasattr(app, '_zoom_step') and not hasattr(app, 'set_scale_zoom')
          and not hasattr(app, 'zoom_btns'))
    # 看板批注「状态描述全部删掉」：原来还有三行灰字（固定 150 帧 / 下一条还差 N
    # 帧 / 轴文本改在独立窗口）—— 2026-10-11 全删了，刻度表自己就把这些画出来。
    check('展开区那三行状态描述删干净了',
          not hasattr(app, 'lb_span_note') and not hasattr(app, 'lb_cue_next')
          and not hasattr(app, 'lb_axis_hint') and not hasattr(app, 'lb_help'))

    app._prom_scroll(1)
    check('没有轴时翻条只提示，不动偏移',
          app._prom_shift == 0.0 and '先导入' in app.lb_note.cget('text'),
          app.lb_note.cget('text'))
    app._set_axis_text('300 部署 银灰\n330 技能 银灰\n360 撤退')
    app.apply_axis_text()
    check('轴进到提词器',
          app.prompter.timeline is not None and len(app.prompter.timeline.cues) == 3)
    app._prom_scroll(1)
    check('↑ = 往回看 30 帧（1 费 = 1 秒）',
          abs(app._prom_shift - 30.0) < 1e-9, app._prom_shift)
    app._prom_scroll(1, 1000.0)
    check('回看有上限（600 帧）', abs(app._prom_shift - 600.0) < 1e-9, app._prom_shift)
    app._prom_scroll(-1, 1000.0)
    check('↓ 一路回到「现在」',
          app._prom_shift == 0.0 and '现在' in app.lb_note.cget('text'),
          app.lb_note.cget('text'))


def t_removed_controls(app):
    """6：这三样 2026-10-11 全删了（看板批注）。删干净比「还能用」更重要 ——
    留着残件下次改动很容易又被接回去。"""
    from akframe import ui as ui_mod

    check('「CSV」按钮没了', not hasattr(app, 'btn_csv'))
    check('「刻度缩放」四个键没了', not hasattr(app, 'zoom_btns'))
    check('「局内负费」下拉没了', not hasattr(app, 'neg_menu'))
    check('底层 CSV 能力还在（run_monitor --csv 走这条路）',
          hasattr(app.monitor, 'set_csv_path'))
    check('局内负费仍是「一直按画面判」',
          ui_mod.parse_negative(getattr(app, 'negative_mode', 'auto')) == 'auto',
          getattr(app, 'negative_mode', '?'))


def t_diag_gone(app):
    """7：诊断那一层（二级折叠）2026-10-11 删掉了。

    用户看板批注：「诊断删掉」—— 那颗「▸ 诊断 / 预览」按钮和它的折叠方法都没有了；
    底下那五行读数（费用条 / 暂停键 / 费用周期 / 性能 / 当前输入）只留着控件给
    _tick_body 写，**不 pack**（界面零占位，查漏帧时还看得到原始证据）。
    """
    check('「诊断 / 预览」按钮没了', not hasattr(app, 'btn_diag'))
    check('折叠方法 toggle_diag 也没了', not hasattr(app, 'toggle_diag'))
    check('fr_diag 从头到尾不 pack（零占位）',
          app.fr_diag.winfo_manager() == '', app.fr_diag.winfo_manager())
    check('五行读数控件还活着（_tick_body 照旧写）',
          hasattr(app, 'lb_bar') and hasattr(app, 'lb_pause')
          and hasattr(app, 'lb_cycle') and hasattr(app, 'lb_perf')
          and hasattr(app, 'lb_source'))


def _btn_texts(widget):
    """递归收一个容器里所有按钮的文字（面板上还印着哪些按钮）。"""
    import tkinter as tk

    out = []
    for c in widget.winfo_children():
        if isinstance(c, tk.Button):
            try:
                out.append(c.cget('text'))
            except Exception:                # noqa: BLE001
                pass
        out.extend(_btn_texts(c))
    return out


def t_calib_dialog(app):
    """8：手动校准二级窗口（2026-10-11 用户：「改成手动校准」）。

    面板上只剩一个入口按钮；「目标 / 微调 / 保存校准 / 恢复默认 / ROI 框 / 换窗口」
    全搬进窗口里。这里不抓图，只钉「面板只剩一个按钮 + 窗口里该有的都在」。
    """
    from akframe import ui as ui_mod

    check('面板上只有一个「手动校准」按钮', '手动校准' in app.btn_calib.cget('text'),
          app.btn_calib.cget('text'))
    panel_btns = _btn_texts(app.fr_more)
    check('面板展开区没有「保存校准 / 恢复默认」了（搬进窗口）',
          not any(t in ('保存校准', '恢复默认') for t in panel_btns), panel_btns)
    app.open_calib_dialog()
    try:
        check('开出来后 _calib_win 有了、微调也开了',
              app._calib_win is not None and app._calib)
        check('窗口里有三个目标键（费用条 / 暂停键 / 倍速键）',
              set(app.calib_tgt_btns) == set(ui_mod.ROI_ORDER) and all(
                  b.cget('text') in ui_mod.ROI_LABELS.values()
                  for b in app.calib_tgt_btns.values()),
              sorted(app.calib_tgt_btns))
        check('步长三档 1 / 5 / 10px', sorted(app.calib_step_btns) == [1, 5, 10],
              sorted(app.calib_step_btns))
        check('预览画布是 430x64',
              app.cv_bar is not None and int(app.cv_bar.cget('width')) == 430
              and int(app.cv_bar.cget('height')) == 64)
        pos = app.lb_calib_pos.cget('text')
        check('位置读数有 x= / y= 或「ROI 未知」',
              ('x=' in pos and 'y=' in pos) or '未知' in pos, pos)
        app.set_target('pause')
        check('点「暂停键」目标真的切了', app.target == 'pause', app.target)
        app.set_step(5)
        app.nudge(1, 0)
        check('5px 步长：nudge 一次挪 5', app.deltas['pause'][0] == 5,
              app.deltas['pause'])
        app.reset_calib()
        check('窗口里的「恢复默认」把 delta 清零',
              app.deltas['pause'] == [0, 0, 0, 0], app.deltas['pause'])
    finally:
        app.close_calib_dialog()
    check('关窗口：cv_bar 清空、按钮字典清空',
          app.cv_bar is None and app.calib_tgt_btns == {}
          and app.calib_step_btns == {})


def t_layout_top(app):
    """10：展开区拆两块 —— 打轴器插到大数字**上面**，手动校准留在最底下。

    用户 2026-10-11：「展开是向上展开，打轴器在计数表的上方」。判据用 pack 顺序
    （= 从上往下的顺序）而不是像素 —— px 会随字号 / DPI 变，顺序不会。
    """
    root = app.root
    root.deiconify()
    root.update()
    if not app._more:
        app.toggle_more()
    root.update_idletasks()
    order = list(root.pack_slaves())
    check('展开：打轴器（fr_more_top）在**大数字**上面',
          order.index(app.fr_more_top) < order.index(app.fr_nums)
          and app.fr_more_top.winfo_manager() == 'pack',
          '顺序=%d vs %d' % (order.index(app.fr_more_top), order.index(app.fr_nums)))
    check('展开：打轴器在标题栏 / 分隔线之后（没压在最上面）',
          order.index(app.fr_more_top) >= 2, order.index(app.fr_more_top))
    check('展开：手动校准那一行在**最末尾**',
          order.index(app.fr_more) == len(order) - 1
          and app.fr_more.winfo_manager() == 'pack',
          '%d / 共 %d' % (order.index(app.fr_more), len(order)))
    check('两块是两个独立的 Frame（打轴器才插得了队）',
          app.fr_more is not app.fr_more_top
          and app.fr_more_top.master is root and app.fr_more.master is root)
    app.toggle_more()
    root.update_idletasks()
    check('收起：两块一起收回去',
          app.fr_more_top.winfo_manager() == '' and app.fr_more.winfo_manager() == '',
          '%r / %r' % (app.fr_more_top.winfo_manager(), app.fr_more.winfo_manager()))
    order2 = list(root.pack_slaves())
    check('收起：常驻区没被展开区带走',
          app.fr_nums in order2 and app.fr_ctrl in order2 and app.lb_note in order2)

    def park(x, y):
        app._user_pos = (x, y)
        app._place(app._win_w, app._win_h)
        root.update_idletasks()

    park(300, 1000)
    y0 = root.winfo_y()
    app.toggle_more()
    root.update_idletasks()
    y1 = root.winfo_y()
    app.toggle_more()
    root.update_idletasks()
    y2 = root.winfo_y()
    check('展开是「往上长」：y 只减不增', y1 <= y0, 'y0=%d y1=%d' % (y0, y1))
    check('收起 -> 展开 -> 收起：y 回原处（A 方案没引入新漂移）',
          y2 == y0, 'y0=%d 展开=%d 收起=%d' % (y0, y1, y2))


def t_scrub_render():
    """8：刻度表的「回看」多画一条「现在」灰线。"""
    g = ScaleGeom(px_per_frame=1.5)
    cues = [Cue(300, '部署 银灰', 1, '300 部署 银灰'), Cue(360, '撤退', 2, '360 撤退')]
    plain = build(342.0, cues, 0, g)
    check('build 的默认 now = frame（老行为不变）', plain.now == 342.0, plain.now)
    scrubbed = build(312.0, cues, 0, g, now=342.0)
    check('回看时视图帧是 312（刻度按它摆）', scrubbed.frame == 312.0, scrubbed.frame)
    a = render_scale(plain, Theme()).tobytes()
    b = render_scale(scrubbed, Theme()).tobytes()
    check('回看的图和「现在」的图不一样（多画了那条灰线）', a != b)


def t_drag(app):
    """9：按住标题栏拖动（用户视频 1：「窗口拖拽卡顿，位置漂移」）。

    判据是**可量化**的两条：

      * 位移精确：拖 (dx, dy)，窗口就得正好走 (dx, dy)；
      * 展开态竖直方向也要跟手：_place 的展开分支只认 _anchor_bottom（「下边缘钉住、
        往上长」），拖动时不把它一起挪，竖着拖窗口**一动都不动**，而攒在 _user_pos
        里的偏移会在收起那一刻冒出来（面板往下一跳 = 用户看到的「位置漂移」）。
    """
    import types as _types

    root = app.root
    root.deiconify()
    root.update()

    def park(x, y):
        app._user_pos = (x, y)
        app._place(app._win_w, app._win_h)
        root.update_idletasks()

    def drag(dx, dy, steps=40):
        x0, y0 = root.winfo_x(), root.winfo_y()

        def ev(a, b):
            return _types.SimpleNamespace(x_root=a, y_root=b)

        app._drag_start(ev(x0 + 200, y0 + 10))
        for i in range(1, steps + 1):
            app._drag_move(ev(x0 + 200 + dx * i // steps, y0 + 10 + dy * i // steps))
        app._dragging = False           # 只验 _drag_move；_drag_end 单独测
        root.update_idletasks()
        return root.winfo_x() - x0, root.winfo_y() - y0

    was = app._more
    if was:
        app.toggle_more()
    park(300, 1000)
    got = drag(60, -30)
    check('收起态拖 (+60, -30)：窗口正好走 (60, -30)', got == (60, -30), str(got))
    if not was:
        app.toggle_more()
    park(300, 300)
    got = drag(60, -30)
    check('展开态拖 (+60, -30)：竖直方向也跟手', got == (60, -30),
          '%s（展开态竖直拖不动 = 拖动时没挪 _anchor_bottom）' % (got,))
    app.toggle_more()
    park(300, 1000)
    app._dragging = True
    app._drag_end()
    check('松手（_drag_end）结束「拖动中」并把布局夹回来', app._dragging is False)


def main():
    print('面板键位 / 折叠自检（2026-10-10 示意图那批 + 2026-10-11 看板批注）')
    t_scrub_render()
    print('1-2. 读数搬家 / 开始-暂停-继续三态')
    try:
        import tkinter as tk                   # noqa: F401
        from akframe import ui as ui_mod       # noqa: F401
    except Exception as exc:                   # noqa: BLE001
        skip('面板那几组', 'Tk 不可用：%s' % exc)
    else:
        try:
            app, dm = make_app()
        except Exception as exc:               # noqa: BLE001
            skip('面板那几组', '建不出窗口：%s' % exc)
            app = None
        if app is not None:
            try:
                t_readouts(app, dm)
                t_toggle_count(app, dm)
                print('3. Esc / Ctrl+Q')
                t_escape(app)                  # 这一组最后会把窗口关掉
            finally:
                if alive(app):
                    kill_afters(app)
                    try:
                        app.root.destroy()
                    except Exception:          # noqa: BLE001
                        pass
            t_ctrl_q()
            app2, dm2 = make_app()             # 上面那个已经关了，重开一个
            try:
                print('4. ROI 校准模式')
                t_calib_mode(app2)
                print('5. 刻度固定 150 帧 / ↑↓ 翻条')
                t_scale_and_scroll(app2)
                print('6. CSV / 刻度缩放 / 局内负费 都删掉了')
                t_removed_controls(app2)
                print('7. 诊断那一层删掉了')
                t_diag_gone(app2)
                print('8. 手动校准二级窗口')
                t_calib_dialog(app2)
                print('9. 拖动标题栏')
                t_drag(app2)
                print('10. 展开区拆两块（打轴器在最上）')
                t_layout_top(app2)
            finally:
                kill_afters(app2)
                try:
                    app2.root.destroy()
                except Exception:              # noqa: BLE001
                    pass
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
