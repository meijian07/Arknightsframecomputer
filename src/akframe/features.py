"""逐帧特征提取：费用条 / 暂停键 / 倍速键。

全部基于「相对阈值 + 占比」而不是绝对像素数，这样才能免疫按钮的亮/暗两种渲染，
也不依赖分辨率。实测依据见 docs/费用条读取逻辑.md。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import (CORR_2X_MIN, DIGIT_RATIO_MIN, PAUSE_SHAPE_BAND,
                       RUN_PAUSE_RATIO_MIN, SPEED_DIGIT_BAND, SPEED_DIGIT_COLS,
                       Layout)

WHITE_MIN = 240      # 白色填充下限（实测填充恒为 255，未填充轨道 <= 69）
# 判 MAX / MAX_FADE 时，落在目标灰度 ±10 的像素数下限。
# **必须是占条宽的比例，不能是绝对像素数**：离线验证时条宽 240，150 = 62.5%；
# 换到 120 宽的窗口上，如果还用 150 就永远判不出满费（实时链路实测踩到了这个坑）。
BAR_MIN_FRAC = 0.625
GREY_MAX = 146       # 满费底色（不透明，实测精确等于 146）
GREY_FADE = 88       # 满费退出过渡底色，持续约 14 帧
GREY_TOL = 10
REL_THR = 0.35       # 相对阈值：median + REL_THR * (max - median)


@dataclass
class BarReading:
    """费用条读数。"""
    fill: int        # 左端起连续 >=240 的像素数，0..bar_w
    grey146: int     # 落在 146±10 的像素数
    grey88: int      # 落在 88±8 的像素数
    state: str       # TRACK / MAX / MAX_FADE / EMPTY
    row: np.ndarray  # 逐列中位数（调试用）


@dataclass
class PauseReading:
    """暂停键读数。"""
    ratio: float     # 形状特征占比
    running: bool    # True = 正在运行，False = 已暂停


@dataclass
class SpeedReading:
    """倍速键读数。"""
    digit_ratio: float    # 数字条带亮点占比
    corr_2x: float        # 与 2X 模板的相关系数（无模板时为 nan）
    has_digit: bool       # 是否显示倍速数字
    m: float              # 倍速；子弹时间时沿用上一次已知值
    bullet: bool          # 是否子弹时间（>>>）
    conf: str             # 倍速判断的置信度：ok / low


def rel_threshold(roi: np.ndarray) -> float:
    """相对阈值：对亮度不敏感的「什么是亮点」判据。

    按钮有亮/暗两种渲染（暗态半透明、能透出场景），绝对值阈值会失效；
    用 median/max 归一化后，两种渲染下判据一致。
    """
    med = float(np.median(roi))
    mx = float(roi.max())
    return med + REL_THR * (mx - med)


def read_bar(roi: np.ndarray, layout: Layout) -> BarReading:
    """读费用条。

    fill 的物理含义：条槽左端起连续亮像素的长度。
    未填充部分在场景亮时也可能出现亮像素（实测 1132 帧里只有 7 帧轨道 >100，
    且没有一帧 >240），所以阈值取 240 很安全，偶发异常交给状态机的容差剔除。
    """
    r0, r1 = layout.bar_band
    row = np.median(roi[r0:r1 + 1, :], axis=0).astype(np.int16)

    fill = 0
    while fill < row.size and row[fill] >= WHITE_MIN:
        fill += 1

    grey146 = int((np.abs(row - GREY_MAX) <= GREY_TOL).sum())
    grey88 = int((np.abs(row - GREY_FADE) <= GREY_TOL).sum())

    min_count = max(8, int(round(BAR_MIN_FRAC * row.size)))
    if fill <= 4 and grey146 >= min_count:
        state = 'MAX'
    elif fill <= 4 and grey88 >= min_count:
        state = 'MAX_FADE'
    elif fill <= 4:
        state = 'EMPTY'
    else:
        state = 'TRACK'
    return BarReading(fill=fill, grey146=grey146, grey88=grey88, state=state, row=row)


def read_pause(roi: np.ndarray, layout: Layout) -> PauseReading:
    """读暂停键。

    只取字形顶部的条带：单三角（已暂停）顶部很窄，双竖条（运行中）顶部是两个整宽矩形，
    两者亮点数差约 4 倍（实测 0.063 vs 0.24），所以用占比判别很稳。
    """
    r0, r1 = layout.pause.band_rows(*PAUSE_SHAPE_BAND)
    band = roi[r0:r1 + 1, :]
    thr = rel_threshold(roi)
    ratio = float((band > thr).sum()) / float(band.size)
    return PauseReading(ratio=ratio, running=ratio >= RUN_PAUSE_RATIO_MIN)


def read_speed(roi: np.ndarray, layout: Layout, templates=()) -> SpeedReading:
    """读倍速键。

    三种形态：
        2X + 双箭头   粗白、不透明
        1X + 单箭头   细、半透明
        >>>          只有三条细箭头，不显示数字（子弹时间）

    子弹时间下倍速信息根本不存在于画面上，只能沿用上一次已知值 —— 这是信息缺失，
    不是实现问题。同理，字形处于过渡/异形状态（匹配不上任何模板）时也沿用上次值，
    并标记 conf='low'，绝不瞎猜一个倍速。

    templates: [(倍速值, 名字, 归一化模板数组), ...]，由 cli 从 templates/ 目录加载。
    """
    thr = rel_threshold(roi)
    r0, r1 = layout.speed.band_rows(*SPEED_DIGIT_BAND)
    c0, c1 = layout.speed.cols_range(*SPEED_DIGIT_COLS)
    band = roi[r0:r1 + 1, c0:c1 + 1]

    digit_ratio = float((band > thr).sum()) / float(band.size)
    has_digit = digit_ratio >= DIGIT_RATIO_MIN

    corr = float('nan')
    if not has_digit:
        return SpeedReading(digit_ratio=digit_ratio, corr_2x=corr,
                            has_digit=False, m=float('nan'), bullet=True, conf='ok')

    # 归一化：按钮有亮/暗两种渲染，绝对值阈值和直接比较都会失效，
    # 用 (值 - 中位数) / (峰值 - 中位数) 归一化后再比形状。
    med = float(np.median(roi))
    span = max(1.0, float(roi.max()) - med)
    norm = np.clip((band.astype(np.float32) - med) / span, 0.0, 1.0)

    best_m = None
    best_name = ''
    best_corr = -2.0
    for m_val, name, arr in templates:
        # 模板是在某个特定窗口尺寸下采集的，形状不一定和当前 ROI 一致。
        # 直接按最近邻缩放过去再比：UI 是等比缩放的，这样换分辨率也能用同一批模板。
        tpl = arr if arr.shape == norm.shape else _resize_nearest(arr, *norm.shape)
        if tpl is None:
            continue
        c = _ncc(norm, tpl)
        if c > best_corr:
            best_m, best_name, best_corr = m_val, name, c
    corr = best_corr if best_m is not None else float('nan')

    if best_m is None or best_corr < CORR_2X_MIN:
        # 匹配不上：沿用上次已知倍速（由 state.Counter 负责），并标记低置信
        return SpeedReading(digit_ratio=digit_ratio, corr_2x=corr,
                            has_digit=True, m=float('nan'), bullet=False, conf='low')

    conf = 'low' if best_corr < CORR_2X_MIN + 0.15 else 'ok'

    return SpeedReading(digit_ratio=digit_ratio, corr_2x=corr,
                        has_digit=True, m=best_m, bullet=False, conf=conf)


def _resize_nearest(src: np.ndarray, h: int, w: int):
    """最近邻缩放（不依赖 PIL，保持 features 只依赖 numpy）。"""
    sh, sw = src.shape
    if sh <= 0 or sw <= 0 or h <= 0 or w <= 0:
        return None
    yi = np.clip((np.arange(h) * sh) // h, 0, sh - 1)
    xi = np.clip((np.arange(w) * sw) // w, 0, sw - 1)
    return src[yi][:, xi]


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """归一化互相关，范围 -1..1。对整体亮度缩放不敏感。"""
    av = a.ravel().astype(np.float32) - float(a.mean())
    bv = b.ravel().astype(np.float32) - float(b.mean())
    na = float(np.sqrt((av * av).sum()))
    nb = float(np.sqrt((bv * bv).sum()))
    if na <= 0 or nb <= 0:
        return 0.0
    return float((av * bv).sum() / (na * nb))
