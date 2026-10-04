"""简易测试前端：实时帧数显示 + 三个 ROI 的实时预览 + ROI 校准工具。

为什么要有预览和校准：geometry.py 里的尺寸常数目前只在 2560x1600 这一个窗口尺寸上
实测过。换个窗口大小 / 换个平台（目标是「多样接口」），ROI 可能偏几个像素，而偏几像素
就足以让读数全错。所以前端的第一职责不是好看，而是**让你能一眼看出 ROI 有没有对准**：

    预览图上的红线 = 特征提取实际使用的那条带
    屏幕上加粗的方框（K 键）= 当前 ROI 位置，直接叠在游戏画面上

操作：
    Tab 切换要校准的 ROI   方向键 移动   Shift+方向键 改大小
    K 显示/隐藏屏幕上的 ROI 框    S 保存校准   R 恢复默认
    C 清零计数    L 开始/停止记录 CSV    T 导入 md 轴文件    Esc 退出

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
                 poll_ms: int = 60, timeline_path: str = None):
        self.monitor = monitor
        self.calib_path = calib_path
        self.poll_ms = poll_ms
        self.deltas = {'bar': [0, 0, 0, 0], 'pause': [0, 0, 0, 0], 'speed': [0, 0, 0, 0]}
        self.calib_scale = None
        self.target = 'bar'
        self.overlay = None
        self._photos = {}
        # 打轴器：prompter 只管「当前帧号 -> 该看哪条轴」，这里只管画
        self.prompter = Teleprompter()
        self._timeline_path = None      # 当前轴来自哪个 md 文件；None = 来自面板输入框
        self._prom_flash_until = 0.0
        self._load_calib()

        self.root = tk.Tk()
        self.root.title('明日方舟 全局帧数计数器')
        self.root.configure(bg=UI_BG)
        self.root.attributes('-topmost', True)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry('724x1050+24+%d' % max(10, (sh - 1050) // 2))
        self.root.protocol('WM_DELETE_WINDOW', self.quit)

        self._build_ui()
        self._bind_keys()
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
        self.lb_perf = self._label(info, '性能 —', 9, UI_DIM)
        self.lb_perf.grid(row=2, column=0, columnspan=3, sticky='w', pady=(4, 0))
        self.lb_warn = self._label(info, '', 9, WARN)
        self.lb_warn.grid(row=3, column=0, columnspan=3, sticky='w')

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
        self.txt_axis = tk.Text(editor, height=7, bg='#0b0c0f', fg=UI_FG, relief='flat',
                                insertbackground=UI_FG, selectbackground='#333947',
                                font=('Consolas', 10), wrap='word', undo=True)
        self.txt_axis.pack(fill='both', expand=True, pady=(4, 0))
        self.txt_axis.insert('1.0', AXIS_HINT)
        # 在这两个键上返回 'break'，事件就不会继续冒泡到主窗口的快捷键
        self.txt_axis.bind('<Escape>', self._axis_escape)
        self.txt_axis.bind('<Control-Return>', self._axis_apply_key)

        btns = tk.Frame(self.root, bg=UI_BG)
        btns.pack(fill='x', **pad)
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

        self.lb_note = self._label(self.root, '', 10, UI_DIM)
        self.lb_note.pack(fill='x', padx=10, pady=(2, 0))
        self.lb_help = self._label(
            self.root,
            'Tab 切 ROI ｜ 方向键移动/Shift 缩放 ｜ K ROI 框 ｜ S 保存 ｜ R 默认 ｜ C 清零 ｜ '
            'L 记录 ｜ T 导入轴 ｜ 下框可直接写轴（Ctrl+Enter 应用）',
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

    def _set_note(self, text: str):
        self.lb_note.configure(text=text)

    def _tick(self):
        upd = self.monitor.latest
        st = self.monitor.stats
        if upd.result is not None:
            res = upd.result
            self.lb_frames.configure(text=str(res.elapsed))
            self.lb_time.configure(text='游戏内时间 %s' % _fmt_time(res.elapsed / 30.0))
            if res.mode == 'pause':
                self.lb_state.configure(text='状态 已暂停（不计数）', fg=WARN)
            elif res.mode.startswith('clock'):
                self.lb_state.configure(text='状态 运行中（挂钟估算）', fg=WARN)
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
        self._update_prompter(upd.result)
        self.lb_perf.configure(
            text='采样 %.1f Hz ｜ 抓屏 %.1f ms ｜ 采样 %d 次 ｜ 读不到累计 %.2f s'
                 % (st.hz, st.grab_ms, st.samples, st.unreadable_s))
        win = self.monitor.window
        lay = self.monitor.layout
        if win is not None and lay is not None:
            self._set_note('%s  %dx%d @(%d,%d) ｜ %s' % (
                win.exe, win.client[2], win.client[3], win.client[0], win.client[1],
                upd.note or 'ROI %s' % self.target))
        elif upd.note:
            self._set_note(upd.note)

        self._render_previews(upd, lay)
        if self.overlay is not None:
            self._draw_overlay()
        self.root.after(self.poll_ms, self._tick)

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
