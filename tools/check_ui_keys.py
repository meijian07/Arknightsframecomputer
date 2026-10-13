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
    3. 一个键盘快捷键都不收（2026-10-12：游戏里有快捷键，绑了就会冲突）；
    4. ROI 校准模式（2026-10-11 起跟「手动校准」二级窗口绑定）：Tab / 方向键 /
       Shift+方向键 / S / R / K 只在窗口开着时绑上，关窗口（或 Esc）一定解绑
       （以前是全局的 —— 打字、翻条、误按都会碰到 ROI）；
    5. 刻度跨度**固定 150 帧**（2026-10-11 删了 [ ] 缩放档：跨度常量算出来的
       px_per_frame 得对得上）；翻条走标题栏按钮（回看偏移、边界、没轴时提示）；
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
    15. 第八轮（44）：取景器右边那块放大图能**按住拖动**（看框外面），平移不改倍数；
        框自己微调时视窗反向补，看的还是同一块画面。
    16. 第九轮（46）：放大图能**滚轮局部放大**（盯着鼠标那一点缩，双击复位）；
        同一轮把「吸附左端」删了（用户：实测下来一次没用过）。
    13. 第六轮（41）：切模式**自动清零**（切过去是待命态，点「开始」才数）；取景器
        **取消**之后引用要清掉（否则「手动校准」再也点不动）；「置顶」键删掉、窗口模式
        **默认置顶**（选完窗口就钉，钉完把面板抢回来）。
    14. 第八轮（44）：「开着我们自己的对话框就别去钉」那道门拆了（它让置顶整段停摆，
        用户报的「读取窗口无法保持置顶」），改成钉完由 _raise_ours() 把面板 +
        对话框一起抬回来；另外 _keep_pin 不再要求 self._window_win 已就位。
        同轮第二条真因：标记还在 ≠ 我们没被压住（用户点一下播放器，它跑到置顶层最上面），
        所以 _keep_pin 在「标记还在」那一支也要比一次 z 序（is_above）。

    17. 第十轮（47 / 48 上半）：**整个界面一个键盘快捷键都不收**（用户 2026-10-12：「把快捷键
        删掉！！！游戏里有快捷键会冲突」）—— Esc / Ctrl+Q / 空格 / Tab / 方向键 / s / k /
        [ ] / ↑↓ 全删了（打轴器翻条挪到标题栏那两个按钮上），所以这一份自检里所有关于
        「哪个键归谁」的断言整组改写成「一个键盘事件都没绑」；暂停键多了两块**虚影**
        （boxpick.pause_ghost）当对齐参考。
    18. 第十轮（48 下半，用户：「放大做的什么鬼，先在左侧原画面创建一个框，就像 Windows 的
        截图一样可以拖动框放大缩小移动，右侧为框内放大图，放大图下方可以微调px」）：
        取景器分工摆清楚 —— 左边那张图 = Windows 截图那套（拖框里面 = 挪框、拖八个手柄
        = 拉那条边 / 那个角、框外面拖 = 拉新框）；右边放大图 = 只看（左键拖 = 挪视窗、
        滚轮 = 局部放大、双击 = 复位）；放大图**下面**两行 px 微调（左 / 右 / 上 / 下 ±1、
        整框 ←→↑↓）回来了 —— 这一轮它们才是**唯一**的像素级入口。那套「左键拖框 /
        Shift+拖挪视窗」作废（用户实测：在放大图里随便一拖框就跑了）。按钮那一对现在
        **可以拉大拉小**（原来写死标称尺寸）。

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


def t_no_keys(app):
    """3：**一个键盘快捷键都不收**（用户 2026-10-12：「把快捷键删掉！！！游戏里有快捷键
    会冲突」）。

    为什么必须一条都不留：面板是浮在游戏上的，游戏自己有用键盘的操作（帧操、放技能、
    翻干员…）。我们只要占了一个键，用户在游戏里按它就会同时打到面板上 —— 用户实测就是
    这么发现的。所以这一组从「哪个键归谁」反过来钉：**根窗口上一个键盘事件都不许绑**。
    """
    from akframe import ui as ui_mod

    seqs = [s for s in app.root.bind() if s not in ('<Motion>',)]
    keys = [s for s in seqs if not s.startswith('<Button') and not s.startswith('<B1')
            and not s.startswith('<Double') and not s.startswith('<Enter')
            and not s.startswith('<Leave')]
    check('面板根窗口一个键盘事件都没绑（只剩鼠标）', keys == [], str(seqs))
    check('原来那套绑键 / 解绑的机器整台删掉了',
          not hasattr(ui_mod.MonitorApp, '_bind_keys')
          and not hasattr(ui_mod.MonitorApp, '_base_keys')
          and not hasattr(ui_mod.MonitorApp, '_calib_keys')
          and not hasattr(app, '_on_escape'),
          '%s %s' % (hasattr(ui_mod.MonitorApp, '_bind_keys'), hasattr(app, '_on_escape')))
    # 打轴器的翻条挪到标题栏那两个按钮上（↑↓ 没了）——按钮在、而且真接上了 _prom_scroll
    fr = getattr(app, 'fr_more_top', None)
    btns = _btn_texts(fr) if fr is not None else []
    check('打轴器标题栏有「◀ 回看 / 现在 ▶」两个按钮', '◀ 回看' in btns and '现在 ▶' in btns,
          str(btns))
    app._set_axis_text('300 部署 银灰\n330 技能 银灰')
    app.apply_axis_text()
    app._prom_scroll(1)
    check('「◀ 回看」那条路真能翻条（回看 30 帧）',
          abs(app._prom_shift - 30.0) < 1e-9, app._prom_shift)
    app._prom_scroll(-99)
    check('「现在 ▶」回到 0', app._prom_shift == 0.0, app._prom_shift)


def t_calib_mode(app):
    """4：ROI 校准模式（二级窗口）—— 进不进模式都**不收键盘**，微调只剩按钮。"""
    import tkinter as tk
    from akframe import ui as ui_mod

    try:
        app.open_calib_dialog()
    except tk.TclError as exc:
        skip('ROI 校准模式', '建不出二级窗口：%s' % exc)
        return
    check('开窗口 = 进模式', bool(app._calib))
    check('进了模式也没绑键盘（Tab / 方向键 / s / k 一个都不收）',
          not app.root.bind('<Tab>') and not app.root.bind('s')
          and not app.root.bind('<Shift-Up>') and not hasattr(app, '_nudge'),
          '%s %s' % (app.root.bind('<Tab>'), hasattr(app, '_nudge')))
    check('目标按钮当前那个反色（白底深字）',
          app.calib_tgt_btns[app.target].cget('bg') == ui_mod.RULE,
          app.calib_tgt_btns[app.target].cget('bg'))
    # 微调那条路（原来归方向键，现在只有按钮）——直接调方法，验证接线还在
    app.nudge(1, 0)
    check('按钮那条路照样能挪 ROI（一步 = calib_step）',
          app.deltas[app.target][0] == app.calib_step, app.deltas)
    app.reset_calib()
    check('「恢复默认」把 delta 清零',
          app.deltas[app.target] == [0, 0, 0, 0], app.deltas)
    app.close_calib_dialog()
    check('关窗口 = 退模式', not app._calib)
    check('关窗口后预览画布收掉（cv_bar = None）', app.cv_bar is None)


def t_scale_and_scroll(app):
    """5：刻度固定 150 帧（没有缩放档了）+ 标题栏按钮翻条（↑↓ 键没了）。"""
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
    check('「◀ 回看」 = 往回看 30 帧（1 费 = 1 秒）',
          abs(app._prom_shift - 30.0) < 1e-9, app._prom_shift)
    app._prom_scroll(1, 1000.0)
    check('回看有上限（600 帧）', abs(app._prom_shift - 600.0) < 1e-9, app._prom_shift)
    app._prom_scroll(-1, 1000.0)
    check('「现在 ▶」一路回到「现在」',
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
    # 2026-10-12：取景器里那一堆微调按钮（左端±1 / 右端±1 / 整框…）整排删了，
    # 靠鼠标直接改框（拖身子挪 / 拖边改边）。
    check('取景器那两排微调控件整排删掉了',
          not hasattr(app, 'row_bar') and not hasattr(app, 'row_btn'),
          '%s %s' % (hasattr(app, 'row_bar'), hasattr(app, 'row_btn')))
    check('取景器微调用的 _nudge_pair 也删了',
          not hasattr(ui_mod.BoxPicker, '_nudge_pair'))


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
        check('预览画布是 430x200（装得下按钮 ROI 那一档）',
              app.cv_bar is not None and int(app.cv_bar.cget('width')) == 430
              and int(app.cv_bar.cget('height')) == 200)
        # 放大镜（2026-10-11 用户：「最好加一个放大镜，放大部分画面方便对齐」）：
        # 画布显示**当前目标**那个 ROI，红线就是特征真正取值的那几行（占比换算出来的）。
        app.set_target('bar')
        check('预览红线跟着目标走：费用条 = 行 5..12',
              app._preview_marks((17, 253)) == ((5, 12), None),
              str(app._preview_marks((17, 253))))
        app.set_target('pause')
        check('预览红线跟着目标走：暂停键 = 形状判别条带',
              app._preview_marks((160, 200)) == ((38, 48), None),
              str(app._preview_marks((160, 200))))
        app.set_target('speed')
        marks = app._preview_marks((160, 200))
        check('预览红线跟着目标走：倍速键 = 数字条带 + 横向范围',
              marks == ((18, 49), (60, 169)), str(marks))
        import numpy as _np
        arr = _np.zeros((160, 200), _np.uint8)
        arr[marks[0][0]:marks[0][1] + 1, marks[1][0]:marks[1][1] + 1] = 255
        app._show(app.cv_bar, arr, mark_rows=marks[0], mark_cols=marks[1])
        photo = app._photos[app.cv_bar]
        check('按钮 ROI 在预览里被放大（不是 1 倍、也没被裁掉）',
              photo.width() > 200 and photo.height() <= 200 and photo.width() <= 430,
              '%dx%d' % (photo.width(), photo.height()))
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
    44 轮追加：置顶不再被「开着对话框」那道门停摆，钉完把自己的窗全抬回来。
    """
    import tkinter as tk
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
    real = (ui_mod.set_topmost, ui_mod.is_window, ui_mod.is_topmost, app._window_worker,
            ui_mod.is_above)
    real_raise = app._raise_ours
    app._window_worker = lambda *a, **k: None      # 别真起那条后台线程
    app._raise_ours = lambda: raised.append(1)
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
    ui_mod.is_above = lambda a, b: False           # 目标没压在我们上面
    app._raise_ours = lambda: raised.append(1)
    try:
        ui_mod.is_topmost = lambda h: True
        app._pin_at = 0.0
        app._keep_pin(1000.0)
        check('置顶标记还在：一个系统调用都不发（稳态零闪烁）', calls == [], str(calls))
        # 标记还在 **≠** 我们没被压住：用户点一下正在读的那个播放器，系统就把它抬到
        # 置顶层最上面，我们的面板当场被压下去（真 VLC 实测：VLC rank 17->14、面板
        # 13->17），而标记还好端端挂着。所以标记还在那一支也得复核一遍 z 序。
        ui_mod.is_above = lambda a, b: True
        app._pin_at = 0.0
        app._keep_pin(1000.5)
        check('标记还在、但目标跑到我们上面：不发系统调用，只把自己抬回来',
              calls == [] and raised == [1], '%s / %s' % (calls, raised))
        ui_mod.is_above = lambda a, b: False
        raised = []
        app._pin_at = 0.0
        app._keep_pin(1000.8)
        check('没被压住就一个动作都不做（稳态零闪烁的另一半）',
              calls == [] and raised == [], str(raised))
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
         app._raise_ours) = real[0], real[1], real[2], real_raise
        ui_mod.is_above = real[4]
    app._pinned_hwnd = None

    # ---- 14. 开着我们自己的对话框时，置顶**照样**复核 + 把自己人全抬回来 ----
    # 用户 2026-10-11 第 44 轮：「读取窗口又无法保持置顶了」。病因在这儿：
    # _tick_body 里原来挂着一道 `not self._own_dialog_open()` 的门 —— 门一关 _keep_pin
    # 整段停摆，目标在那期间丢了标记（播放器自己重设窗口 / 换下一段视频 / 切全屏）
    # 就再也没有人把它钉回去，而面板上什么都不会说。现在门拆了：照样复核；「别把取景器
    # 压下去」这件事改由 _raise_ours() 负责 —— 钉完把面板和对话框一起抬回目标上面。
    top = tk.Toplevel(app.root)
    app.root.update()
    calls = []
    lifted = []
    real2 = (ui_mod.set_topmost, ui_mod.is_window, ui_mod.is_topmost,
             ui_mod.top_level, ui_mod.raise_window)
    ui_mod.set_topmost = lambda h, on=True: (calls.append((h, on)), True)[1]
    ui_mod.is_window = lambda h: True
    ui_mod.is_topmost = lambda h: False        # 标记丢了（播放器刚重设过窗口）
    ui_mod.top_level = lambda h: h             # 面板 / 对话框各自给回自己的句柄
    ui_mod.raise_window = lambda h: (lifted.append(h), True)[1]
    try:
        check('开着一个对话框：_own_dialog_open() 为真', app._own_dialog_open())
        panel_h, dlg_h = app.root.winfo_id(), top.winfo_id()
        app._pinned_hwnd = 4242
        app._pin_at = 0.0
        app._keep_pin(time.perf_counter())
        check('对话框开着也照样补钉（第 44 轮修的就是这条）',
              calls == [(4242, True)], str(calls))
        check('钉完把面板和对话框都抬回来（取景器仍然盖在目标上面）',
              lifted == [panel_h, dlg_h], str(lifted))
    finally:
        top.destroy()
        app.root.update()
        (ui_mod.set_topmost, ui_mod.is_window, ui_mod.is_topmost,
         ui_mod.top_level, ui_mod.raise_window) = real2
        app._pinned_hwnd = None
    check('对话框关掉之后 _own_dialog_open() 又为假', not app._own_dialog_open())

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

    # 3) 交回去什么：费用条**总是**交（它就是取景器的正题），按钮只在拖过时才交 ——
    #    没拖过就留给后端按模板搜（拿一份没人验过的预测去盖掉搜索结果是反的）。
    def _apply_with(dirty):
        pk = ui_mod.BoxPicker.__new__(ui_mod.BoxPicker)
        pk._closed = False
        pk.top = _types.SimpleNamespace(destroy=lambda: None)
        pk.on_close = None
        pk.boxes = {'bar': _types.SimpleNamespace(x=1, y=2, w=114, h=17),
                    'pause': _types.SimpleNamespace(x=3, y=4, w=91, h=73),
                    'speed': _types.SimpleNamespace(x=5, y=6, w=91, h=73)}
        pk.dirty = set(dirty)
        got = []
        pk.on_apply = got.append
        pk._apply()
        return got[0]

    only_bar = _apply_with(())
    check('取景器：没拖过按钮就只交费用条那一个框', not isinstance(only_bar, dict)
          and only_bar.x == 1, repr(only_bar))
    with_btn = _apply_with(('bar', 'pause', 'speed'))
    check('取景器：拖过按钮就三个框一起交（按目标名）',
          isinstance(with_btn, dict) and set(with_btn) == set(ui_mod.ROI_ORDER)
          and with_btn['pause'].x == 3, repr(sorted(with_btn)))
    btn_only = _apply_with(('pause', 'speed'))
    check('取景器：只拖了按钮也要带上费用条（否则后端会去跑自动那套）',
          isinstance(btn_only, dict) and btn_only['bar'].x == 1, repr(sorted(btn_only)))

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


def t_picker_pan(app):
    """15：取景器：左边那张图 = Windows 截图那套；右边放大图 = 只看 + 挪视窗。

    2026-10-11 第 44 轮用户要的是「放大图能自己移动，不然很难对准」；2026-10-12 第 48 轮
    又补一句：「放大做的什么鬼，先在左侧原画面创建一个框，就像 Windows 的截图一样可以
    拖动框放大缩小移动，右侧为框内放大图，放大图下方可以微调px」。于是分工摆清楚：

        左边图：拖框里面 = 挪框、拖八个手柄 = 拉那条边 / 那个角、框外面拖 = 拉新框；
        放大图：左键拖 = 挪视窗（看框外面），滚轮 = 局部放大，双击 = 复位；
        放大图下面两行按钮 = 一次 1 个像素的微调。

    位移一律按「按下那一刻」折算（boxpick.resize_box），不逐拍累加 —— 逐拍累加会攒下每
    一步的取整误差，手抖一下再抖回来框就回不到原位，而这是「对 1 个像素」的活儿。
    """
    import types as _types
    import numpy as np
    from akframe import ui as ui_mod
    from akframe.geometry import layout_for, Rect

    guide = layout_for(2560, 1600)
    frame = (np.tile(np.arange(2560, dtype=np.uint16), (1600, 1)) % 190).astype(np.uint8)
    pk = ui_mod.BoxPicker(app.root, lambda i: (frame, guide), 3, 1, lambda _b: None)
    try:
        app.root.update()
        check('取景器：放大图换成了「可以拖」的光标', pk.cv_zoom.cget('cursor') == 'fleur',
              pk.cv_zoom.cget('cursor'))
        pk.set_target('pause')
        check('换目标把视窗重置回框里（别带着上一个目标的平移）',
              pk.pan == [0.0, 0.0], str(pk.pan))
        r = pk.box
        # 暂停键虚影（用户 2026-10-12：「手动框选暂停键参考不明确，最好加一个暂停键的
        # 虚影方便对齐」）：几何对 + **真的贴到画布上了**。量 pk._photo['zoom'] 的像素、
        # 和「不画虚影」那张同一个位置比 —— 单调一次 magnify 证明不了接线没接错。
        bp = ui_mod.boxpick
        gp = bp.pause_ghost(r)

        def pxs(poly):
            return [p[0] for p in poly]

        def pys(poly):
            return [p[1] for p in poly]

        b0p = pk._band_rows(r)[0]
        # 斜切量：底边比顶边往右挪多少（两段真录像量出来是「字形高的 19%」）。
        shear = int(round(bp.PAUSE_GHOST_SHEAR * (max(pys(gp[0])) - min(pys(gp[0])) + 1)))
        check('暂停键虚影 = 两块**平行四边形**：上下两条边一样宽、底边整体往右斜切',
              len(gp) == 2 and all(len(p) == 4 for p in gp)
              and all(pys(p)[0] == pys(p)[1] and pys(p)[2] == pys(p)[3] for p in gp)
              and all(pxs(p)[1] - pxs(p)[0] == pxs(p)[2] - pxs(p)[3] for p in gp)
              and all(pxs(p)[3] - pxs(p)[0] == shear
                      and pxs(p)[2] - pxs(p)[1] == shear for p in gp)
              and shear > 0, '%s shear %s' % (gp, shear))
        check('暂停键虚影：两块都在框里、第二块在第一块右边、顶边压在取样条带最上一行',
              max(pxs(gp[0])) < min(pxs(gp[1]))
              and all(r.x <= min(pxs(p)) and max(pxs(p)) <= r.x + r.w for p in gp)
              and all(min(pys(p)) == b0p and max(pys(p)) <= r.y + r.h for p in gp),
              '%s 条带上沿 %s' % (gp, b0p))
        kzp = bp.zoom_scale(r, ui_mod.BoxPicker.ZOOM_PANEL)
        vxp, vyp = bp.zoom_view(r, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom,
                                pk._frame_size())[:2]
        mid = gp[0]
        cx = int((sum(pxs(mid)) / 4.0 - vxp) * kzp[0])
        cy = int((sum(pys(mid)) / 4.0 - vyp) * kzp[1])

        def zpx(photo, x, y):
            return tuple(int(v) for v in photo.tk.call(photo, 'get', x, y))

        got = zpx(pk._photo['zoom'], cx, cy)
        plain = bp.magnify(pk.frame, r, panel=ui_mod.BoxPicker.ZOOM_PANEL,
                           band=pk._band_rows(r), pan=pk.pan, zoom=pk.zoom,
                           ghost=(), band_fill=True)
        ref = ui_mod.ImageTk.PhotoImage(plain)      # 留着引用，别让 GC 收走
        base = zpx(ref, cx, cy)
        want_ghost = tuple(int(round(g * bp.GHOST_ALPHA / 255.0
                                     + b * (255 - bp.GHOST_ALPHA) / 255.0))
                           for g, b in zip(bp.GHOST, base))
        check('放大图里真的画上了虚影（中心 = 半透明 GHOST 压在画面上）',
              all(abs(a - b) <= 3 for a, b in zip(got, want_ghost)),
              'got %s want %s base %s' % (got, want_ghost, base))
        check('虚影是半透明的（没把底下那层游戏画面盖死）',
              got != base and got != bp.GHOST, '%s base %s' % (got, base))
        kx = ui_mod.boxpick.zoom_scale(r, ui_mod.BoxPicker.ZOOM_PANEL)[0]
        w0, h0 = pk._photo['zoom'].width(), pk._photo['zoom'].height()
        # 放大图现在**只**负责看（左键拖 = 挪视窗）。上一轮那套「拖身子挪框 / 拖边改边」
        # 整段搬回左边那张图了 —— 在放大图里随便一拖框就跑，那正是「放大做的什么鬼」。
        pk._zdown(_types.SimpleNamespace(x=100, y=60))
        pk._zdrag(_types.SimpleNamespace(x=100 + 4 * kx, y=60))
        check('放大图里左键拖动 = 挪视窗：往右拖 4 个画面像素 pan = -4（除以放大倍数）',
              abs(pk.pan[0] + 4.0) < 1e-6 and pk.pan[1] == 0.0, str(pk.pan))
        check('平移不改画布里的图尺寸（倍数只由框决定，越挪不会越小）',
              (pk._photo['zoom'].width(), pk._photo['zoom'].height()) == (w0, h0),
              '%dx%d' % (pk._photo['zoom'].width(), pk._photo['zoom'].height()))
        check('放大图里那一拖只挪视窗，框一动不动（挪框在左边那张图上）',
              pk.box == r, '%s -> %s' % (r, pk.box))
        pk._zup()
        check('松手之后不再跟着手走', pk._zpan_from is None)
        # 往左上拖（鼠标往左上 = 看画面右下角）-> 视窗顶到画面右下角就不动了
        pk._zdown(_types.SimpleNamespace(x=0, y=0))
        pk._zdrag(_types.SimpleNamespace(x=-10 ** 6, y=-10 ** 6))
        pk._zup()
        check('拖出画面 -> 夹在右下角（视窗一直还在画面里）',
              pk.pan == [float(2560 - r.w - r.x), float(1600 - r.h - r.y)], str(pk.pan))
        pk._zdown(_types.SimpleNamespace(x=0, y=0))
        pk._zdrag(_types.SimpleNamespace(x=10 ** 6, y=10 ** 6))
        pk._zup()
        check('往反方向拖出去 -> 夹在左上角（不会越拖越远，回来时不用空拖）',
              pk.pan == [float(-r.x), float(-r.y)], str(pk.pan))
        pk.set_target('bar')
        # 第 45 轮（用户：「查看上下左右是否对齐」）：费用条目标的放大图里**四条边**都画着。
        # 量的是取景器**真正贴上去的那张图**（tk photo 的 get 子命令），不是另调一次
        # magnify —— 量后者只能证明纯函数对（第 11 组已经量了），证明不了没接错线。
        zm = ui_mod.boxpick

        def zsize():
            p = pk._photo['zoom']
            return p.width(), p.height()

        def px(x, y):
            p = pk._photo['zoom']
            return tuple(int(v) for v in p.tk.call(p, 'get', x, y))

        # 橙色那条是「按分辨率推测的位置」参考线（layout）—— 默认框正好等于
        # 预测框，它会盖在四条边上（重叠处谁后画谁赢）。一个参考线遮住了真正
        # 要看的边，那就先把它关掉，单独量四条边。
        pk.guide = None
        pk.redraw()
        iz, jz = zsize()
        jy = jz * 3 // 4            # 避开两条蓝线（取样条带）占的那两行
        check('费用条目标的放大图：左红 / 右绿 / 上下品红四条边压在画布四边上',
              px(0, jy) == zm.EDGE_LEFT and px(iz - 1, jy) == zm.EDGE_RIGHT
              and px(iz // 2, 0) == zm.EDGE_TOP and px(iz // 2, jz - 1) == zm.EDGE_BOTTOM,
              '%s %s %s %s' % (px(0, jy), px(iz - 1, jy), px(iz // 2, 0), px(iz // 2, jz - 1)))
        b0, b1 = pk._band_rows(pk.box)          # 绝对行号（画面坐标，见 _band_rows）
        check('取样条带落在框内（上下都在框里，蓝线不会跑到框外去）',
              pk.box.y < b0 <= b1 < pk.box.y + pk.box.h,
              'band=%s 框 y=%d h=%s' % ((b0, b1), pk.box.y, pk.box.h))
        # 放大图**下面**那两行 px 微调（用户 2026-10-12：「放大图下方可以微调px」）：走的是
        # 和拖框同一套几何（boxpick.resize_box），一次 1 个画面像素 —— 对付的就是「红线和
        # 白块左端差 1~2px」。这里量的是**真的接线**：调 pk.nudge_edge / pk.nudge_move
        # （按钮的 command 打的就是这两个），看框有没有按那一条边 / 整块动。
        check('px 微调按钮是活的（上一轮删掉的那两排，现在长在放大图下面）',
              hasattr(ui_mod.BoxPicker, 'nudge_edge')
              and hasattr(ui_mod.BoxPicker, 'nudge_move'), '')
        rw = pk.box
        pk.nudge_edge('r', 1)
        check('「右 +1」：只把右端往外挪 1 个画面像素（左端不动）',
              pk.box == Rect(rw.x, rw.y, rw.w + 1, rw.h), '%s -> %s' % (rw, pk.box))
        rw = pk.box
        pk.nudge_edge('l', -1)
        check('「左 −1」：左端动、右端不动（x 与宽一起变）',
              pk.box == Rect(rw.x - 1, rw.y, rw.w + 1, rw.h), '%s -> %s' % (rw, pk.box))
        rw = pk.box
        pk.pan = [0.0, 0.0]
        pk.nudge_move(-2, 3)
        check('整框 ← ↑：整块平移（宽高一点不变）',
              pk.box == Rect(rw.x - 2, rw.y + 3, rw.w, rw.h), '%s -> %s' % (rw, pk.box))
        check('微调挪块：pan 反向补同样多（放大图不跟着跳）',
              abs(pk.pan[0] - 2.0) < 1e-6 and abs(pk.pan[1] + 3.0) < 1e-6, str(pk.pan))
        kb, _ = zm.zoom_scale(pk.box, ui_mod.BoxPicker.ZOOM_PANEL)
        pk.pan = [9.0, 5.0]
        pk.redraw()
        # 平移（右移 9px）之后：绿线不再贴在画布最右列，而是跟着框挪到 (w-9)*kx-1 列。
        iz2, jz2 = zsize()
        jy2 = jz2 * 3 // 4
        check('平移后四条边跟着框走（右边挪到 (w-9)*kx-1 列）',
              px((pk.box.w - 9) * kb - 1, jy2) == zm.EDGE_RIGHT
              and px(iz2 - 1, jy2) != zm.EDGE_RIGHT,
              '最右列 %s，期望第 %d 列是绿色'
              % (px(iz2 - 1, jy2), (pk.box.w - 9) * kb - 1))
        pk._down(_types.SimpleNamespace(x=10, y=10))
        check('重新拖框 = 视窗回到「就是框里」', pk.pan == [0.0, 0.0], str(pk.pan))
        pk._up()
        # ---- 左边那张图：Windows 截图那套（八个手柄 / 挪块 / 拉新框）----
        # 用户 2026-10-12：「先在左侧原画面创建一个框，就像 Windows 的截图一样可以拖动框
        # 放大缩小移动」。量三件事：_hit 把点击分派到哪儿、八个手柄认不认、拖完框是不是
        # **正好**走那 1 个画面像素（和微调按钮同一条路：boxpick.resize_box）。
        pk.set_target('pause')
        app.root.update()
        pb0 = pk.box
        hs = pk._handle_pts(pb0)
        check('八个手柄：四角 + 四边中点，一共八个、都不重样',
              len(hs) == 8 and set(hs) == {'tl', 't', 'tr', 'r', 'br', 'b', 'bl', 'l'},
              str(sorted(hs)))

        def next_col(hx, hy):
            """从 (hx, hy) 往右找一对画布坐标：_to_img 的 y 不变、x 多几个画面像素。

            为什么要「找」而不是直接算：参照图按窗口缩过（本机实测 0.366 倍），1 个**画面**
            像素不到 1 个**画布**像素，「正好 +1」的那一列常常不存在（区间宽 0.37 px、
            里面一个整数都没有）。所以这里返回**第一个能落到的**，断言量的是它多出来的
            那个 dx —— 拖出来的框在画面坐标里是精确的，这一点才是要害。
            """
            ox, oy = pk._to_img(hx, hy)
            for d in range(1, 40):
                ix, iy = pk._to_img(hx + d, hy)
                if iy == oy and ix > ox:
                    return (hx + d, hy, ix - ox)
            return None

        def next_diag(hx, hy):
            """同 next_col，但往右下走：返回 (画布点, dx, dy)（dx / dy 都 > 0）。"""
            ox, oy = pk._to_img(hx, hy)
            for d in range(1, 40):
                for e in range(1, 40):
                    ix, iy = pk._to_img(hx + d, hy + e)
                    if ix > ox and iy > oy:
                        return (hx + d, hy + e, ix - ox, iy - oy)
            return None

        for kind, (hx, hy) in hs.items():
            check('手柄 %s：按在上面就是拉那一条边 / 那个角' % kind,
                  pk._hit(hx, hy) == ('handle', kind), str(pk._hit(hx, hy)))
        check('按在框中间 = 挪块（不是拉边、也不是拉新框）',
              pk._hit((hs['tl'][0] + hs['br'][0]) // 2,
                      (hs['tl'][1] + hs['br'][1]) // 2) == ('inside', None))
        check('按在框外面 = 拉一个新框（橡皮筋）',
              pk._hit(5, 5) == ('new', None), str(pk._hit(5, 5)))
        # 费用条那个框在画布上只有 9px 高：上下两条边上的**中点**手柄挤在角手柄上，不
        # 拿出来（画出来却抓不住 = 用户拖了半天框不动）。四角照旧 —— 画的和认的是同一份。
        pk.set_target('bar')
        barlive = pk._live_handles(pk.box)
        check('费用条的框太薄：上下中点手柄不画也不认，四角照旧（画的 = 认的）',
              set(barlive) == {'tl', 'tr', 'l', 'r', 'bl', 'br'}
              and len(pk._handle_pts(pk.box)) == 8, str(sorted(barlive)))
        pk.set_target('pause')
        # 拉右边那个手柄：往右拖 -> 只有右边动（动的正好是手拖出来的那几个画面像素）
        step = next_col(hs['r'][0], hs['r'][1])
        check('手柄那一下找得到「往右多几个画面像素」的画布点', step is not None, str(step))
        pk._down(_types.SimpleNamespace(x=hs['r'][0], y=hs['r'][1]))
        check('按在右边手柄上：认得是拉右边那条边（不是挪块）',
              pk._grab[0] == 'handle' and pk._grab[1] == 'r', str(pk._grab[:2]))
        pk._drag(_types.SimpleNamespace(x=step[0], y=step[1]))
        pk._up()
        check('拖右边那个手柄：只有右端往外走那几个画面像素（左端 / 上下都不动）',
              pk.box == Rect(pb0.x, pb0.y, pb0.w + step[2], pb0.h), '%s -> %s' % (pb0, pk.box))
        spb = pk.boxes['speed']
        check('按钮那一对联动：拉大暂停键，倍速键**一样大**跟着长',
              (spb.w, spb.h) == (pk.box.w, pk.box.h), '%s / %s' % (pk.box, spb))
        # 按在框里面拖 = 整块挪（框的宽高不变）
        pb1 = pk.box
        mid = ((hs['tl'][0] + hs['br'][0]) // 2, (hs['tl'][1] + hs['br'][1]) // 2)
        diag = next_diag(mid[0], mid[1])
        check('框中间那一下找得到「往右下多几个画面像素」的画布点',
              diag is not None, str(diag))
        pk._down(_types.SimpleNamespace(x=mid[0], y=mid[1]))
        pk._drag(_types.SimpleNamespace(x=diag[0], y=diag[1]))
        pk._up()
        check('按在框里面拖 = 整块挪（dx / dy 就是手拖出来的那几个画面像素，宽高不变）',
              pk.box == Rect(pb1.x + diag[2], pb1.y + diag[3], pb1.w, pb1.h),
              '%s -> %s' % (pb1, pk.box))
        # 框外面拖 = 橡皮筋拉一个新框
        ix0, iy0 = pk._to_img(5, 5)
        ix1, iy1 = pk._to_img(hs['br'][0], hs['br'][1])
        bx0, bx1 = sorted((ix0, ix1))
        by0, by1 = sorted((iy0, iy1))
        want = Rect(bx0, by0, max(2, bx1 - bx0), max(2, by1 - by0))
        pk.pan = [12.0, 7.0]                # 拉新框那一下要顺手把视窗 / 缩放复位
        pk._down(_types.SimpleNamespace(x=5, y=5))
        check('框外面按下去就先做一件事：视窗 / 缩放复位（重新对准）',
              pk.pan == [0.0, 0.0] and pk.zoom == 1.0, '%s %s' % (pk.pan, pk.zoom))
        pk._drag(_types.SimpleNamespace(x=hs['br'][0], y=hs['br'][1]))
        pk._up()
        check('橡皮筋拉完：框 = 从按下那点到松开那点（画面坐标）',
              pk.box == want, '%s（期望 %s）' % (pk.box, want))
    finally:
        pk.close()
        app.root.update()


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

def t_picker_zoom(app):
    """16：取景器右边那块的**局部放大**（滚轮），以及「吸附左端」删了。

    用户 2026-10-11 第 46 轮：「不要搞吸附功能了，实测下来一次没用过」、
    「做局部放大！类似 ctrl＋滚轮 的还是框选放大都行」。
    """
    import numpy as np
    from akframe import ui as ui_mod
    from akframe.geometry import layout_for, Rect

    guide = layout_for(2560, 1600)
    frame = (np.tile(np.arange(2560, dtype=np.uint16), (1600, 1)) % 190).astype(np.uint8)
    pk = ui_mod.BoxPicker(app.root, lambda i: (frame, guide), 3, 1, lambda _b: None)
    try:
        app.root.update()
        pk.set_target('bar')
        app.root.update()
        # 2026-10-12：那一排（含「吸附左端」）整排删了 —— 微调改成直接拖框 / 拖边。
        check('「吸附左端」那排微调控件整排删掉了（吸附逻辑一起走；像素微调现在长在'
              '放大图下面 —— row_* 那几个名字不再是谁）',
              not hasattr(pk, 'row_bar') and not hasattr(pk, 'row_btn')
              and not hasattr(pk, '_snap')
              and not hasattr(ui_mod.BoxPicker, '_nudge_pair')
              and hasattr(ui_mod.BoxPicker, 'nudge_edge')
              and hasattr(ui_mod.BoxPicker, 'nudge_move'), '')
        # 拖完不再自动吸附：拖出来的框就是拖出来的那个
        pk._down(types.SimpleNamespace(x=100, y=100))
        pk._drag(types.SimpleNamespace(x=300, y=140))
        pk._up(types.SimpleNamespace(x=300, y=140))
        x0, y0 = int(100 / pk.scale), int(100 / pk.scale)
        x1, y1 = int(300 / pk.scale), int(140 / pk.scale)
        check('拖完不再自动吸附：框就是拖出来的那个',
              pk.boxes['bar'] == Rect(x0, y0, max(2, x1 - x0), max(2, y1 - y0)),
              '%s / %s' % (pk.boxes['bar'], Rect(x0, y0, x1 - x0, y1 - y0)))
        # 滚轮向上 = 局部放大
        w0, h0 = pk._photo['zoom'].width(), pk._photo['zoom'].height()
        pk._wheel(types.SimpleNamespace(delta=120, x=200, y=80))
        check('滚轮一格 = 放大 1.25 倍', abs(pk.zoom - 1.25) < 1e-9, str(pk.zoom))
        check('放大之后那张图确实变大了（视窗小了一块、画得更大）',
              (pk._photo['zoom'].width(), pk._photo['zoom'].height()) != (w0, h0),
              '%dx%d -> %dx%d' % (w0, h0, pk._photo['zoom'].width(),
                                  pk._photo['zoom'].height()))

        def frame_at(x, y, clamp=True):
            """放大图上 (x, y) 那一列 / 行对应的画面点（clamp = 和 _zoom_at 同一口径：
            光标在图上空处时先夹进图里）。"""
            r = pk.box
            vx, vy, vw, vh, kx, ky = ui_mod.boxpick.zoom_view(
                r, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom, pk._frame_size(),
                pk._zwin_base())
            if clamp:
                x = min(max(x, 0), max(0, int(vw * kx) - 1))
                y = min(max(y, 0), max(0, int(vh * ky) - 1))
            return vx + x / float(kx), vy + y / float(ky)

        pk.zoom, pk.pan = 1.0, [0.0, 0.0]
        pk.redraw()
        bx, by = frame_at(200, 80)
        pk._wheel(types.SimpleNamespace(delta=120, x=200, y=80))
        pk._wheel(types.SimpleNamespace(delta=120, x=200, y=80))
        ax, ay = frame_at(200, 80)
        check('放大盯着鼠标那一点：光标底下的画面点前后几乎不动',
              abs(ax - bx) <= 1.0 and abs(ay - by) <= 1.0,
              '(%s, %s) -> (%s, %s) zoom=%.3f' % (bx, by, ax, ay, pk.zoom))
        for _ in range(30):
            pk._wheel(types.SimpleNamespace(delta=-120, x=200, y=80))
        check('一直往小了滚 = 停在缩小下限（面板装得下 = 1:1），不会再小',
              abs(pk.zoom - pk._zoom_lo()) < 1e-9 and pk.zoom <= 1.0,
              'zoom=%s 下限=%s' % (pk.zoom, pk._zoom_lo()))
        for _ in range(30):
            pk._wheel(types.SimpleNamespace(delta=120, x=200, y=80))
        check('一直往大了滚 = 停在 ZOOM_MAX',
              pk.zoom == ui_mod.BoxPicker.ZOOM_MAX, 'zoom=%s' % pk.zoom)
        pk._zfit()
        check('双击复位：倍数和平移一起回到「就是框里」',
              pk.zoom == 1.0 and pk.pan == [0.0, 0.0], '%s %s' % (pk.zoom, pk.pan))
        # 拖动的除数要按**当前**倍数算
        pk._zoom_at(2.0, 200, 80)
        kx2 = ui_mod.boxpick.zoom_scale(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.zoom)[0]
        before = list(pk.pan)
        pk._zdown(types.SimpleNamespace(x=100, y=60))
        pk._zdrag(types.SimpleNamespace(x=100 + 2 * kx2, y=60))
        pk._zup()
        check('放大之后拖动：位移仍按当前倍数折算（拖 2*kx 像素 = pan 减 2）',
              abs(pk.pan[0] - (before[0] - 2)) < 1e-6, '%s -> %s' % (before, pk.pan))
        pk._zoom_at(2.0, 200, 80)
        pk.set_target('pause')
        check('换目标把缩放也复位（别带着上一个目标的倍数）',
              pk.zoom == 1.0, str(pk.zoom))
        pk.set_target('bar')
        pk._zoom_at(2.0, 200, 80)
        # 在**框外面**按下去 = 重新拉一个框 -> 视窗 / 缩放复位（框变了，视窗得跟上）。
        # 按在框里面 / 手柄上不会复位 —— 那是在调同一个框，放大倍数得留着。
        pk._down(types.SimpleNamespace(x=5, y=5))
        check('重新拉框 = 缩放回 1（框变了，视窗得跟上）',
              pk.zoom == 1.0, str(pk.zoom))
        pk._up(types.SimpleNamespace(x=5, y=5))
        # ---- 2026-10-13 第七轮（用户：「放大图也能用 ctrl 滚轮缩放，放大鼠标单击的地方，
        # 不要跟随框会很乱」）----
        check('ctrl+滚轮 也接在放大图上（和滚轮同一条路）',
              bool(pk.cv_zoom.bind('<Control-MouseWheel>')), '')
        # (a) 基准窗口：改框不改倍数
        pk.set_target('bar')
        pk.boxes['bar'] = Rect(2300, 1229, 239, 25)      # 真录像里费用条的几何
        pk.pan, pk.zoom, pk._zbase = [0.0, 0.0], 1.0, None
        pk.redraw()
        base0 = list(pk._zwin_base())
        check('基准 = 打开取景器那一刻的框', base0 == [239.0, 25.0], str(base0))
        k0 = ui_mod.boxpick.zoom_scale(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.zoom,
                                       pk._zwin_base())
        pk.nudge_edge('r', 6)                            # 改框：框本身 +6px
        pk.nudge_edge('b', 3)
        k1 = ui_mod.boxpick.zoom_scale(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.zoom,
                                       pk._zwin_base())
        check('改框不改倍数（「不要跟随框」）：倍数只认基准，框变了它不动',
              k0 == k1 and [pk.box.w, pk.box.h] != base0,
              'k %s -> %s，框 %s / 基准 %s' % (k0, k1, (pk.box.w, pk.box.h), base0))
        pk.boxes['bar'] = Rect(2300, 1229, 239, 25)
        pk.pan, pk.zoom, pk._zbase = [0.0, 0.0], 1.0, None
        pk.redraw()
        pk._zbase = [999.0, 999.0]                       # 故意搞坏，看复位有没有重取
        pk._zfit()
        check('双击复位 = 缩放 / 平移归零，基准跟着当前的框重取',
              pk.zoom == 1.0 and pk.pan == [0.0, 0.0]
              and pk._zbase == [float(pk.box.w), float(pk.box.h)],
              'zoom=%s pan=%s base=%s 框=%s' % (pk.zoom, pk.pan, pk._zbase, pk.box))
        # (b) 缩到下限：视窗 = 面板装得下（1 画面像素 = 1 屏像素）
        pk.set_target('bar')
        pk.boxes['bar'] = Rect(2300, 1229, 239, 25)
        pk.pan, pk.zoom, pk._zbase = [0.0, 0.0], 1.0, None
        pk.redraw()
        lo = pk._zoom_lo()
        for _ in range(40):
            pk._wheel(types.SimpleNamespace(delta=-120, x=200, y=80))
        vw, vh, kx, ky = ui_mod.boxpick.zoom_view(
            pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom, pk._frame_size(),
            pk._zwin_base())[2:]
        check('往下滚能缩过「整框」，停在面板装得下（视窗 = 面板宽、k = 1）',
              abs(pk.zoom - lo) < 1e-9 and vw == ui_mod.BoxPicker.ZOOM_PANEL[0] and kx == 1,
              'zoom=%.3f 下限=%.3f 视窗=%dx%d k=%dx%d' % (pk.zoom, lo, vw, vh, kx, ky))
        check('下限本身 ≤ 1（1:1 就是最远的视野，再远要缩像素，不做）', lo <= 1.0, str(lo))
        # (c) 光标落在放大图**外面**（右边 / 下面空着的那截）滚轮：锚点夹进图里，
        #     不能像以前那样把视窗整个拽到框的左上角去。
        pk._zfit()
        pk.redraw()
        iw, ih = pk._photo['zoom'].width(), pk._photo['zoom'].height()
        gx, gy = ui_mod.BoxPicker.ZOOM_PANEL[0] - 4, ui_mod.BoxPicker.ZOOM_PANEL[1] - 4
        check('这一格光标确实在图外（画布比图大，右边 / 下面空着一截）',
              gx >= iw and gy >= ih, '图=%dx%d 画布=%s' % (iw, ih, ui_mod.BoxPicker.ZOOM_PANEL))
        cx0, cy0 = min(gx, iw - 1), min(gy, ih - 1)      # 夹进图里 = 锚点
        fx0, fy0 = frame_at(gx, gy)                      # 那个锚点对应的画面点
        pk._wheel(types.SimpleNamespace(delta=120, x=gx, y=gy))
        vx, vy, vw, vh, kx, ky = ui_mod.boxpick.zoom_view(
            pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom, pk._frame_size(),
            pk._zwin_base())
        col = (fx0 - vx) * kx                            # 同一个画面点现在落在哪一列 / 行
        row = (fy0 - vy) * ky
        want_c, want_r = min(cx0, int(vw * kx) - 1), min(cy0, int(vh * ky) - 1)
        # 容差 = 半个画面像素（视窗要落在整数画面行列上，见 zoom_view 的四舍五入）：
        # 1 个画面像素 = k 列，所以落点天生能差到 k/2 列。
        check('在图外的空处滚轮：锚点还是「离光标最近的那个画面点」（不跟着框跑）',
              abs(col - want_c) <= kx / 2.0 + 1.0 and abs(row - want_r) <= ky / 2.0 + 1.0,
              '列 %.2f / 行 %.2f（想要 %d / %d，k=%dx%d）' % (col, row, want_c, want_r, kx, ky))
        check('在图外的空处滚轮：滚完那一点还在视窗里（不会滚着滚着把目标丢了）',
              vx <= fx0 < vx + vw and vy <= fy0 < vy + vh,
              '视窗=(%d,%d,%d,%d) 锚点=(%.1f,%.1f)' % (vx, vy, vw, vh, fx0, fy0))
    finally:
        pk.close()
        app.root.update()



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
                print('3. 一个键盘快捷键都不收（游戏里按键盘不会打到面板上）')
                t_no_keys(app)
            finally:
                if alive(app):
                    kill_afters(app)
                    try:
                        app.root.destroy()
                    except Exception:          # noqa: BLE001
                        pass
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
                print('15. 取景器右边的放大图能按住拖动')
                t_picker_pan(app2)
                print('16. 放大图能滚轮局部放大 / 「吸附左端」删了')
                t_picker_zoom(app2)
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
