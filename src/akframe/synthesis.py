"""相位 -> 画面上那条可见的像素宽度。

这是前人项目（ArknightsCostBarRuler）从实测里反推出来的几何：
费用条真正的长度比可见的条槽宽 3.892%，左端有一小截在条槽外面（看不见），所以
相位 p 下画出来的可见宽度是

    raw = round(L * p - hidden) + 1        L = 1.03892 * 条槽宽, hidden = L - 条槽宽

再 clamp 到 [0, 条槽宽]；宽度不足 2px 时画面上等于没画（读数是 0）。

为什么要这个：线性假设「每个逻辑帧走 条槽宽/30 像素」只在中间段对 —— 一费的头 1~2 帧
条其实没动（宽度还是 0），尾端又会被 clamp。只有这条「相位-宽度」曲线才是可靠标尺，
计数和标定都必须以它为准。
"""

from __future__ import annotations

import math

BAR_LENGTH_RATIO = 1.03892     # 真实条长 / 可见条槽宽
MIN_DETECTABLE_WIDTH = 2       # 宽度小于这个就等于没画出来


def round_half_away(value: float) -> int:
    """四舍五入到整数，.5 一律远离 0（和 Rust f64::round 一致，别用 Python 的银行家舍入）。"""
    if value >= 0.0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def synthesized_width(total_bar_width: int, bar_width_frac: float, phase: float) -> int:
    """相位 phase（0..1）在条槽宽 total_bar_width 的屏幕上应该显示多少像素。

    bar_width_frac 是「没被裁、没取整」的真实条槽宽（亚像素），条长 L 必须用它来算；
    用取整后的整数会让若干帧的宽度差 1px，标定/计数都会在 round() 边界上抖。
    """
    if total_bar_width <= 0 or phase != phase:
        return 0
    if not (bar_width_frac > 0.0) or bar_width_frac != bar_width_frac:
        bar_width_frac = float(total_bar_width)

    bar_length = BAR_LENGTH_RATIO * bar_width_frac
    hidden = bar_length - float(total_bar_width)
    if phase < 0.0:
        phase = 0.0
    elif phase > 1.0:
        phase = 1.0

    raw = round_half_away(bar_length * phase - hidden) + 1
    if raw < MIN_DETECTABLE_WIDTH:
        return 0
    if raw > total_bar_width:
        return int(total_bar_width)
    return int(raw)
