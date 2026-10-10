"""把**屏幕上正在显示的一个窗口**当成帧来源 —— 「打开录屏 → 校准 → 计数」那条路。

和 video.py 的区别只有一处：帧从哪来。
    录像   ffmpeg 解码整段 -> 写缓存 -> 内存映射 -> 随便读第 i 帧（能拖进度条）
    窗口   抓屏一次就是一帧（屏幕上有什么就是什么，抓到哪一帧不由我们决定）

所以这里复用的正是「与来源无关」的那一层：自动对齐（align）、量条槽（calib）、搜按钮、
_fit / _reconcile_client / _check_buttons 全在 video.VideoSource 里，这里只覆盖几个钩子：
    __init__ / _acquire / _strip / _crop / _measure        （换帧来源）
    _profile_key / _profiles / _save_profile               （换一份校准表）
其余一行都不用改。

校准表为什么另开一份（window_calib.json）：窗口的 ROI 跟**进程名 + 客户区尺寸**绑定 ——
同一个尺寸下游戏和播放器的三个 ROI 完全不同，混在一张表里必然互相覆盖。

谁在用它：面板上「读取窗口… / 校准窗口」那条路。校准出来的 layout 交给
live.LiveMonitor 去采样计数 —— 采样、计数、打轴、CSV 全都还是实时那一条链路。
"""

from __future__ import annotations

import json
import os
import time
from collections import namedtuple
from dataclasses import replace

import numpy as np

from .align import DY_RANGE, DX_RANGE, offset_from_frames
from .calib import calibrate_video
from .features import read_bar
from .geometry import (BAR_ROI_H, BAR_W_UNIT, REF_H, REF_W, UI_SCALER_DEFAULT, Layout,
                       Rect, layout_for, layout_from_bar_box, reframe_layout,
                       ui_edge_scale)
from .live import LiveMonitor
from .paths import data_path
from .settings import DEFAULT_FRAMES_PER_COST
from .video import (CHECK_SAMPLES, MEASURE_FRAMES, ROI_NAMES, VideoSource, pad_layout)
from .win32 import (ScreenGrabber, arknights_ui_scaler, client_rect, set_topmost, to_gray)

WINDOW_PROFILE_NAME = 'window_calib.json'
WINDOW_SEARCH_FRAMES = 14     # 搜按钮 / 通用取帧时默认抓几张
GRAB_PAUSE = 0.004            # 两帧之间歇一下。1 倍速一个逻辑帧 33.3ms，隔 4ms 抓一张
                              # 已经能看出条在动；满速连抓只会把 CPU 烧掉。

# ---- 自动对齐：网格搜索 ----
# 窗口链路和录像链路最大的区别：录像的「画面」就是客户区（只差一个窗口边框量级的偏移），
# 播放器窗口里画面却被**缩放过**，客户区在窗口里的位置完全靠猜。实测 VLC 播放
# 2560x1600 的录屏、窗口客户区 1378x844 时：画面缩到 0.4777 倍，游戏客户区落在窗口里
# (85,50) 的 1218x688 —— 而 layout_for(1378,844) 的预测横向偏 59px、纵向偏 80px。
#
# 为什么是「网格」而不是「把半径一次开大」：offset_from_frames 的亮暗阈值是按**抽出来的
# 这一块**的 p10/p99 定的。半径一开大，块里绝大部分是背景（实测播放器窗口里费用条只占
# 0.4% 的像素），p99 掉到背景电平上 —— 阈值失效、阶跃消失，对齐整个崩（实测）。
# 所以每一格**单独抽一小块、单独定阈值**，在外面串成网格；格内仍是原来的 ±40/±120 微调。
WINDOW_GRID_R = 300          # 网格半径（客户区坐标），覆盖播放器缩放 / 黑边 / 标题栏
WINDOW_GRID_STEP = 60        # 步长要 < 2 x DX_RANGE，否则格子之间会漏掉条
GRID_SAMPLES = 16            # 网格搜索抓几帧
GRID_PAUSE = 0.08            # 抓帧间隔：16 x 0.08s 约 1.3 秒，1 倍速下条正好走一整个周期
GRID_TOP_N = 12              # 分数最高的这么多**个不同位置**再逐个做「在动」自检
# 「平台至少要占条槽宽的几成」这一条在窗口链路要放宽：分母是**预测**的条宽，而播放器
# 把画面缩放了多少倍是未知量（实测预测 129px、真条只有 114px）。放宽的代价是候选变多
# （播放器的音量条 / 进度条也会进来），但第二级的「条在不在动」会把它们全筛掉，
# 而候选本来就截到 GRID_TOP_N 个。
WINDOW_MIN_SUSTAIN_FRAC = 0.15

# ---- 自动校准的自检：这条到底是不是费用条 ----
# 判据只有一条：费用条是画面上**唯一**一种「左边缘固定、右端在长」的横线。
# 横线本身很好仿（播放器的音量条 / 进度条、任务栏那条亮线就有），但「动」仿不了 ——
# 实测那几条假线的阶跃分（step 1.00、after 1.00）比真条还整齐，只有「一个字都不动」
# 能把它们筛掉。
VERIFY_FRAMES = 12
VERIFY_PAUSE = 0.09          # 12 x 0.09s 约 1.1 秒：1 倍速下正好走完一费多一点
PROBE_FRAMES = 8             # 候选筛选用的短探针（12 个候选 x 0.35s 也不能太慢）
PROBE_PAUSE = 0.05
MIN_FILL_SWING = 0.12        # 填充宽度要变这么多（占条宽比例）才算「在动」
# 给 align/calib 用的「探测信息」替身：窗口没有文件可探，尺寸我们自己知道，
# duration=0 表示「没有整段时长」这个概念（stride 就按 1 解释）。
Info = namedtuple('Info', 'size fps duration')


# layout_from_bar_box 挪进 geometry.py：那是纯几何换算，录像链路
# （video.VideoSource 的手动框选）也要用同一份 —— 而 window.py 是 import
# video.py 的那一方，video 不能反过来 import window（循环导入）。
# 这里 import 进来是为了兼容：老的 `from akframe.window import ...` 照旧能用。
def rescaled_layout(layout: Layout, width: int, height: int) -> Layout:
    """窗口被改大改小后的**应急**布局：三个 ROI 按比例缩过去。

    为什么要有：采样线程每拍都会问「窗口现在多大」，尺寸一变它就要一份新布局 ——
    拿不到就会崩。比例缩放在「客户区跟着窗口等比变」时是对的（游戏窗口拉大就是这样），
    播放器窗口里不一定对，所以只是应急：面板会提示「窗口尺寸变了，要重新校准」，
    用户重校准或重新框选就恢复精确。
    """
    fx = float(width) / float(layout.width)
    fy = float(height) / float(layout.height)

    def scaled(r) -> Rect:
        x = max(0, min(int(round(r.x * fx)), width - 1))
        y = max(0, min(int(round(r.y * fy)), height - 1))
        w = max(1, min(int(round(r.w * fx)), width - x))
        h = max(1, min(int(round(r.h * fy)), height - y))
        return Rect(x, y, w, h)

    kw = {name: scaled(getattr(layout, name)) for name in ROI_NAMES}
    # 减号框不在 ROI_NAMES 里（那三个是抓屏通道），但它和它们一样锚在客户区右下角，
    # 窗口尺寸一变就得一起缩 —— 不缩的话它会留在旧位置上（偏多少 = 窗口差多少）。
    kw['sign'] = scaled(layout.sign)
    out = replace(layout, width=int(width), height=int(height),
                  scale=float(layout.scale) * (fx + fy) / 2.0, **kw)
    if layout.bar_width_frac > 0:
        out = replace(out, bar_width_frac=float(layout.bar_width_f) * fx)
    out.validate()
    return out


def window_profile_path(path: str = None) -> str:
    return path or data_path(WINDOW_PROFILE_NAME)


def load_window_profiles(path: str = None) -> dict:
    try:
        with open(window_profile_path(path), encoding='utf-8') as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_window_profile(key, client, offset, ui_scaler: float = 1.0, source: str = '',
                        note: str = '', layout: Layout = None, path: str = None) -> str:
    """把一份窗口 ROI 写进校准表（先写临时文件再换名，写一半断电不会毁掉整张表）。"""
    p = window_profile_path(path)
    profs = load_window_profiles(p)
    profs[str(key)] = {
        'client': [int(client[0]), int(client[1])],
        'offset': [int(offset[0]), int(offset[1])],
        'ui_scaler': float(ui_scaler),
        'source': source,
        'note': note,
        'layout': layout.to_dict() if layout is not None else None,
    }
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(profs, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def _size_of(client) -> tuple:
    """客户区尺寸。接受 (w, h)，也接受 win32.WindowInfo.client 那样的 (x, y, w, h)
    —— 那个四元组太容易顺手整个传进来，键就拼错了（实测踩过一次）。"""
    if len(client) >= 4:
        return int(client[-2]), int(client[-1])
    return int(client[0]), int(client[1])


def window_key(exe: str, client) -> str:
    """校准表的键：同一个进程 + 同一个客户区尺寸 = 同一份布局。"""
    w, h = _size_of(client)
    return '%s|%dx%d' % ((exe or '?').lower(), w, h)


def layout_for_window(exe: str, client, path: str = None):
    """窗口校准表里那份 ROI；没有、或者尺寸对不上就返回 None。"""
    prof = load_window_profiles(path).get(window_key(exe, client))
    if not prof or not prof.get('layout'):
        return None
    try:
        lay = Layout.from_dict(prof['layout'])
    except (KeyError, TypeError, ValueError):
        return None
    w, h = _size_of(client)
    if lay.width != w or lay.height != h:
        return None
    return lay


def default_ui_scaler(exe: str) -> float:
    """读某个窗口时该按哪一档「UI 缩放」推 ROI。

    窗口里那一帧是**画出来的像素**，而 UI 缩放是「画它的那个游戏」当时的档位：
      * 游戏窗口 —— 就是注册表里这一档（和实时链路同一个来源，见 win32.arknights_ui_scaler）；
      * 播放器窗口里放的是录屏 —— 那是**录制那一刻**的档位，同一个人同一套设置，一般还是它；
      * 认不出是明日方舟的窗口（比如别的游戏的录屏）—— 只能 1.0（= 不缩放）。

    这一档只影响**预测**（自动对齐的起点、框选时反推客户区），三个 ROI 后面一律按实测
    像素钉死，所以取错档最坏只是「按钮那两路搜不到」（费用条照旧准），不会把读数带偏。
    """
    if not exe or 'arknights' not in exe.lower():
        return UI_SCALER_DEFAULT
    try:
        return arknights_ui_scaler()
    except Exception:                          # noqa: BLE001  读注册表失败不该拦住校准
        return UI_SCALER_DEFAULT


def build_window_layout(exe: str, width: int, height: int, fallback: Layout = None,
                        ui_scaler: float = None, path: str = None) -> Layout:
    """窗口链路要的 ROI，三级取：
        1. window_calib.json 里这个 (进程名, 客户区尺寸) 的那份 —— 框选或自动校准存下的，
           这是唯一「准」的来源，读游戏窗口和读播放器窗口都靠它；
        2. fallback（上一次那份）按比例缩过去 —— 窗口被拉大拉小先顶上，是**应急**：
           播放器窗口里画面缩放不一定跟着窗口等比走，面板要提示重新框选；
        3. 游戏分辨率表 —— 真的一次都没校准过时才落到这里（读的是游戏窗口就对，
           读播放器窗口基本是错的，所以这时面板要提示框选）。
    """
    lay = layout_for_window(exe, (width, height), path)
    if lay is not None:
        return lay
    if fallback is not None and fallback.width > 0 and fallback.height > 0:
        return rescaled_layout(fallback, int(width), int(height))
    scaler = default_ui_scaler(exe) if ui_scaler is None else float(ui_scaler)
    return layout_for(int(width), int(height), scaler)


class WindowMonitor(LiveMonitor):
    """读一个**窗口**的实时链路：游戏窗口，或者播放器里正在放的一段录屏。

    和 LiveMonitor 只差一件事：ROI 从 window_calib.json 来（键 = 进程名 + 客户区尺寸），
    不是从游戏分辨率表来。采样、计数、打轴、CSV 一行都不用改 —— 「帧从哪来」和
    「怎么数」分开写，收益在这里兑现第二次（第一次是录像链路 video.VideoMonitor）。

    为什么另开一份校准表（window_calib.json）：同一个客户区尺寸下，游戏窗口和播放器窗口
    的三个 ROI 差着几百像素（播放器里画面还被缩放过），混在一张表里必然互相覆盖。

    谁在用它：面板上「读取窗口…」那条路（ui.MonitorApp.read_window）。
    典型用法：
        src = WindowSource(win, ...)          # 先校准（后台线程里）
        mon = WindowMonitor(win, source=src, layout=src.auto_layout)
        mon.start()                           # 用户点「开始计数」才起线程
    """

    def __init__(self, win, *, source=None, layout: Layout = None, rate: float = 120.0,
                 templates=(), csv_path: str = None,
                 cost_rate=DEFAULT_FRAMES_PER_COST, ui_scaler: float = None,
                 profiles_path: str = None, negative: str = 'auto'):
        # 先摆好自己的字段：LiveMonitor.__init__ 里会调 build_layout，那个要读它们
        self.src = source                  # 校准用的 WindowSource（没有就是 None）
        self.calib = None                  # 校准表 / 分辨率表给的那份（不含人工微调）
        # 用哪张校准表：None = data_path 里那份（正常用），检查脚本给临时文件
        self.profiles_path = profiles_path
        scaler = default_ui_scaler(win.exe) if ui_scaler is None else float(ui_scaler)
        super().__init__(None, exe=win.exe, pid=win.pid, rate=rate,
                         templates=templates, csv_path=csv_path, cost_rate=cost_rate,
                         ui_scaler=scaler, build_layout=self._layout_for_size,
                         negative=negative)
        self.window = win
        self.set_layout(layout if layout is not None
                        else self._layout_for_size(win.client[2], win.client[3]))

    def _layout_for_size(self, width: int, height: int) -> Layout:
        """窗口**尺寸**变了（或被「重新校准」问到）时算一份新 ROI。

        窗口被拖走不用重算 —— 采样每一拍都重新问「客户区在屏幕上哪儿」（_resolve_window），
        跟随移动是自动的。只有尺寸变了才需要新布局，而那时只能按比例缩放应急
        （见 rescaled_layout）：播放器窗口里画面的缩放比例不一定跟着窗口等比走，
        所以面板要提示「尺寸变了，重新框选一次」。
        """
        self.calib = build_window_layout(self.exe, width, height, fallback=self.calib,
                                        ui_scaler=self.ui_scaler,
                                        path=self.profiles_path)
        return self.calib

    def base_layout(self) -> Layout:
        """没叠人工微调的布局（面板方向键的基准，见 ui.MonitorApp._base_layout）。

        给的是**校准表里那份**，不是面板上可能已经被挪过的 self.layout —— 否则按一下
        方向键、再按回去会越挪越远（和 VideoMonitor.base_layout 一个道理）。
        """
        return self.calib if self.calib is not None else self.layout

    def save_current(self, layout: Layout = None, source: str = '面板校准',
                     note: str = '') -> str:
        """把**当前**这份 ROI 写进 window_calib.json（面板「保存校准」在窗口模式走这里）。

        存的是「最后真正在用的」那份 —— 用户可能已经按方向键把它挪过几个像素。
        下次读同一个进程 + 同一个尺寸的窗口就直接照它读，连自动对齐都不用跑
        （语义和 VideoSource.save_current 完全一致）。
        """
        lay = layout if layout is not None else self.layout
        if lay is None:
            raise ValueError('还没有布局可存')
        off = getattr(self.src, 'offset', None) or (0, 0)
        return save_window_profile(window_key(self.exe, (lay.width, lay.height)),
                                   (lay.width, lay.height), off, self.ui_scaler,
                                   source=source, note=note, layout=lay,
                                   path=self.profiles_path)

    def pin(self, on: bool = True) -> bool:
        """把目标窗口钉到最上层 —— 被盖住就等于读不到（见 win32.set_topmost）。"""
        return bool(self.window is not None and set_topmost(self.window.hwnd, on))

    def describe(self) -> str:
        w = self.window
        if w is None:
            return '窗口（还没找到）'
        out = ('窗口 %s ｜ 客户区 %dx%d @(%d,%d)'
               % (w.exe, w.client[2], w.client[3], w.client[0], w.client[1]))
        src = self.src
        if src is not None and getattr(src, 'client', None):
            out += ' ｜ 画面里的游戏客户区 %dx%d @(%d,%d)' % (
                src.client[0], src.client[1], src.offset[0], src.offset[1])
        return out


class WindowSource(VideoSource):
    """抓屏版的帧来源：拿一个窗口的**客户区**当画面。

    造出来之后 `src.auto_layout` 就是那份对好位的三个 ROI，交给
    live.LiveMonitor 去按它采样计数（这个对象自己不跑采样循环）。

    注意：它读的是**屏幕上画出来的东西**，所以
      * 目标窗口不能被别的窗口挡住（挡住的那块抓到的是挡它的东西）；
      * 目标窗口最小化 / 关掉就抓不到 —— 和读游戏窗口是一回事。
    """

    profile_name = WINDOW_PROFILE_NAME

    def __init__(self, win, *, ui_scaler=None, templates=(), progress=None,
                 use_profile=True, save_on_auto=True, snap=True, client=None,
                 offset=None, profile_path: str = None):
        # 不走 VideoSource.__init__：那条路是「文件名 -> ffprobe -> 抽整段流」，
        # 窗口这三件事一件都没有。
        self.window = win
        # 用哪张校准表。None = data_path 里那份（正常用）；检查脚本给一个临时文件，
        # 免得把用户真正的 window_calib.json 写花。
        self.profile_path = profile_path
        self.hwnd = int(win.hwnd)
        self.path = '<窗口 %s>' % (win.title or win.exe or win.hwnd)
        self.ffmpeg = None
        self._progress = progress or (lambda stage, frac, note='': None)
        self.templates = list(templates)
        self.save_manual = False
        self.notes = []
        self.measured = None
        self.buttons_ok = True
        self.size = (int(win.client[2]), int(win.client[3]))
        self.fps = 60.0
        self.duration = 0.0
        self.frames_est = WINDOW_SEARCH_FRAMES    # 让搜按钮那一步的 stride 算成 1
        self.ui_scaler = (default_ui_scaler(win.exe) if ui_scaler is None
                           else float(ui_scaler))
        self.info = Info(self.size, self.fps, 0.0)
        self.frames = CHECK_SAMPLES               # 名义帧数：_check_indexes 按它铺开取样
        self.cache_dir = None
        self._mmaps = {}
        self._grabber = None
        # 这份 ROI 靠不靠得住：自动校准要量完再自检（见 _verify_bar），
        # 手动框选是「用户自己指的」，直接算靠得住。
        self.verified = False
        self.manual = False
        self._prepare(client, offset, snap, use_profile, save_on_auto, True)

    def _prepare(self, client, offset, snap, use_profile, save_on_auto, force,
                 cache_dir=None):
        # 进度：基类在收尾时会喊一声 'ready'，可窗口链路后面还有一步自检没跑完 ——
        # 那一句会让面板先跳到「就绪」再退回来。按住它，等自检完了自己再喊一声。
        self._hold_ready = True
        try:
            super()._prepare(client, offset, snap, use_profile, save_on_auto, force,
                             cache_dir)
        finally:
            self._hold_ready = False
        if self.manual:
            self.verified = True
            self._say('ready', 1.0, self.describe())
            return
        self._say('verify', -1.0, '自检：费用条在不在动（拓 %d 帧）…' % VERIFY_FRAMES)
        self.verified, why = self._verify_bar()
        self.notes.append(('自动校准自检通过：%s' % why) if self.verified else
                          '⚠ 自动校准没把握：%s。请点「框选费用条」手动圈一下费用条 ——'
                          '比在缩放过的窗口里猜位置靠谱' % why)
        self._say('ready', 1.0, self.describe())

    def _say(self, stage, frac, note=''):
        """基类收尾那句 'ready' 要先按住（自检还没跑完），其余照喊。"""
        if stage == 'ready' and getattr(self, '_hold_ready', False):
            return
        super()._say(stage, frac, note)

    def _verify_bar(self) -> tuple:
        """抓一小段，看费用条那一条带上的填充宽度有没有在变。

        判据只有一条：费用条是画面上**唯一**一种「左边缘固定、右端在长」的横线。
        横线本身很好仿（播放器的音量条、任务栏那条亮线就有），但「动」仿不了。
        代价：画面里费用条本来就没在动（游戏暂停着、录像停在暂停段落）时判不过 ——
        这时提示用户让画面跑起来再「重新校准」，或者直接框选。
        """
        swing, why = self._fill_swing(self.layout)
        if swing >= MIN_FILL_SWING:
            return True, '费用条在动（%s，动幅 %.0f%% 条宽）' % (why, 100.0 * swing)
        return False, ('这 %d 帧里 %s —— 要么画面里费用条没在动，要么量到的不是费用条'
                       % (VERIFY_FRAMES, why))

    def manual_layout(self, box: Rect, ui_scaler: float = None):
        """用户框出来的费用条 -> 三个 ROI，并写进校准表。返回 (layout, 提示)。

        框完立刻做两件事：按模板把右上角两个按钮搜一遍（搜不到就关掉那两条通道，
        费用条不受影响）；把这份 ROI 记进 window_calib.json（下次开同一个进程 + 同尺寸
        的窗口直接套用，连自动校准都不用跑）。
        """
        scaler = self.ui_scaler if ui_scaler is None else float(ui_scaler)
        self._say('buttons', -1.0, '按框选钉住费用条：搜右上角两个按钮…')
        client, offset, layout = layout_from_bar_box(self.size, box, scaler)
        layout = self._fit_buttons_coarse(layout, client, offset)
        self.client, self.offset = client, offset
        self.layout = layout
        self.auto_layout = layout          # 面板手动微调的基准（见 VideoSource.adopt_layout）
        self.padded, self.pad_off = pad_layout(layout)
        self.manual = True
        self.verified = True
        note = ('已按框选钉住费用条：x=%d 宽 %d px（反推客户区 %dx%d @(%d,%d)）'
                % (layout.bar.x, layout.bar.w, client[0], client[1],
                   offset[0], offset[1]))
        self.notes.append(note)
        try:
            self._save_profile(self._profile_key(), client, offset, '手动框选费用条',
                               '%dpx 的条槽' % layout.bar.w, layout=layout)
            self.notes.append('已记进 %s（下次开同尺寸的窗口直接套用）' % self.profile_name)
        except OSError as exc:
            self.notes.append('校准表写不进去（%s）' % exc)
        return layout, note

    # ---------- 帧来源 ----------

    def _origin(self):
        """客户区左上角在屏幕上的位置。每次都重新问 —— 窗口会被拖走 / 改大小。"""
        x, y, _w, _h = client_rect(self.hwnd)
        return x, y

    def _grab(self, rect: Rect) -> np.ndarray:
        """抓窗口客户区坐标下的这一块，返回灰度图（越界部分夹回客户区内）。"""
        if self._grabber is None:
            self._grabber = ScreenGrabber()
        cw, ch = self.size
        x = max(0, min(int(rect.x), cw - 1))
        y = max(0, min(int(rect.y), ch - 1))
        w = max(1, min(int(rect.w), cw - x))
        h = max(1, min(int(rect.h), ch - y))
        ox, oy = self._origin()
        return to_gray(self._grabber.grab_bgra(ox + x, oy + y, w, h))

    def _acquire(self, force: bool, cache_dir: str = None) -> None:
        """窗口没有「抽流」这一步：帧是抓的时候才有的，建个抓屏器就够。"""
        self._grabber = ScreenGrabber()

    def _strip(self, rect: Rect, stride: int = 1, frames: int = None,
               seek: float = None) -> np.ndarray:
        """连着抓 frames 张。stride / seek 对窗口没有意义（帧不是我们挑的），
        只为接口和录像链路一致（基类的 _verify_layout 会带 seek 调它）。"""
        n = max(2, int(frames or WINDOW_SEARCH_FRAMES))
        out = np.empty((n, rect.h, rect.w), np.uint8)
        for i in range(n):
            out[i] = self._grab(rect)
            if GRAB_PAUSE:
                time.sleep(GRAB_PAUSE)
        return out

    def _crop(self, name: str, i: int, dy: int) -> np.ndarray:
        """_check_buttons 要的那一小块：当前这一瞬的画面，竖直再偏 dy。"""
        r = getattr(self.layout, name)
        return self._grab(Rect(r.x, r.y + dy, r.w, r.h))

    def _measure(self, client, offset, scaler):
        """量条槽：抓两轮。抓得越久越可能碰上「费用条正在动」的那一段 ——
        窗口没法「跳到录像后面看看」（录像那条链路是靠 stride 扫整段的）。"""
        for n in (MEASURE_FRAMES, MEASURE_FRAMES * 3):
            self._say('measure', -1.0, '量费用条槽（抓 %d 帧画面）…' % n)
            rep = calibrate_video(self.path, self.ffmpeg, client_size=client,
                                  ui_scaler=scaler, size=self.size, frames=n,
                                  offset=offset, stride=1,
                                  strip_fn=self._strip, info=self.info)
            if rep is not None:
                return rep
        return None

    def _raw_frame(self, i: int):
        return tuple(self._grab(getattr(self.layout, n)) for n in ROI_NAMES)

    def frame(self, i: int):
        return self._raw_frame(i)

    def __len__(self) -> int:
        return int(self.frames)

    def close(self) -> None:
        if self._grabber is not None:
            try:
                self._grabber.close()
            except Exception:                  # noqa: BLE001  关不掉也不能拦住退出
                pass
            self._grabber = None

    # ---------- 校准表 ----------

    def _grab_stack(self, n: int, pause: float) -> np.ndarray:
        """一次抓 n 帧**整窗**画面（灰度栈）。网格搜索共用这一份，不用每格重抓。"""
        if self._grabber is None:
            self._grabber = ScreenGrabber()
        w, h = self.size
        out = np.empty((max(1, int(n)), h, w), np.uint8)
        for i in range(out.shape[0]):
            ox, oy = self._origin()
            out[i] = to_gray(self._grabber.grab_bgra(ox, oy, w, h))
            if pause:
                time.sleep(pause)
        return out

    def _fill_swing(self, layout: Layout, frames: int = None,
                    pause: float = None) -> tuple:
        """抓几帧，看这份 ROI 里的填充宽度变没变。返回 (变化量/条宽, 说明)。

        「变化量 / 条宽」就是「这条在不在动」：真费用条一秒走完一整个周期（1 倍速），
        所以哪怕只抓半秒，宽度也会变一大截；而播放器的音量条、进度条的读数是一根直线。
        """
        n = VERIFY_FRAMES if frames is None else int(frames)
        dt = VERIFY_PAUSE if pause is None else float(pause)
        if layout is None or layout.bar is None:
            return 0.0, '没有 ROI'
        fills = []
        for _ in range(max(2, n)):
            fills.append(int(read_bar(self._grab(layout.bar), layout).fill))
            if dt:
                time.sleep(dt)
        lo, hi = min(fills), max(fills)
        span = max(1.0, layout.bar_width_f)
        return (hi - lo) / span, '填充 %d..%d px / 条宽 %.0f px' % (lo, hi, span)

    def _detect_offset(self, client):
        """自动对齐：在 (dx, dy) 网格上一格一格试，再用「条在不在动」定谁是真的。

        两级：
          1. 每一格按 layout_for(客户区) 的预测整体平移，抽出**条那么大**的一小块
             （±DX_RANGE / ±DY_RANGE 微调），跑 offset_from_frames 拿阶跃分
             （step = 左边缘的陡峭度，after = 边缘右边确实被填充盖住的比例）；
          2. 分数最高的若干个**不同位置**，各抓几帧看填充宽度有没有在变 —— 通过的那
             一个才是费用条。

        第 2 步不是锦上添花：实测播放器的音量条在网格里拿到的阶跃分是 step 1.00 /
        after 1.00（比真条还整齐），位置也很「稳定」，只有「一个字都不动」能把它筛掉。
        """
        cw, ch = int(client[0]), int(client[1])
        lay = layout_for(cw, ch, self.ui_scaler)
        bar = lay.bar
        stack = self._grab_stack(GRID_SAMPLES, GRID_PAUSE)
        fh, fw = int(stack.shape[1]), int(stack.shape[2])
        cands = {}
        for gy in range(-WINDOW_GRID_R, WINDOW_GRID_R + 1, WINDOW_GRID_STEP):
            for gx in range(-WINDOW_GRID_R, WINDOW_GRID_R + 1, WINDOW_GRID_STEP):
                bx, by = bar.x + gx, bar.y + gy
                x0 = max(0, bx - DX_RANGE)
                y0 = max(0, by - DY_RANGE)
                x1 = min(fw, bx + bar.w + DX_RANGE)
                y1 = min(fh, by + bar.h + DY_RANGE)
                if x1 - x0 < 16 or y1 - y0 < 8:
                    continue
                cell = replace(lay, bar=Rect(bx, by, bar.w, bar.h))
                found = offset_from_frames(stack[:, y0:y1, x0:x1], cell, x0, y0,
                                           DX_RANGE, DY_RANGE, WINDOW_MIN_SUSTAIN_FRAC)
                # 弱阶跃（小于 align.MIN_STEP）在 offset_from_frames 里就已经被丢掉，
                # 这里只剩「找到没找到」。
                if found is None:
                    continue
                dx, dy, step, after, plat = found
                # 同一处「亮线」会被相邻好几格同时找到，报出来的偏移也几乎一样 ——
                # 按 24px 归并，同一个目标只留分最高的那一格。
                key = (int(round((gx + dx) / 24.0)), int(round((gy + dy) / 24.0)))
                rank = step * after
                if key not in cands or rank > cands[key][0]:
                    cands[key] = (rank, gx + dx, gy + dy, step, after, plat)
        order = sorted(cands.values(), key=lambda t: -t[0])[:GRID_TOP_N]
        if not order:
            self.notes.append('自动对齐：网格里一个像样的横线都没找到')
            return None
        self._say('align', -1.0, '网格找到 %d 个候选，正在挨个看「条在不在动」…'
                  % len(order))
        for i, (rank, dx, dy, step, after, plat) in enumerate(order):
            probe = reframe_layout(lay, dx, dy, fw, fh)
            swing, why = self._fill_swing(probe, PROBE_FRAMES, PROBE_PAUSE)
            if swing >= MIN_FILL_SWING:
                self.notes.append('自动对齐：第 %d 个候选通过（位移 %+d,%+d，阶跃 %.2f/%.2f，'
                                  '%s，在动 %.0f%% 条宽）'
                                  % (i + 1, dx, dy, step, after, why, 100.0 * swing))
                return (int(dx), int(dy), float(step), float(after), int(plat))
        self.notes.append('自动对齐：%d 个候选的填充宽度都没在变（%s）—— 画面里费用条'
                          '要么没在动，要么量不到' % (len(order), order[0][5]))
        return None

    def _profile_key(self) -> str:
        return window_key(self.window.exe, self.size)

    def _profiles(self) -> dict:
        return load_window_profiles(self.profile_path)

    def _save_profile(self, key, client, offset, source, note, layout=None) -> str:
        return save_window_profile(key, client, offset, self.ui_scaler,
                                   source=source, note=note, layout=layout,
                                   path=self.profile_path)

    def describe(self) -> str:
        return ('窗口 %s ｜ 客户区 %dx%d ｜ 游戏客户区 %dx%d @(%d,%d)'
                % (self.path, self.size[0], self.size[1], self.client[0],
                   self.client[1], self.offset[0], self.offset[1]))
