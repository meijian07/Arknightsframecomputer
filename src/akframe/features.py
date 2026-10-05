"""逐帧特征提取：费用条 / 暂停键 / 倍速键。

全部基于「相对阈值 + 占比」而不是绝对像素数，这样才能免疫按钮的亮/暗两种渲染，
也不依赖分辨率。实测依据见 docs/费用条读取逻辑.md。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import (ARROW_MIN_CONTRAST, ARROW_RATIO_MIN, ARROW_REL, CORR_2X_MIN,
                       DIGIT_RATIO_MIN, PAUSE_SHAPE_BAND, RUN_PAUSE_RATIO_MIN,
                       SPEED_ARROW_BAND, SPEED_DIGIT_BAND, SPEED_DIGIT_COLS, Layout)

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

# ---- 亮度自适应（HDR / 整体压暗）----
# PC 端打开 HDR 之后，视频/抓屏的像素是 PQ 编码的，UI 白会从 255 掉到 ~122
# （实测用户两段 NVIDIA 录屏：白填充 122、轨道 51..68；同一天 SDR 录像里是 255 / 55..68）。
# 暂停键、倍速键读得到（它们本来就用相对阈值），只有费用条永远 EMPTY ——
# 因为费用条的判据全是绝对阈值（250 / 150..165 / 146 / 88）。费用条一 EMPTY，
# 状态机就退回挂钟通道，画面上的表现就是「不看条了，按 1 秒 30 帧瞎涨」。
# 处理办法：主路径（绝对阈值）完全读不到东西时，再按这一行自己的白电平抬回 255 试一次。
# 只在主路径读不到时才走这一步，所以已经标定过的 SDR 画面逐像素不变。
# 白/暗电平由 _row_levels() 统一给（1 / 99 分位，理由见那里）
DIM_HI_MIN = 100       # 行里没有这个亮度以上的高光，就不可能是被压暗的游戏 HUD
DIM_HI_MAX = MASKED_WHITE_MIN   # = 150。到这一档就当成 SDR 的半透明渲染（蒙遮罩/渐变），
                                # 不做 HDR 还原：实测 SDR 满费退出过渡那一行的亮点是 154，
                                # 只看「没有 255 的纯白」会把它误当成 HDR 去还原
DIM_MIN_CONTRAST = 25  # 还原之前要求行内本来就有这个对比度，否则是在放大噪声
WHITE_REF = 255.0
# HDR10 / PQ（SMPTE ST 2084）常数：把「被 PQ 压暗的码值」反变换回亮度要用
PQ_M1 = 0.1593017578125
PQ_M2 = 78.84375
PQ_C1 = 0.8359375
PQ_C2 = 18.8515625
PQ_C3 = 18.6875
SDR_BREAK = 0.0031308  # sRGB 编码的分段点
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
    dim: bool = False      # 是不是靠「亮度自适应」那一遍读出来的（HDR / 压暗画面）


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


ROW_DARK_PCTL = 1.0     # 暗电平（轨道）取 1 分位
ROW_WHITE_PCTL = 99.0   # 白电平（填充）取 99 分位


def _row_levels(row: np.ndarray) -> tuple:
    """行内「暗电平（轨道）/ 白电平（填充）」两个水位。

    **必须用 1 / 99 分位，不能用 5 / 95**：填充最多能占整行的 97%（一费最后一帧实测
    232~233 / 240），这时第 5 百分位已经落进填充里（暗电平 = 白电平，台阶判据和压暗
    判据都会自己判死自己）；反过来填充只有 3% 时第 95 百分位又落进轨道里。1 / 99
    在 240px 的条槽上只吃最边上 ~2px，两端都留得住。
    """
    return (float(np.percentile(row, ROW_DARK_PCTL)),
            float(np.percentile(row, ROW_WHITE_PCTL)))


def _row_is_step(row: np.ndarray, fill: int) -> bool:
    """条的形状检查：必须是「左亮右暗」的一个台阶。

    fill 左边（跳过最左 1px）至少 70% 是亮点、右边至少 70% 是暗点。
    阈值取自己暗/白电平（_row_levels，1 / 99 分位）的中点 —— 不能用中位数：
    填充超过半条时中位数本身就变成填充值了，也不能用 p5/p95：填充占 97%（一费最后
    一帧）时 p5 落进填充、只占 3%（一费第一帧）时 p95 落进轨道，两种极端都会把
    真台阶判掉。
    为什么必须有这道闸：亮度自适应把整行抬亮的同时，也把 UI 杂色抬成了假填充 ——
    实测一段暂停菜单录像被误判成「整条 TRACK」，于是那些帧进了像素通道。
    台阶形状是费用条独有的：场景再亮，也不会有 200px 宽的一整条台阶。
    """
    n = row.size
    lo, hi = _row_levels(row)
    thr = lo + 0.5 * (hi - lo)
    left = min(VALIDITY_LEFT_INSET, n - 1)
    lseg = row[left:fill]
    if lseg.size and float((lseg > thr).sum()) / lseg.size < 0.7:
        return False
    rseg = row[fill:]
    if rseg.size and float((rseg <= thr).sum()) / rseg.size < 0.7:
        return False
    return True


def _dim_anchor(row: np.ndarray):
    """判断这一行是不是「整体被压暗」，是就返回白电平（0..255），不是就返回 None。

    SDR 白是 255（不需要还原），HDR10 的白实测 ~122。两个都要先要求行里本来就有
    对比度：关卡外的暗场景（实测整行 9..24）如果只看「白电平低」就抬亮，
    整行会被抬到 88 —— 正好落进满费退出灰底的判据，于是关卡外也一秒 30 帧地涨。
    所以「有高光」本身就是必要条件：没有 >= DIM_HI_MIN 的高光，就不是被压暗的游戏 HUD。
    """
    lo, hi = _row_levels(row)
    if not (DIM_HI_MIN <= hi < DIM_HI_MAX) or hi - lo < DIM_MIN_CONTRAST:
        return None
    return hi


def brighten_row(row: np.ndarray):
    """按白电平等比抬亮一行（只抬不压）；判断为不需要时返回 None。

    只给「填充」判据用：填充就是白电平附近的一整块，等比抬亮之后阈值等价于
    0.965×白，比 PQ 还原稳 —— PQ 在白色附近斜率极大，编码上 122±2 的抖动
    还原出来是 214..255，填充边界会碎掉。
    """
    hi = _dim_anchor(row)
    if hi is None:
        return None
    return np.clip(row.astype(np.float32) * (WHITE_REF / hi), 0, 255).astype(np.int16)


def _pq_eotf(code):
    """PQ 码值(0..1) → 相对亮度(0..1，1.0 = 10000 cd/m²)。SMPTE ST 2084 反变换。"""
    p = np.power(np.clip(code, 0.0, 1.0), 1.0 / PQ_M2)
    return np.power(np.maximum(p - PQ_C1, 0.0) / (PQ_C2 - PQ_C3 * p), 1.0 / PQ_M1)


def _sdr_oetf(lum):
    """线性亮度(0..1) → sRGB 码值(0..1)。"""
    x = np.clip(lum, 0.0, 1.0)
    return np.where(x <= SDR_BREAK, x * 12.92,
                    1.055 * np.power(np.maximum(x, 1e-8), 1.0 / 2.4) - 0.055)


def restore_pq_row(row: np.ndarray):
    """把被 PQ 压暗的一行还原回 SDR 码值；判断为不需要时返回 None。

    只给「灰底」判据用。做法：白电平 hi 对应的亮度当作 SDR 白，逐像素先 PQ 反变换
    成亮度、除以白电平、再走 sRGB 编码。实测（HDR 白 122）：满费灰底 94 → ~146、
    满费退出过渡 70 → ~88，正好落回原来的绝对判据。
    等比抬亮做不到这件事：它会把 94 抬成 197，两个灰底判据都命中不了。
    """
    hi = _dim_anchor(row)
    if hi is None:
        return None
    white = float(_pq_eotf(hi / 255.0))
    if white <= 0.0:
        return None
    lum = _pq_eotf(row.astype(np.float64) / 255.0) / white
    return np.clip(_sdr_oetf(lum) * 255.0, 0, 255).astype(np.int16)


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
    dim = False
    if gray_ok and _dim_anchor(row) is not None:
        # 整幅画面被压暗（HDR / PQ 采集）时 146 / 88 这些绝对阈值没有意义：
        # 实测 HDR 下满费灰底是 94，正好落进「满费退出过渡 88±10」区间，
        # 真正的满费会被读成过渡态。先清零，交给下面的还原通道重读。
        grey146 = grey88 = 0
    if fill <= 4 and grey146 < min_count and grey88 < min_count and not masked:
        # 主路径一点东西都没读到：可能整幅画面被压暗（HDR / PQ 采集）。
        # 填充按白电平等比抬亮再扫一遍；灰底按 PQ 曲线还原再数一遍；
        # 两边都读不到就维持原判（EMPTY）。
        bright = brighten_row(row)
        if bright is not None:
            f2, m2 = _scan_fill(bright)
            f2 = f2 if _row_is_step(bright, f2) else 0
            fixed = restore_pq_row(row)
            if fixed is None:
                g146 = g88 = 0
            else:
                g146 = int((np.abs(fixed - GREY_MAX) <= GREY_TOL).sum())
                g88 = int((np.abs(fixed - GREY_FADE) <= GREY_TOL).sum())
            if f2 > 4 or g146 >= min_count or g88 >= min_count:
                fill, masked = f2, m2
                grey146, grey88 = g146, g88
                dim = True
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
                      masked=masked, gray_ok=gray_ok, dim=dim)


def read_pause(roi: np.ndarray, layout: Layout) -> PauseReading:
    """读暂停键。

    只取字形顶部的条带：单三角（已暂停）顶部很窄，双竖条（运行中）顶部是两个整宽矩形，
    两者亮点数差约 4 倍（实测 0.063 vs 0.24），所以用占比判别很稳。
    """
    if not layout.buttons_visible:
        # 画面里根本没有暂停键（截屏 / 放大 / 裁剪录像只截了右下角）：这是「读不到」，
        # 不是「读到暂停」。返回「运行中」而不是猜「已暂停」，理由：
        #   - 条还在画面上，条不动时像素通道自己就不涨，「已暂停」并不能多保护什么；
        #   - 反过来，误判成「已暂停」会把满费挂钟通道整段掐掉 —— 那是系统性少记。
        # 代价：满费（没有相位信息、要走挂钟）期间真按暂停，挂钟会多走几帧。
        return PauseReading(ratio=1.0, running=True)
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
    if not layout.buttons_visible:
        # 同 read_pause：倍速键不在画面里。返回「不知道倍速」（m=nan），
        # 让挂钟通道按既有规则冻结，而不是硬按 1 倍速补帧。
        return SpeedReading(digit_ratio=0.0, corr_2x=float('nan'), has_digit=False,
                            m=float('nan'), bullet=False, conf='low')
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
        # 箭头带里「亮」的判据必须是相对的：绝对阈值 200 在 HDR 录像上命中率 0.0000
        # （实测 UI 白只有 122、箭头带 p99 只有 91），子弹时间永远认不出来。
        # 相对判据 = 这一帧自己的白电平，换 HDR 亮度档 / 换 SDR 白电平都不用改常数。
        ar0, ar1 = layout.speed.band_rows(*SPEED_ARROW_BAND)
        arrow = roi[ar0:ar1 + 1, :]
        hi = float(np.percentile(roi, 99.0))
        med = float(np.median(roi))
        if hi - med >= ARROW_MIN_CONTRAST:
            thr_arrow = med + ARROW_REL * (hi - med)
            arrow_ratio = float((arrow >= thr_arrow).sum()) / float(arrow.size)
        else:
            # 整块 ROI 没有对比度（按钮淡出 / 被遮罩糊平）：画面上根本没画东西
            arrow_ratio = 0.0
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
