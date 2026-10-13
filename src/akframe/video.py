"""录像输入：把一段录屏变成帧源，喂给**和实时链路同一个**状态机。

用户的第 3 条需求（「多样接口」）：不光能对着游戏窗口数帧，也能拿一段录像数帧 ——
而且面板上看到的要和实时一模一样（大数字、三个 ROI 预览、打轴器、CSV 都是同一套代码）。

做法是把「帧从哪来」换掉：实时链路每拍 BitBlt 抓一次屏幕，这里改成从录像里读第 i 帧。
于是链路其余部分（features.read_bar / read_pause / read_speed、state.Counter、
ui.MonitorApp）一行都不用为录像改动 —— 本类继承 LiveMonitor，只覆盖 _sample()，
线程模型、看门狗心跳、统计、CSV、清零、手动暂停全部照旧（demo.py 已证明这条路可行）。

和实时有三处本质差别，都必须处理对：

1. **ROI 从哪来**。实时：游戏窗口的客户区矩形 -> geometry.layout_for(客户区尺寸)。
   录像：画面里是整块桌面或一个窗口，客户区原点未知，而且可能是别人在别的机器上录的。
   所以这里自带一套校准（见 _calibrate）：
     * 自动对齐（align.detect_offset）先把费用条放到预测位置附近；
     * 再用「白块并集」量出条槽的**真实像素**（calib.measure_slot）钉死费用条 ROI
       —— 费用条的相位全靠它的横向位置，差 8px（= 1 个逻辑帧）就会让「一费 30 帧」
       变成 29 或 31；
     * 量出来的条宽还顺便告诉我们「客户区到底多大」（录像=客户区 这个默认猜测
       在桌面录像里是错的）。
   校准结果按**画面尺寸**存进 video_calib.json：同一台机器下次开同尺寸的录像直接套用
   （用户本机所有桌面录像都是 2560x1600，等于只解一次）。

2. **时间戳**。实时的 t 来自挂钟（perf_counter）；录像的 t = 帧号 / fps，也就是
   「录像本身的游戏内时间」。两个直接好处：
     * 计数结果**与播放速度无关**：播放倍速只决定看得多快，喂进状态机的 t 不变；
     * 随便拖进度条：拖到第 k 帧 = 把 0..k 帧重新喂一遍状态机（特征有缓存，很快），
       得到的就是「如果从头放到这里」的那个帧号。
   满费挂钟通道用的是相邻两帧 t 之差，所以播放暂停时它自然停住 —— 不会像实时链路那样
   需要靠「读不到画面」去猜这段时间该不该算。

3. **播放**。屏幕天然实时，录像得自己按挂钟推进：每拍算出「按当前播放倍速现在该播到
   第几帧」，把中间的帧一帧一帧喂进状态机（和离线 reader 的做法一致，所以离线验证过的
   结论在这里同样成立）。一拍的批处理有上限（MAX_BATCH），机器慢就多花几拍追上，
   既不会卡住界面，也不会丢帧。

代码复用关系：抽流用 capture.extract（一次 ffmpeg 把三路 ROI 写成原始灰度流，之后
随机读），对齐用 align，量条槽用 calib，读数用 features，状态机用 state。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import replace
from typing import Callable

import numpy as np

from .align import detect_offset, extract_strip
from .calib import MIN_RUN, PAD_Y, calibrate_video
from .capture import extract, find_ffmpeg, probe_video
from .features import CORR_2X_MIN, fill_run, read_bar, read_pause, read_speed
from .geometry import (BAR_BAND_TOP, BAR_CENTER_FROM_BOTTOM_UNIT, BAR_ROI_H,
                       BAR_W_UNIT, BTN_TOP_UNIT, REF_W, Layout, Rect, layout_for,
                       layout_from_bar_box, reframe_layout, ui_edge_scale)
from .live import LiveMonitor, LiveUpdate
from .paths import app_dir
from .settings import DEFAULT_FRAMES_PER_COST

ROI_NAMES = ('bar', 'pause', 'speed')

# 抽流时每个 ROI 往外扩多少像素。费用条扩得少（只要挡住「预测差一两像素」）；
# 右上角两个按钮扩得多，是为了**手动**微调留余量（面板上按 Tab + 方向键挪），
# 不用重新解码整段录像。真正的粗对位在抽流**之前**做，见 _fit_buttons_coarse。
PAD_BAR = 16
PAD_BTN = 48

# 右上角按钮的粗对位（抽流之前，见 _fit_buttons_coarse）：
# 只解十几帧、只解右上角一块，代价约 1 秒；换来的是整段录像的暂停 / 倍速两条通道可用。
BTN_SEARCH_FRAMES = 14      # 抽多少帧来搜
BTN_DX = 40                 # 横向搜索半径：按钮横坐标由客户区宽度定死，只留模型误差
BTN_DY_LO = -140            # 纵向搜索范围：实测差 45px（窗口标题栏），两头都多留一倍余量
BTN_DY_HI = 300
BTN_COARSE = 4              # 先粗搜（步长 4px）再精搜（步长 1px）
BTN_MIN_HITS = 2            # 至少要这么多帧命中模板才算搜到
BTN_STRONG_CORR = 0.85      # 或者平均相关高到这个程度（数字只出现过一两帧也认）

# 量条槽只解前多少帧：1 倍速下 240 帧 = 8 秒 = 8 个费用周期，足够「白块并集」量出条槽。
# 开头是菜单 / 加载动画的录像量不出来时会再放大采样步长重试（见 _measure）。
MEASURE_FRAMES = 240
MEASURE_STRIDE = 12

# 实测条宽和预测条宽差多少就认为「客户区猜错了」。4% 是「差一档缩放」的判据：
# 2560x1600 的录像里若是 1920x1200 的窗口，预测 240px、实测 180px，差 25%；
# 同尺寸下预测和实测只差 1~2px。
WIDTH_TOL = 0.04
# 条槽左边缘和预测差多少像素就算「客户区原点没猜对」。实测录像里这个差是 9px
# （2560x1600 上预测的条槽左边缘比真实位置偏右 9px），而同尺寸下不会超过 2px。
LEFT_TOL = 4

# 一拍最多喂多少帧：全速档靠它「既不停顿也不丢帧」。2000 帧约 0.4~0.8 秒的活，
# 放在采样线程里做，界面（主线程）照常刷新。
MAX_BATCH = 2000

PROFILE_NAME = 'video_calib.json'
# 右上角按钮的自检取样帧数（抽完流之后拿真数据验一遍 ROI 有没有对准）
CHECK_SAMPLES = 16

# ---- 「校准表里那份 ROI 到底对不对」的自检 ----
#
# 为什么非要有这一步（实测踩过，整条录像链路因此全废）：校准表按**画面尺寸**归档，
# 可画面尺寸和游戏窗口的位置/大小根本没关系 —— 用户本机所有桌面录像都是 2560x1600，
# 但录的时候游戏一次是 1920x1200 @(627,198)、一次是 1920x1200 @(592,132)、OBS 那两次
# 又不一样。表里那条「上一轮量准的 ROI」套到下一段录像上就是错位的，而快路径
# 「表里有 ROI 就照抄、跳过量条槽」不做任何校验，于是每段录像都只数出 0~52 帧
# （实测：2904 帧的录像数出 3 帧、1262 帧的数出 0 帧 —— 用户看到的就是「完全识别不到」）。
#
# 判据只用一条硬事实：layout 的约定是「bar.x = 条槽左端」，而费用条的填充是从条槽
# 左端开始长的 —— 所以**白块最左那一列应当压在 ROI 的左边缘上**。错位的 ROI 要么
# 根本没有白块，要么白块起点差几十像素，一眼就能否掉。
VERIFY_SEEKS = (0.0, 0.5, 0.9)   # 在整段的这几个位置各取一小段来验（开头常是菜单）
VERIFY_FRAMES = 8                # 每个位置取几帧
VERIFY_LPAD = 12                 # 验的时候往 ROI 左边多取几列：白块在左边缘哪一侧才判得出
VERIFY_LEFT_TOL = 5              # 白块起点离 ROI 左边缘超过这么多像素 = ROI 没钉在条槽上
VERIFY_JITTER = 3.0              # 白块左端的抖动上限（像素）
VERIFY_MIN_HITS = 2              # 一小段里至少这么多帧看得见白块才有结论
SNAP_LPAD = 40                   # 「吸附左端」时往左多看的列数（= 允许的最大回移量）
SNAP_MAX = 24                    # 白块起点离框左端超过这么多像素就不敢吸（多半框错了）




def profile_path() -> str:
    return os.path.join(app_dir(), PROFILE_NAME)


def load_profiles(path: str = None) -> dict:
    """读录像校准表：{'2560x1600': {'client': (w,h), 'offset': (x,y), ...}}。

    文件不存在 / 坏掉 / 某条不合法一律跳过（宁可重新解一遍，也不要因为一个坏文件
    让录像打不开）。按**画面尺寸**归档：同一台机器录出来的尺寸是固定的，
    客户区位置也是固定的，所以解一次就够了。
    """
    p = path or profile_path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, rec in raw.items():
        try:
            entry = {
                'client': (int(rec['client'][0]), int(rec['client'][1])),
                'offset': (int(rec['offset'][0]), int(rec['offset'][1])),
                'ui_scaler': float(rec.get('ui_scaler') or 1.0),
                'source': str(rec.get('source') or ''),
                'note': str(rec.get('note') or ''),
                'layout': None,
            }
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        lay = rec.get('layout')
        if isinstance(lay, dict):
            try:
                Layout.from_dict(lay).validate()
            except (KeyError, IndexError, TypeError, ValueError):
                lay = None      # 坏记录当没有：宁可重新量一遍，也别拿它去读像素
            entry['layout'] = lay
        out[str(key)] = entry
    return out


def save_profile(key: str, client, offset, ui_scaler: float = 1.0,
                 source: str = '', note: str = '', path: str = None,
                 layout=None) -> str:
    """写一条录像校准记录（按画面尺寸归档）。返回写到的文件名。

    layout 是**量准之后的那三个 ROI 矩形**（画面坐标）。有它，下次开同尺寸的录像就能
    跳过「自动对齐 + 量条槽」那几十秒，直接抽流；没有它就只记客户区，下次还得重量一遍。
    """
    p = path or profile_path()
    table = load_profiles(p)
    table[str(key)] = {'client': (int(client[0]), int(client[1])),
                       'offset': (int(offset[0]), int(offset[1])),
                       'ui_scaler': float(ui_scaler or 1.0),
                       'source': source, 'note': note,
                       'layout': (layout.to_dict() if hasattr(layout, 'to_dict') else layout)}
    data = {}
    for k, v in table.items():
        item = {'client': list(v['client']), 'offset': list(v['offset']),
                'ui_scaler': v['ui_scaler'], 'source': v['source'], 'note': v['note']}
        if v.get('layout'):
            item['layout'] = v['layout']
        data[k] = item
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    os.replace(tmp, p)
    return p


def cache_dir_for(video: str) -> str:
    """抽流缓存目录。按**绝对路径**取哈希：换机器 / 改文件名都算另一段录像。

    为什么不把 ROI 编进目录名（离线 CLI 就是那么做的）：这里改一次 ROI 就得重抽，
    编进名字会留下一堆几十上百 MB 的兄弟目录；ROI 已经写在 meta.json 里，
    extract 自己会发现不一致并重抽。
    """
    stem = os.path.splitext(os.path.basename(video))[0]
    stem = re.sub(r'[^\w]+', '_', stem)[:40].strip('_') or 'video'
    h = hashlib.md5(os.path.abspath(video).encode('utf-8')).hexdigest()[:8]
    return os.path.join(app_dir(), 'out', 'cache', 'v_%s_%s' % (stem, h))


def bar_probe(layout: Layout, lpad: int = VERIFY_LPAD) -> tuple:
    """验 / 吸附费用条时取的框，以及「ROI 左边缘」在框里的列号。

    往左多取 lpad 列：白块起点是压在 ROI 左边缘上、还是差十几像素，只有把左边缘
    左边那几列也看进来才判得出来（见 judge_bar_layout）。
    """
    x0 = max(0, int(layout.bar.x) - int(lpad))
    rect = Rect(x0, layout.bar.y, min(layout.width, layout.bar.x2 + 1) - x0, layout.bar.h)
    return rect, int(layout.bar.x) - x0


def bar_left_offsets(strip: np.ndarray, layout: Layout, left_col: int) -> np.ndarray:
    """每帧「白块左端」相对 ROI 左边缘的偏移（看不见白块 -> nan）。

    判据和 read_bar 一致：取条带行（ROI 第 5..12 行）的中位数当这一帧的剖面，再找
    最长的一段连续白。strip 的列 left_col 就是 ROI 的左边缘。
    """
    r0, r1 = layout.bar_band
    out = []
    for img in strip:
        row = np.median(img[r0:r1 + 1, :], axis=0).astype(np.int16)
        run = fill_run(row)
        if run is None or run[1] - run[0] + 1 < MIN_RUN:
            out.append(np.nan)
        else:
            out.append(float(run[0] - left_col))
    return np.array(out, dtype=np.float64)


def judge_bar_layout(strip: np.ndarray, layout: Layout, left_col: int) -> tuple:
    """白块左端压在 ROI 左边缘上吗？返回 (True/False/None, 说明)。None = 没法判断。

    None 只在「这一小段里一帧白块都没看见」时出现（菜单 / 剧情 / 条全程满费 —— 那种
    画面里没有可以对照的东西），这时不能据此否掉一份 ROI。
    """
    offs = bar_left_offsets(strip, layout, left_col)
    got = offs[np.isfinite(offs)]
    if got.size < VERIFY_MIN_HITS:
        return None, '这一小段里没看见白块（%d/%d 帧）' % (int(got.size), offs.size)
    med = float(np.median(got))
    mad = float(np.mean(np.abs(got - med)))
    if abs(med) > VERIFY_LEFT_TOL:
        return False, '白块左端离 ROI 左边缘 %+.0fpx（应当在 0 附近）' % med
    if mad > VERIFY_JITTER:
        return False, '白块左端抖动 %.1fpx（条槽边缘不会抖）' % mad
    return True, ('白块左端压在 ROI 左边缘（偏 %+.0fpx，抖 %.1fpx，%d 帧）'
                  % (med, mad, int(got.size)))


def pad_layout(layout: Layout, pad_bar: int = PAD_BAR, pad_btn: int = PAD_BTN):
    """把三个 ROI 各往外扩一点（夹在画面内），返回 (外扩后的 layout, 每路的偏移)。

    读一帧时先照外扩后的框切下来，再在内存里按偏移切出真正的 ROI —— 于是「面板里
    微调几个像素」完全不用碰 ffmpeg。偏移 = 原 ROI 左上角在外扩框里的位置。
    """
    off = {}
    kw = {}
    for name in ROI_NAMES:
        r = getattr(layout, name)
        pad = pad_bar if name == 'bar' else pad_btn
        x0 = max(0, r.x - pad)
        y0 = max(0, r.y - pad)
        x1 = min(layout.width, r.x + r.w + pad)
        y1 = min(layout.height, r.y + r.h + pad)
        kw[name] = Rect(x0, y0, max(1, x1 - x0), max(1, y1 - y0))
        off[name] = (r.x - x0, r.y - y0)
    out = replace(layout, **kw)
    out.validate()
    return out, off


def client_from_slot(slot, s_meas: float, size, ui_scaler: float,
                     speed_y: float = None, right_unit: float = 0.0):
    """按实测出来的条槽（有按钮位置时再加上按钮）反推客户区尺寸和原点。

    用到的三条硬关系（都在 geometry.py 里）：
        u = ui_scale(客户区) * edge，其中 ui_scale(客户区) = 客户区宽 / 1920
        条槽宽 = 180 * u                      <- 所以 u 是量出来的（条槽没被画面裁掉时）
        条槽中心距客户区**底边** = 268 * u      <- 给底边
        右上角按钮顶边距客户区**顶边** = 30 * u  <- 给顶边（speed_y = 实测的按钮 y）

    只有条槽时，底边有了、顶边没有，客户区**高**只能假设「窗口宽高比 = 画面宽高比」——
    实测用户本机那段桌面录像里这一猜差了 160px（窗口是 2558x1440 的 1440p，录进 2560x1600
    的桌面里），于是「客户区 + 原点」这一对底边对、顶边错；下一次打开同一段录像时，
    _measure 按这份预测去抽条槽那一块，会把条槽整块抽偏、**量不出白块**（实测踩过）。
    所以按钮一量出来就顺手把高也定死。

    right_unit 是「条槽右边缘距客户区右边 / u」（实测表里的 right_margin_unit）。
    """
    vw, vh = int(size[0]), int(size[1])
    edge = ui_edge_scale(ui_scaler)
    u = float(s_meas) * edge
    cw = max(320, int(round(REF_W * s_meas)))
    bottom = float(slot.center_y) + BAR_CENTER_FROM_BOTTOM_UNIT * u
    if speed_y is None:
        ch = int(round(cw * vh / float(vw)))          # 没有按钮：只能按画面宽高比猜
        ch = max(240, min(ch, int(round(bottom))))    # 顶边不能高过画面顶边
    else:
        ch = max(240, int(round(bottom - (float(speed_y) - BTN_TOP_UNIT * u))))
    right = float(slot.right) + 1.0 - right_unit * u   # 条槽右边缘 ≈ 客户区右边缘
    return (cw, ch), (int(round(right - cw)), int(round(bottom - ch)))


class VideoSource:
    """一段录像 -> 可随机访问的三路 ROI 帧流（外加一份对好位的 layout）。

    一次干完这几件事：探测 -> 校准（定 ROI）-> 抽流（写缓存）-> 打开内存映射 -> 自检。
    抽流是唯一贵的一步（要解码整段录像，20 秒的录像约 30~60 秒），之后读第 i 帧
    只是从内存映射里切一块，随便拖进度条。

    progress(stage, frac, note)：给界面显示进度用。
        stage  'probe' / 'align' / 'measure' / 'extract' / 'ready'
        frac   0..1（不知道给 -1）
    """

    def __init__(self, path: str, *, ffmpeg: str = None, cache_dir: str = None,
                 ui_scaler: float = None, client=None, offset=None, snap: bool = True,
                 templates=(), progress: Callable = None, force: bool = False,
                 use_profile: bool = True, save_on_auto: bool = True,
                 save_manual: bool = False, bar_box=None):
        self.path = os.path.abspath(path)
        if not os.path.isfile(self.path):
            raise FileNotFoundError('找不到这个录像文件：%s' % self.path)
        self.ffmpeg = find_ffmpeg(ffmpeg)
        self._progress = progress or (lambda stage, frac, note='': None)
        self.templates = list(templates)
        # 手工填了客户区（面板的「客户区…」）时要不要也记进校准表。默认不记 ——
        # 「手填」意味着用户知道自己在干什么，但一次成功的校准值得留到下次。
        self.save_manual = bool(save_manual)
        self.notes = []
        self.measured = None
        self.buttons_ok = True
        # 手动框选那条路（面板的「框选费用条」）：三个 ROI 全部按用户框出来的费用条推。
        # manual / verified 是给面板显示用的（和 window.WindowSource 同一套语义）。
        self.manual = False
        self.verified = True
        self.bar_box = None

        self._say('probe', -1.0, '读视频信息…')
        info = probe_video(self.ffmpeg, self.path)
        if not info.size:
            raise RuntimeError('读不出这个文件的画面尺寸（不是视频？）：%s' % self.path)
        self.info = info
        self.size = (int(info.size[0]), int(info.size[1]))
        self.fps = float(info.fps or 60.0)
        self.duration = float(info.duration or 0.0)
        self.frames_est = max(1, int(round(self.duration * self.fps)))
        self.ui_scaler = 1.0 if ui_scaler is None else float(ui_scaler)

        self._prepare(client, offset, snap, use_profile, save_on_auto, force, cache_dir,
                      bar_box=bar_box)

    def _prepare(self, client, offset, snap, use_profile, save_on_auto, force,
                 cache_dir=None, bar_box=None):
        """校准 -> 定出三个 ROI -> 拿到帧源 -> 自检。

        拆出来是为了让「读一个窗口」（window.WindowSource）整个复用这一套：那边只是把
        _acquire（拿到整段的帧）和 _strip（抽一小块的帧）换成抓屏，校准 / 自动对齐 /
        量条槽 / 搜按钮一份代码都不用改。
        """
        # ---- 校准：定出三个 ROI ----
        client, offset, layout = self._calibrate(client, offset, snap, use_profile,
                                                save_on_auto, bar_box=bar_box)
        self.client = client
        self.offset = offset
        self.layout = layout
        # 「刚校准出来」的那一份，之后面板手动挪 ROI 只动 self.layout，这一份永远不动 ——
        # 方向键的 delta 就是相对它算的（VideoMonitor.base_layout），否则挪一下再挪回去会叠加。
        self.auto_layout = layout
        # 外扩后的框决定抽流；按钮的可见性自检放在抽流**之后**（要用真像素），
        # 而它不改这里的矩形 —— 否则下次缓存就命不中了（见 _check_buttons）。
        self.padded, self.pad_off = pad_layout(self.layout)
        if bar_box is not None:
            self.bar_box = bar_box
            if cache_dir is None:
                # 手动框选的 ROI 和自动量出来的那份**不是同一套**，共用缓存目录会互相
                # 覆盖（extract 发现 ROI 对不上就重抽，两边来回切一次重抽一次）。
                # 离线 CLI 也是这么分的：见 cli.cache_suffix。
                cache_dir = cache_dir_for(self.path) + '_barbox'
        self._acquire(force, cache_dir)
        self._check_buttons()
        self._say('ready', 1.0, self.describe())

    def _acquire(self, force: bool, cache_dir: str = None) -> None:
        """拿到整段的帧：录像 = 抽流 + 内存映射（窗口那条链路把这一步整个换掉）。"""
        self.cache_dir = cache_dir or cache_dir_for(self.path)
        meta = extract(self.ffmpeg, self.path, self.padded, self.cache_dir, force=force,
                       progress=self._extract_progress, total_frames=self.frames_est)
        self.frames = int(meta['frames'])
        self.fps = float(meta['fps'])         # 抽流写进 meta 的帧率，和缓存的帧数配套
        self._open()

    def _strip(self, rect: Rect, stride: int = 1, frames: int = None,
               seek: float = None) -> np.ndarray:
        """整段里某一块的灰度原始流。默认从录像抽；子类可以换来源（窗口 = 抓屏）。

        seek 给秒数就从那儿开抽（见 align.extract_strip）：验一份 ROI 时要在整段的几个
        位置各取一小段，否则每次都得从头解到那儿。
        """
        return extract_strip(self.ffmpeg, self.path, rect, stride, frames, seek=seek)

    # ---------- 校准 ----------

    def _calibrate(self, client, offset, snap, use_profile, save_on_auto, bar_box=None):
        vw, vh = self.size
        scaler = self.ui_scaler
        key = self._profile_key()
        if bar_box is not None:
            # 用户框出来了：不查表、也不量条槽 —— 框本身就是从像素上读出来的位置
            return self._calibrate_from_box(bar_box)
        manual = client is not None or offset is not None
        prof = None
        if not manual and use_profile:
            prof = self._profiles().get(key)
            if prof:
                client, offset = prof['client'], prof['offset']
                self.notes.append('套用 %s 里记的客户区 %dx%d @(%d,%d)（%s）'
                                  % (self.profile_name, client[0], client[1],
                                     offset[0], offset[1], prof['source'] or '来源未记'))
        # 表里连三个 ROI 都记着：直接照抄，连「自动对齐 + 量条槽」（几秒到几十秒）都省掉。
        # 敢跳过测量，是因为那份 ROI 本来就是上一轮**量准了之后**才写进去的。抽完流
        # _check_buttons 还会拿真像素把按钮那两路再验一遍（那条自检本来就在跑）。
        #
        # 但「上一轮量准」不等于「这一段也准」：表是按画面尺寸归档的，而游戏窗口的位置
        # 和大小录一次变一次（用户本机所有桌面录像都是 2560x1600，游戏却在四个不同的
        # 地方出现过）。所以照抄之前先拿像素验一遍（_verify_layout）；对不上就把**那份
        # ROI** 丢掉，重量条槽。实测这就是「录像里完全识别不到」的成因与修法：同一段
        # 1262 帧的录像，照抄表里那份数出 0 帧，重新量一遍立刻正常（47 帧，正是那段
        # 里游戏真正跑过的长度）。
        #
        # 客户区 / 原点**要留着**：重量的探针框就是按它们算出来的预测位置往外扩一圈，
        # 表里那两个值哪怕来自另一套窗口几何，也比「假设画面就是客户区」准 ——
        # 实测那 5 段旧录像上，用表里的 client/offset 当预测分别数出 18/52/31/0/5 帧，
        # 换成按画面尺寸硬猜只剩 0~1 帧。
        retry_align = False
        if snap and prof and prof.get('layout'):
            lay = Layout.from_dict(prof['layout'])
            if lay.width == vw and lay.height == vh:
                ok, why = self._verify_layout(lay)
                if ok is not False:
                    self.notes.append('费用条 / 按钮 ROI 直接用表里记的那份（跳过量条槽）'
                                      '；自检：%s' % why)
                    return (int(client[0]), int(client[1])), (int(offset[0]), int(offset[1])), lay
                self.notes.append('⚠ 表里那份 ROI 对不上画面（%s）—— 丢掉它，'
                                  '重新量条槽' % why)
                retry_align = True
        if client is None:
            client = (vw, vh)                 # 默认：这段录像就是客户区

        # 先按 client 算出预测 ROI；原点不知道就自动对齐
        if offset is None:
            self._say('align', -1.0, '自动对齐费用条…')
            found = self._detect_offset(client)
            if found is not None:
                offset = (int(found[0]), int(found[1]))
                self.notes.append('自动对齐 dx=%+d dy=%+d（阶跃强度 %.2f）'
                                  % (offset[0], offset[1], found[2]))
            else:
                offset = (0, 0)
                self.notes.append('自动对齐没找到费用条，按画面原点当客户区原点')

        rep = self._measure(client, offset, scaler) if snap else None
        if rep is None and retry_align:
            # 表里那份已经否掉了，而按它算出来的预测位置也量不到条槽：再赌一次
            # 「自动对齐」—— 让画面自己说客户区原点在哪儿（费用条的相位阶跃是最好的路标）。
            # 桌面录像里游戏窗口的位置录一次变一次，这一步能自己把它找出来。
            self._say('align', -1.0, '表里那份对不上：重新自动对齐…')
            found = self._detect_offset((vw, vh))
            if found is not None:
                c2, o2 = (vw, vh), (int(found[0]), int(found[1]))
                rep2 = self._measure(c2, o2, scaler)
                if rep2 is not None:
                    client, offset, rep = c2, o2, rep2
                    self.notes.append('重新自动对齐 dx=%+d dy=%+d（阶跃强度 %.2f）'
                                      % (found[0], found[1], found[2]))
        if rep is not None:
            self.measured = rep
            client, offset, layout = self._fit(client, offset, rep, manual, vw, vh, scaler)
        else:
            layout = reframe_layout(layout_for(client[0], client[1], scaler),
                                    int(offset[0]), int(offset[1]), vw, vh)
            self.notes.append('没量出条槽（前 %d 帧里没看到白块），费用条 ROI 用预测值 '
                              '—— 读数不对就用面板的「重测录像」' % MEASURE_FRAMES)
        layout = self._fit_buttons_coarse(layout, client, offset)
        if not manual:
            # 客户区是用户自己填的：那是他的判断，不要用模型反推的值去盖掉
            client, offset = self._reconcile_client(client, offset, layout, scaler)
        # 存校准表必须在**按钮也对完之后**：表里那条 client/offset 下次会拿来当预测用的
        # 原点（_measure 就是按它去抽条槽那一块的），早一步存就会存进一份自相矛盾的值。
        if rep is not None:
            self._record(client, offset, rep, key, manual, save_on_auto, layout)
        return (int(client[0]), int(client[1])), (int(offset[0]), int(offset[1])), layout

    def _verify_layout(self, layout: Layout) -> tuple:
        """拿真像素验一份 layout：白块左端到底压在没压在 ROI 的左边缘上（见 judge_bar_layout）。

        在整段里挑三个位置各取一小段来验，不是只看开头：录像开头常是菜单 / 加载 / 剧情，
        那几个位置根本没有费用条 —— 只看开头会把「好 ROI」误判成坏的（反过来也一样）。
        取的是 8 帧连着的短带子，三段加起来不到 1 秒的解码，代价可以忽略。

        返回 (True/False/None, 说明)。None = 三小段里一帧白块都没看见，没法判 ——
        这时按「表里那份先信着」处理，但话要说清楚（面板上能看见这句）。
        """
        rect, left_col = bar_probe(layout)
        dur = float(self.duration or 0.0)
        verdicts = []
        for frac in VERIFY_SEEKS:
            seek = None if (frac <= 0.0 or dur <= 0.0) else frac * dur
            try:
                strip = self._strip(rect, 1, VERIFY_FRAMES, seek=seek)
            except Exception as exc:                     # noqa: BLE001
                verdicts.append((None, '取第 %.0f%% 处那一小段失败（%s）' % (frac * 100, exc)))
                continue
            verdicts.append(judge_bar_layout(strip, layout, left_col))
        for ok, why in verdicts:
            if ok is False:
                return False, why                          # 只要有一处对不上就否掉
        for ok, why in verdicts:
            if ok is True:
                return True, why
        return None, verdicts[0][1] if verdicts else '一段都没验到'

    def _snap_bar_left(self, layout: Layout) -> tuple:
        """把费用条 ROI 的左边缘吸附到白块起点（右端不动）。返回 (layout, 说明)。

        为什么值得单独做这一步：相位（「现在第几帧」）全靠条槽左端这一个锚点，差 1px
        就是差 1/6 个逻辑帧，攒几个周期就能让「一费 30 帧」变成 29 或 31。而白块起点是
        画面上本来就有的尺子 —— 用户拖框时把条槽左边多框进来十几个像素太正常了，
        与其让他自己抠，不如直接按像素吸过去。
        """
        rect, left_col = bar_probe(layout, lpad=SNAP_LPAD)
        try:
            strip = self._strip(rect, 1, VERIFY_FRAMES)
        except Exception as exc:                         # noqa: BLE001
            return layout, '吸附左端没做成（%s），先按框的位置用' % exc
        offs = bar_left_offsets(strip, layout, left_col)
        got = offs[np.isfinite(offs)]
        if got.size == 0:
            return layout, '框里这几帧没看见白块（条空着 / 满费？），左端先按框的位置用'
        med = int(round(float(np.median(got))))
        if med == 0:
            return layout, '左端已经压在白块起点上（不动）'
        if abs(med) > SNAP_MAX:
            return layout, ('白块起点离框的左端 %+dpx（超过 ±%dpx）—— 框可能没圈对，'
                            '左端不动' % (med, SNAP_MAX))
        r = layout.bar
        x = r.x + med
        w = r.w - med
        if x < 0 or w < 8 or x + w > layout.width:
            return layout, '吸附之后 ROI 跑到画面外了（%+dpx），左端不动' % med
        true_w = max(8.0, layout.bar_width_f - med)
        out = replace(layout, bar=Rect(x, r.y, w, r.h), bar_width_frac=true_w)
        out.validate()
        self.notes.append('费用条 ROI 左端按白块起点吸附 %+dpx（真宽 %.0fpx）'
                          % (med, true_w))
        return out, '左端吸附到白块起点：%+dpx（条槽左端就在白块最左那一列）' % med

    def _calibrate_from_box(self, box):
        """用户框出来的费用条 -> 三个 ROI（录像链路的手动校准）。

        框能给出两条硬关系（都在 layout_from_bar_box 里用着）：
            * 条槽左端 = 框的左端 —— 白块起点就在那儿（再由 _snap_bar_left 按像素钉死）；
            * 条槽宽 = 框宽      —— s = 框宽 / (180 * edge)，客户区 = 1920s x 1080s。
        右上角两个按钮的**横向**位置由「条槽右端贴着客户区右边」定死，而框的右端是
        用户拖出来的，所以它也准；竖直方向只能从客户区顶边推，而顶边是猜的 ——
        交给 _fit_buttons_coarse 用模板搜（搜不到就关掉那两条通道，费用条不受影响）。
        """
        self.manual = True
        if not hasattr(box, 'x'):                  # (x, y, w, h) 也能传（命令行、测试都用）
            box = Rect(*[int(v) for v in box])
        client, offset, layout = layout_from_bar_box(self.size, box, self.ui_scaler)
        self.notes.append('按框选钉住费用条：x=%d y=%d 宽 %dpx（反推客户区 %dx%d @(%d,%d)）'
                          % (box.x, box.y, box.w, client[0], client[1],
                             offset[0], offset[1]))
        self._say('measure', -1.0, '按框选钉住费用条：搜右上角两个按钮…')
        layout = self._fit_buttons_coarse(layout, client, offset)
        layout, why = self._snap_bar_left(layout)
        self.notes.append('框选校准：%s' % why)
        ok, why2 = self._verify_layout(layout)
        self.verified = ok is not False
        if ok is False:
            self.notes.append('⚠ 框可能没贴住条槽：%s —— 可以重新框一次，'
                              '或在面板上按 Tab + 方向键微调' % why2)
        else:
            self.notes.append('框选校准自检：%s' % why2)
        if self.save_manual:
            # 记进校准表（按画面尺寸归档）。下次开同尺寸的录像会**先自检再套用** ——
            # 所以就算这段录像的窗口摆得不一样，也不会像以前那样读错地方。
            try:
                self._save_profile(self._profile_key(), client, offset, '手动框选费用条',
                                   '%dpx 的条槽' % layout.bar.w, layout=layout)
                self.notes.append('已记进 %s（同尺寸的下次先自检、通过就直接套用）'
                                  % self.profile_name)
            except OSError as exc:
                self.notes.append('校准表写不进去（%s）' % exc)
        return client, offset, layout

    def _reconcile_client(self, client, offset, layout, scaler):
        """按钮也找到之后，把「客户区 + 原点」重算成一份**自洽**的（见 client_from_slot）。

        为什么必须自洽：这一对值下次打开同一段录像时要拿来当预测原点（_measure 按它抽
        条槽那一块）。只按条槽报的话高是猜的，实测里差了 160px，第二次开就量不出白块，
        整个计数直接崩 —— 这不是「报得难看」的问题，是会真的读错。
        """
        rep = self.measured
        if rep is None or not self.buttons_ok:
            return client, offset
        slot = rep['slot']
        if slot.clipped_left:
            return client, offset              # 左边缘都被裁：相位锚点没了，反推没意义
        # 条槽宽必须用 layout 里那份「真宽」：右端被画面裁掉（游戏窗口比画面宽，这很常见）
        # 时 slot.w 只是下限，拿它算 s 会得到 1.256 这种荒唐值 —— 实测就是这么反推出
        # 2411x1419 的。这一条直接决定客户区宽度，错了整套 ROI 就全错。
        bw = float(layout.bar_width_f)
        s_meas = bw / (BAR_W_UNIT * ui_edge_scale(scaler)) if bw > 0 else rep['s_meas']
        if s_meas <= 0:
            return client, offset
        want, _guess = client_from_slot(slot, s_meas, self.size, scaler,
                                        speed_y=layout.speed.y)
        base = layout_for(want[0], want[1], scaler)
        if base.speed.w != layout.speed.w or base.speed.h != layout.speed.h:
            return client, offset              # 尺寸档变了，按钮那套位置对不上，别硬套
        off = (layout.speed.x - base.speed.x, layout.speed.y - base.speed.y)
        # 自检：拿这份「按钮定原点」的预测去抽条槽，条槽必须还落在探针框里。落不进去就说明
        # 反推出来的高不像话（模型在新平台 / 新 UI 缩放下不成立），退回原来那份条槽口径的。
        pred = base.bar.y + off[1]
        if abs(pred - (int(round(slot.band_top)) - BAR_BAND_TOP)) > PAD_Y:
            self.notes.append('按按钮反推的客户区高 %d 让条槽预测偏出探针框，客户区原点'
                              '改回条槽口径（不影响读数）' % want[1])
            return client, offset
        if want != client:
            self.notes.append('客户区按实测反推成 %dx%d @(%d,%d)（条槽定底边、按钮定顶边）'
                              % (want[0], want[1], off[0], off[1]))
        return want, off

    def _fit_buttons_coarse(self, layout: Layout, client, offset) -> Layout:
        """抽流**之前**，用模板把右上角两个按钮搜到正确位置（搜不到就关掉这两条通道）。

        为什么非得单独搜一次：这两个按钮的**竖直**位置挂在客户区的**顶边**上，而顶边只能
        推、不能量 —— 条槽给的是客户区**底边**（条槽中心距底 268*u），顶边要靠「窗口宽高比
        = 画面宽高比」去猜，实测用户本机那段桌面录像里这一猜差了 45px，正好是窗口标题栏
        的高度。抽流框每个按钮只留 ±PAD_BTN，够不着 45px。

        怎么搜：横坐标不用太费劲（由客户区宽度定死，误差几像素，而按钮 ROI 有 200px 宽），
        纵向搜 ±300px。判据是一条硬事实：**1X / 2X 的数字笔画和 templates/ 里的模板对得上，
        就说明 ROI 罩住了倍速键**（read_speed 的置信度本来就靠这个互相关，而模板会按 ROI
        尺寸最近邻缩放，换分辨率不失效）。

        搜不到只有两种可能：ROI 位置实在离谱，或者这段录像里压根没画数字（全程子弹时间 /
        一直不放倍速键）。两种都只能「不猜」——关掉暂停 / 倍速两条通道，费用条那条通道
        不受影响（见 _no_buttons）。
        """
        if not self.templates or not layout.buttons_visible:
            return layout
        vw, vh = self.size
        # 尺寸和锚点都取**标称**的：layout 里的按钮可能已经被 reframe_layout 裁过
        # （客户区原点猜偏时按钮会跑到画面顶上，高度被裁掉一半 —— 实测那个 84px 的
        # 假高度会让数字条带整个错位，相关度从 0.99 掉到 0.76，搜出来的位置也就跟着偏）。
        base = layout_for(client[0], client[1], self.ui_scaler)
        w, h = base.speed.w, base.speed.h
        if w <= 0 or h <= 0 or h > vh or w > vw:
            return self._no_buttons(layout, '按钮标称尺寸比画面还大')
        ax, ay = base.speed.x + int(offset[0]), base.speed.y + int(offset[1])
        cx0, cx1 = max(0, ax - BTN_DX), min(vw - w, ax + BTN_DX)
        cy0, cy1 = max(0, ay + BTN_DY_LO), min(vh - h, ay + BTN_DY_HI)
        if cx1 - cx0 < 2 or cy1 - cy0 < 2:
            return self._no_buttons(layout, '画面太小，右上角没地方搜')
        rect = Rect(cx0, cy0, cx1 - cx0 + w, cy1 - cy0 + h)
        stride = max(1, int(self.frames_est // BTN_SEARCH_FRAMES))
        self._say('measure', -1.0, '对右上角按钮（每 %d 帧取 1 帧）…' % stride)
        strip = self._strip(rect, stride)
        if strip.shape[0] < 2:
            return self._no_buttons(layout, '右上角那一块一帧都没解出来')
        # read_speed 要的是「裁到 ROI 那么大的小图」，所以给它一个原点在 (0,0) 的布局，
        # 真正的坐标换算（画面 -> 带子）由下面 score() 里的切片负责。
        lay = replace(layout, speed=Rect(0, 0, w, h))

        def score(x, y):
            """这一格：命中模板的帧数 + 这些帧的平均相关。"""
            hits = n = 0
            tot = 0.0
            for f in strip:
                crop = f[y - rect.y:y - rect.y + h, x - rect.x:x - rect.x + w]
                if crop.shape != (h, w):
                    continue
                sp = read_speed(crop, lay, self.templates)
                if sp.corr_2x != sp.corr_2x:      # nan：这一帧没画数字
                    continue
                n += 1
                tot += sp.corr_2x
                if sp.corr_2x >= CORR_2X_MIN:
                    hits += 1
            return hits, (tot / n if n else -2.0)

        best = None
        found = False
        for y in range(cy0, cy1 + 1, BTN_COARSE):
            if found:
                break
            for x in range(cx0, cx1 + 1, BTN_COARSE):
                got = score(x, y)
                if best is None or got > (best[0], best[1]):
                    best = (got[0], got[1], x, y)
                    # 「每一帧都命中 + 平均相关够强」= 这一格已经到顶了（hits 最多
                    # 就是帧数），再往下扫只会在同样命中的格子里比小数点后几位。
                    # 为什么值得加这一条：整片搜索网格是 21 x 111 格，每格都要把
                    # 十几帧全做一遍模板互相关 —— 实测最坏情况要几十秒（自检里
                    # 「框选之后重新算了一份 ROI」那条就是这么超时红的），提前收手
                    # 之后一秒出头。精搜那一段照旧跑，位置精度不受影响。
                    if got[0] >= strip.shape[0] and got[1] >= BTN_STRONG_CORR:
                        found = True
                        break
        for y in range(max(cy0, best[3] - BTN_COARSE), min(cy1, best[3] + BTN_COARSE) + 1):
            for x in range(max(cx0, best[2] - BTN_COARSE), min(cx1, best[2] + BTN_COARSE) + 1):
                got = score(x, y)
                if got > (best[0], best[1]):
                    best = (got[0], got[1], x, y)
        hits, mean, bx, by = best
        if hits < BTN_MIN_HITS and mean < BTN_STRONG_CORR:
            return self._no_buttons(layout, '倍速数字和模板对不上（最好的一格 %d/%d 帧命中，'
                                            '相关 %.2f）' % (hits, strip.shape[0], mean))
        dx, dy = bx - ax, by - ay          # 相对**标称**位置的位移，同样报给人看
        out = replace(layout, speed=Rect(bx, by, w, h),
                      pause=Rect(bx + (base.pause.x - base.speed.x),
                                 by + (base.pause.y - base.speed.y),
                                 base.pause.w, base.pause.h))
        try:
            out.validate()
        except ValueError:
            return self._no_buttons(layout, '搜出来的按钮位置跑出画面了')
        self.buttons_ok = True
        self.notes.append('右上角按钮按模板对上：整体移动 %+d,%+d（%d/%d 帧命中，相关 %.2f）'
                          % (dx, dy, hits, strip.shape[0], mean))
        ratios = self._pause_ratios(strip, rect, out)
        if ratios:
            self.notes.append('暂停键读数 %.3f~%.3f（0.07 上下 = 已暂停，0.25 上下 = 运行中）'
                              % (min(ratios), max(ratios)))
        return out

    def _pause_ratios(self, strip, rect, layout):
        """按钮对完之后，顺手把暂停键的读数范围报一下 —— 只是给人看的自检。

        不做判定：这段录像里游戏大部分时间是**暂停中**（实测 ratio 0.069），
        而「暂停中」恰恰是要认出来的那一档，拿它当失败依据会反着来。
        """
        lay = replace(layout, pause=Rect(0, 0, layout.pause.w, layout.pause.h))
        out = []
        for f in strip:
            crop = f[layout.pause.y - rect.y:layout.pause.y - rect.y + layout.pause.h,
                     layout.pause.x - rect.x:layout.pause.x - rect.x + layout.pause.w]
            if crop.shape != (layout.pause.h, layout.pause.w):
                continue
            out.append(read_pause(crop, lay).ratio)
        return out

    def _no_buttons(self, layout: Layout, why: str) -> Layout:
        """按钮没对上：关掉暂停 / 倍速两条通道，并记一条说明。"""
        self.buttons_ok = False
        self.notes.append('⚠ 右上角按钮没对上：%s —— 暂停 / 倍速两条通道已关掉，不猜。'
                          '费用条计数不受影响；要修就在面板上按 Tab + 方向键手动挪，'
                          '或用「设置客户区」改客户区尺寸 / 位置' % why)
        return replace(layout, buttons_visible=False)

    def _fit(self, client, offset, rep, manual, vw, vh, scaler):
        """把预测布局对齐到实测条槽上，必要时按实测反推客户区。返回 (client, offset, layout)。"""
        slot = rep['slot']
        base = layout_for(client[0], client[1], scaler)
        layout = reframe_layout(base, int(offset[0]), int(offset[1]), vw, vh)
        # 宽度只有在「左、右都没被画面裁掉」时才当真：右边缘被裁（游戏窗口比画面宽）
        # 或左边缘被裁时，量出来的只是下限，当成真宽会让「一像素 = 几帧」整个偏掉。
        width_ok = not (slot.clipped_left or slot.clipped_right)
        slot_w = float(slot.w)

        if width_ok and not manual and client == (vw, vh) and base.bar.w > 0 \
                and abs(slot_w - base.bar.w) / float(base.bar.w) > WIDTH_TOL:
            # 「录像=客户区」和实测差得多：画面里有别的东西（录的是整个桌面，
            # 游戏只是个窗口）。按实测反推客户区，再整体对齐到实测条槽上。
            s_meas = float(rep['s_meas'])
            new_client, _guess = client_from_slot(slot, s_meas, self.size, scaler)
            new_base = layout_for(new_client[0], new_client[1], scaler)
            layout, dx, dy = self._snap(new_base, slot, vw, vh)
            client, offset = new_client, (int(offset[0]) + dx, int(offset[1]) + dy)
            self.notes.append('实测条宽 %.0fpx（按画面当客户区预测 %.0fpx）对不上：'
                              '按它反推客户区 %dx%d @(%d,%d)'
                              % (slot_w, base.bar.w, client[0], client[1], offset[0], offset[1]))
        else:
            # 客户区不动，只把整块布局平移过去，让条槽压在实测位置上
            layout, dx, dy = self._snap(layout, slot, vw, vh)
            offset = (int(offset[0]) + dx, int(offset[1]) + dy)
            if dx or dy:
                self.notes.append('按实测条槽平移 %+d,%+d' % (dx, dy))

        true_w = slot_w if width_ok else float(layout.bar_width_f)
        layout = _with_measured_bar(layout, slot, true_w, vw, vh)
        self.notes.append('费用条 ROI 钉到实测：x=%d 宽 %.0f（预测 x=%d 宽 %.0f）%s'
                          % (slot.left, true_w, base.bar.x, base.bar_width_f,
                             '，右端被画面裁掉（真宽只能推）' if slot.clipped_right else ''))
        if abs(layout.bar.x - base.bar.x) > LEFT_TOL:
            # 不是错：客户区原点本来就是猜的，ROI 一律按实测钉住（见 _fit_buttons_coarse）。
            self.notes.append('条左边缘和预测差 %+dpx（预测用的客户区原点不保证准，'
                              '三个 ROI 都已按实测钉住）' % (layout.bar.x - base.bar.x))
        return client, offset, layout

    def _snap(self, layout: Layout, slot, vw: int, vh: int):
        """把 layout 平移，让条槽左边缘 / 中心线压在实测位置上。

        为什么用「平移」而不是重算三个 ROI：三个 ROI 的相对位置由 layout_for 定死
        （按钮跟着客户区右上角、条跟着右下角），平移解出来的是「客户区原点在画面里的
        位置」这一个自由度，按钮也就跟着归位了。
        纵向只在条带没顶到探针框边时才对（顶到了说明这一行不是条身）。
        """
        dx = 0 if slot.clipped_left else int(round(slot.left - layout.bar.x))
        dy = int(round(slot.center_y - (layout.bar.y + layout.bar.h // 2)))
        if dx or dy:
            layout = reframe_layout(layout, dx, dy, vw, vh)
        return layout, dx, dy

    def _record(self, client, offset, rep, key, manual, save_on_auto, layout=None):
        slot = rep['slot']
        clipped = '（右端超出画面 %.0fpx，按预测补）' % max(0.0, rep['pred_bar_w'] - rep['bar_w_meas']) \
            if slot.clipped_right else ''
        self.notes.append('实测条槽 x=%d..%d 行 %d..%d（%.0fpx，抖动 %.2f）%s'
                          % (slot.left, slot.right, slot.band_top, slot.band_bottom,
                             rep['bar_w_meas'], rep['jitter'], clipped))
        if save_on_auto and (not manual or self.save_manual):
            try:
                self._save_profile(key, client, offset,
                                   '面板指定的客户区' if manual else '自动实测条槽',
                                   '%.0fpx 的条槽反推' % rep['bar_w_meas'], layout=layout)
                self.notes.append('已记进 %s（同尺寸的下次直接套用）' % self.profile_name)
            except OSError as exc:
                self.notes.append('校准表写不进去（%s）' % exc)

    def _measure(self, client, offset, scaler):
        """量条槽。先解前 MEASURE_FRAMES 帧；量不出来再放大步长重试一次。

        为什么要重试：录像开头常有菜单 / 加载动画 / 剧情，那段画面里没有费用条。
        放大步长（每 12 帧取 1 帧）能把整段录像都扫到，代价是要解码整段（慢），
        所以放在第二次。
        """
        for stride in (1, MEASURE_STRIDE):
            self._say('measure', -1.0, '量费用条槽（每 %d 帧取 1 帧）…' % stride)
            rep = calibrate_video(self.path, self.ffmpeg, client_size=client,
                                  ui_scaler=scaler, size=self.size,
                                  frames=MEASURE_FRAMES, offset=offset, stride=stride,
                                  strip_fn=self._strip, info=self.info)
            if rep is not None:
                return rep
        return None

    # ---------- 抽流 / 打开 ----------

    def _extract_progress(self, frac, done, total):
        per = max(1, self._per_frame())
        if total > 0:
            self._say('extract', frac, '抽流 %.0f%%（%d/%d 帧）'
                      % (frac * 100.0, done // per, total // per))
        else:
            self._say('extract', -1.0, '抽流 %d 帧…' % (done // per))

    def _per_frame(self) -> int:
        return sum(getattr(self.padded, n).pixels for n in ROI_NAMES)

    def _open(self):
        self._mmaps = {}
        for n in ROI_NAMES:
            r = getattr(self.padded, n)
            path = os.path.join(self.cache_dir, n + '.raw')
            self._mmaps[n] = np.memmap(path, dtype=np.uint8, mode='r',
                                       shape=(self.frames, r.h, r.w))

    def _check_buttons(self):
        """抽完流之后的**自检 / 微调**：拿真像素再验一遍按钮 ROI，偏了就挪，够不着就关通道。

        大头已经由 _fit_buttons_coarse 在抽流**之前**做完了（那一步能搜 ±300px）；这里只搜
        ±PAD_BTN，管的是两种残差：粗搜差一两像素，或者面板上按方向键手动挪过之后要对一下。
        搜不到就关掉这两条通道 —— ROI 偏了读出来的是**看着像真的**垃圾读数，比「读不到」更糟。

        怎么搜：横坐标不用搜（由客户区宽度定死，误差几像素，而按钮 ROI 有 200px 宽），
        只搜竖直方向 ±PAD_BTN。判据是一条硬事实：**1X / 2X 的数字笔画和 templates/
        里的模板对得上，就说明 ROI 罩住了倍速键**（read_speed 的置信度本来就靠这个互相关，
        而模板会按 ROI 尺寸最近邻缩放，换分辨率不失效）。
        搜不到只有两种可能：ROI 没对准，或者这段录像里压根没画数字（全程子弹时间 /
        一直不放倍速键）。两种都只能「不猜」——直接关掉暂停 / 倍速两条通道
        （read_pause / read_speed 会返回「读不到」）。费用条那条通道不受影响。
        """
        if not self.templates or not self.layout.buttons_visible:
            return
        idxs = self._check_indexes()
        r = self.layout.speed
        lo, hi = self._shift_range('speed')
        best = None                      # (最高相关, dy, 命中帧数)
        for dy in range(lo, hi + 1):
            lay = replace(self.layout, speed=Rect(r.x, r.y + dy, r.w, r.h))
            top = -2.0
            hits = 0
            for i in idxs:
                sp = read_speed(self._crop('speed', i, dy), lay, self.templates)
                if sp.corr_2x != sp.corr_2x:         # nan：没数字/子弹时间
                    continue
                if sp.corr_2x > top:
                    top = sp.corr_2x
                if sp.corr_2x >= CORR_2X_MIN:
                    hits += 1
            if top > -2.0 and (best is None or top > best[0]):
                best = (top, dy, hits)
        if best is None or best[0] < CORR_2X_MIN + 0.05:
            why = ('倍速数字和模板对不上（最高相关 %.2f）' % best[0] if best
                   else '倍速数字根本没画出来（全程子弹时间？）')
            self.layout = self._no_buttons(self.layout, why)
            return
        top, dy, hits = best
        if dy:
            self._shift_roi('pause', dy)
            self._shift_roi('speed', dy)
            self.notes.append('右上角按钮按模板对上了：整体下移 %+dpx（相关 %.2f，'
                              '%d/%d 帧命中）' % (dy, top, hits, len(idxs)))
        else:
            self.notes.append('右上角按钮 ROI 一次就对上了（相关 %.2f）' % top)

    def _check_indexes(self):
        """自检 / 搜按钮时取哪几帧：均匀铺满整段，去重。"""
        n = max(1, len(self))
        return sorted({int(round(i * (n - 1) / max(1, CHECK_SAMPLES - 1)))
                       for i in range(CHECK_SAMPLES)})

    def _shift_range(self, name: str):
        """这个 ROI 还能上/下挪多少像素（外扩框是有限的，别读越界）。"""
        r = getattr(self.layout, name)
        pr = getattr(self.padded, name)
        _ox, oy = self.pad_off[name]
        return max(-oy, -PAD_BTN), min(pr.h - r.h - oy, PAD_BTN)

    def _crop(self, name: str, i: int, dy: int):
        """从抽好的流里切第 i 帧的某个 ROI（竖直再偏移 dy —— 搜按钮位置用）。"""
        r = getattr(self.layout, name)
        ox, oy = self.pad_off[name]
        return np.array(self._mmaps[name][int(i), oy + dy:oy + dy + r.h, ox:ox + r.w])

    def _shift_roi(self, name: str, dy: int) -> None:
        """把某个 ROI 整体平移 dy 像素（外扩框的偏移跟着改，缓存不用重抽）。"""
        r = getattr(self.layout, name)
        self.layout = replace(self.layout, **{name: Rect(r.x, r.y + dy, r.w, r.h)})
        ox, oy = self.pad_off[name]
        self.pad_off[name] = (ox, oy + dy)

    # ---------- 校准表 ----------
    #
    # 默认按**画面尺寸**归档（video_calib.json）。子类可以换成自己的那份：窗口那条链路
    # 按「进程名 + 窗口客户区尺寸」归档，因为同一个尺寸下游戏和播放器的 ROI 完全不同。

    profile_name = PROFILE_NAME

    def _profile_key(self) -> str:
        return '%dx%d' % self.size

    def _profiles(self) -> dict:
        return load_profiles()

    def _save_profile(self, key, client, offset, source, note, layout=None) -> str:
        return save_profile(key, client, offset, self.ui_scaler, source=source,
                            note=note, layout=layout)

    def _detect_offset(self, client):
        """自动对齐：找 (dx, dy) 让预测的三个 ROI 落到画面里正确的位置。"""
        return detect_offset(self.path, self.ffmpeg, client_size=client,
                             ui_scaler=self.ui_scaler, size=self.size,
                             strip_fn=self._strip, info=self.info)

    # ---------- 给面板用的几个动作 ----------

    def save_current(self, layout: Layout = None, source: str = '面板校准',
                     note: str = '') -> str:
        """把**当前**这份 ROI 写进校准表（面板的「保存校准」在录像模式走这里）。

        和 _record 的区别：_record 存的是自动量出来的那份，这里存的是「最后真正在用的」那份
        —— 用户可能已经按方向键把它挪过几个像素。下次开同尺寸的录像就直接照它读。
        """
        return self._save_profile(self._profile_key(), self.client, self.offset,
                                  source, note,
                                  layout=layout if layout is not None else self.layout)

    def adopt_layout(self, layout: Layout):
        """把面板挪过的那份 ROI 接过来（夹进抽流时外扩的框里），返回 (实际布局, 提示)。

        为什么不「挪一下就重抽流」：抽一次要十几秒到几分钟，而面板上按一下方向键就重抽
        等于没法用。当初抽流时每个 ROI 都往外扩了 PAD_BAR / PAD_BTN 像素（见 pad_layout），
        那份余量本来就是留给手动微调的。顶到框边只能夹住，并给一句「去按重解录像」。
        """
        off = {}
        clamped = []
        for n in ROI_NAMES:
            r = getattr(layout, n)
            p = getattr(self.padded, n)
            w = min(r.w, p.w)
            h = min(r.h, p.h)
            x = max(p.x, min(r.x, p.x2 + 1 - w))
            y = max(p.y, min(r.y, p.y2 + 1 - h))
            if (x, y, w, h) != (r.x, r.y, r.w, r.h):
                clamped.append(n)
            off[n] = (x - p.x, y - p.y)
            layout = replace(layout, **{n: Rect(x, y, w, h)})
        self.layout = layout
        self.pad_off = off
        warn = ''
        if clamped:
            warn = ('%s 已顶到抽流时外扩的框边（手动挪只能挪这么远）—— 还要往外就按'
                    '「重解录像」重新抽流' % '/'.join(clamped))
        return layout, warn

    # ---------- 读帧 ----------

    def __len__(self) -> int:
        return int(self.frames)

    def _raw_frame(self, i: int):
        i = int(i)
        out = []
        for n in ROI_NAMES:
            r = getattr(self.layout, n)
            ox, oy = self.pad_off[n]
            out.append(np.array(self._mmaps[n][i, oy:oy + r.h, ox:ox + r.w]))
        return tuple(out)

    def frame(self, i: int):
        """第 i 帧的三个 ROI 灰度图（**真正要用的那份**，从外扩的抽流里切出来）。"""
        return self._raw_frame(i)

    def close(self) -> None:
        for arr in getattr(self, '_mmaps', {}).values():
            try:
                arr._mmap.close()
            except (AttributeError, OSError, ValueError):
                pass
        self._mmaps = {}

    def describe(self) -> str:
        return ('%s ｜ %dx%d ｜ %.3f fps ｜ %d 帧 ｜ 客户区 %dx%d @(%d,%d)'
                % (os.path.basename(self.path), self.size[0], self.size[1], self.fps,
                   self.frames, self.client[0], self.client[1],
                   self.offset[0], self.offset[1]))

    def _say(self, stage, frac, note=''):
        try:
            self._progress(stage, frac, note)
        except Exception:                     # noqa: BLE001  进度回调不许影响抽流
            pass


def _with_measured_bar(layout: Layout, slot, true_w: float, width: int, height: int) -> Layout:
    """费用条 ROI 换成实测像素：左边缘用实测（准确到 1px），宽用 true_w（可能比看得见的宽）。

    ROI 被画面右边缘裁掉时，bar.w 会比 bar_width_frac 小 —— 这正是 Counter(visible_w=...)
    存在的理由（合成模型按真宽算，绘制截到看得见的那一段），实时链路在窗口被拖出屏幕时
    也是这个情况。
    """
    x = int(slot.left)
    vis_w = max(1, min(int(round(true_w)), layout.width - x))
    y = int(round(slot.band_top)) - BAR_BAND_TOP
    y = max(0, min(y, height - BAR_ROI_H))
    try:
        out = replace(layout, bar=Rect(x, y, vis_w, BAR_ROI_H),
                      bar_width_frac=float(true_w))
        out.validate()
    except ValueError:
        return layout                     # 越界就维持预测值，不硬套
    return out


class VideoMonitor(LiveMonitor):
    """录像监视器：接口和 LiveMonitor 一样，只是帧从文件来。

    ui.MonitorApp 完全不用改就能驱动它（latest / stats / counter / layout / reset /
    set_manual_pause / set_cost_rate / CSV 全是继承来的）；新增的只有播放控制：
    play() / pause() / set_play_speed() / seek()。
    """

    # 录像一拍可能要喂 2000 帧（全速档 / 往回拖一大段），一两秒很正常 —— 而面板的
    # 看门狗门限默认是 1 秒（那是照实时 120Hz 定的）。不放宽的话，「全速跑完整段」
    # 会被判成「采样线程已停」，然后热心地建议你去点「重新校准」。
    stall_warn_s = 15.0

    def __init__(self, source: VideoSource, *, rate: float = 120.0, templates=(),
                 csv_path: str = None, cost_rate=DEFAULT_FRAMES_PER_COST,
                 clock=time.perf_counter):
        # 先把自己的字段摆好：LiveMonitor.__init__ 里会调 set_layout()，
        # 而那个覆盖版要用到这些字段（顺序不能反）。
        self.src = source
        self.fps = float(source.fps)
        self.playing = False
        self.play_speed = 1.0
        self._read_cache = {}
        self._fed = -1                  # 已经喂到第几帧（-1 = 还没喂）
        self._applied = None            # 当前 layout（用来判断要不要丢特征缓存）
        self._plate = None              # (images, result)：最近一次喂进去的那一帧
        self._play_base = 0
        self._play_t0 = 0.0
        self._roi_warn = ''              # 「手动挪 ROI 顶到抽流框边了」这类提示
        # 喂帧跑在采样线程里，而播放控制（空格 / 拖滑块 / 回到开头）跑在界面主线程里，
        # 两边都会动状态机的相位。加一把锁把它们串起来 —— 不加的话「一边喂一边 seek」
        # 会让 _fed 和 Counter 的相位对不上（表现是帧号突然跳或倒退）。
        self._lock = threading.RLock()
        self._clock = clock
        super().__init__(source.layout, exe='(录像：%s)' % os.path.basename(source.path),
                         rate=rate, templates=templates, csv_path=csv_path,
                         cost_rate=cost_rate, ui_scaler=source.ui_scaler)
        self.build_layout = self.base_layout
        self._sync_counter_fps()

    # ---------- 布局 ----------

    def base_layout(self) -> Layout:
        """没叠人工微调的布局。

        录像的客户区是算出来的，不存在「窗口当前尺寸」这回事，所以直接给校准出来的那份
        （auto_layout，不是可能在面板上被挪过的 src.layout）。
        """
        return self.src.auto_layout

    def set_layout(self, layout: Layout, keep_elapsed: bool = True) -> None:
        """换布局：ROI 变了就把特征缓存丢掉（缓存里存的是按旧 ROI 读出来的特征）。

        为什么要缓存：拖进度条 / 全速跑完都要把 0..k 帧重新喂一遍状态机（帧号是整段
        累计的，倒着走不了）。读像素每帧 0.3ms 上下，特征只有几个数，缓存下来之后
        重喂一遍几万帧只要一两秒。
        """
        # 面板挪过的 ROI 先夹进抽流时外扩的框（挪出去了就只能夹住，见 adopt_layout）
        layout, self._roi_warn = self.src.adopt_layout(layout)
        new = layout.to_dict()
        rewound = self._applied is not None and self._applied != new
        with self._lock:
            # 一换 ROI 就得把 0..当前位置重喂一遍（特征缓存里的读数是按旧 ROI 读的），
            # 而重喂会让计数**再加一遍** —— 所以这时候 keep_elapsed 必须是 False，否则
            # adopt 过来的帧号会和重喂的帧叠起来（表现：挪一下 ROI，帧号直接翻倍）。
            super().set_layout(layout, keep_elapsed=False if rewound else keep_elapsed)
            self._applied = new
            if rewound:
                self._read_cache.clear()
                self._plate = None
                self._fed = -1
                self._play_base, self._play_t0 = 0, self._clock()
            self._sync_counter_fps()

    def _sync_counter_fps(self) -> None:
        """Counter.fps 只在「第一帧」当 dt 用，但录像第一帧的 dt 应当是一帧视频的时长，
        而 set_layout 里按采样率（120）填了 1/120。改过来，免得起锚那一帧按错的 dt 攒余量。"""
        if self.counter is not None:
            self.counter.fps = max(1.0, float(self.fps))

    # ---------- 读数 ----------

    def _readings(self, i: int):
        """第 i 帧的三个读数（含给面板看的灰度图）。带缓存。"""
        hit = self._read_cache.get(i)
        if hit is not None:
            # 命中缓存的是**读数**（那三个数才是贵的），图还得从内存映射里切出来 ——
            # 面板上的 ROI 预览要用它。以前这里直接返回那个三元组，于是「往回拖进度条」
            # （要重喂一遍 = 必然命中缓存）会 unpack 成 6 个值直接崩掉。
            bar_img, pause_img, speed_img = self.src.frame(i)
            return (bar_img, pause_img, speed_img) + hit
        bar_img, pause_img, speed_img = self.src.frame(i)
        bar = read_bar(bar_img, self.layout)
        pause = read_pause(pause_img, self.layout)
        speed = read_speed(speed_img, self.layout, self.templates)
        out = (bar, pause, speed)
        self._read_cache[i] = out
        return (bar_img, pause_img, speed_img, bar, pause, speed)

    # ---------- 喂帧 ----------

    def _feed_one(self, i: int) -> None:
        bar_img, pause_img, speed_img, bar, pause, speed = self._readings(i)
        # t = 帧号 / fps：就是录像里的游戏内时间（见模块开头第 2 条）
        res = self.counter.step(i, i / self.fps, bar, pause, speed)
        self._plate = (bar_img, pause_img, speed_img, res)
        if self.keep_rows:
            self.rows.append(res.as_row())
        self._write_row(res.as_row())

    def _feed_to(self, target: int) -> int:
        target = max(-1, min(int(target), len(self.src) - 1))
        want = target - self._fed
        if want <= 0:
            return 0
        want = min(want, MAX_BATCH)
        for i in range(self._fed + 1, self._fed + 1 + want):
            self._feed_one(i)
            # 位置**逐帧**更新，不是喂完一批才更新：全速跑长录像时一批要一两秒，
            # 攒到最后才动的话，面板上的滑块和「第 N 帧」会卡住不动（看着像死了），
            # 中途出异常也会把这一批白喂（真实位置丢了）。
            self._fed = i
        # 喂完一批 = 采样循环又转了一圈：也算一次心跳。不然「一拍喂 2000 帧」期间
        # 心跳一直不涨，面板会以为线程死了（见 VideoMonitor.stall_warn_s）。
        self.stats.ticks += 1
        return want

    def _restart(self) -> None:
        with self._lock:
            self._restart_locked()

    def _restart_locked(self) -> None:
        """把状态机退回起点（计数清零，特征缓存留着）。拖进度条往回拖要用。"""
        self.set_layout(self.layout, keep_elapsed=False)
        self._plate = None
        self._fed = -1

    # ---------- 播放控制 ----------

    def play(self) -> None:
        with self._lock:
            self._play_locked()

    def _play_locked(self) -> None:
        if self._fed >= len(self.src) - 1:
            self._restart()                 # 已经播到结尾：从头再来
        self._play_base, self._play_t0 = max(0, self._fed), self._clock()
        self.playing = True

    def pause(self) -> None:
        with self._lock:
            self._play_base, self._play_t0 = max(0, self._fed), self._clock()
            self.playing = False

    def set_play_speed(self, speed) -> float:
        """改播放倍速。只影响**看得多快**，不影响数出来的帧号（见模块开头第 2 条）。"""
        s = max(0.0, float(speed))
        with self._lock:
            self._play_base, self._play_t0 = max(0, self._fed), self._clock()
            self.play_speed = s
        return s

    def seek(self, frame: int) -> int:
        """跳到第 frame 帧。往回跳 = 从 0 帧重喂一遍（帧号是整段累计的）。"""
        with self._lock:
            frame = max(0, min(int(frame), len(self.src) - 1))
            if frame < self._fed:
                self._restart_locked()
            # 一次只喂 MAX_BATCH 帧（那是给「全速」留的余量），拖得远就多喂几轮 ——
            # 只喂一轮的话，长录像往回拖会停在半路，位置和帧号都对不上。
            while self._fed < frame:
                if self._feed_to(min(frame, self._fed + MAX_BATCH)) <= 0:
                    break
            self._play_base, self._play_t0 = max(0, self._fed), self._clock()
            return self._fed

    @property
    def position(self) -> int:
        """已经喂到第几帧（0 起）。面板上那个「第 N / M 帧」用的就是它。"""
        return max(0, self._fed)

    # ---------- 采样循环 ----------

    def _sample(self) -> LiveUpdate:
        with self._lock:
            return self._sample_locked()

    def _sample_locked(self) -> LiveUpdate:
        now = self._clock()
        t = now - self._t0
        self.stats.samples += 1
        if not self.playing:
            # 播放暂停：一帧都不喂。数字按住不动，和实时的「暂停计数」观感一致。
            self.latest = replace(self.latest, t=t, note=self._note('播放已暂停'))
            return self.latest

        target = self._play_base
        if self.play_speed > 0:
            target += int((now - self._play_t0) * self.fps * self.play_speed)
        else:
            target = len(self.src) - 1        # 全速：每拍尽可能多喂（有 MAX_BATCH 兜着）
        last = len(self.src) - 1
        ended = target >= last
        if ended:
            target = last
        moved = self._feed_to(target)
        if moved == 0:
            # 这一拍还没到下一帧（倍速低、拍频高时会经常这样）：保持上一次的读数
            self.latest = replace(self.latest, t=t, note=self._note(''))
            return self.latest
        if ended and self._fed >= last:
            self.playing = False

        bar_img, pause_img, speed_img, res = self._plate
        self.stats.hz = self._loop_hz(now)
        self.latest = LiveUpdate(t, res, bar_img, pause_img, speed_img, 0.0,
                                 self._note(''), True)
        return self.latest

    def _loop_hz(self, now: float) -> float:
        self._hz_window.append(now)
        if len(self._hz_window) > 60:
            self._hz_window.pop(0)
        if len(self._hz_window) < 2:
            return 0.0
        span = self._hz_window[-1] - self._hz_window[0]
        return (len(self._hz_window) - 1) / span if span > 0 else 0.0

    # ---------- 给界面看的文本 ----------

    def _note(self, prefix: str = '') -> str:
        n = len(self.src)
        head = '录像 %s / %s ｜ %s ｜ 第 %d/%d 帧' % (
            fmt_time(max(0, self._fed) / self.fps), fmt_time(n / self.fps),
            self._speed_label(), max(0, self._fed + 1), n)
        tail = self.src.notes[0] if self.src.notes else ''
        return ' ｜ '.join(p for p in (prefix, self._roi_warn, head, tail) if p)

    def _speed_label(self) -> str:
        if not self.playing:
            return '已暂停'
        if self.play_speed <= 0:
            return '全速'
        return '播放 %gx' % self.play_speed

    def perf_line(self, ui_hz: float, cur_m: float = 1.0):
        """面板「性能」那一行：录像模式改成显示播放位置（返回 (文本, 是否告警)）。"""
        mode = getattr(self.latest.result, 'mode', '') if self.latest.result else ''
        txt = ('录像 %.2f/%.2fs ｜ %s ｜ 第 %d/%d 帧 ｜ 已数 %d 帧 ｜ 通道 %s ｜ 循环 %.0f Hz'
               % (max(0, self._fed) / max(1e-9, self.fps), len(self.src) / self.fps,
                  self._speed_label(), max(0, self._fed + 1), len(self.src),
                  self.counter.elapsed if self.counter else 0, mode or '—', self.stats.hz))
        return txt, False

    def describe(self) -> str:
        return self.src.describe()

    def recalibrate(self) -> str:
        """录像模式没有「窗口动了」这回事：重新校准 = 重新打开这段录像（界面负责，
        抽流要几十秒，得放到后台线程里跑，别把面板冻住）。"""
        return ('录像模式：「重新校准」= 重新解一遍 ROI。费用条计数正常的话不用管；'
                '要改客户区就用面板上的「设置客户区」')


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    return '%02d:%05.2f' % (m, seconds - m * 60)