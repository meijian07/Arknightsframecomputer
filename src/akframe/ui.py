"""简易测试前端：实时帧数显示 + 三个 ROI 的实时预览 + ROI 校准工具。

为什么要有预览和校准：geometry.py 里的尺寸常数目前只在 2560x1600 这一个窗口尺寸上
实测过。换个窗口大小 / 换个平台（目标是「多样接口」），ROI 可能偏几个像素，而偏几像素
就足以让读数全错。所以前端的第一职责不是好看，而是**让你能一眼看出 ROI 有没有对准**：

    预览图上的红线 = 特征提取实际使用的那条带
    屏幕上加粗的方框（K 键）= 当前 ROI 位置，直接叠在游戏画面上

操作：
    Tab 切换要校准的 ROI   方向键 移动   Shift+方向键 改大小
    K 显示/隐藏屏幕上的 ROI 框    S 保存校准   R 恢复默认
    C 清零计数    L 开始/停止记录 CSV    P 暂停/继续计数（手动）
    T 导入 md 轴文件    Esc 退出

回费速率（减费 tag）：默认 30 帧/费（1 秒 1 费）。点了「回费速度 -50% / -75%」的 tag
时，在面板「回费速率」那行选 60 / 120，或直接填数字后点「应用」——改完立刻生效，
并写回设置文件 cost_rate.json，下次启动自动带上。

打轴器（提词器）：导入一个 md 轴文件，或直接在下面的输入框里写（Ctrl+Enter 应用），
计数器走到对应帧号时，面板上就把那条说明放大显示出来。轴文件格式见 timeline.py 顶部注释
和 maps/example.md。
"""

from __future__ import annotations

import json
import os
import time
import tkinter as tk
from tkinter import filedialog

import numpy as np
from PIL import Image, ImageDraw, ImageTk

from .geometry import (PAUSE_SHAPE_BAND, SPEED_DIGIT_BAND, SPEED_DIGIT_COLS,
                       apply_deltas, layout_for, load_calibration)
from .live import LiveMonitor
from .settings import (COST_RATE_PRESETS, DEFAULT_FRAMES_PER_COST, clamp_frames_per_cost,
                       format_frames_per_cost, save_cost_rate)
from .state import LOGICAL_FPS
from .timeline import Teleprompter, parse_lines
from .win32 import (arknights_ui_scaler, enable_dpi_awareness, set_click_through,
                    virtual_screen_bounds)


# 输入框里的初始提示（# 开头 = 注释，解析器会跳过，不会变成轴）
# 注意：提示行必须用 // 开头（解析器整行跳过）。写成 # 开头的话，'# 120 部署银灰'
# 剥掉井号后正好是一条合法轴，会被当成真的轴吃掉。
AXIS_HINT = ('// 在这里直接写轴：帧号开头，后面跟说明，和 md 轴文件同一个格式\n'
             '//   120 部署银灰\n'
             '//   350 开技能\n'
             '//   1:05 撤退        （也可以用 分:秒，1:05 = 1950 帧）\n'
             '// 写完点右边的「应用到提词器」，或按 Ctrl+Enter\n')

UI_BG = '#16181d'
UI_FG = '#e6e6e6'
UI_DIM = '#8b93a1'
ACCENT = '#39d98a'
WARN = '#ffb020'
ROI_COLORS = {'bar': '#39d98a', 'pause': '#4da3ff', 'speed': '#ff9f43'}


# 「自己填一个数」的那个下拉项。选中它不切档，只把光标丢进输入框。
RATE_CUSTOM_LABEL = '自定义（在右边输入）'
AUTO_RATE_LABEL = '自动识别中…'


def _rate_label(frames: float) -> str:
    """档位 -> 下拉框里显示的文字。落在已知档位上就显示那个档，否则显示数字。"""
    f = clamp_frames_per_cost(frames)
    for v, desc in COST_RATE_PRESETS:
        if abs(v - f) < 1e-9:
            return '%.0f  %s' % (v, desc)
    return '%.0f  自定义' % f


def _one_line(text: str) -> str:
    """轴说明里可能有换行；大字区只显示一行，换行折叠成 ·。"""
    return ' · '.join(x.strip() for x in (text or '').split('\n') if x.strip())


def _clip(text: str, n: int) -> str:
    s = _one_line(text)
    return s if len(s) <= n else s[:n - 1] + '…'


def _fmt_time(seconds: float) -> str:
    """把秒数格式化成 mm:ss.cc（游戏内时间，1 倍速等效）。"""
    if seconds < 0:
        seconds = 0.0
    m = int(seconds // 60)
    s = seconds - m * 60
    return '%02d:%05.2f' % (m, s)


class MonitorApp:
    def __init__(self, monitor: LiveMonitor, *, calib_path: str = 'layout_calib.json',
                 settings_path: str = 'cost_rate.json', poll_ms: int = 16,
                 timeline_path: str = None):
        # poll_ms 是**面板重绘**的周期，不是采样周期（采样在 live.py 的线程里独立跑）。
        # 必须是 16 而不是 60：游戏 1 倍速下每秒走 30 个逻辑帧，面板 60ms 才重绘一次的话，
        # 显示出来的数字每次跳 2 帧 —— 看着就像「漏帧」，而帧操时每次只加 1 帧，
        # 看着又是对的（用户实测反馈正是「一帧一帧过是对的、1 倍速下像在漏帧」）。
        # 16ms ≈ 60Hz，和游戏画面同速，数字才会每拍只变化 0 或 1。
        self.monitor = monitor
        self.calib_path = calib_path
        self.settings_path = settings_path      # 回费速率设置文件（改档时写回这里）
        self.poll_ms = poll_ms
        self.deltas = {'bar': [0, 0, 0, 0], 'pause': [0, 0, 0, 0], 'speed': [0, 0, 0, 0]}
        self.calib_scale = None
        self.target = 'bar'
        self._tick_times = []      # 面板重绘的实测速率（见 _tick 开头）
        self.overlay = None
        self._photos = {}
        # 打轴器：prompter 只管「当前帧号 -> 该看哪条轴」，这里只管画
        self.prompter = Teleprompter()
        self._timeline_path = None      # 当前轴来自哪个 md 文件；None = 来自面板输入框
        self._prom_flash_until = 0.0
        self._note_hold = 0.0           # 提示行的保留截止时刻（见 _set_note）
        self._load_calib()

        self.root = tk.Tk()
        self.root.title('明日方舟 全局帧数计数器')
        self.root.configure(bg=UI_BG)
        self.root.attributes('-topmost', True)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # 先给个大概的初始尺寸，真正的尺寸等 _build_ui() 之后按内容需求算（见下）。
        self.root.geometry('724x1000+24+%d' % max(10, (sh - 1000) // 2))
        self.root.protocol('WM_DELETE_WINDOW', self.quit)

        self._build_ui()
        self._bind_keys()
        # 建完界面才知道内容到底要多宽多高（换台机器 / 换字体缩放就不一样），这里按实际
        # 需求夹一次：窄了右边按钮会被裁（换窗口 / 退出），矮了最下面几行会被裁
        # （回费速率 / 提示 / 帮助）。写死 724 的那版在 150% 缩放的屏幕上就是裁着的 ——
        # tk 的点数会跟着系统 DPI 放大，同样的字号在物理像素上要宽 1.5 倍。
        self.root.update_idletasks()
        win_w = min(max(724, self.root.winfo_reqwidth()), max(640, sw - 40))
        win_h = min(max(self.root.winfo_reqheight(), 640), max(640, sh - 80))
        wrap = max(400, win_w - 30)
        for lb in (self.lb_cycle, self.lb_perf, self.lb_warn, self.lb_note):
            lb.configure(wraplength=wrap)
        self.root.geometry('%dx%d+24+%d' % (win_w, win_h, max(10, (sh - win_h) // 2)))
        if timeline_path:
            self.load_timeline(timeline_path)
        # 把键盘焦点拿回主窗口：否则 Tk 可能把焦点给输入框，快捷键（C/S/T…）就失效了
        self.root.after(300, self.root.focus_set)
        self.root.after(80, self._tick)

    # ---------------- 界面 ----------------

    def _label(self, parent, text='', size=10, color=UI_FG, bold=False, **kw):
        font = ('Microsoft YaHei UI', size, 'bold' if bold else 'normal')
        return tk.Label(parent, text=text, bg=UI_BG, fg=color, font=font, **kw)

    def _build_ui(self):
        pad = dict(padx=10, pady=4)
        top = tk.Frame(self.root, bg=UI_BG)
        top.pack(fill='x', **pad)

        self.lb_frames = tk.Label(top, text='0', bg=UI_BG, fg=ACCENT,
                                  font=('Consolas', 44, 'bold'), anchor='w')
        self.lb_frames.pack(side='left')
        right = tk.Frame(top, bg=UI_BG)
        right.pack(side='left', padx=12, pady=6)
        self.lb_frames_hint = self._label(right, '逻辑帧（1 倍速 30 帧/秒）', 9, UI_DIM)
        self.lb_frames_hint.pack(anchor='w')
        self.lb_time = self._label(right, '游戏内时间 00:00.00', 13)
        self.lb_time.pack(anchor='w')
        self.lb_state = self._label(right, '状态 —', 11, ACCENT)
        self.lb_state.pack(anchor='w')

        info = tk.Frame(self.root, bg=UI_BG)
        info.pack(fill='x', **pad)
        self.lb_bar = self._label(info, '费用条 —', 10)
        self.lb_bar.grid(row=0, column=0, columnspan=3, sticky='w')
        self.lb_pause = self._label(info, '按钮 —', 10)
        self.lb_pause.grid(row=1, column=0, sticky='w', padx=(0, 16))
        self.lb_speed = self._label(info, '倍速 —', 10)
        self.lb_speed.grid(row=1, column=1, columnspan=2, sticky='w')
        # 费用周期 = 「条从空走到下一次空」一共走了几个逻辑帧。这是唯一不依赖任何
        # 帧率/倍速假设的「一费 = 几帧」口径：普通关卡应当恒为 30（偶尔 31 是游戏
        # 那套定点累加器取整攒够一次误差造成的，见 fp24.py 顶部说明）。
        # 「本费 x/30」是当前这一费已经走到第几个逻辑帧，用来核对一秒之内的位置。
        # 这三行的内容长度会变（尤其「费用周期」和「性能」），固定 wraplength 让它们
        # 换行而不是一直向窗口要宽度 —— 否则窗口宽度会被它们顶到屏幕以外去。
        self.lb_cycle = self._label(info, '费用周期 —', 10, anchor='w', justify='left',
                                    wraplength=690)
        self.lb_cycle.grid(row=2, column=0, columnspan=3, sticky='w')
        self.lb_perf = self._label(info, '性能 —', 9, UI_DIM, anchor='w', justify='left',
                                   wraplength=690)
        self.lb_perf.grid(row=3, column=0, columnspan=3, sticky='w', pady=(4, 0))
        self.lb_warn = self._label(info, '', 9, WARN, anchor='w', justify='left',
                                   wraplength=690)
        self.lb_warn.grid(row=4, column=0, columnspan=3, sticky='w')

        prev = tk.Frame(self.root, bg=UI_BG)
        prev.pack(fill='x', **pad)
        self.cv_bar = tk.Canvas(prev, width=704, height=46, bg='#0b0c0f', highlightthickness=0)
        self.cv_bar.grid(row=0, column=0, columnspan=2, pady=(0, 4))
        self.cv_pause = tk.Canvas(prev, width=348, height=170, bg='#0b0c0f', highlightthickness=0)
        self.cv_pause.grid(row=1, column=0, padx=(0, 6))
        self.cv_speed = tk.Canvas(prev, width=348, height=170, bg='#0b0c0f', highlightthickness=0)
        self.cv_speed.grid(row=1, column=1)

        # ---- 打轴器（提词器）：导入 md 轴文件后，到帧就显示那一条 ----
        prom = tk.Frame(self.root, bg=UI_BG)
        prom.pack(fill='both', expand=True, **pad)
        head = tk.Frame(prom, bg=UI_BG)
        head.pack(fill='x')
        self.lb_prom_title = self._label(head, '打轴器 — 未导入（按 T 或点右边按钮选 md 轴文件）',
                                         10, UI_DIM)
        self.lb_prom_title.pack(side='left')
        for text, cmd in (('重载', self.reload_timeline), ('导入轴 (T)', self.import_timeline)):
            tk.Button(head, text=text, command=cmd, font=('Microsoft YaHei UI', 9),
                      bg='#262a33', fg=UI_FG, activebackground='#333947', relief='flat',
                      padx=8, pady=2).pack(side='right', padx=3)
        # 大字 = 当前该看的那条轴；到帧瞬间整块反色 1.4 秒（肉眼能抓住）
        self.lb_cue = self._label(prom, '—', 15, UI_DIM, True,
                                  anchor='w', justify='left', wraplength=684)
        self.lb_cue.pack(fill='x', pady=(6, 2))
        # 下一条 + 倒计时（帧操真正用得上的是这个数）
        self.lb_cue_next = self._label(prom, '', 11, ACCENT, True, anchor='w')
        self.lb_cue_next.pack(fill='x')
        self.lb_cue_list = tk.Label(prom, text='', bg=UI_BG, fg=UI_DIM,
                                    font=('Consolas', 9), anchor='w', justify='left')
        self.lb_cue_list.pack(fill='x')

        # ---- 轴文本输入：不想开文件就直接在这里写/粘，格式和 md 一样 ----
        editor = tk.Frame(self.root, bg=UI_BG)
        editor.pack(fill='both', expand=True, **pad)
        ehead = tk.Frame(editor, bg=UI_BG)
        ehead.pack(fill='x')
        self._label(ehead, '轴文本（帧号开头，格式同 md；Ctrl+Enter 应用）',
                    10, UI_DIM).pack(side='left')
        for text, cmd in (('清空输入框', self.clear_axis_text),
                          ('保存为 md', self.save_axis_text),
                          ('应用到提词器', self.apply_axis_text)):
            tk.Button(ehead, text=text, command=cmd, font=('Microsoft YaHei UI', 9),
                      bg='#262a33', fg=UI_FG, activebackground='#333947', relief='flat',
                      padx=8, pady=2).pack(side='right', padx=3)
        # width 只是给窗口定「初始需要多宽」用的（Text 是 expand=True，实际宽度跟着窗口
        # 走）。不写的话 Tk 默认按 80 字符算，光是它就能把窗口需求宽度顶到 1000+。
        self.txt_axis = tk.Text(editor, height=7, width=40, bg='#0b0c0f', fg=UI_FG, relief='flat',
                                insertbackground=UI_FG, selectbackground='#333947',
                                font=('Consolas', 10), wrap='word', undo=True)
        self.txt_axis.pack(fill='both', expand=True, pady=(4, 0))
        self.txt_axis.insert('1.0', AXIS_HINT)
        # 在这两个键上返回 'break'，事件就不会继续冒泡到主窗口的快捷键
        self.txt_axis.bind('<Escape>', self._axis_escape)
        self.txt_axis.bind('<Control-Return>', self._axis_apply_key)

        btns = tk.Frame(self.root, bg=UI_BG)
        btns.pack(fill='x', **pad)
        # 「暂停计数」单独建：文案/颜色要跟开关状态变，其余按钮不用
        self.btn_manual = tk.Button(btns, text='暂停计数 (P)', command=self.toggle_manual_pause,
                                    font=('Microsoft YaHei UI', 9), bg='#262a33', fg=UI_FG,
                                    activebackground='#333947', relief='flat', padx=8, pady=3)
        self.btn_manual.pack(side='left', padx=3)
        for text, cmd in (('清零计数', self.clear_count),
                          ('开始记录', self.toggle_csv),
                          ('校准模式', self.toggle_overlay),
                          ('保存校准', self.save_calib),
                          ('恢复默认', self.reset_calib),
                          ('换窗口', self.pick_window),
                          ('退出', self.quit)):
            b = tk.Button(btns, text=text, command=cmd, font=('Microsoft YaHei UI', 9),
                          bg='#262a33', fg=UI_FG, activebackground='#333947',
                          relief='flat', padx=8, pady=3)
            b.pack(side='left', padx=3)

        # ---- 回费速率（减费 tag）：手动档，改完立刻生效并写回设置文件 ----
        # 为什么不自动认：认错档会让绝对帧号整段漂移（差一档就是几百帧），而打轴器
        # 要的正是绝对帧号。档位取值 = 「一费几个逻辑帧」，和倍速、画面帧率都无关。
        rate_row = tk.Frame(self.root, bg=UI_BG)
        rate_row.pack(fill='x', **pad)
        self._label(rate_row, '回费速率', 10).pack(side='left')
        cur = (self.monitor.counter.frames_per_cost if self.monitor.counter is not None
               else DEFAULT_FRAMES_PER_COST)
        self.rate_var = tk.StringVar(value=_rate_label(cur))
        self._rate_labels = {}
        for v, desc in COST_RATE_PRESETS:
            self._rate_labels['%.0f  %s' % (v, desc)] = v
        items = list(self._rate_labels) + [RATE_CUSTOM_LABEL]
        self.rate_menu = tk.OptionMenu(rate_row, self.rate_var, *items,
                                       command=self._on_rate_pick)
        self.rate_menu.config(bg='#262a33', fg=UI_FG, activebackground='#333947',
                              activeforeground=UI_FG, relief='flat', highlightthickness=0,
                              font=('Microsoft YaHei UI', 9), width=26, anchor='w',
                              padx=4, pady=2)
        self.rate_menu['menu'].config(bg='#262a33', fg=UI_FG, activebackground=ACCENT,
                                      activeforeground=UI_BG, relief='flat',
                                      font=('Microsoft YaHei UI', 9))
        self.rate_menu.pack(side='left', padx=(6, 6))
        self._label(rate_row, '自定义', 9, UI_DIM).pack(side='left')
        self.en_rate = tk.Entry(rate_row, width=5, bg='#0b0c0f', fg=UI_FG, relief='flat',
                                insertbackground=UI_FG, justify='center',
                                font=('Consolas', 10))
        self.en_rate.pack(side='left', padx=(4, 4))
        self.en_rate.bind('<Return>', lambda _e: self.apply_rate_entry())
        for text, cmd in (('应用', self.apply_rate_entry),
                          ('自动识别（可选）', self.enable_auto_rate)):
            tk.Button(rate_row, text=text, command=cmd, font=('Microsoft YaHei UI', 9),
                      bg='#262a33', fg=UI_FG, activebackground='#333947', relief='flat',
                      padx=8, pady=2).pack(side='left', padx=3)

        # height=2：提示行会换行，先把位置占住，免得它一变长就把下面的帮助行挤掉
        self.lb_note = self._label(self.root, '', 10, UI_DIM, anchor='w', justify='left',
                                   wraplength=690, height=2)
        self.lb_note.pack(fill='x', padx=10, pady=(2, 0))
        self.lb_help = self._label(
            self.root,
            'Tab 切 ROI ｜ 方向键移动/Shift 缩放 ｜ K ROI 框 ｜ S 保存 ｜ R 默认 ｜ C 清零 ｜ '
            'L 记录 ｜ P 暂停计数 ｜ T 导入轴 ｜ 下框可直接写轴（Ctrl+Enter 应用）',
            9, UI_DIM, wraplength=694, justify='left')
        self.lb_help.pack(fill='x', padx=10, pady=(2, 8))

    def _guard(self, handler):
        """给快捷键加一层「输入框保护」。

        Tk 的按键会沿 bindtags 依次送给 控件 -> 控件类 -> 顶层窗口；Text 插入字符时
        不会返回 'break'，所以不拦一下的话，在轴输入框里打字会顺手触发主窗口的快捷键
        （打 c 清零、打 t 弹文件框、打 s 存校准……）。
        """
        def wrapped(event):
            if isinstance(self.root.focus_get(), (tk.Text, tk.Entry)):
                return None
            return handler(event)
        return wrapped

    def _bind_keys(self):
        r = self.root
        for seq, fn in (('<Tab>', self._cycle_target),
                        ('<Escape>', self.quit),
                        ('k', self.toggle_overlay), ('K', self.toggle_overlay),
                        ('s', self.save_calib), ('S', self.save_calib),
                        ('r', self.reset_calib), ('R', self.reset_calib),
                        ('c', self.clear_count), ('C', self.clear_count),
                        ('l', self.toggle_csv), ('L', self.toggle_csv),
                        ('p', self.toggle_manual_pause), ('P', self.toggle_manual_pause),
                        ('t', self.import_timeline), ('T', self.import_timeline)):
            r.bind(seq, self._guard(lambda _e, f=fn: f()))
        for seq in ('<Left>', '<Right>', '<Up>', '<Down>',
                    '<Shift-Left>', '<Shift-Right>', '<Shift-Up>', '<Shift-Down>'):
            r.bind(seq, self._guard(self._nudge))

    # ---------------- 校准 ----------------

    def _load_calib(self):
        deltas, scale = load_calibration(self.calib_path)
        if deltas:
            self.deltas = {k: list(v) for k, v in deltas.items()}
            self.calib_scale = scale

    def _nudge(self, event):
        shift = bool(event.state & 0x1)
        name = self.target
        d = self.deltas[name]
        step = 1
        if event.keysym == 'Left':
            d[2 if shift else 0] -= step
        elif event.keysym == 'Right':
            d[2 if shift else 0] += step
        elif event.keysym == 'Up':
            d[3 if shift else 0] -= step
        elif event.keysym == 'Down':
            d[3 if shift else 0] += step
        self._apply_deltas()

    def _cycle_target(self):
        order = ('bar', 'pause', 'speed')
        self.target = order[(order.index(self.target) + 1) % len(order)]
        self._set_note('当前校准目标：%s' % self.target)

    def _apply_deltas(self):
        base = self._base_layout()
        if base is None:
            return
        try:
            self.monitor.set_layout(apply_deltas(base, self.deltas, self.calib_scale))
        except ValueError as exc:
            self._set_note('ROI 越界，已忽略这次调整：%s' % exc)

    def _base_layout(self):
        win = self.monitor.window
        if win is None:
            return None
        try:
            return layout_for(win.client[2], win.client[3], arknights_ui_scaler())
        except ValueError:
            return None

    def save_calib(self):
        base = self._base_layout()
        if base is None:
            self._set_note('还没找到窗口，无法保存')
            return
        data = {'client': [base.width, base.height], 'scale': base.scale,
                'deltas': self.deltas,
                'abs': self.monitor.layout.to_dict() if self.monitor.layout else base.to_dict()}
        with open(self.calib_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        self._set_note('已保存校准到 %s' % os.path.abspath(self.calib_path))

    def reset_calib(self):
        self.deltas = {'bar': [0, 0, 0, 0], 'pause': [0, 0, 0, 0], 'speed': [0, 0, 0, 0]}
        self.calib_scale = None
        self._apply_deltas()
        self._set_note('已恢复默认布局')

    # ---------------- ROI 叠加框 ----------------

    def toggle_overlay(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
            self._set_note('已关闭 ROI 框')
            return
        if self.monitor.layout is None or self.monitor.window is None:
            self._set_note('还没找到窗口，无法显示 ROI 框')
            return
        x, y, w, h = virtual_screen_bounds()
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        top.geometry('%dx%d+%d+%d' % (w, h, x, y))
        top.configure(bg='#010101')
        try:
            top.attributes('-transparentcolor', '#010101')
        except tk.TclError:
            pass
        cv = tk.Canvas(top, bg='#010101', highlightthickness=0)
        cv.pack(fill='both', expand=True)
        self.overlay = top
        self.overlay_canvas = cv
        top.update_idletasks()
        # 关键：Tk 的 -transparentcolor 是设在**包装窗口**（wm_frame）上的，winfo_id()
        # 拿到的是里面的子窗口。如果对子窗口设 WS_EX_LAYERED，子窗口会变成「不带透明色
        # 的」不透明分层窗口，直接把整屏盖黑（本机实测踩过）。所以必须作用在 wrapper 上。
        try:
            wrapper = int(top.wm_frame(), 16)
        except (ValueError, TypeError):
            wrapper = top.winfo_id()
        set_click_through(wrapper)
        self._draw_overlay()
        self._set_note('ROI 框已显示（鼠标可以穿透，不影响游戏）')

    def _draw_overlay(self):
        if self.overlay is None:
            return
        cv = self.overlay_canvas
        cv.delete('all')
        lay, win = self.monitor.layout, self.monitor.window
        if lay is None or win is None:
            return
        ox, oy = win.client[0], win.client[1]
        for name in ('bar', 'pause', 'speed'):
            r = getattr(lay, name)
            color = ROI_COLORS[name] if name != self.target else '#ffffff'
            cv.create_rectangle(ox + r.x - 3, oy + r.y - 3, ox + r.x2 + 3, oy + r.y2 + 3,
                                outline=color, width=3)
            cv.create_text(ox + r.x, oy + r.y - 12, text='%s %dx%d' % (name, r.w, r.h),
                           fill=color, anchor='w', font=('Consolas', 12, 'bold'))

    # ---------------- 动作 ----------------

    def clear_count(self):
        self.monitor.reset()
        self._set_note('已清零')

    def toggle_manual_pause(self):
        """手动「暂停计数」：数字按住不动，方便对着游戏/录像逐段核对。

        和游戏自己的暂停无关 —— 那条路一直按费用条算。这里只是让计数器不再往
        total 上加：读数、预览照常刷新，只是数字不动。再按一次恢复（恢复时重新起锚，
        按住的那一段既不会事后补进来、数字也不会跳）。符号画面变了的话是因为这个
        开关只管计数，不管特征提取。
        """
        on = self.monitor.toggle_manual_pause()
        self.btn_manual.configure(text='继续计数 (P)' if on else '暂停计数 (P)',
                                  fg=WARN if on else UI_FG)
        self._set_note('已暂停计数（手动）：数字按住不动，再按 P 继续'
                       if on else '已收开关：继续计数（重新起锚）')

    def _on_rate_pick(self, label):
        """下拉框选中某一项。选「自定义」不切档，只把光标丢进输入框。"""
        if label == RATE_CUSTOM_LABEL:
            self.en_rate.focus_set()
            self.en_rate.select_range(0, 'end')
            self._set_note('在输入框里填「一费几个逻辑帧」（例如 45），再点「应用」')
            # 下拉框回显真实值，免得看上去像已经切过去了
            self.rate_var.set(_rate_label(self._current_rate()))
            return
        v = self._rate_labels.get(label)
        if v is not None:
            self.apply_cost_rate(v)

    def _current_rate(self) -> float:
        return (self.monitor.counter.frames_per_cost if self.monitor.counter is not None
                else DEFAULT_FRAMES_PER_COST)

    def apply_rate_entry(self):
        """「应用」按钮 / 输入框回车：把输入框里的数当成一费逻辑帧数切档。"""
        text = self.en_rate.get().strip()
        try:
            v = float(text)
        except ValueError:
            self._set_note('「一费几个逻辑帧」要填数字，例如 60（回费速度 -50%% 就是 60）：%r'
                           % text)
            return
        if not (v > 0):
            self._set_note('「一费几个逻辑帧」要填正数，例如 60')
            return
        self.apply_cost_rate(v)

    def apply_cost_rate(self, value):
        """切到某个回费速率：立刻生效（已数的像素帧按比例换算）+ 写回设置文件。"""
        v = clamp_frames_per_cost(value)
        self.monitor.set_cost_rate(v)
        self.rate_var.set(_rate_label(v))
        self.en_rate.delete(0, 'end')
        note = '回费速率 = %s' % format_frames_per_cost(v)
        try:
            save_cost_rate(self.settings_path, v)
            self._set_note('%s，已存到 %s' % (note, os.path.abspath(self.settings_path)))
        except OSError as exc:
            self._set_note('%s，但存盘失败：%s' % (note, exc))

    def enable_auto_rate(self):
        """可选功能：让程序自己数条宽认档位（认错档会让帧号整段漂移，默认不用）。"""
        self.monitor.set_cost_rate('auto')
        self.rate_var.set(AUTO_RATE_LABEL)
        self._set_note('已开启自动识别：数出一个完整周期里条出现过多少种宽度，'
                       '认准了才切档（结果不写回设置文件）')

    def toggle_csv(self):
        if self.monitor._csv_path:
            path = self.monitor._csv_path
            self.monitor.set_csv_path(None)
            self._set_note('已停止记录：%s' % path)
        else:
            path = os.path.join('out', 'live_%s.csv' % time.strftime('%m%d_%H%M%S'))
            self.monitor.set_csv_path(path)
            self._set_note('开始记录到 %s' % os.path.abspath(path))

    def pick_window(self):
        from .win32 import list_windows
        wins = [w for w in list_windows() if w.client[2] >= 320 and w.client[3] >= 240]
        top = tk.Toplevel(self.root)
        top.title('选择要监视的窗口')
        top.configure(bg=UI_BG)
        top.attributes('-topmost', True)
        top.geometry('760x420+200+150')
        lb = tk.Listbox(top, font=('Consolas', 10), bg='#0b0c0f', fg=UI_FG,
                        selectbackground='#333947', activestyle='none')
        lb.pack(fill='both', expand=True, padx=8, pady=8)
        for w in wins:
            lb.insert('end', str(w))

        def choose(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            win = wins[sel[0]]
            self.monitor.exe, self.monitor.title, self.monitor.pid = win.exe, None, win.pid
            self.monitor.window = win
            self.monitor.set_layout(
                layout_for(win.client[2], win.client[3], arknights_ui_scaler()), keep_elapsed=True)
            self._apply_deltas()
            top.destroy()
            self._set_note('已切换到 %s' % win)

        lb.bind('<Double-Button-1>', choose)
        tk.Button(top, text='选这个窗口', command=choose, font=('Microsoft YaHei UI', 10),
                  bg='#262a33', fg=UI_FG, relief='flat').pack(pady=(0, 8))

    # ---------------- 打轴器 ----------------

    def import_timeline(self):
        """选一个 md 轴文件（默认从项目里的 maps/ 开始找）。"""
        path = filedialog.askopenfilename(
            parent=self.root, title='选择轴文件（Markdown）', initialdir=self._maps_dir(),
            filetypes=[('Markdown 轴文件', '*.md'), ('文本文件', '*.txt'),
                       ('所有文件', '*.*')])
        if path:
            self.load_timeline(path)

    def load_timeline(self, path: str):
        try:
            with open(path, encoding='utf-8-sig') as fh:      # utf-8-sig 顺带吃掉 BOM
                text = fh.read()
        except OSError as exc:
            self._set_note('读不了这个轴文件：%s' % exc)
            return
        self._timeline_path = os.path.abspath(path)
        self._set_axis_text(text)       # 顺手填进输入框，接着就能在面板里改
        tl = parse_lines(text.splitlines(), source=self._timeline_path)
        if not tl.cues:
            self._set_note('这个文件里没解析出任何一条轴，格式见 maps/example.md')
            return
        self._use_timeline(tl)
        note = '已导入 %s：%d 条轴' % (tl.name, len(tl))
        if tl.warnings:
            note += '  ｜  ' + tl.warnings[0]
        self._set_note(note)

    def reload_timeline(self):
        """重新解析当前轴：来自文件就重读文件，来自面板就重新解析输入框。"""
        if self._timeline_path:
            self.load_timeline(self._timeline_path)
            return
        if self.prompter.timeline is not None:
            self.apply_axis_text()
            return
        self._set_note('还没有轴：按 T 导入 md，或直接在下面的输入框里写')

    def apply_axis_text(self):
        """把输入框里的文本解析成轴，换掉当前提词用的轴。"""
        tl = parse_lines(self.txt_axis.get('1.0', 'end').splitlines(), source='(面板输入)')
        if not tl.cues:
            self._set_note('这段文本里没解析出轴。格式是「帧号 + 说明」，例如：120 部署银灰')
            return
        self._timeline_path = None      # 轴现在来自面板，不再是某个文件
        self._use_timeline(tl)
        note = '已应用面板里的 %d 条轴' % len(tl)
        if tl.warnings:
            note += '  ｜  ' + tl.warnings[0]
        self._set_note(note)

    def save_axis_text(self):
        """把输入框里的文本存成 md 文件（存完仍以面板内容为准）。"""
        text = self.txt_axis.get('1.0', 'end')
        if not text.strip():
            self._set_note('输入框是空的，没东西可存')
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='把轴保存成 md 文件', defaultextension='.md',
            initialdir=self._maps_dir(), initialfile='my_axis.md',
            filetypes=[('Markdown 轴文件', '*.md'), ('文本文件', '*.txt')])
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text.rstrip('\n') + '\n')
        self._set_note('已保存到 %s（当前轴仍按面板内容走，改完点「应用到提词器」）' % path)

    def clear_axis_text(self):
        """只清空输入框；已经在跑的提词轴不动。"""
        self.txt_axis.delete('1.0', 'end')
        self._set_note('输入框已清空（当前提词轴不变；换轴请点「应用到提词器」）')

    def _maps_dir(self) -> str:
        d = os.path.join(os.getcwd(), 'maps')
        return d if os.path.isdir(d) else os.getcwd()

    def _set_axis_text(self, text: str) -> None:
        self.txt_axis.delete('1.0', 'end')
        self.txt_axis.insert('1.0', text)

    def _use_timeline(self, tl) -> None:
        """装上一份新轴（不管是 md 来的还是面板文本来的）。"""
        self.prompter.set_timeline(tl)
        self.lb_prom_title.configure(text='打轴器 — %s ｜ 0/%d' % (tl.name, len(tl)))
        self.lb_cue.configure(text='等计数器走到第一条…', fg=UI_DIM, bg=UI_BG)
        self.lb_cue_next.configure(text='')
        self.lb_cue_list.configure(text='')

    def _axis_escape(self, _event):
        """在输入框里按 Esc 只把焦点交回主窗口，不直接退出程序。"""
        self.root.focus_set()
        return 'break'

    def _axis_apply_key(self, _event):
        self.apply_axis_text()
        return 'break'

    def _update_prompter(self, res):
        if self.prompter.timeline is None or res is None:
            return
        p = self.prompter.update(res.elapsed)
        tl = self.prompter.timeline
        self.lb_prom_title.configure(text='打轴器 — %s ｜ %d/%d' % (tl.name, p.done, p.total))

        if p.newly:
            self._prom_flash_until = time.time() + 1.4
        flash = time.time() < self._prom_flash_until

        if p.current is None:
            self.lb_cue.configure(text='还没到第一条轴', fg=UI_DIM, bg=UI_BG)
        else:
            # 说明太长会把下面的「下一条」顶出窗口，这里截一下（原文在 md 里没动）
            text = _clip(p.current.text, 120) or '(这条轴没写说明)'
            if len(p.newly) > 1:
                text += '   ← 这一拍一下跨过 %d 条' % len(p.newly)
            self.lb_cue.configure(text='%d 帧 ｜ %s' % (p.current.frame, text),
                                  fg='#101418' if flash else ACCENT,
                                  bg=ACCENT if flash else UI_BG)

        # 倒计时秒数按当前倍速折算：2 倍速下 30 帧只要 0.5 秒
        rate = (res.m if res.m else 1.0) * (0.2 if res.bullet else 1.0)
        if p.next_cue is None:
            self.lb_cue_next.configure(text='轴已走完（共 %d 条）' % p.total, fg=UI_DIM)
        else:
            secs = p.next_in / max(1e-6, 30.0 * rate)
            self.lb_cue_next.configure(
                text='还有 %d 帧（≈ %.1f 秒）→ %s' % (p.next_in, secs,
                                                      _clip(p.next_cue.text, 34)),
                fg=ACCENT)
        self.lb_cue_list.configure(
            text='\n'.join('%6d 帧  (+%d)  %s' % (c.frame, d, _clip(c.text, 34))
                           for c, d in p.upcoming))

    def quit(self):
        try:
            self.monitor.stop()
        finally:
            self.root.destroy()

    # ---------------- 每帧刷新 ----------------

    def _set_note(self, text: str, hold: float = 3.0):
        """更新底部提示行。

        hold > 0 时这条提示会顶住 _tick 的自动状态行几秒 —— 否则面板每 16ms 就把状态行
        刷一遍，用户刚点的操作（改回费速率、保存校准）连一眼都看不见。
        """
        self.lb_note.configure(text=text)
        self._note_hold = time.perf_counter() + hold

    def _tick(self):
        # 面板自己的重绘速率，用来核对「显示跟不跟得上游戏」：
        # 1 倍速下游戏每秒走 30 个逻辑帧，面板必须显著高于 30 Hz 才可能每拍只跳 1 帧。
        # 实测过 60ms 的旧值（16.7 Hz）会每拍跳 2 帧，看着就像在漏帧。
        now = time.perf_counter()
        self._tick_times.append(now)
        if len(self._tick_times) > 30:
            self._tick_times.pop(0)
        ui_hz = 0.0
        if len(self._tick_times) >= 2:
            span = self._tick_times[-1] - self._tick_times[0]
            ui_hz = (len(self._tick_times) - 1) / span if span > 0 else 0.0
        upd = self.monitor.latest
        st = self.monitor.stats
        # 「最大间隔」的告警门限必须跟着当前倍速走：采样间隔一旦超过「一个逻辑帧在屏幕上
        # 停留的时长」就会整帧漏采。停留时长 = 1000 / (30 x 倍速)：1X -> 33.3ms、
        # 2X -> 16.7ms。门限写死 33.3ms 的话，2 倍速下明明在漏采也不会报警。
        # 倍速读不到时按 1X 算（保守，宁可多报也不漏报）。
        cur_m = 1.0
        if upd.result is not None and upd.result.m == upd.result.m and upd.result.m > 0:
            cur_m = float(upd.result.m)
        if upd.result is not None:
            res = upd.result
            self.lb_frames.configure(text=str(res.elapsed))
            self.lb_time.configure(text='游戏内时间 %s' % _fmt_time(res.elapsed / 30.0))
            if res.mode == 'manual-pause':
                self.lb_state.configure(text='状态 计数已暂停（手动，按住不动）', fg=WARN)
            elif res.mode == 'pause':
                self.lb_state.configure(text='状态 已暂停（不计数）', fg=WARN)
            elif res.mode.startswith('clock'):
                self.lb_state.configure(text='状态 运行中（满费，挂钟估算）', fg=WARN)
            elif res.mode == 'bar-reject' and res.warn == 'speed-unknown':
                self.lb_state.configure(text='状态 运行中（满费，倍速未知：冻结）', fg=WARN)
            elif res.mode == 'anchor-head':
                # 条第一次被读到：画面上的「空格子」就是本费第 1 帧，这一拍已经是
                # 第 k 帧了，所以起锚时要把它补上（见 docs 第 13 节）。
                self.lb_state.configure(
                    text='状态 起锚：已补上费用周期开头的 %d 帧' % res.delta, fg=ACCENT)
            elif res.warn == 'anchor-late':
                self.lb_state.configure(
                    text='状态 起锚（条是半路出现的，前面丢了多少帧无法证明：不补）',
                    fg=WARN)
            elif res.mode == 'wrap':
                # 条走满之后游戏把条收起来一拍（回费动画）。那一帧就是新一费的第 1 帧，
                # 必须当场记下来（见 docs/费用条读取逻辑.md 第 13 节）。
                self.lb_state.configure(
                    text='状态 刚回费（条归零那一帧，记 %d 帧）' % res.delta, fg=ACCENT)
            elif res.mode == 'wrap-missed':
                # 归零那一帧没被采到，这一拍把漏的帧一起补了 —— 不是计数错，是采样漏拍。
                self.lb_state.configure(
                    text='状态 刚回费（归零那一帧没采到，这一拍一次补 %d 帧；'
                         '提高采样率可消除）' % res.delta, fg=WARN)
            elif res.warn == 'phase-resync':
                # 相位模型连续 REJECT_RESYNC_SECONDS(0.2s) 和画面对不上（采样卡了太久，
                # 缺口跨过 1/4 个周期）：模型自己已经不可信，按当前画面重新起锚。
                # 丢掉的帧无法证明，这一拍 delta=0 —— 宁可少记也不猜。
                self.lb_state.configure(
                    text='状态 相位重锚（采样卡顿太久、模型对不上：这段不补帧）', fg=WARN)
            elif not res.running:
                # 帧操：采样这一瞬图标是「暂停」，但两次采样之间游戏跑过了、条动过了
                self.lb_state.configure(text='状态 已暂停（条在动，按像素计数）', fg=ACCENT)
            else:
                self.lb_state.configure(text='状态 运行中（像素计数）', fg=ACCENT)
            self.lb_bar.configure(
                text='费用条 fill=%d/%d  %s  通道=%s' % (res.fill, self.monitor.layout.bar_w,
                                                      res.bar_state, res.mode))
            self.lb_pause.configure(
                text='按钮 ratio=%.3f  %s' % (res.pause_ratio, '运行中' if res.running else '已暂停'))
            bullet = ' 子弹时间' if res.bullet else ''
            self.lb_speed.configure(
                text='倍速 %.0fX%s  conf=%s  digit=%.3f corr=%s' % (
                    res.m, bullet, 'ok' if res.corr_2x == res.corr_2x else 'low',
                    res.digit_ratio, '%.3f' % res.corr_2x if res.corr_2x == res.corr_2x else '—'))
            self.lb_warn.configure(text=('告警：' + res.warn) if res.warn else '')
            self._update_cycle(res)
        self._update_prompter(upd.result)
        # 「最大间隔」是漏帧的直接指标：采样间隔一旦超过一个逻辑帧的显示时长，
        # 就有可能整帧没被采到（表现：某一拍一次 +2）。门限见上面的 cur_m。
        jam_thr = 1000.0 / (LOGICAL_FPS * max(1.0, cur_m))
        jam = st.gap_max_ms >= jam_thr
        self.lb_perf.configure(
            text='采样 %.1f Hz ｜ 最大间隔 %s%.1f ms%s ｜ 面板 %.0f Hz ｜ 抓屏 %.1f ms '
                 '｜ 采样 %d 次 ｜ 读不到累计 %.2f s'
                 % (st.hz, '⚠ ' if jam else '', st.gap_max_ms,
                    '（超过 %.1f ms，可能漏采）' % jam_thr if jam else '',
                    ui_hz, st.grab_ms, st.samples, st.unreadable_s),
            fg=WARN if jam else UI_DIM)
        win = self.monitor.window
        lay = self.monitor.layout
        if now < self._note_hold:
            pass                     # 刚弹过的提示还留着，先别盖
        elif win is not None and lay is not None:
            self._set_note('%s  %dx%d @(%d,%d) ｜ %s' % (
                win.exe, win.client[2], win.client[3], win.client[0], win.client[1],
                upd.note or 'ROI %s' % self.target), hold=0.0)
        elif upd.note:
            self._set_note(upd.note, hold=0.0)

        self._render_previews(upd, lay)
        if self.overlay is not None:
            self._draw_overlay()
        self.root.after(self.poll_ms, self._tick)

    def _update_cycle(self, res):
        """更新「费用周期」那一行：一费几帧 + 本费走到第几帧。

        一费几帧由 Counter.frames_per_cost 给出：普通关卡 30，带了减回费的 tag 时是
        60 / 120（自动认，见 cost_rate.py；也可以用 --cost-rate 手工指定）。
        这里的「一费 N 帧（中位）」是**实测**值，正常应和它一致；
        稳定不一致才说明相位模型有问题。
        """
        counter = self.monitor.counter
        per = float(counter.frames_per_cost) if counter is not None else LOGICAL_FPS
        into = int(round(res.phase * per))
        parts = []
        # 回费速率（减费 tag）：手工设置的档位，或「自动识别」认准后自动切的档
        est = getattr(self.monitor, 'rate_estimate', None)
        auto_ok = est is not None and est.frames_per_cost
        if self.rate_var.get() == AUTO_RATE_LABEL and self.monitor.rate_locked:
            # 认准了：下拉框也跟着切过去，免得显示和实际不一致
            self.rate_var.set(_rate_label(per))
        if counter is not None and abs(per - LOGICAL_FPS) > 1e-9:
            parts.append('回费速率 %.0f 帧/费（%s）'
                         % (per, '自动认定' if auto_ok else '设置档'))
        elif auto_ok:
            parts.append('回费速率 %.0f 帧/费（自动认定）' % est.frames_per_cost)
        n_cyc, med, lo, hi = counter.cycle_stats() if counter is not None else (0, 0, 0, 0)
        if n_cyc:
            parts.append('一费 %d 帧（中位）' % med)
            parts.append('上次 %d 帧' % counter.cycle_lengths[-1])
            if lo != hi:
                parts.append('范围 %d..%d' % (lo, hi))
            parts.append('共 %d 个' % n_cyc)
        else:
            parts.append('还没走完一个完整周期')
        parts.append('本费 %d/%d 帧' % (into, int(round(per))))
        self.lb_cycle.configure(text='费用周期 ' + ' ｜ '.join(parts))

    def _render_previews(self, upd, lay):
        if upd.bar_img is None:
            return
        self._show(self.cv_bar, upd.bar_img, mark_rows=(5, 12))
        if lay is not None:
            self._show(self.cv_pause, upd.pause_img,
                       mark_rows=lay.pause.band_rows(*PAUSE_SHAPE_BAND))
            self._show(self.cv_speed, upd.speed_img,
                       mark_rows=lay.speed.band_rows(*SPEED_DIGIT_BAND),
                       mark_cols=lay.speed.cols_range(*SPEED_DIGIT_COLS))

    def _show(self, canvas, arr, mark_rows=None, mark_cols=None):
        """把 ROI 灰度图放大画进固定大小的画布（按比例缩放并居中，不拉伸）。

        红线 = 特征提取真正使用的那条带（费用条是行 5..12；暂停键是形状判别条带），
        蓝线 = 倍速数字的横向范围。ROI 偏了的话，一眼就能从预览上看出来。
        """
        h, w = arr.shape
        box_w = int(canvas['width'])
        box_h = int(canvas['height'])
        k = max(1, min(box_w // w, box_h // h))
        ox = (box_w - w * k) // 2
        oy = (box_h - h * k) // 2
        img = Image.fromarray(arr, mode='L').convert('RGB').resize((w * k, h * k), Image.NEAREST)
        d = ImageDraw.Draw(img)
        if mark_rows:
            r0, r1 = mark_rows
            d.rectangle([0, r0 * k, w * k - 1, (r1 + 1) * k - 1], outline=(230, 60, 60))
        if mark_cols:
            c0, c1 = mark_cols
            d.rectangle([c0 * k, 0, (c1 + 1) * k - 1, h * k - 1], outline=(60, 160, 255))
        photo = ImageTk.PhotoImage(img)
        self._photos[canvas] = photo
        canvas.delete('all')
        canvas.create_image(ox, oy, anchor='nw', image=photo)

    def run(self):
        self.root.mainloop()
