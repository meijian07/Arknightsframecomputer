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
       没跟着挪），松手结束「拖动中」；以及**绕开面板**把窗口挪走（系统标题栏 / 系统
       贴边 / 换分辨率）之后，下一次布局重算不许把它拽回去（见 9b）。

    10. A 方案（2026-10-11）：展开区拆两块 —— 打轴器（fr_more_top）展开时插到
        **大数字上面**；收起 -> 展开 -> 收起 y 回原处（没有新漂移）。
    11. 读窗口那个键（2026-10-11 第四轮）：「框选费用条」改名「手动校准」，**并进控制
        行**（两种模式面板尺寸一模一样，不用展开）；「置顶」键第五轮删掉，改成**默认
        置顶**（选完窗口就钉）；窗口那一行下面的多行「教程」删掉，改由「告警：」那一
        行在面板压住 ROI 时说话。
    12. 第五轮：模式名换成「PC端 / 窗口」；「手动校准」**两种模式都有**（PC端开二级
        窗口、窗口走参照图取景器，按模式分派 manual_calib）；底部那一行平时写**短状态**
        （未开始 / 正在计数… / 已暂停 / 正在校准…），动作反馈说完自己让位。
    13. 第六轮（41）：切模式**自动清零**（切过去是待命态，点「开始」才数）；取景器
        **取消**之后引用要清掉（否则「手动校准」再也点不动）；「置顶」键删掉、窗口模式
        **默认置顶**（选完窗口就钉，钉完把面板抢回来）。

用法：python tools/check_ui_keys.py
"""

import ast
import io
import os
import sys
import time
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

    二级窗口本身还在（自检直接调 open_calib_dialog），但面板上已经没有入口了；
    「目标 / 微调 / 保存校准 / 恢复默认 / ROI 框 / 换窗口」全在窗口里。
    """
    from akframe import ui as ui_mod

    check('二级窗口的入口从面板上删掉了（用户：「原有的手动校准删除」）',
          not hasattr(app, 'btn_calib'))
    panel_btns = _btn_texts(app.root)
    check('面板上没有「保存校准 / 恢复默认」了（它们在二级窗口里）',
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
    check('展开：fr_more（曾装「手动校准…」那一行）不再铺出来',
          app.fr_more.winfo_manager() == '', app.fr_more.winfo_manager())
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


def t_window_row(app):
    """11：读窗口那个键并进控制行 + 默认置顶 + 「教程」那一块删掉。

    用户 2026-10-11：「把读取窗口功能中的框选费用条替换手动校准，原有的手动校准删除；
    置顶中删除默认置顶；下方的教程删除，两种模式的窗口大小不变，不需要展开」。
    41 轮追加：「窗口模式置顶删除，默认置顶」—— 键删了，选完窗口直接钉。
    """
    import types as _types
    from akframe import ui as ui_mod

    check('面板上不再有独立的一行 fr_window（两个键并进控制行了）',
          not hasattr(app, 'fr_window'))
    check('窗口那一行的「教程」连控件一起删了',
          not hasattr(app, 'lb_window_hint') and not hasattr(app, '_window_hint_base'))
    check('展开区那个「手动校准…」（自动模式的 ROI 二级窗口）也删了',
          not hasattr(app, 'btn_calib'))
    check('「手动校准」挂在控制行上（「置顶」那一格 41 轮删了）',
          app.btn_manual.master is app.fr_ctrl and not hasattr(app, 'btn_pin'))
    check('PC端：「手动校准」在（第五轮：PC端也要手动校准）',
          bool(app.btn_manual.winfo_manager()))

    def req():
        app._set_note('')          # 提示行那句话的长短会带着宽度走，量尺寸前清掉
        app.root.update_idletasks()
        return (app.root.winfo_reqwidth(), app.root.winfo_reqheight())

    live_w, live_h = req()
    app._src_kind = 'window'
    app._sync_ctrl_row()                     # 校准按钮上的字跟着模式走
    app._window_win = _types.SimpleNamespace(hwnd=4242, exe="t.exe",
                                             client=(0, 0, 1920, 1080))
    row = [b.cget("text") for b in app.fr_ctrl.pack_slaves()]
    check('读窗口模式：两个字铺出来、排在「开始」左边',
          row == ['校准：窗口▾', '手动校准', '开始', '清零'], row)
    check('「框选费用条」这个名字换成了「手动校准」（同一件事：手动圈条）',
          app.btn_manual.cget('text') == '手动校准'
          and '框选费用条' not in row)
    check('两种模式的行数一样、高度一模一样（面板尺寸不变）',
          req()[1] == live_h, '%d vs %d' % (req()[1], live_h))
    check('并进控制行之后宽度仍然落进基准宽 470（没把面板顶宽）',
          req()[0] <= app.COLLAPSED_W, req()[0])

    # ---- 默认置顶：选完窗口就该顺手 SetWindowPos(TOPMOST) + 把面板抢回来 ----
    calls = []
    raised = []
    real = (ui_mod.set_topmost, ui_mod.is_window, ui_mod.is_topmost, app._window_worker)
    real_raise = app._raise_panel
    app._window_worker = lambda *a, **k: None      # 别真起那条后台线程
    app._raise_panel = lambda: raised.append(1)
    ui_mod.set_topmost = lambda h, on=True: (calls.append((h, on)), True)[1]
    try:
        app.start_window_calib(_types.SimpleNamespace(hwnd=4242, exe="t.exe"),
                               box=None, force=True)
    finally:
        ui_mod.set_topmost = real[0]
        app._window_worker = real[3]
    check('选完窗口**默认就钉**（「窗口模式置顶删除，默认置顶」）',
          calls == [(4242, True)] and app._pinned_hwnd == 4242, str(calls))
    check('钉完把面板抢回来（抬目标那一下会把面板压在下面）',
          raised == [1], str(raised))

    # ---- _keep_pin：标记还在就一个系统调用都不发（这就是「不闪」）----
    app._pinned_hwnd = 4242
    calls = []
    raised = []
    ui_mod.set_topmost = lambda h, on=True: (calls.append((h, on)), True)[1]
    ui_mod.is_window = lambda h: True
    app._raise_panel = lambda: raised.append(1)
    try:
        ui_mod.is_topmost = lambda h: True
        app._pin_at = 0.0
        app._keep_pin(1000.0)
        check('置顶标记还在：一个系统调用都不发（稳态零闪烁）', calls == [], str(calls))
        ui_mod.is_topmost = lambda h: False
        app._pin_at = 0.0
        app._keep_pin(1001.0)
        check('标记丢了（播放器重建了窗口）：补钉一次 + 把面板抬回来',
              calls == [(4242, True)] and raised == [1],
              '%s / %s' % (calls, raised))
        ui_mod.is_window = lambda h: False
        app._pin_at = 0.0
        app._keep_pin(1002.0)
        check('窗口没了：放开置顶、提示换一个窗口（不再乱钉）',
              app._pinned_hwnd is None and '不见了' in app.lb_note.cget('text'),
              app.lb_note.cget('text'))
    finally:
        (ui_mod.set_topmost, ui_mod.is_window, ui_mod.is_topmost,
         app._raise_panel) = real[0], real[1], real[2], real_raise
    app._pinned_hwnd = None

    # ---- 原来那段「教程」里唯一必须留的：面板压住了 ROI ----
    class _R:
        def __init__(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h

    had = hasattr(app.monitor, "layout")
    old_layout = getattr(app.monitor, "layout", None)
    app.monitor.layout = _types.SimpleNamespace(
        bar=_R(app.root.winfo_rootx() + 20, app.root.winfo_rooty() + 20, 200, 20))
    app._refresh_roi_warn()
    check('面板压住费用条 -> 写进「告警：」那一行（教程没了，这条不能丢）',
          '压住了' in app.lb_warn.cget('text')
          and app.fr_warn.winfo_manager() == 'pack', app.lb_warn.cget('text'))
    app.monitor.layout = None
    app._refresh_roi_warn()
    check('没压住 -> 告警整行收起来',
          app.lb_warn.cget('text') == '' and app.fr_warn.winfo_manager() == '')
    app._set_warn('采样已停 1.0s（测试用）')          # 假装别的告警正占着那一行
    app._refresh_roi_warn()
    check('别的告警在说话时，ROI 这条不会把它收掉（否则告警行每拍一收一铺＝闪）',
          app.fr_warn.winfo_manager() == 'pack'
          and '采样已停' in app.lb_warn.cget('text'), app.lb_warn.cget('text'))
    app._set_warn('')
    if had:
        app.monitor.layout = old_layout
    else:
        try:
            del app.monitor.layout
        except AttributeError:
            pass

    app._src_kind = 'live'
    app._sync_ctrl_row()
    check('切回 PC端：「手动校准」留着（两种模式都有），尺寸不变',
          bool(app.btn_manual.winfo_manager()) and req() == (live_w, live_h),
          '%s vs %s' % (req(), (live_w, live_h)))


def t_status_row(app):
    """12：模式名 -> PC端 / 窗口；PC端也手动校准；底部那一行写短状态。

    用户 2026-10-11 第五轮：「把模式切换文本改成 pc端和窗口，pc端也加入手动校准功能，
    下方的状态描述用一行简短的文字，比如正在校准。。。；正在计数。。。」
    """
    # 11 组把校准线程 stub 掉了，那道工序的状态会一直挂着（_window_busy=True）——
    # 那样 _tick_body 每拍提前 return，底下一行永远写不到状态。
    app._window_busy = False
    app._window_result = None
    app._window_win = None
    app._src_kind = 'live'
    app._sync_ctrl_row()

    row = [b.cget('text') for b in app.fr_ctrl.pack_slaves()]
    check('模式名换成「PC端 / 窗口」（不再叫自动 / 手动）',
          row[0] == '校准：PC端▾' and '自动' not in ' '.join(row)
          and '手动' not in row[0], row)
    check('PC端也有「手动校准」（用户：「pc端也加入手动校准功能」）',
          row == ['校准：PC端▾', '手动校准', '开始', '清零'], row)

    # 「手动校准」按模式分派：PC端 -> 二级窗口（挪推算出来的 ROI）；
    # 窗口 -> 参照图取景器（画面被缩放过，只能自己圈）
    calls = []
    box, dlg = app.box_bar, app.open_calib_dialog
    app.box_bar = lambda: calls.append('box')
    app.open_calib_dialog = lambda: calls.append('dialog')
    try:
        app._src_kind = 'live'
        app.manual_calib()
        app._src_kind = 'window'
        app.manual_calib()
    finally:
        app.box_bar, app.open_calib_dialog = box, dlg
        app._src_kind = 'live'
    check('「手动校准」按模式分派：PC端 -> 二级窗口，窗口 -> 圈条取景器',
          calls == ['dialog', 'box'], calls)

    def status(reset=True):
        if reset:
            app._note_hold = 0.0
        app._tick_body()
        return app.lb_note.cget('text')

    app._counting_started = False
    check('没在数 -> 底下写「未开始」', status() == '未开始',
          repr(app.lb_note.cget('text')))
    app.monitor.start()
    app._counting_started = True
    check('在数 -> 底下写「正在计数…」', status() == '正在计数…',
          repr(app.lb_note.cget('text')))
    app.toggle_manual_pause()
    check('暂停 -> 底下写「已暂停」', status() == '已暂停',
          repr(app.lb_note.cget('text')))
    app.toggle_manual_pause()
    app._calib = True
    check('在微调 ROI -> 底下写「正在校准…」', status() == '正在校准…',
          repr(app.lb_note.cget('text')))
    app._calib = False

    # 动作反馈说完要让位（不让位的话底下一行又变成甩不掉的一句旧话）
    app._set_note('已清零')
    check('动作反馈先顶上来', app.lb_note.cget('text') == '已清零')
    status(reset=False)
    check('反馈还没到点：仍然是它（不许被状态抢掉）',
          app.lb_note.cget('text') == '已清零', repr(app.lb_note.cget('text')))
    status()
    check('反馈到点：自己让位给状态', app.lb_note.cget('text') == '正在计数…',
          repr(app.lb_note.cget('text')))

    # 「一行简短的文字」查得住：所有写底下的字面量都不超 28 字、不换行
    bad = []
    src = io.open(os.path.join(ROOT, 'src', 'akframe', 'ui.py'), encoding='utf-8').read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ('_set_note', '_note_pin') and node.args:
            try:
                val = ast.literal_eval(node.args[0])
            except Exception:                  # noqa: BLE001  拼接 / f-string，跳过
                continue
            if isinstance(val, str) and (len(val) > 28 or '\n' in val):
                bad.append('%d: %r' % (node.lineno, val))
    check('写底部那一行的字面量全长在 28 字以内、没有换行（用户要「一行简短的文字」）',
          not bad, bad[:3])

    t_switch_clears(app)
    t_picker_cancel(app)


def t_switch_clears(app):
    '''13a：切模式 = 自动清零，而且切过去是**待命**态（41 轮「切换模式自动清零」）。

    为什么必须清零：两个来源的帧号不是一个坐标系（PC端 = 游戏窗口，窗口 = 播放器 /
    录像），接在一起没有意义；不清的话大数字会挂着上一个来源数到的值，看着像「已经
    数了 500 帧」，而这一拍其实什么都没数。
    为什么是待命而不是接着跑：读窗口那条链路本来就是「校准完等你点开始」，切回去两边
    一个规矩 —— 一眼看得出没在数，也不会把「切模式」偷偷变成「开始」。
    '''
    if not app.monitor.alive:              # 上一组可能刚把它停了
        app.monitor.start()
    app._counting_started = True
    app._src_kind = 'window'               # 假装读着窗口，再切回 PC端
    app.back_to_live()
    check('切换模式自动清零：帧数归 0、按钮回「开始」、停采样待命',
          app._src_kind == 'live' and app._counting_started is False
          and app.monitor.alive is False and app.lb_frames.cget('text') == '0'
          and app.btn_start.cget('text') == '开始',
          '%s / %s / %s / %r / %s' % (app._src_kind, app._counting_started,
                                      app.monitor.alive, app.lb_frames.cget('text'),
                                      app.btn_start.cget('text')))
    check('切模式也给一句话（提示切过去是待命、点「开始」才数）',
          '已清零' in app.lb_note.cget('text'), repr(app.lb_note.cget('text')))


def t_picker_cancel(app):
    '''13b：取景器「取消」之后引用要清掉，否则「手动校准」再也点不动（41 轮）。

    用户原话：「手动校准点取消后有时会无法再次手动校准」。根因：BoxPicker.close() 只
    destroy 窗口，而「取消 / Esc / 右上角 ×」都不经过 on_apply —— 面板那边的
    self._picker 就永远不是 None，box_bar 开头那句 `if self._picker is not None: return`
    一直命中。修法：close() 收尾统一回调 on_close（且幂等），面板在那个回调里清引用。
    '''
    import types as _types
    from akframe import ui as ui_mod

    # 1) close() 自己：幂等 + on_close 恰好一次（应用那条路是 close() 之后才 on_apply()）
    raw = ui_mod.BoxPicker.__new__(ui_mod.BoxPicker)
    seen = []
    raw._closed = False
    raw.top = _types.SimpleNamespace(destroy=lambda: seen.append('destroy'))
    raw.on_close = lambda: seen.append('cb')
    raw.close()
    raw.close()
    check('取景器 close() 幂等：销毁一次、on_close 恰好一次（不重复回调）',
          seen == ['destroy', 'cb'], seen)

    # 2) 接线：取消（走 on_close）之后 _picker 清空，于是能再点开
    made = []

    class _FakePicker(object):
        def __init__(self, parent, fetch, frames, index, on_apply, on_note=None,
                     fullest=None, on_close=None):
            self.on_close = on_close
            made.append(self)

        def close(self):                   # 照着 BoxPicker.close 那条路走
            if self.on_close is not None:
                self.on_close()

    real_cls = ui_mod.BoxPicker
    app._picker = None
    app._src_kind = 'window'
    app._window_win = _types.SimpleNamespace(hwnd=7, exe='t.exe',
                                             client=(0, 0, 1920, 1080))
    ui_mod.BoxPicker = _FakePicker
    try:
        app.box_bar()
        check('第一次点「手动校准」开出取景器', app._picker is made[0])
        app.box_bar()
        check('取景器开着时再点不重开（一次只该有一个）', len(made) == 1)
        made[0].close()                    # 用户点了「取消」
        check('取景器「取消」之后面板把引用清掉了', app._picker is None)
        app.box_bar()
        check('取消之后还能再点开「手动校准」（用户报的就是这一条）',
              len(made) == 2 and app._picker is made[1])
    finally:
        ui_mod.BoxPicker = real_cls
        app._picker = None
        app._src_kind = 'live'
        app._window_win = None


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

    # ---- 9b. 窗口被**别的方式**挪走之后，布局重算不能把它拽回去（2026-10-11）----
    # 用户：「点击按钮都有可能导致窗口重置位置（漂移）」。_place 是照 _user_pos /
    # _anchor_bottom 算位置的，而这两份缓存**只有拖面板自己那条 head** 才会更新
    # （见 _drag_move）—— 用系统标题栏拖、系统贴边、换分辨率都会让缓存停在原地，于是
    # 下一次布局重算（展开 / 收起、告警行收起来、内容长高触发的 _fit_window…）就照旧
    # 缓存把窗口拽回去。修法：_place 之前先 _sync_pos，把缓存按「挪走的那一段」平移。
    def move_to(x, y):
        root.geometry('+%d+%d' % (x, y))       # 绕开面板那套缓存，像系统标题栏那样挪
        root.update()

    move_to(700, 200)
    m_y = root.winfo_y()
    check('绕开面板把窗口挪走（geometry 直接挪）：窗口真的到了 (700,200)',
          (root.winfo_x(), root.winfo_y()) == (700, 200),
          str((root.winfo_x(), root.winfo_y())))
    app.toggle_more()                          # 展开 -> 收起
    root.update()
    check('挪走之后收起：x 不许被拽回旧缓存（修之前会被拽回 _user_pos[0]）',
          root.winfo_x() == 700, 'x=%d（缓存 x=%d）' % (root.winfo_x(), app._user_pos[0]))
    check('挪走之后收起：y 回到挪走时那个 y（平移没丢「想要的位置」）',
          root.winfo_y() == m_y, 'y=%d（挪走时 %d）' % (root.winfo_y(), m_y))
    app._refit()
    root.update()
    check('再 _refit 一次（点按钮顺手会干的事）：x / y 都不动',
          (root.winfo_x(), root.winfo_y()) == (700, m_y),
          str((root.winfo_x(), root.winfo_y())))
    app.toggle_more()                          # 收起 -> 展开
    root.update()
    check('再展开：x 还是 700，y 只往上（<= 收起时那个 y）',
          root.winfo_x() == 700 and root.winfo_y() <= m_y,
          'x=%d y=%d（收起时 %d）' % (root.winfo_x(), root.winfo_y(), m_y))
    app.toggle_more()                          # 回到收起态，后面照旧
    root.update()

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
                print('11. 读窗口那两个键 / 不默认置顶 / 教程删掉')
                t_window_row(app2)
                print('12. PC端 / 窗口 改名 + PC端也能手动校准 + 底下一行是短状态')
                t_status_row(app2)
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
