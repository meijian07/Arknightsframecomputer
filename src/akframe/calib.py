"""从录像里量出费用条槽的**真实几何** —— 换分辨率时的那把尺子。

背景：geometry.layout_for() 的单位常数只在 2560x1600 上实测标定过，别的尺寸都是按
ui_scale() 的规则**推**出来的。推得对不对，只能拿真实画面量一遍。

量三个数，每个都有一条硬道理：

  条槽左右边缘  费用「填充白块」永远从条槽左边缘开始长、最多铺满整格（1X / 2X /
                子弹时间都一样），所以把所有帧里的白块**并起来**，它的最左列就是条槽
                左边缘、最右列就是条槽右边缘（= 条槽宽度）。并集不需要任何模型。
                为什么不用「每一列被填充覆盖的时间比例」线性外推：实测那条曲线不是直线
                （有阶梯也有拐点），外推会把交点推到条槽外面很远 —— 一段 240px 的条被
                量成 376 / 478px，而按并集量就是 239~240。
  条槽竖直位置  白块出现的那几行就是条身。逐行量一遍，取「量得出宽度的那几行」的中点，
                直接给 bar_cy_unit。

探针框必须**左右都留出余量**：白块很可能长在预测 ROI 的左边缘之外（实测 2560x1600 上
预测的条槽左边缘比真实位置右移了 9px），探针裁在预测位置上会把白块的左端切掉，量出来
的宽度只会偏短、甚至因为左端不一致而完全量不出来。

量不出来（画面里没有费用条、这段录像条没动过、对比度太低、抽到的帧太少）一律返回
None，绝不猜一个数 —— 校准数据是要写进仓库、给别人的机器用的。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .align import extract_strip
from .capture import probe_video
from .features import fill_run
from .geometry import (BAR_W_UNIT, Rect, layout_for, reframe_layout, ui_edge_scale,
                       ui_scale)

PAD_X = 60           # 探针框在预测左边缘外留多少像素（白块很可能长在预测左边之外）
PAD_RIGHT = 12       # 右边缘外留多少：条槽右端就在预测附近，留多了会框进别的 UI
PAD_Y = 14           # 上下各留多少行（预测的竖直误差在 10px 量级）
MIN_RUN = 6          # 短于这么多列的白色行程当作噪声
MIN_FILL_FRAMES = 10 # 这一行至少要有这么多帧看到白块，才算「条在这一行上」
MAX_JITTER = 3.0     # 左边缘的平均绝对抖动量超过这个数，就不是费用条
MIN_SUPPORT = 3      # 最大宽度至少要有这么多帧支持，否则当野点丢掉
MAX_FRAMES = 1500    # 最多解多少帧（省解码时间；20 秒 @60fps 够量几十个费用周期）
WIDTH_LO = 0.5       # 量出来的宽度必须落在预测的这个倍数区间里（挡住场景里的白块）
WIDTH_HI = 2.0
BAND_FRAC = 0.8      # 「和最佳行宽度差不多」的行算一条条身


@dataclass
class SlotShape:
    """量出来的条槽形状（**画面坐标**，含两端）。"""
    left: int
    right: int
    band_top: int          # 条身首行
    band_bottom: int       # 条身末行
    frames: int            # 解了多少帧
    fill_frames: int       # 其中多少帧在这一行看到了白块
    jitter: float          # 左边缘抖动（平均绝对偏差，px）
    support: int           # 最大宽度有多少帧支持
    clipped: bool = False  # 白块顶到探针边框 —— 宽度只能是下限
    # 具体是哪一边被裁掉了。录像里非常常见：游戏窗口比画面还宽（客户区右边缘在画面外），
    # 条槽右端跟着跑出画面，这时「量出来的宽度」只是下限，不能当成条槽真宽用。
    # 但左边缘仍然可信（除非左边缘也被裁掉）—— 费用条的相位全靠它。
    clipped_left: bool = False
    clipped_right: bool = False

    @property
    def w(self) -> int:
        return self.right - self.left + 1

    @property
    def center_y(self) -> float:
        return (self.band_top + self.band_bottom) / 2.0


def measure_slot(frames: np.ndarray, pred_bar: Rect, x0: int = 0, y0: int = 0):
    """在一段灰度原始流里量条槽。frames = (n, h, w)，左上角在画面 (x0, y0)。

    pred_bar 只用来定两件事：探针框的搜索范围（调用方算好）和「宽度该在什么量级」
    （挡掉场景里的白块）。量出来的左右边缘与它无关 —— 这正是这个工具能发现预测偏了
    的原因。
    """
    n, sh, sw = frames.shape
    if n < MIN_FILL_FRAMES:
        return None
    pred_w = float(pred_bar.w)
    rows = []
    for r in range(sh):
        runs = []
        for i in range(n):
            run = fill_run(frames[i, r])
            if run is None or run[1] - run[0] + 1 < MIN_RUN:
                continue
            runs.append(run)
        if len(runs) < MIN_FILL_FRAMES:
            continue
        lefts = np.array([s for s, _ in runs], dtype=np.float64)
        rights = np.array([e for _, e in runs], dtype=np.float64)
        med = float(np.median(lefts))
        jitter = float(np.mean(np.abs(lefts - med)))
        if jitter > MAX_JITTER:
            continue
        keep = np.abs(lefts - med) <= MAX_JITTER
        rmax = float(rights[keep].max())
        support = int((rights[keep] >= rmax - 2).sum())
        if support < MIN_SUPPORT:
            continue
        left = int(round(med))
        width = int(round(rmax)) - left + 1
        if not (WIDTH_LO * pred_w <= width <= WIDTH_HI * pred_w):
            continue
        # 条槽会空着（那时这一行没有白块），场景里的常亮白块不会 —— 空着的帧占一点
        # 比例的行更像条身
        dark = 1.0 - len(runs) / float(n)
        score = width * (1.0 - jitter / (MAX_JITTER + 1.0)) \
            * (0.5 + 0.5 * min(1.0, dark / 0.2))
        rows.append((score, r, left, width, jitter, len(runs), support))
    if not rows:
        return None
    rows.sort(key=lambda t: -t[0])
    _score, r, left, width, jitter, fill_frames, support = rows[0]
    ok = {t[1] for t in rows if t[3] >= BAND_FRAC * width}
    band = [r]
    k = r - 1
    while k in ok:
        band.append(k)
        k -= 1
    k = r + 1
    while k in ok:
        band.append(k)
        k += 1
    band.sort()
    right = left + width - 1
    return SlotShape(left=x0 + left, right=x0 + right,
                     band_top=y0 + band[0], band_bottom=y0 + band[-1],
                     frames=n, fill_frames=fill_frames, jitter=jitter,
                     support=support,
                     clipped=(left <= 0) or (right >= sw - 1),
                     clipped_left=(left <= 0), clipped_right=(right >= sw - 1))


def calibrate_video(video: str, ffmpeg: str, client_size=None, ui_scaler: float = 1.0,
                    size=None, frames: int = MAX_FRAMES, offset=None, stride: int = 1,
                    strip_fn=None, info=None):
    """量一段录像，返回一份校准报告（dict）；量不出来返回 None。

    client_size  客户区尺寸 (w, h)。不传就当成「这段录像正好就是客户区」。
    offset       客户区原点在画面里的位置 (dx, dy)。不传就自动对齐
                 （align.detect_offset）。自动对齐会**自己解**这个原点，
                 所以那份报告里的锚点（距底 / 距右）不能当真，只有条宽可信 ——
                 报告里用 auto_offset 标出来。
    stride       每 stride 帧取一帧来量（默认 1 = 连着取）。录像开头是菜单 / 剧情
                 的时候，连着取的前几百帧里根本没有费用条，放大步长才能扫到后面。
                 代价是要解码整段录像，所以只在前一种量不出来时再用。
    strip_fn     换帧来源用（录像 = ffmpeg 抽，窗口 = 屏幕抓）；info 传 probe_video 的
                 结果可以省一次探测（窗口没有文件可探）。
    """
    from .align import detect_offset

    if info is None:
        info = probe_video(ffmpeg, video)
    if size is None:
        size = info.size
    if not size:
        return None
    vw, vh = int(size[0]), int(size[1])
    cw, ch = (int(client_size[0]), int(client_size[1])) if client_size else (vw, vh)

    base = layout_for(cw, ch, ui_scaler)          # 客户区坐标下的预测
    auto = offset is None
    if auto:
        found = detect_offset(video, ffmpeg, client_size=(cw, ch),
                              ui_scaler=ui_scaler, size=(vw, vh),
                              strip_fn=strip_fn, info=info)
        if found is None:
            return None
        ox, oy = int(found[0]), int(found[1])
    else:
        ox, oy = int(offset[0]), int(offset[1])

    base_v = reframe_layout(base, ox, oy, vw, vh)   # 换算到画面坐标
    x0 = max(0, base_v.bar.x - PAD_X)
    x1 = min(vw, base_v.bar.x2 + 1 + PAD_RIGHT)
    y0 = max(0, base_v.bar.y - PAD_Y)
    y1 = min(vh, base_v.bar.y2 + 1 + PAD_Y)
    if x1 - x0 < 16 or y1 - y0 < 8:
        return None
    rect = Rect(x0, y0, x1 - x0, y1 - y0)
    strip = strip_fn or (lambda r, st, n: extract_strip(ffmpeg, video, r, st, n))
    slot = measure_slot(strip(rect, stride, frames), base_v.bar, x0, y0)
    if slot is None:
        return None

    edge = ui_edge_scale(ui_scaler)
    return {
        'video': video,
        'size': (vw, vh),
        'client': (cw, ch),
        'offset': (ox, oy),
        'auto_offset': auto,
        'ui_scaler': ui_scaler,
        'edge': edge,
        'bar_w_meas': slot.w,
        'bar_left_client': slot.left - ox,
        'bar_cy_from_bottom': ch - (slot.center_y - oy),
        'right_margin_px': cw - slot.w - (slot.left - ox),
        'band_h_meas': slot.band_bottom - slot.band_top + 1,
        's_meas': slot.w / (BAR_W_UNIT * edge),
        's_pred': base.scale,
        's_rule': ui_scale(cw, ch),
        'pred_bar_w': base.bar.w,
        'pred_bar_w_frac': base.bar_width_frac,
        'pred_bar_left': base.bar.x,
        'pred_bar_cy_from_bottom': ch - (base.bar.y + base.bar.h // 2),
        'jitter': slot.jitter,
        'support': slot.support,
        'frames': slot.frames,
        'fill_frames': slot.fill_frames,
        'probe': rect,
        'clipped': slot.clipped,
        # 量出来的条槽本体（左 / 右 / 条身上下行）。调用方要靠它把费用条 ROI 钉到
        # 像素上 —— 报告里那些 client/offset 是「拍」出来的，slot 才是尺子本身。
        'slot': slot,
    }


def record_from(rep: dict, source: str = None, date: str = None) -> dict:
    """把一份报告变成要写进校准表的那条记录。

    scale 一定写（它只由条宽定，和原点无关）；距底 / 距右只有在**原点是自己给的时候**
    才写 —— 自动对齐时那两个数是被拟合出来的，写进去等于把预测抄一遍当实测。
    """
    rec = {
        'scale': round(rep['s_meas'], 4),
        'edge': round(rep['edge'], 4),
        'bar_w': round(float(rep['bar_w_meas']), 1),
    }
    denom = rep['s_meas'] * rep['edge']
    if not rep['auto_offset'] and denom > 0:
        rec['bar_cy_unit'] = round(rep['bar_cy_from_bottom'] / denom, 2)
        rec['right_margin_unit'] = round(rep['right_margin_px'] / denom, 2)
    if source:
        rec['source'] = source
    if date:
        rec['date'] = date
    return rec


def _delta(meas, pred) -> str:
    d = meas - pred
    return '%+d' % int(round(d))


def format_report(rep: dict) -> str:
    """把报告排成人看的样子。"""
    cw, ch = rep['client']
    vw, vh = rep['size']
    pw = rep['pred_bar_w_frac'] if rep['pred_bar_w_frac'] > 0 else float(rep['pred_bar_w'])
    err = (rep['s_meas'] / rep['s_pred'] - 1.0) if rep['s_pred'] else 0.0
    lines = []
    lines.append('画面尺寸  : %dx%d' % (vw, vh))
    lines.append('客户区    : %dx%d' % (cw, ch))
    lines.append('客户区原点: %d,%d%s' % (
        rep['offset'][0], rep['offset'][1],
        '（自动对齐 —— 锚点仅供参考，不会写进表）' if rep['auto_offset'] else '（手工指定）'))
    lines.append('探针框    : x%d..%d y%d..%d（画面坐标）'
                 % (rep['probe'].x, rep['probe'].x2, rep['probe'].y, rep['probe'].y2))
    lines.append('证据      : %d 帧里 %d 帧看到白块；左边缘抖动 %.2fpx；'
                 '最大宽度 %d 帧支持' % (rep['frames'], rep['fill_frames'],
                                       rep['jitter'], rep['support']))
    lines.append('')
    lines.append('                实测             预测             差')
    lines.append('条槽宽度    %6.1f px      %6.1f px      %s px'
                 % (rep['bar_w_meas'], pw, _delta(rep['bar_w_meas'], pw)))
    lines.append('条槽高度    %6d px      %6d px      %s px'
                 % (rep['band_h_meas'], 8, _delta(rep['band_h_meas'], 8)))
    lines.append('scale       %8.4f        %8.4f        %+.2f%%'
                 % (rep['s_meas'], rep['s_pred'], 100.0 * err))
    lines.append('条左边缘    %6d（客户区）  %6d（客户区）  %s px'
                 % (rep['bar_left_client'], rep['pred_bar_left'],
                    _delta(rep['bar_left_client'], rep['pred_bar_left'])))
    lines.append('条槽中心    %6.1f（距底）  %6.1f（距底）  %s px'
                 % (rep['bar_cy_from_bottom'], rep['pred_bar_cy_from_bottom'],
                    _delta(rep['bar_cy_from_bottom'], rep['pred_bar_cy_from_bottom'])))
    lines.append('条槽右边距  %6d px' % rep['right_margin_px'])
    lines.append('')
    if rep['clipped']:
        lines.append('⚠ 白块顶到了探针边框：宽度只能是**下限**。'
                     '多半是预测偏得比较多，先把 --roi-offset 校正一下再量。')
    d_left = rep['bar_left_client'] - rep['pred_bar_left']
    d_cy = rep['bar_cy_from_bottom'] - rep['pred_bar_cy_from_bottom']
    if abs(err) <= 0.005 and abs(d_left) <= 3 and abs(d_cy) <= 3:
        lines.append('结论：实测和推算一致（条宽差 %.2f%%、位置差 %d/%d px）—— '
                     '这个尺寸不用校准。' % (100.0 * abs(err), int(round(d_left)),
                                          int(round(d_cy))))
    else:
        lines.append('结论：实测和推算不一致（条宽差 %.2f%%、位置差 %d/%d px）—— '
                     '加 --write 把实测值写进表。'
                     % (100.0 * abs(err), int(round(d_left)), int(round(d_cy))))
    return '\n'.join(lines)
