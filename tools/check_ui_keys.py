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
       px_per_frame 得对得上）；翻条那两颗键（◀ 回看 / 现在 ▶）2026-10-13 按用户要求
       **删干净**（连 _prom_scroll / _prom_shift / build(now=) / 那条灰线一起）；
    6. 「CSV」按钮、「刻度缩放」四个键、「局内负费」下拉**都删掉了**（用户看板批注）；
    7. 诊断那一层删掉了（2026-10-11）：按钮 / 折叠方法没了，fr_diag 一直不 pack；
    8. （作废）刻度表的「回看」灰线 —— 2026-10-13 跟着翻条键一起删了，组也删了；
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
        （未开始 / 正在计数 / 暂停计数 / 正在校准），动作反馈说完自己让位。
    2026-10-16：这四个词**收敛成只剩这四个**（用户：「不需要这么多，只留下未开始，
    正在计数，暂停计数，正在校准」）—— 「采样已停」不再写底下一行（那件事归告警
    行）、「已暂停」改名「暂停计数」。
    2026-10-17：词表**钉死成七个**（用户：「只留下未开始，正在计数，暂停计数，正在
    校准，校准完成请开始，应用完成，导入失败请重试这几种状态，其他状态全删掉」）——
    四十来句动作反馈（已清零 / 已保存校准 / 已切换到 X / 回费速率 = … / 已导入 N 条轴 …）
    全删，_set_note 从此**卡表**（不在表里的字一个都不写）。
    2026-10-18：用户把报备过的六条例外也删了 —— _set_alert / _note_pin 两条通道整个
    拆掉，出错只印控制台；这一组把 ui.py 扫一遍，确认词表外的写字通道**不存在**。
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

    17. 第八轮（53）：**二级窗口统一外壳** —— 四个二级窗口都 overrideredirect（系统标题栏
        拿掉）+ 自画一条 header（3px 色条 / 标题 / ? / ×）；标题只写一遍（原来手动校准写
        两遍）；按钮分档（主 / 次 / 危险 / 幽灵）；轴窗平时不把「pip install tkinterdnd2」
        印给用户；读取窗口不再是「一行 5 个字段」的 Listbox（两行一条 + 刷新）；二级窗口
        一律夹在工作区里（本机工作区到 y=1528，不然最下面那排键压在任务栏底下）。
    18. 第八轮（53）：取景器顶部从 12 行 / 681 字的总说明换成「一行提示（跟着目标走）+
        ? 展开全文」，界面上不再漏 markdown 星号；左图从屏幕 0.55 倍收到 0.40 倍。
    22. 2026-10-17（用户：「把所有面板的直角换成经典的圆角」）：主面板 + 四个二级窗口
        都换成 Win11 的 DWM 圆角（DWMWCP_ROUND），不用 SetWindowRgn（那是按区域裁剪，
        边缘不抗锯齿：实测过渡像素 0 vs 86）。这一组钉住「系统真的记下了」（读回来）、
        「二级窗口是映射之后才生效的」（建窗那一拍设会被系统丢掉）、「重新映射还在」、
        「Win10 上安静退回直角」。

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

    # <Map> 是窗口级事件（圆角那个钩子挂在它上面），跟 <Motion> 一样收不到按键
    seqs = [s for s in app.root.bind() if s not in ('<Motion>', '<Map>')]
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
    # 「◀ 回看 / 现在 ▶」2026-10-13 按用户要求删了（「回看现在是什么加的功能，删掉」）：
    # 那颗键 + 它背后的偏移 + 刻度表那条灰线是一条链，这里钉住整条链都不在。
    fr = getattr(app, 'fr_more_top', None)
    btns = _btn_texts(fr) if fr is not None else []
    check('打轴器标题栏只剩「重载 / 导入轴…」两个键',
          '重载' in btns and '导入轴…' in btns
          and '◀ 回看' not in btns and '现在 ▶' not in btns, str(btns))
    check('翻条那一套名字全没了（_prom_scroll / _prom_shift）',
          not hasattr(app, '_prom_scroll') and not hasattr(app, '_prom_shift'))
    app._set_axis_text('300 部署 银灰\n330 技能 银灰')
    app.apply_axis_text()
    check('轴照旧能进提词器（删的只是翻条）',
          app.prompter.timeline is not None and len(app.prompter.timeline.cues) == 2)


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
    """5：刻度固定 150 帧（没有缩放档了）；翻条那两颗键 2026-10-13 删了。"""
    from akframe import ui as ui_mod
    from akframe.scale import Layout

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

    app._set_axis_text('300 部署 银灰\n330 技能 银灰\n360 撤退')
    app.apply_axis_text()
    check('轴进到提词器',
          app.prompter.timeline is not None and len(app.prompter.timeline.cues) == 3)
    # 翻条（◀ 回看 / 现在 ▶）2026-10-13 删了：刻度表摆的那一帧永远是「现在」。
    check('翻条那套连名字都不在（方法 / 状态 / 灰线参数）',
          not hasattr(app, '_prom_scroll') and not hasattr(app, '_prom_shift')
          and 'now' not in Layout.__dataclass_fields__)
    import inspect
    from akframe.scale import build as _scale_build
    check('build 不再收 now（刻度摆的那一帧就是「现在」）',
          'now' not in inspect.signature(_scale_build).parameters,
          str(list(inspect.signature(_scale_build).parameters)))


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
          app.fr_nums in order2 and app.fr_ctrl in order2 and app.fr_note in order2)

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
        check('窗口没了：放开置顶，底部那一行不写消息（只许七个状态词）',
              app._pinned_hwnd is None
              and app.lb_note.cget('text') in ui_mod.NOTE_WORDS,
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
    """12：模式名 -> PC端 / 窗口；PC端也手动校准；底部那一行**只有七个词**。

    用户 2026-10-11 第五轮：「把模式切换文本改成 pc端和窗口，pc端也加入手动校准功能，
    下方的状态描述用一行简短的文字，比如正在校准。。。；正在计数。。。」

    2026-10-17 用户把这一行的词表钉死成七个（未开始 / 正在计数 / 暂停计数 / 正在校准 /
    校准完成请开始 / 应用完成 / 导入失败请重试），其余**全删**。
    2026-10-18 连报备过的六条例外也删了（_set_alert / _note_pin 整个拆掉，出错只印
    控制台）。所以这一组除了四个状态各读一次，还要：① 词表外的字**写不进去**（运行时
    卡表）；② 把 ui.py 里写底部那一行的调用点扫一遍 —— 只许有 _set_note，而且字面量
    必须在词表里。
    """
    from akframe import ui as ui_mod
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

    # 四个状态各读一次，顺便把「词表就只有这四个」钉住（用户 2026-10-16）
    seen = []
    app._counting_started = False
    seen.append(status())
    check('没在数 -> 底下写「未开始」', seen[-1] == '未开始', repr(seen[-1]))
    app.monitor.start()
    app._counting_started = True
    seen.append(status())
    check('在数 -> 底下写「正在计数」', seen[-1] == '正在计数', repr(seen[-1]))
    app.toggle_manual_pause()
    seen.append(status())
    check('暂停 -> 底下写「暂停计数」', seen[-1] == '暂停计数', repr(seen[-1]))
    app.toggle_manual_pause()
    app._calib = True
    seen.append(status())
    check('在微调 ROI -> 底下写「正在校准」', seen[-1] == '正在校准', repr(seen[-1]))
    app._calib = False
    check('底部状态就只有这四个词', set(seen) == {'未开始', '正在计数', '暂停计数', '正在校准'},
          seen)

    # 词表外的字**一个都写不进去**（用户 2026-10-17：「其他状态全删掉」）
    was = app.lb_note.cget('text')
    app._set_note('已清零')
    check('词表外的字写不进去（「已清零」这类反馈全删了）',
          app.lb_note.cget('text') == was, repr(app.lb_note.cget('text')))
    app._set_note('随便编一句提示')
    check('现编一句也写不进去（_set_note 卡着 NOTE_WORDS）',
          app.lb_note.cget('text') == was, repr(app.lb_note.cget('text')))

    # 动作反馈说完要让位（不让位的话底下一行又变成甩不掉的一句旧话）
    app._set_note('应用完成')
    check('词表里的反馈顶得上来', app.lb_note.cget('text') == '应用完成',
          repr(app.lb_note.cget('text')))
    status(reset=False)
    check('反馈还没到点：仍然是它（不许被状态抢掉）',
          app.lb_note.cget('text') == '应用完成', repr(app.lb_note.cget('text')))
    status()
    check('反馈到点：自己让位给状态', app.lb_note.cget('text') == '正在计数',
          repr(app.lb_note.cget('text')))

    # 词表这一条**查得住**：把 ui.py 里写死的那些字扫一遍 —— 只许有 _set_note，而且
    # 每条字面量都必须在 NOTE_WORDS 里（删掉的通道不许再长回来）。
    src = io.open(os.path.join(ROOT, 'src', 'akframe', 'ui.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    words, long_bad = set(), []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == '_set_note'
                and node.args):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.BinOp) and isinstance(arg.left, ast.Constant):
            arg = arg.left                 # '读窗口失败：%s' % exc -> 收那个模板串
        try:
            val = ast.literal_eval(arg)
        except Exception:                  # noqa: BLE001  拼接 / f-string，跳过
            continue
        if not isinstance(val, str):
            continue
        if len(val) > 28 or '\n' in val:
            long_bad.append('%d: %r' % (node.lineno, val))
        words.add(val)
    check('ui.py 里写底部那一行的只有 _set_note，字面量全在那七个词里',
          words <= set(ui_mod.NOTE_WORDS), sorted(words - set(ui_mod.NOTE_WORDS)))
    gone = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr in ('_set_alert', '_note_pin')]
    check('词表外的两条通道已经拆掉（没有 _set_alert / _note_pin 了）', not gone, gone)
    check('底部那一行的字面量全长在 28 字以内、没有换行（用户要「一行简短的文字」）',
          not long_bad, long_bad[:3])

    t_switch_clears(app)
    t_rate_switch_clears(app)
    t_picker_cancel(app)


def t_switch_clears(app):
    '''13a：切模式 = 自动清零，而且切过去是**待命**态（41 轮「切换模式自动清零」）。

    为什么必须清零：两个来源的帧号不是一个坐标系（PC端 = 游戏窗口，窗口 = 播放器 /
    录像），接在一起没有意义；不清的话大数字会挂着上一个来源数到的值，看着像「已经
    数了 500 帧」，而这一拍其实什么都没数。
    为什么是待命而不是接着跑：读窗口那条链路本来就是「校准完等你点开始」，切回去两边
    一个规矩 —— 一眼看得出没在数，也不会把「切模式」偷偷变成「开始」。
    '''
    from akframe import ui as ui_mod

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
    check('切模式之后底下那行只有一个词表里的词（不再有「已切到 PC端…已清零」那串）',
          app.lb_note.cget('text') in ui_mod.NOTE_WORDS, repr(app.lb_note.cget('text')))


def t_rate_switch_clears(app):
    '''20：切「回费效率」= 自动清零（用户 2026-10-14：「切换回费效率自动清零的优化」）。

    切档位就是「从现在起按新档数」：旧行为是按比例换算已经数出来的帧（30 -> 60 时
    已数的 30 帧当场变成 60 帧），数字跳一下看着像数错了。现在直接复用 _reset_count：
    停采样 + 归零 + 主按钮三态拉回「开始」，和「清零」那个键一模一样。
    '''
    if not app.monitor.alive:
        app.monitor.start()
    app._counting_started = True
    app._set_text('frames', app.lb_frames, '1234')     # 假装已经数了一千多帧
    app.apply_cost_rate(120.0)
    check('切回费效率自动清零：数字回 0、按钮回「开始」、停采样待命',
          app._counting_started is False and app.monitor.alive is False
          and app.lb_frames.cget('text') == '0'
          and app.btn_start.cget('text') == '开始',
          '%s / %s / %r / %s' % (app._counting_started, app.monitor.alive,
                                 app.lb_frames.cget('text'),
                                 app.btn_start.cget('text')))
    check('切档位刷成新档 + 底下说「应用完成」',
          app.rate_var.get().startswith('120')
          and app.lb_note.cget('text') == '应用完成',
          '%r / %r' % (app.rate_var.get(), app.lb_note.cget('text')))
    app.apply_cost_rate(30.0)          # 复位成默认档，别影响后面几组


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
        # 先把框挪到画面正中再拖：右下角那个框的视窗本来就被画面夹住了（这是对的），
        # 往右拖根本动不了 —— 那测不出「按倍数折算」。
        r = pk.boxes['pause'] = Rect(1000, 600, 199, 159)
        pk.pan = [0.0, 0.0]
        pk.redraw()
        pk._zdown(_types.SimpleNamespace(x=100, y=60))
        pk._zdrag(_types.SimpleNamespace(x=100 + 4 * kx, y=60))
        check('放大图里左键拖动 = 挪视窗：往右拖 4 个画面像素 pan ≈ -4（除以放大倍数）',
              abs(pk.pan[0] + 4.0) <= 0.5 and pk.pan[1] == 0.0, str(pk.pan))
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
        vwx, vhx = [int(round(v)) for v in bp.zoom_window(r, ui_mod.BoxPicker.ZOOM_PANEL,
                                                          pk.zoom)]
        check('拖出画面 -> 视窗夹在画面右下角（一直还在画面里）',
              bp.zoom_view(r, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom,
                           pk._frame_size())[:2] == (2560 - vwx, 1600 - vhx), str(pk.pan))
        pk._zdown(_types.SimpleNamespace(x=0, y=0))
        pk._zdrag(_types.SimpleNamespace(x=10 ** 6, y=10 ** 6))
        pk._zup()
        check('往反方向拖出去 -> 夹在左上角（不会越拖越远，回来时不用空拖）',
              bp.zoom_view(r, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom,
                           pk._frame_size())[:2] == (0, 0), str(pk.pan))
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
        el, er, et, eb = zm.edge_lines(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan,
                                       pk.zoom, pk._frame_size())
        check('费用条目标的放大图：左红 / 右绿压在画布左右两边（条槽宽正好铺满面板）',
              px(0, jy) == zm.EDGE_LEFT and px(iz - 1, jy) == zm.EDGE_RIGHT
              and el == 0 and er >= iz - 2,
              '%s %s el=%d er=%d iz=%d' % (px(0, jy), px(iz - 1, jy), el, er, iz))
        check('费用条目标的放大图：上下两条品红画在框的顶边 / 底边上（不在画布边上）',
              px(iz // 2, et) == zm.EDGE_TOP and px(iz // 2, eb) == zm.EDGE_BOTTOM
              and 0 < et < eb < jz - 1,
              '%s %s et=%d eb=%d jz=%d' % (px(iz // 2, et), px(iz // 2, eb), et, eb, jz))
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
        pk.redraw()
        vx0, vy0 = zm.zoom_view(rw, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom,
                                pk._frame_size())[:2]
        pk.nudge_move(-2, 3)
        check('整框 ← ↑：整块平移（宽高一点不变）',
              pk.box == Rect(rw.x - 2, rw.y + 3, rw.w, rw.h), '%s -> %s' % (rw, pk.box))
        vx1, vy1 = zm.zoom_view(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom,
                                pk._frame_size())[:2]
        check('微调挪块：视窗跟着框**同方向**走同样多（pan 不再反向补 —— 第 55 轮）',
              pk.pan == [0.0, 0.0] and (vx1 - vx0, vy1 - vy0) == (-2, 3),
              'pan=%s 视窗 (%d,%d) -> (%d,%d)' % (pk.pan, vx0, vy0, vx1, vy1))
        kb, _ = zm.zoom_scale(pk.box, ui_mod.BoxPicker.ZOOM_PANEL)
        pk.pan = [9.0, 5.0]
        pk.redraw()
        # 平移（右移 9px）之后：绿线不再贴在画布最右列，而是跟着框挪到 (w-9)*kx-1 列。
        iz2, jz2 = zsize()
        jy2 = jz2 * 3 // 4
        er3 = zm.edge_lines(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom,
                            pk._frame_size())[1]
        check('平移后四条边跟着框走（右边从画布最右列挪到第 %d 列）' % er3,
              px(er3, jy2) == zm.EDGE_RIGHT and px(iz2 - 1, jy2) != zm.EDGE_RIGHT
              and er3 < iz2 - 1,
              'er=%d 最右列 %s kb=%.3f' % (er3, px(iz2 - 1, jy2), kb))
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
        el0, er0 = ui_mod.boxpick.edge_lines(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan,
                                             1.0, pk._frame_size())[:2]
        pk._wheel(types.SimpleNamespace(delta=120, x=200, y=80))
        check('滚轮一格 = 放大 1.25 倍', abs(pk.zoom - 1.25) < 1e-9, str(pk.zoom))
        el1, er1 = ui_mod.boxpick.edge_lines(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan,
                                             pk.zoom, pk._frame_size())[:2]
        # 放大图现在**永远铺满面板**（第 55 轮同一句：「固定 4:3 的比例」），所以「变大了」
        # 不能再看图尺寸 —— 看图里那一小块占了多少列（同一个框，倍数大了就更宽）。
        check('放大之后框在图里确实变大了（视窗小了一块、画得更大）',
              er1 - el1 > er0 - el0,
              '框宽 %d -> %d 列（图 %dx%d -> %dx%d）'
              % (er0 - el0, er1 - el1, w0, h0,
                 pk._photo['zoom'].width(), pk._photo['zoom'].height()))

        def frame_at(x, y, clamp=True):
            """放大图上 (x, y) 那一列 / 行对应的画面点（clamp = 和 _zoom_at 同一口径：
            光标在图上空处时先夹进图里）。"""
            r = pk.box
            vx, vy, vw, vh, kx, ky = ui_mod.boxpick.zoom_view(
                r, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom, pk._frame_size())
            if clamp:
                x = min(max(x, 0), max(0, int(round(vw * kx)) - 1))
                y = min(max(y, 0), max(0, int(round(vh * ky)) - 1))
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
        check('一直往小了滚 = 停在缩小下限（缩到底 = 整幅画面），不会再小',
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
        check('放大之后拖动：位移仍按当前倍数折算（拖 2*kx 像素 ≈ pan 减 2）',
              abs(pk.pan[0] - (before[0] - 2)) <= 0.6, '%s -> %s' % (before, pk.pan))
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
        # (a) 改框：视窗 / 倍数跟着框走（第 55 轮的口径；上一轮是「冻在旧框上」）
        pk.set_target('bar')
        pk.boxes['bar'] = Rect(2300, 1229, 239, 25)      # 真录像里费用条的几何
        pk.pan, pk.zoom = [0.0, 0.0], 1.0
        pk.redraw()
        k0 = ui_mod.boxpick.zoom_scale(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.zoom)[0]
        pk.nudge_edge('r', 6)                            # 改框：框本身 +6px
        pk.nudge_edge('b', 3)
        k1 = ui_mod.boxpick.zoom_scale(pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.zoom)[0]
        check('改框 -> 倍数跟着框走（视窗的形状跟着框，不再冻在打开取景器那一刻的框上）',
              k1 < k0 and (pk.box.w, pk.box.h) == (245, 28),
              'k %.4f -> %.4f，框 %s' % (k0, k1, (pk.box.w, pk.box.h)))
        pk.boxes['bar'] = Rect(2300, 1229, 239, 25)
        pk.pan, pk.zoom = [0.0, 0.0], 1.0
        pk.redraw()
        pk._zfit()
        check('双击复位 = 缩放 / 平移一起归零',
              pk.zoom == 1.0 and pk.pan == [0.0, 0.0],
              'zoom=%s pan=%s 框=%s' % (pk.zoom, pk.pan, pk.box))
        # (b) 缩到下限：视窗 = 面板装得下（1 画面像素 = 1 屏像素）
        pk.set_target('bar')
        pk.boxes['bar'] = Rect(2300, 1229, 239, 25)
        pk.pan, pk.zoom = [0.0, 0.0], 1.0
        pk.redraw()
        lo = pk._zoom_lo()
        for _ in range(40):
            pk._wheel(types.SimpleNamespace(delta=-120, x=200, y=80))
        vw, vh, kx, ky = ui_mod.boxpick.zoom_view(
            pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom,
            pk._frame_size())[2:]
        check('往下滚能缩过「整框」，停在缩小下限（视窗 = 整幅画面那么宽）',
              abs(pk.zoom - lo) < 1e-9 and abs(vw - 2560) <= 1 and kx < 1,
              'zoom=%.4f 下限=%.4f 视窗=%dx%d k=%.4f' % (pk.zoom, lo, vw, vh, kx))
        check('下限本身 ≤ 1（「看整个框」这一档永远给得出来）', lo <= 1.0, str(lo))
        # (c) 光标落在放大图**外面**（右边 / 下面空着的那截）滚轮：锚点夹进图里，
        #     不能像以前那样把视窗整个拽到框的左上角去。
        pk._zfit()
        pk.redraw()
        iw, ih = pk._photo['zoom'].width(), pk._photo['zoom'].height()
        # 放大图铺满面板之后，图外只剩画布那 4px 边（560x190 vs 556x186）—— 拿这个角量。
        gx, gy = ui_mod.BoxPicker.ZOOM_PANEL[0] + 2, ui_mod.BoxPicker.ZOOM_PANEL[1] + 2
        check('这一格光标确实在图外（画布比图大，右边 / 下面空着一截）',
              gx >= iw and gy >= ih, '图=%dx%d 画布=%s' % (iw, ih, ui_mod.BoxPicker.ZOOM_PANEL))
        cx0, cy0 = min(gx, iw - 1), min(gy, ih - 1)      # 夹进图里 = 锚点
        fx0, fy0 = frame_at(gx, gy)                      # 那个锚点对应的画面点
        pk._wheel(types.SimpleNamespace(delta=120, x=gx, y=gy))
        vx, vy, vw, vh, kx, ky = ui_mod.boxpick.zoom_view(
            pk.box, ui_mod.BoxPicker.ZOOM_PANEL, pk.pan, pk.zoom, pk._frame_size())
        col = (fx0 - vx) * kx                            # 同一个画面点现在落在哪一列 / 行
        row = (fy0 - vy) * ky
        want_c = min(cx0, int(round(vw * kx)) - 1)
        want_r = min(cy0, int(round(vh * ky)) - 1)
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



def _walk(widget, cls):
    """递归找某一类控件（要控件本身，不只是文字）。"""
    out = []
    for c in widget.winfo_children():
        if isinstance(c, cls):
            out.append(c)
        out.extend(_walk(c, cls))
    return out


def _hdr_btn(top, prefix):
    """二级窗口 header 上那个 ? / ×（文字以 prefix 开头）。"""
    import tkinter as tk

    for b in _walk(top, tk.Button):
        if str(b.cget('text')).startswith(prefix):
            return b
    return None


def t_panel_titlebar_fonts(app):
    """20：去掉系统标题栏 / 打轴器字号放大到「时间」那一号 / 展开收起不闪（2026-10-16）。

    三条用户原话：
        「面板上方的『明日方舟 全局帧数计数器』那行白色的能不能删掉」
        「打轴器上的字体太小，操作字体和帧号放大到主面板的『时间大小』」
        「展开收起面板时 ui 会闪动一下」
    """
    from PIL import Image, ImageDraw, ImageFont

    from akframe import scale_render, win32

    root = app.root
    root.deiconify()
    root.update()

    check('主面板也没有系统标题栏了（那条白字就是它）',
          bool(root.overrideredirect()))

    # 无边框窗口 Tk 的 iconify() 会直接拒绝（TclError: override-redirect flag is set），
    # 「−」必须改走 Win32 的 SW_MINIMIZE —— 这一条按源码钉，不真去最小化面板
    # （最小化之后自检进程自己还得把它弄回来，反而更容易把后面几组弄乱）。
    src = io.open(os.path.join(ROOT, 'src', 'akframe', 'ui.py'), encoding='utf-8').read()
    fn = [n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == '_minimize']
    calls = set()
    for node in (ast.walk(fn[0]) if fn else []):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
    check('「−」改走 Win32 的 minimize_window（Tk 的 iconify 对无边框窗口会报错）',
          calls == {'minimize_window'}, calls)
    check('无边框面板在任务栏里有按钮（不然最小化就点不回来）',
          win32.add_taskbar_button(root) is True)

    # 展开 / 收起：**真要现量**的那一小段窗口应该是全透明的 —— 否则 Tk 会先按旧尺寸
    # 把新布局画一拍。缓存命中的路（平时那条）根本不进这里，所以把缓存清掉，逼出一条
    # 真·现量路径来验。2026-10-16 之前这里靠「换过提示文字」来制造没命中，提示行不进
    # 指纹之后就造不出来了 —— 换成清缓存，验的还是同一件事（_relayout 的契约）。
    seen = []
    real_attr = root.attributes

    def spy(*a, **kw):
        if a and a[0] == '-alpha':
            seen.append(a[1])
        return real_attr(*a, **kw)

    app._fit_cache.clear()
    root.attributes = spy
    try:
        app.toggle_more()
        root.update_idletasks()
        app.toggle_more()
        root.update_idletasks()
    finally:
        root.attributes = real_attr
    check('真要现量时那一拍窗口是全透明的（画在看不见的窗口上）',
          seen.count(0.0) == 2 and seen[-1] == 1.0, seen)
    check('换完布局透明度一定回到 1（否则面板一直是半透明的）',
          float(root.attributes('-alpha')) == 1.0)

    # 打轴器字号：26 = 150% 缩放下 Tk 13pt 的「时间」（一个汉字 26px）。
    sizes = scale_render.font_sizes()
    check('刻度表的字放大到主面板「时间」那一号（26px）',
          sizes == (26, 26, 26), sizes)
    g = app.scale_geom
    dr = ImageDraw.Draw(Image.new('RGB', (8, 8)))
    num_w = dr.textlength('8888', font=ImageFont.truetype(scale_render.FONT_NUM, sizes[0]))
    check('刻度条装得下最宽的四位帧号、还留得下刻度线',
          num_w + 12 <= g.ruler_w, '四位帧号 %.0fpx -> 刻度条 %dpx' % (num_w, g.ruler_w))
    check('行高 / 行距够放这一号字（上下两行叠不到一起）',
          g.label_gap >= g.label_h >= sizes[0], '%s / %s / %s'
          % (g.label_h, g.label_gap, sizes[0]))


def t_toggle_flicker(app):
    """21：展开 / 收起那一拍的闪烁（2026-10-16 第二轮：「现在还是会闪」）。

    连拍 300 帧/秒量出来的账（探针见 out/p74_* 那批图）：
      - 老做法每次换布局都要**现量**尺寸，而量只能在 update_idletasks 里量 —— 那一拍
        会按**旧**尺寸把新布局画一遍（展开时 = 打轴器已经插好、窗口还是收起的，大数字
        被挤出窗口）。整段异常实测 26~47ms，等于 2~3 个屏幕刷新；
      - 之后尺寸变化还自带一次整窗擦除 + 重画，而且系统擦的是窗口类的刷子 ——
        Tk 那把在建窗时就定成黑色了（实测擦完那一帧是 #000000，面板底色是 #151517），
        于是中间这一帧是一整块**纯黑**。

    修法三条：尺寸按内容指纹缓存（命中就一次测量都不做、中间那一拍不存在）；
    类擦除刷子换成面板底色（那一帧从「整块纯黑」降成「底色在、字晚一帧」）；
    提示行的文字从指纹里去掉（它是定高行 —— 不去掉的话每换一句提示，下一次展开就
    缓存没命中，而没命中那条路要 _relayout，用户看到的是面板整块消失一瞬）。

    高帧率连拍（300 帧/秒，探针 probe24）实测这一次的账：
      - 上一轮那版（每次换布局 _relayout 全透明 + 现量）：面板**整个消失 20ms**，
        接着还有 27ms 的半成品，一共约 47ms = 3 个屏幕刷新；
      - 现在这一版：整段只剩 1~2 帧「和两态都差一点点」的中间帧（差 2.7~2.9 /
        量程 0~26），面板不消失、也不出现纯黑。
    剩下那一帧是改窗口尺寸的固有代价（Tk 的擦除 + 重画不是一次成型的），要彻底消掉
    得让面板在展开 / 收起时**根本不改尺寸**（另一套架构），这里不碰。
    """
    from akframe import ui as ui_mod, win32

    root = app.root
    root.deiconify()
    root.update()

    keys = list(app._fit_cache)
    check('开机就把展开 / 收起两态尺寸都量好了（第一次点展开不用现量）',
          {k[0] for k in keys} == {True, False} and len(keys) >= 2, keys)

    app.toggle_more()
    root.update_idletasks()
    app.toggle_more()
    root.update_idletasks()
    calls = []
    real = root.update_idletasks

    def spy(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    root.update_idletasks = spy
    try:
        app.toggle_more()
    finally:
        root.update_idletasks = real
    check('命中缓存时展开 / 收起一次都不测量（没有「按旧尺寸画新布局」那一拍）',
          calls == [], len(calls))

    k0 = app._size_key()
    note0, warn0 = app.lb_note.cget('text'), app.lb_warn.cget('text')
    try:
        # 提示行是 NOTE_H(22px) 的定高 Frame + place，写多少个字都不占尺寸。把它算进
        # 指纹的代价是「每换一句提示，下一次展开就缓存没命中」，而没命中那条路得用
        # _relayout 遮中间那一拍 —— 用户看到的是面板**整块消失**一瞬（实测过）。
        app.lb_note.configure(text='一句会换行的长提示' * 3)
        k1 = app._size_key()
        check('提示行的文字不进尺寸指纹（定高行，字多了也不占位）', k1 == k0, (k0, k1))
        calls[:] = []
        root.update_idletasks = spy
        try:
            app.toggle_more()
            app.toggle_more()
        finally:
            root.update_idletasks = real
        check('提示换过之后展开 / 收起还是命中缓存（面板不会消失一瞬）',
              calls == [], len(calls))
        app.lb_warn.configure(text='告警：采样停了')
        k2 = app._size_key()
        app.lb_warn.configure(text='')
        k3 = app._size_key()
        check('告警行进尺寸指纹（它是真会铺出来 / 收起来的整行）', k2 != k3, (k2, k3))
        app.lb_warn.configure(text=warn0)
    finally:
        app.lb_note.configure(text=note0)
        app.lb_warn.configure(text=warn0)

    win32.set_erase_brush(root, ui_mod.UI_BG)
    hwnd = win32.toplevel_hwnd(root)
    check('类擦除刷子换成了面板底色（展开那一帧不再是一整块纯黑）',
          win32._get_class_long(hwnd, win32.GCLP_HBRBACKGROUND)
          == win32._ERASE_BRUSHES[ui_mod.UI_BG]
          and win32._ERASE_BRUSHES[ui_mod.UI_BG] != 0)
    n = len(win32._ERASE_BRUSHES)
    win32.set_erase_brush(root, ui_mod.UI_BG)
    win32.set_erase_brush(root, ui_mod.UI_BG)
    check('擦除刷子每色只建一把（_place 每次换尺寸都调它，不许每次新建拿 GDI 换内存）',
          len(win32._ERASE_BRUSHES) == n)


def t_dialog_shell(app):
    """17：二级窗口统一外壳（2026-10-13 第八轮，用户看完设计图：「都改成这一版吧」）。

    四个二级窗口以前三套做法：只有系统标题栏 / 系统标题栏 + 自己又画一遍「◆ 手动校准」/
    裸 Listbox。现在统一 = overrideredirect 去掉系统标题栏 + 自画一条 header（3px 色条 +
    标题 + ? + ×）+ 按钮分档（`_btn_kw`）。这里钉住「外壳真的换了」和「说明真的收起来了」。
    """
    import tkinter as tk

    from akframe import ui as ui_mod

    # ---- 手动校准 ----
    app.open_calib_dialog()
    try:
        top = app._calib_win
        check('手动校准：系统标题栏拿掉了（overrideredirect）',
              bool(top.overrideredirect()))
        labels = [str(x.cget('text')) for x in _walk(top, tk.Label)]
        heads = [t for t in labels if t == '手动校准']
        check('手动校准：标题只写一遍（以前系统栏 + 窗内「◆ 手动校准」两遍）',
              heads == ['手动校准'] and not any('◆' in t for t in labels), labels)
        acts = {}
        for b in _walk(top, tk.Button):
            t = str(b.cget('text'))
            if t in ('保存校准', 'ROI 框', '恢复默认', '重新选窗口…'):
                acts[t] = b
        check('手动校准：四个动作键都在', len(acts) == 4, sorted(acts))
        check('手动校准：保存校准 = 主键（白底深字）',
              acts['保存校准'].cget('bg') == ui_mod.PRIMARY
              and acts['保存校准'].cget('fg') == ui_mod.UI_BG,
              acts['保存校准'].cget('bg'))
        check('手动校准：恢复默认 = 危险键（红字）',
              acts['恢复默认'].cget('fg') == ui_mod.ERR, acts['恢复默认'].cget('fg'))
        check('手动校准：ROI 框 = 次键（底色同背景 + 1px 描边）',
              acts['ROI 框'].cget('bg') == ui_mod.UI_BG
              and int(acts['ROI 框'].cget('highlightthickness')) == 1)
        check('手动校准：重新选窗口 = 幽灵键（纯文字）',
              acts['重新选窗口…'].cget('bg') == ui_mod.UI_BG
              and acts['重新选窗口…'].cget('fg') == ui_mod.UI_DIM
              and int(acts['重新选窗口…'].cget('highlightthickness')) == 0)
        check('手动校准：header 上有 ×', _hdr_btn(top, '×') is not None)
        check('手动校准：窗口夹在工作区里（下缘不钻到任务栏底下）',
              top.winfo_y() + top.winfo_reqheight() <= ui_mod.work_area()[1]
              + ui_mod.work_area()[3],
              'y=%d h=%d' % (top.winfo_y(), top.winfo_reqheight()))
    finally:
        app.close_calib_dialog()
        app.root.update()

    # ---- 导入 / 编辑轴 ----
    app.open_axis_dialog()
    try:
        top = app._axis_win
        check('导入轴：系统标题栏拿掉了（overrideredirect）',
              bool(top.overrideredirect()))
        texts = [str(x.cget('text')) for x in _walk(top, tk.Label)
                 if x.winfo_manager()]
        check('导入轴：平时不把「pip install tkinterdnd2」印给用户看',
              not any('pip install' in t for t in texts), texts[:2])
        check('导入轴：提示行说人话（一行）',
              any('拖进来' in t and '帧号开头' in t for t in texts))
        q = _hdr_btn(top, '?')
        q.invoke()
        top.update_idletasks()
        texts = [str(x.cget('text')) for x in _walk(top, tk.Label)
                 if x.winfo_manager()]
        check('导入轴：点 ? 才铺开完整说明（含那句 pip）',
              any('pip install' in t for t in texts))
        hint2 = [x for x in _walk(top, tk.Label) if x.winfo_manager()
                 and 'pip install' in str(x.cget('text'))]
        appl = [b for b in _walk(top, tk.Button)
                if str(b.cget('text')) == '应用到提词器'][0]
        check('导入轴：完整说明挂在提示行下面（在按钮行之前）',
              bool(hint2) and hint2[0].winfo_rooty() < appl.winfo_rooty(),
              [x.winfo_rooty() for x in hint2] + [appl.winfo_rooty()])
        q.invoke()
        top.update_idletasks()
        check('导入轴：再点一次收回去',
              not any(x.winfo_manager() and 'pip install' in str(x.cget('text'))
                      for x in _walk(top, tk.Label)))
        got = {}
        for b in _walk(top, tk.Button):
            t = str(b.cget('text'))
            if t in ('应用到提词器', '选择 md 文件…', '清空', '保存为 md'):
                got[t] = b
        check('导入轴：应用到提词器 = 主键，选择 md = 次键，清空 / 保存 = 幽灵键',
              got['应用到提词器'].cget('bg') == ui_mod.PRIMARY
              and int(got['选择 md 文件…'].cget('highlightthickness')) == 1
              and got['清空'].cget('fg') == ui_mod.UI_DIM
              and got['保存为 md'].cget('fg') == ui_mod.UI_DIM)
    finally:
        app._close_axis_dialog()
        app.root.update()

    # ---- 读取窗口（顺手修掉的「点开必崩」就在这条路上）----
    app._window_dialog('读取窗口', lambda _w: None, sub='选中哪个就读它', ok='读这个窗口')
    try:
        tops = [w for w in app.root.winfo_children()
                if isinstance(w, tk.Toplevel) and w.winfo_exists()]
        top = tops[-1] if tops else None
        check('读取窗口：开得出来（以前 _btn(font=...) 直接 TypeError）',
              top is not None)
        check('读取窗口：系统标题栏拿掉了（overrideredirect）',
              bool(top.overrideredirect()))
        check('读取窗口：标题只有两个词',
              [str(x.cget('text')) for x in _walk(top, tk.Label)
               if x.winfo_manager()][:1] == ['读取窗口'],
              [str(x.cget('text')) for x in _walk(top, tk.Label)][:2])
        check('读取窗口：列表不再是 Listbox（自己画的两行一条）',
              not _walk(top, tk.Listbox) and len(_walk(top, tk.Canvas)) == 1)
        labels = [str(x.cget('text')) for x in _walk(top, tk.Button)]
        check('读取窗口：有「刷新」和主键「读这个窗口」',
              '刷新' in labels and '读这个窗口' in labels, labels)
        okb = [b for b in _walk(top, tk.Button)
               if str(b.cget('text')) == '读这个窗口'][0]
        check('读取窗口：读这个窗口 = 主键（白底深字）',
              okb.cget('bg') == ui_mod.PRIMARY and okb.cget('fg') == ui_mod.UI_BG)
        rs = [b for b in _walk(top, tk.Button) if str(b.cget('text')) == '刷新'][0]
        check('读取窗口：刷新 = 幽灵键', rs.cget('fg') == ui_mod.UI_DIM)
        _, wy, _, wh = ui_mod.work_area()
        check('读取窗口：窗口夹在工作区里（最下那排键点得到）',
              top.winfo_y() + top.winfo_reqheight() <= wy + wh,
              'y=%d h=%d' % (top.winfo_y(), top.winfo_reqheight()))
    finally:
        for w in app.root.winfo_children():
            if isinstance(w, tk.Toplevel) and w.winfo_exists():
                w.destroy()
        app.root.update()


def t_picker_head(app):
    """18：取景器顶部（2026-10-13 第八轮）。

    原来顶上挂着 12 行 / 681 字的总说明，还带字面的 ** （tk.Label 不认 markdown，
    用户看到的就是「**左边**」）；左图按屏幕 0.55 倍排，本机把窗口顶到 1996x1366
    （屏幕 78% x 85%）。现在：一行提示跟着目标走、全文进 ?、左图 0.40 倍。
    """
    import tkinter as tk

    import numpy as np

    from akframe import ui as ui_mod
    from akframe.geometry import layout_for

    h, w = 1600, 2560
    frame = np.zeros((h, w), dtype=np.uint8)
    pk = ui_mod.BoxPicker(app.root, lambda i: (frame, layout_for(w, h)), 3, 1,
                          lambda _b: None)
    try:
        check('取景器：系统标题栏拿掉了（overrideredirect）',
              bool(pk.top.overrideredirect()))
        sw, sh = pk.top.winfo_screenwidth(), pk.top.winfo_screenheight()
        want = min(1.0, max(420, int(sw * 0.40)) / float(w),
                   max(280, int(sh * 0.40)) / float(h))
        old = min(1.0, max(420, int(sw * 0.55)) / float(w),
                  max(280, int(sh * 0.55)) / float(h))
        check('取景器：左图按屏幕 0.40 倍排（原来是 0.55，本机 1408x880）',
              abs(pk.scale - want) < 1e-9 and pk.scale < old,
              'scale=%s want=%s 0.55 时会是 %s' % (pk.scale, want, old))
        check('取景器：左图画布 = 缩过的那一帧',
              int(pk.cv.cget('width')) == int(w * pk.scale), pk.cv.cget('width'))
        text = [str(x.cget('text')) for x in _walk(pk.top, tk.Label)]
        check('取景器：界面上不再漏 markdown 星号（原来印出字面 **左边**）',
              not any('**' in t for t in text))
        long_lab = [x for x in _walk(pk.top, tk.Label) if len(str(x.cget('text'))) > 200]
        check('取景器：那段总说明只有一份，而且默认是收着的',
              len(long_lab) == 1 and not long_lab[0].winfo_manager())
        check('取景器：顶部换成一行提示，跟着目标走',
              pk.lb_hint.cget('text') == ui_mod.PICKER_HINTS['bar'],
              pk.lb_hint.cget('text'))
        pk.set_target('pause')
        check('取景器：换目标，那行提示跟着换',
              pk.lb_hint.cget('text') == ui_mod.PICKER_HINTS['pause'],
              pk.lb_hint.cget('text'))
        pk.top.update_idletasks()
        h0 = pk.top.winfo_reqheight()
        q = _hdr_btn(pk.top, '?')
        q.invoke()
        pk.top.update_idletasks()
        check('取景器：点 ? 铺开完整说明（窗口跟着变高）',
              bool(long_lab[0].winfo_manager())
              and pk.top.winfo_reqheight() > h0,
              '%d -> %d' % (h0, pk.top.winfo_reqheight()))
        q.invoke()
        pk.top.update_idletasks()
        check('取景器：再点一次收回去，窗口回原高',
              not long_lab[0].winfo_manager()
              and pk.top.winfo_reqheight() == h0,
              '收起后 %d，原高 %d' % (pk.top.winfo_reqheight(), h0))
        check('取景器：读数不再是 Consolas（全项目就轴编辑器留一处等宽）',
              'Consolas' not in str(pk.lb_read.cget('font')),
              pk.lb_read.cget('font'))
        check('取景器：主键「用这些框」= 白底深字，取消 = 幽灵键',
              pk.btn_ok.cget('bg') == ui_mod.PRIMARY
              and [b for b in _walk(pk.top, tk.Button)
                   if str(b.cget('text')) == '取消'][0].cget('fg') == ui_mod.UI_DIM)
    finally:
        pk.close()
        app.root.update()


def t_panel_skin(app):
    """19：面板头一段的皮肤（2026-10-13 看板批注 ① ⑤ ⑥ ⑦ + 两条追加）。

    这一组盯的是「看一眼就能看出来」的那几件事：窗内那行重复的标题没了、展开区不再
    有两条挨着的分隔线、打轴器标题里不印内部标记、四颗控制键分了权重、回费那一行
    右边的「自定义」按需铺。判据一律用 pack 顺序 / 控件属性 / 高度，不用坐标 ——
    坐标会随字号和 DPI 走，属性不会。
    """
    import tkinter as tk
    from akframe import ui as ui_mod
    from akframe.scale import ScaleGeom, Theme

    root = app.root
    root.deiconify()
    root.update()
    if not app._more:
        app.toggle_more()
    root.update_idletasks()

    # ① 标题行
    check('窗内那行重复的标题删了（系统标题栏那条还在）',
          not hasattr(app, 'lb_title'))
    texts = sorted(b.cget('text') for b in app.head.pack_slaves())
    check('标题行只剩 ▾ / − / × 三个键，行高 = HEAD_H(36)',
          app.head.winfo_height() == ui_mod.HEAD_H
          and texts == sorted(['▾', '−', '×']),
          '%d %s' % (app.head.winfo_height(), texts))
    # 标题行的键矮下来的真因：Tk 的按钮默认 bd=2（白吃 4px 高）+ 默认
    # pady=3。不写死像素：自检进程不是 DPI aware，高度只有真界面的两分之一。
    check('标题行的键不再白吃高度（bd=2 -> 0，pady=0）',
          all(int(b.cget('bd')) == 0 and int(b.cget('pady')) == 0
              for b in app.head.pack_slaves()),
          [(b.cget('bd'), b.cget('pady')) for b in app.head.pack_slaves()])
    check('标题行整条还能拖着走（绑在 head 上）',
          bool(app.head.bind('<Button-1>')))

    # ② 分隔线只留一条
    seps = [w for w in root.pack_slaves()
            if w.winfo_class() == 'Frame' and w.winfo_reqheight() == 1]
    check('展开态上下只有一条 1px 分隔线（原来 57 / 65 两条）',
          len(seps) == 1, len(seps))

    # ③ 打轴器标题不印内部标记
    check('打轴器标题：没导入写「未导入」',
          app.lb_prom_title.cget('text') == '打轴器 · 未导入',
          app.lb_prom_title.cget('text'))
    app._set_axis_text('300 部署 银灰\n330 技能 银灰\n360 撤退')
    app.apply_axis_text()
    check('手写轴的标题也不写「(轴窗口)」那类内部名字',
          app.lb_prom_title.cget('text') == '打轴器 · 手写轴 ｜ 0/3',
          app.lb_prom_title.cget('text'))
    app.root.update_idletasks()

    # ④ 打轴器两颗键：矮下来 + 按级分色
    tb = dict((b.cget('text'), b) for b in app.lb_prom_title.master.pack_slaves()
              if isinstance(b, tk.Button))
    check('打轴器两颗键也不白吃高度',
          bool(tb) and all(int(b.cget('bd')) == 0 and int(b.cget('pady')) == 0
                           for b in tb.values()),
          [(k, v.cget('bd'), v.cget('pady')) for k, v in tb.items()])
    check('「导入轴…」是次一级（1px 描边）「重载」是 ghost（纯字）',
          tb['导入轴…'].cget('highlightbackground') == ui_mod.RING
          and tb['重载'].cget('highlightthickness') == 0
          and tb['重载'].cget('fg') == ui_mod.UI_DIM,
          [(k, v.cget('fg')) for k, v in tb.items()])

    # ⑤ ⑥ 刻度区：底色 + 描边 + 过去的轴抬亮
    th = Theme()
    check('刻度区底 #0b0b0e + 1px #2a2a2e 描边',
          th.bg == '#0b0b0e' and th.stroke == '#2a2a2e', '%s / %s' % (th.bg, th.stroke))
    check('已经过去的轴抬亮到 #8f8f95（2.99:1 -> 5.72:1）',
          th.mark_past == '#8f8f95' and th.faint == '#8f8f95',
          '%s / %s' % (th.mark_past, th.faint))

    # 追加：「现在」读数往左让位。右边那列帧号最多四位数（≈ 32px，
    # 右对齐在 W-pad_right），所以读数的右边距至少要比 pad_right 多 34px。
    g = ScaleGeom()
    check('「现在」读数的右边距给帧号那列让位',
          g.now_pad_right >= g.pad_right + 34 and g.now_pad_right != g.pad_right,
          '%d vs %d' % (g.now_pad_right, g.pad_right))

    # ⑦ 控制行：四颗键分档 + 矮下来
    cb = dict((b.cget('text'), b) for b in app.fr_ctrl.pack_slaves())
    check('控制行：暂停 = 主（白底深字）',
          cb['开始'].cget('bg') == ui_mod.PRIMARY
          and cb['开始'].cget('fg') == ui_mod.UI_BG,
          cb['开始'].cget('bg'))
    check('控制行：校准 = 次（1px 描边），手动校准 / 清零 = ghost',
          cb['校准：PC端▾'].cget('highlightbackground') == ui_mod.RING
          and cb['手动校准'].cget('fg') == ui_mod.UI_DIM
          and cb['清零'].cget('fg') == ui_mod.UI_DIM,
          [(k, v.cget('fg')) for k, v in cb.items()])
    check('控制行四颗键都矮下来（bd=0，pady=0）',
          all(int(b.cget('bd')) == 0 and int(b.cget('pady')) == 0
              for b in cb.values()),
          [(k, v.cget('bd'), v.cget('pady')) for k, v in cb.items()])

    # 追加：回费效率那一行：「自定义」那一截按需铺，且不动面板尺寸
    row = app.fr_rate_custom.master
    check('回费行右边「自定义」那一截默认收着',
          app.fr_rate_custom.winfo_manager() == '' and app._rate_custom_open is False,
          app.fr_rate_custom.winfo_manager())
    h0, w0 = root.winfo_height(), root.winfo_width()
    app._on_rate_pick(ui_mod.RATE_CUSTOM_LABEL)
    root.update_idletasks()
    texts = [g2.cget('text') if 'text' in g2.keys() else ''
             for g2 in app.fr_rate_custom.pack_slaves()]
    check('选「自定义」才铺出输入框 / 应用',
          app.fr_rate_custom.winfo_manager() == 'pack'
          and texts == ['自定义', '', '应用'], texts)
    check('铺出来也不动面板尺寸（那一行行高是定死的）',
          root.winfo_height() == h0 and root.winfo_width() == w0,
          '%dx%d vs %dx%d' % (root.winfo_width(), root.winfo_height(), w0, h0))
    check('输入框比原来（width=4）宽一格 + 1px 描边',
          int(app.en_rate.cget('width')) == 5
          and int(app.en_rate.cget('highlightthickness')) == 1,
          '%s / %s' % (app.en_rate.cget('width'),
                       app.en_rate.cget('highlightthickness')))
    check('铺开之后整行还塞得下（没有谁被挤掉）',
          sum(g2.winfo_reqwidth() for g2 in row.pack_slaves()) <= row.winfo_width(),
          '%d vs %d' % (sum(g2.winfo_reqwidth() for g2 in row.pack_slaves()),
                        row.winfo_width()))
    app.apply_cost_rate(60.0)
    root.update_idletasks()
    check('切回预设档（60）就把那一截收起来',
          app.fr_rate_custom.winfo_manager() == ''
          and app.rate_var.get().startswith('60'), app.rate_var.get())
    check('回费行的行高固定（RATE_ROW_H），不跟内容跑',
          row.winfo_height() == ui_mod.RATE_ROW_H, row.winfo_height())
    app._show_rate_custom(False)

    # 追加 2（2026-10-13）：图上没编号的那两条 —— 刻度画布铺满内宽、提示行钉 22px
    check('刻度画布铺满面板内宽（COLLAPSED_W - 两侧 padx 10）',
          int(app.cv_scale.cget('width')) == app.COLLAPSED_W - 20
          and app.scale_geom.width == app.COLLAPSED_W - 20,
          '%s / %s' % (app.cv_scale.cget('width'), app.scale_geom.width))
    check('提示行钉在 NOTE_H(22px) 的定高 Frame 里（原来是 10pt 的自然高 33）',
          app.fr_note.winfo_height() == ui_mod.NOTE_H
          and app.lb_note.master is app.fr_note,
          '%d（NOTE_H=%d）' % (app.fr_note.winfo_height(), ui_mod.NOTE_H))
    check('提示行改用 place，不参与 Frame 的尺寸传播（否则 33px 又把它顶开）',
          app.lb_note.winfo_manager() == 'place'
          and float(app.lb_note.place_info().get('relwidth')) == 1.0,
          '%r %r' % (app.lb_note.winfo_manager(), app.lb_note.place_info().get('relwidth')))


def t_round_corners(app):
    """22：圆角（2026-10-17 用户：「把所有面板的直角换成经典的圆角」）。

    走的是 Win11 的 DWM（DWMWA_WINDOW_CORNER_PREFERENCE = ROUND），不是
    SetWindowRgn：后者按区域裁剪、边缘不抗锯齿（实测同一块角：过渡像素 86 vs 0）。

    这里钉四件看一眼看不出来的事：
      - 系统**真的**记下了 —— 读 DwmGetWindowAttribute 回来，不光看自己那个返回值
        （属性号写错、系统把偏好丢掉，DwmSetWindowAttribute 照样返回 0）；
      - 二级窗口**映射出来之后**才是 ROUND：建窗那一拍窗口还没映射，toplevel_hwnd()
        拿到的是另一个句柄，设进去的会被系统丢掉（本机实测踩到过），真正生效的是
        win32.set_round_corners_on_map 那个 <Map> 钩子；
      - 重新映射（withdraw / deiconify）之后还在；
      - 系统不支持这个属性（Win10）/ 拿不到句柄时安静退回直角，不抛异常。
    """
    import tkinter as tk
    from akframe import ui as ui_mod, win32

    root = app.root
    root.deiconify()
    root.update()

    check('主面板设得进去（set_round_corners 返回 True）',
          win32.set_round_corners(root, win32.DWMWCP_ROUND) is True)
    got = win32.get_round_corners(root)
    if got is None:
        skip('系统读回圆角偏好', '系统不支持这个属性（Win10）/ 窗口读不到句柄')
    else:
        check('系统读回来就是 ROUND(2) —— 不是我们自己在记账',
              got == win32.DWMWCP_ROUND, got)

    # 主面板抖过 -alpha（_place 里那一下），窗口是分层的；实测圆角照样在
    root.attributes('-alpha', 0.0)
    root.update()
    root.attributes('-alpha', 1.0)
    root.update()
    check('抖过 -alpha（分层窗口）之后圆角还在',
          win32.get_round_corners(root) in (None, win32.DWMWCP_ROUND),
          win32.get_round_corners(root))

    # 二级窗口：四个都经过 _dialog_header（框选 ROI / 手动校准 / 读取窗口 / 导入编辑轴）
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.geometry('240x160+300+300')
    ui_mod._dialog_header(top, '自检用二级窗口', top.destroy)
    top.update()
    root.update()
    check('二级窗口映射出来之后就是 ROUND',
          win32.get_round_corners(top) in (None, win32.DWMWCP_ROUND),
          win32.get_round_corners(top))
    # 读回是活的（设成直角就该读回直角）—— 不然上面那几条等于在测一个常量
    win32.set_round_corners(top, win32.DWMWCP_DONOTROUND)
    check('设成 DONOTROUND 之后读回来就是 1（读回这条链是通的）',
          win32.get_round_corners(top) in (None, win32.DWMWCP_DONOTROUND),
          win32.get_round_corners(top))
    top.withdraw()
    top.update()
    top.deiconify()
    top.update()
    check('偏好被抹掉之后重新映射会补回 ROUND —— <Map> 钩子那一次真的干活了',
          win32.get_round_corners(top) in (None, win32.DWMWCP_ROUND),
          win32.get_round_corners(top))
    top.destroy()

    # 真的开一次「手动校准」（用户看得见的那个二级窗口）
    app.open_calib_dialog()
    try:
        top = app._calib_win
        for _ in range(3):
            root.update()
        hwnd = win32.toplevel_hwnd(top)
        check('真开一次「手动校准」：窗口句柄认得到、偏好是 ROUND',
              bool(hwnd) and win32.get_round_corners(top) in (None, win32.DWMWCP_ROUND),
              win32.get_round_corners(top))
    finally:
        app.close_calib_dialog()
        root.update()

    # `_place` 每次换尺寸都调它 —— 一次 DwmSetWindowAttribute 是微秒级的，
    # 不记账（句柄会被系统回收再发，记账迟早漏窗口），这里只钉「不发疯」。
    calls = []
    real = win32._dwmapi.DwmSetWindowAttribute

    def spy(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    win32._dwmapi.DwmSetWindowAttribute = spy
    try:
        app.toggle_more()
        root.update()
        app.toggle_more()
        root.update()
    finally:
        win32._dwmapi.DwmSetWindowAttribute = real
    check('展开 / 收起走一次只发一两次系统调用（不是每帧都在发）',
          0 < len(calls) <= 4, len(calls))

    # Win10 / 窗口还没建出来：安静退回直角
    check('拿不到窗口句柄时返回 False（Win10 就是这么退回直角的）',
          win32.set_round_corners(None) is False
          and win32.set_round_corners(root.winfo_toplevel().winfo_children()[0]
                                      if root.winfo_children() else None) is False)


def main():
    print('面板键位 / 折叠自检（2026-10-10 示意图那批 + 2026-10-11 看板批注）')

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
                print('5. 刻度固定 150 帧（翻条那两颗键已删）')
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
                print('17. 二级窗口统一外壳 / 按钮分档 / ? 展开说明（第八轮）')
                t_dialog_shell(app2)
                print('18. 取景器顶部：一行提示 + 左图 0.40 倍')
                t_picker_head(app2)
            finally:
                kill_afters(app2)
                try:
                    app2.root.destroy()
                except Exception:              # noqa: BLE001
                    pass
            app3, _dm3 = make_app()            # 19 组要一个干净的（还没导入过轴的）面板
            try:
                print('19. 面板头一段：标题行 / 分隔线 / 按钮分档 / 回费行按需')
                t_panel_skin(app3)
                print('20. 去掉系统标题栏 / 打轴器字号放大 / 展开收起不闪')
                t_panel_titlebar_fonts(app3)
                print('21. 展开/收起那一拍：尺寸缓存 + 擦除刷子')
                t_toggle_flicker(app3)
                print('22. 圆角（Win11 的 DWM）')
                t_round_corners(app3)
            finally:
                kill_afters(app3)
                try:
                    app3.root.destroy()
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
