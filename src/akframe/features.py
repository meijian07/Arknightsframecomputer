"""逐帧特征提取：费用条 / 暂停键 / 倍速键。

全部基于「相对阈值 + 占比」而不是绝对像素数，这样才能免疫按钮的亮/暗两种渲染，
也不依赖分辨率。实测依据见 docs/费用条读取逻辑.md。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import (ARROW_BRIGHT, ARROW_RATIO_MIN, CORR_2X_MIN, DIGIT_RATIO_MIN,
                       PAUSE_SHAPE_BAND, RUN_PAUSE_RATIO_MIN, SPEED_ARROW_BAND,
                       SPEED_DIGIT_BAND, SPEED_DIGIT_COLS, Layout)

# ---- 阈值 ----
# 纯白下限：实测填充恒为 255，用 250 而不是 240（240 会把接近白的场景像素也算进去）
WHITE_MIN = 250
# 「整体被压暗」的白：游戏切场景时会给整个 HUD 蒙一层半透明遮罩，条变成 151..165 的灰白。
# 亮度超过 MASKED_WHITE_MAX 就说明场景是亮的、这帧不该按压暗处理。
MASKED_WHITE_MIN = 150
MASKED_WHITE_MAX = 165
GREY_MAX = 146       # 满费底色（不透明，实测精确等于 146）
GREY_FADE = 88       # 满费退出过渡底色，持续约 14 帧
GREY_TOL = 10
# 判 MAX / MAX_FADE 时，落在目标灰度 ±10 的像素数下限。
# **必须是占条宽的比例，不能是绝对像素数**：离线验证时条宽 240，150 = 62.5%；
# 换到 120 宽的窗口上，如果还用 150 就永远判不出满费（实时链路实测踩到了这个坑）。
BAR_MIN_FRAC = 0.625
REL_THR = 0.35       # 相对阈值：median + REL_THR * (max - median)
GRAY_TOL = 20        # 灰度校验：R/G/B 两两之差超过它就不算灰度像素
VALIDITY_LEFT_INSET = 1   # 判「条还在不在」时跳过最左 1px（HUD 高光高发区）


@dataclass
class BarReading:
    """费用条读数。"""
    fill: int        # 条槽左端起、到最右侧白像素的宽度，0..bar_w
    grey146: int     # 落在 146±10 的像素数
    grey88: int      # 落在 88±10 的像素数
    state: str       # TRACK / MAX / MAX_FADE / EMPTY
    row: np.ndarray  # 逐列中位数（调试用）
    masked: bool = False   # 是不是按「整体压暗」那一遍扫出来的
    gray_ok: bool = True   # 彩色输入时的灰度校验有没有通过


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


def _band_is_grayscale(band: np.ndarray) -> bool:
    """彩色输入（BGRA）的灰度校验：R/G/B 两两之差都在 GRAY_TOL 内才算灰度像素。

    为什么需要：条槽右边紧挨着费用数字/装饰，非灰度像素混进来会把「白」判错。
    我们的链路正常都是先转灰度（离线 ffmpeg format=gray、实时 to_gray），
    这个校验只是给直接喂彩色的调用兜底。
    """
    lo = band.min(axis=2).astype(np.int16)
    hi = band.max(axis=2).astype(np.int16)
    return bool(((hi - lo) <= GRAY_TOL).all())


def _scan_fill(row: np.ndarray) -> tuple:
    """从右端往左扫出填充宽度，返回 (fill, masked)。

    两遍扫描：先按纯白（>250）找；找不到再按「整体压暗的白」（151..165）找。
    第二遍里如果在找到白像素之前先撞到 >165 的像素，就判这帧读不准（返回 0）——
    那是场景亮起来了，不是条被压暗。

    为什么从右往左而不是从左往右数连续白像素：左数只在「从条槽最左端起、白像素完全
    连续」时才成立，而实测里最左 1px 在满费转空的那一帧会沾上 HUD 高光、条中间偶尔
    也会有一两个暗像素，都会把左数打断 —— 那正是以前「一费只记到 22~28 帧」的来源之一。
    """
    n = int(row.size)
    if n <= 0:
        return 0, False
    left = min(VALIDITY_LEFT_INSET, n - 1)   # 判有效性时跳过的最左像素数

    # ---- 第一遍：纯白 ----
    if row[n - 1] > WHITE_MIN:
        return n, False
    if n > 1:
        hit = np.nonzero(row[left:n - 1] > WHITE_MIN)[0]
        if hit.size:
            return int(hit[-1]) + left + 1, False
    if left > 0 and row[0] > WHITE_MIN:
        return 1, False

    # ---- 第二遍：整体压暗的白 ----
    end = int(row[n - 1])
    if MASKED_WHITE_MIN < end <= MASKED_WHITE_MAX:
        return n, True
    if end > MASKED_WHITE_MAX:
        return 0, False
    if n > 1:
        seg = row[left:n - 1]
        too_bright = np.nonzero(seg > MASKED_WHITE_MAX)[0]
        masked = np.nonzero(seg > MASKED_WHITE_MIN)[0]
        if too_bright.size and (not masked.size or too_bright[-1] > masked[-1]):
            return 0, False
        if masked.size:
            return int(masked[-1]) + left + 1, True
    if left > 0 and MASKED_WHITE_MIN < row[0] <= MASKED_WHITE_MAX:
        return 1, True
    return 0, False


def read_bar(roi: np.ndarray, layout: Layout) -> BarReading:
    """读费用条。

    fill 的物理含义（和前人实现 ArknightsCostBarRuler 一致）：条槽左端起、
    到「最右侧那个白像素」为止的宽度，0..bar_w。做法见 _scan_fill。
    """
    r0, r1 = layout.bar_band
    band = roi[r0:r1 + 1, :]
    if band.ndim == 3:
        gray_ok = _band_is_grayscale(band)
        row = np.median(band.mean(axis=2), axis=0).astype(np.int16)
    else:
        gray_ok = True
        row = np.median(band, axis=0).astype(np.int16)

    if gray_ok:
        fill, masked = _scan_fill(row)
    else:
        fill, masked = 0, False

    grey146 = int((np.abs(row - GREY_MAX) <= GREY_TOL).sum())
    grey88 = int((np.abs(row - GREY_FADE) <= GREY_TOL).sum())

    min_count = max(8, int(round(BAR_MIN_FRAC * row.size)))
    # 灰底判定必须放在前面，而且要压过白像素读数：
    # 满费时条槽会被整条换成灰底，同时**在条槽里画出白色的费用数字（99）**——
    # 从右往左扫会把这个数字当成填充（实测读到 145px，而参考值是 0）。
    # 反过来，正常推进时条槽里只有白填充 + 暗轨道（实测 55..68），不会有灰底，
    # 所以「灰底占多数」只可能是满费/满费退出。
    if grey146 >= min_count:
        state, fill = 'MAX', 0
    elif grey88 >= min_count:
        state, fill = 'MAX_FADE', 0
    elif fill <= 4:
        state = 'EMPTY'
    else:
        state = 'TRACK'
    return BarReading(fill=fill, grey146=grey146, grey88=grey88, state=state, row=row,
                      masked=masked, gray_ok=gray_ok)


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
        # 没有数字有两种完全不同的情况，必须分开：
        #   (a) 真子弹时间（>>>）：箭头带里画着三条锐利的白箭头（实测 255）；
        #   (b) 按钮淡出/整块变暗（过场、部署慢动作、暂停遮罩）：什么都有没画。
        # 旧逻辑一律当成 (a)，于是挂钟通道按 1/5 速率算 —— 实测一段 2 倍速录像里
        # 有 713 拍被这样误判，白白少记 140 帧（56 秒里差了 2.3 秒）。
        ar0, ar1 = layout.speed.band_rows(*SPEED_ARROW_BAND)
        arrow = roi[ar0:ar1 + 1, :]
        arrow_ratio = float((arrow > ARROW_BRIGHT).sum()) / float(arrow.size)
        if arrow_ratio >= ARROW_RATIO_MIN:
            return SpeedReading(digit_ratio=digit_ratio, corr_2x=corr, has_digit=False,
                                m=float('nan'), bullet=True, conf='ok')
        # 倍速信息这一刻根本不存在于画面上：沿用上次已知倍速，也不按 1/5 打折
        return SpeedReading(digit_ratio=digit_ratio, corr_2x=corr, has_digit=False,
                            m=float('nan'), bullet=False, conf='low')

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
