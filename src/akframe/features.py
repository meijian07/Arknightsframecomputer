"""逐帧特征提取：费用条 / 暂停键 / 倍速键。

全部基于「相对阈值 + 占比」而不是绝对像素数，这样才能免疫按钮的亮/暗两种渲染，
也不依赖分辨率。实测依据见 docs/费用条读取逻辑.md。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import (ARROW_MIN_CONTRAST, ARROW_REL, ARROW_SOLID_MIN,
                       CORR_2X_MIN, DIGIT_RATIO_MIN, PAUSE_GLYPH_MIN, PAUSE_SHAPE_BAND,
                       RUN_PAUSE_RATIO_MIN, SPEED_ARROW_BAND, SPEED_DIGIT_BAND,
                       SPEED_DIGIT_COLS, Layout)

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

# ---- 局内负费：费用数字左边那个「减号」 ----
# 判据照抄前人（cost_bar.rs 的 is_cost_negative），只有三条：
#   ① 纯白：> WHITE_MIN(250)。数字的抗锯齿边只有「亮」，凑不出一条长纯白横杠；
#   ② 这条纯白横杠要**连续**够长（SIGN_MIN_RUN_UNIT）—— 数字的竖笔画只有 5~8px，
#      而减号本身就是一条横杠，整条都是纯白；
#   ③ 有一行满足就够（720p 下减号的纯白芯只有一行高，分辨率越高越厚）。
# 前人在 1280x720 上量出来的下限是 14px，换成 s = 1（1920x1080）就是 21px。
# 实测依据（用户截图 屏幕截图 2026-10-02 104850.png，客户区 1905x1085）：
# 「22」这两个数字最长的纯白横杠是 18px（y=804，阈值 21），判定为「不是负费」——
# 正常画面下留了 3px 余量，见 docs 第 31 节。
SIGN_MIN_RUN_UNIT = 21.0
SIGN_MIN_ROWS = 1
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
class SignReading:
    """局内负费读数（费用数字左边那个减号）。

    negative 是给状态机用的那一个；run / rows / thr 是给面板调框用的原始证据 ——
    认不出负费时，先看 run 离 thr 差多少，比猜阈值省事。
    """
    negative: bool    # 扫框里出现了那条够长的纯白横杠
    run: int          # 扫框里实测的**最长**纯白连续段（像素）
    rows: int         # 有多少行出现了够长的横杠
    thr: int          # 这一拍用的宽度阈值（= SIGN_MIN_RUN_UNIT x scale，四舍五入）


@dataclass
class PauseReading:
    """暂停键读数。"""
    ratio: float     # 形状特征占比
    running: bool    # True = 正在运行，False = 已暂停
    # 这个键**画出来了没有**（实心字形在形状带里的占比，阈值见 PAUSE_GLYPH_MIN）。
    # 为什么必须有它：菜单 / 加载 / 桌面 / 切场景这些画面上，ROI 里根本没有按钮，
    # 形状占比只有 0.000~0.0146，和「已暂停」（0.064）分不开 —— 旧代码只能当成
    # 「已暂停」，于是分不清「关卡里按了暂停」和「压根不在关卡里」。
    # 默认 True：老调用方不知道这件事时，行为跟以前完全一样。
    drawn: bool = True
    solid: float = 0.0   # 上面那个判据用的**实心占比**（3x3 腐蚀一遍后的），
                         # 自检 / 面板显示用：ratio 到 0.24 也可能一个实心块都没有。


@dataclass
class SpeedReading:
    """倍速键读数。"""
    digit_ratio: float    # 数字条带亮点占比
    corr_2x: float        # 与 2X 模板的相关系数（无模板时为 nan）
    has_digit: bool       # 是否显示倍速数字
    m: float              # 倍速；子弹时间时沿用上一次已知值
    bullet: bool          # 是否子弹时间（>>>）
    conf: str             # 倍速判断的置信度：ok / low
    # 倍速键画出来了没有（有数字，或者箭头带里有**实心**箭头块）。默认 True，理由同上。
    drawn: bool = True
    solid: float = 0.0   # 箭头带的实心占比（判 drawn 用的那个数），自检用
    # 「三个三角（>>>）」的形状分：跟 templates/bullet.npy 的最大相关系数（见
    # GLYPH_NCC_MIN 那段）。子弹时间就是靠它认的；读得到数字时不算，是 nan。
    glyph: float = float('nan')


def rel_threshold(roi: np.ndarray) -> float:
    """相对阈值：对亮度不敏感的「什么是亮点」判据。

    按钮有亮/暗两种渲染（暗态半透明、能透出场景），绝对值阈值会失效；
    用 median/max 归一化后，两种渲染下判据一致。
    """
    med = float(np.median(roi))
    mx = float(roi.max())
    return med + REL_THR * (mx - med)


def erode3(mask: np.ndarray) -> np.ndarray:
    """3x3 全邻域腐蚀一次：只有「自己和周围八格都亮」的像素留下。

    为什么要这一步：rel_threshold 是跟着画面自己的电平走的，细笔画 / 噪点在暗画面上
    很容易被放大成「亮点」。腐蚀一遍之后细笔画全没了，只有**画上去的实心形状**
    （▶ / ❚❚ / 箭头 / 数字笔画）活得下来 —— 「这块地方到底画没画东西」才判得准。
    用切片实现，不依赖 scipy（打包出来的 exe 里只有 numpy）。
    """
    e = mask.copy()
    e[1:, :] &= mask[:-1, :]
    e[:-1, :] &= mask[1:, :]
    e[:, 1:] &= mask[:, :-1]
    e[:, :-1] &= mask[:, 1:]
    e[1:, 1:] &= mask[:-1, :-1]
    e[:-1, :-1] &= mask[1:, 1:]
    e[1:, :-1] &= mask[:-1, 1:]
    e[:-1, 1:] &= mask[1:, :-1]
    return e


def solid_frac(mask: np.ndarray, times: int = 1) -> float:
    """亮块占比：连续 times 次 3x3 腐蚀之后还剩多少像素（占整块面积的比例）。"""
    m = mask
    for _ in range(max(1, int(times))):
        m = erode3(m)
        if not m.any():
            return 0.0
    return float(m.mean())


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


def fill_run(row: np.ndarray, gap: int = 2):
    """一行里「白填充」的列区间 (起, 止)，含两端；这一行没有填充时返回 None。

    与 _scan_fill 用同一套判据（绝对白 250，读不到就按「整体压暗」的白电平抬亮重扫），
    区别是它同时给出**左端** —— 校准工具要的是「白块铺在条槽的哪一段」，只有右端的话
    没法知道条槽左边缘在哪。

    取的是**最长的那一段连续白**（允许 gap 列空隙），不是「第一个白到最后一个白」：
    条槽左边紧挨着费用数字 / 场景里的高光，按首尾取会把区间拉到条槽外面去。
    """
    n = int(row.size)
    if n <= 0:
        return None
    for band in (row, brighten_row(row)):
        if band is None:
            continue
        hit = np.nonzero(band > WHITE_MIN)[0]
        if hit.size == 0:
            continue
        runs = []
        s = p = int(hit[0])
        for q in hit[1:]:
            q = int(q)
            if q - p <= gap + 1:
                p = q
            else:
                runs.append((s, p))
                s = p = q
        runs.append((s, p))
        s, e = max(runs, key=lambda t: t[1] - t[0])
        return (s, e)
    return None


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


def brighten_row(row: np.ndarray, roi: np.ndarray = None):
    """按白电平等比抬亮一行（只抬不压）；判断为不需要时返回 None。

    只给「填充」判据用：填充就是白电平附近的一整块，等比抬亮之后阈值等价于
    0.965×白，比 PQ 还原稳 —— PQ 在白色附近斜率极大，编码上 122±2 的抖动
    还原出来是 214..255，填充边界会碎掉。

    roi 给了就多一条兜底（只在这一行自己判不出「被压暗」时才用）：**条画满的时候
    整行都是白，行内一点对比度都没有**（实测 HDR 录像里 226 列全是 124），
    _dim_anchor 的对比度闸会把这种行判掉 —— 于是「条正好画满」的那几帧读成 EMPTY。
    代价是实打实的：那几帧本来该按「相位贴在 1.0」处理（回费边界），读成空条之后
    会提前一拍触发 wrap 判定，边界归属就差 1 帧。
    兜底用整个 ROI 的电平：ROI 里除了条身还有上下那几行暗轨道/背景，对比度一直存在，
    所以满条也能判出来；而空条时整个 ROI 都是暗的（p99 < DIM_HI_MIN），照样不还原。
    """
    hi = _dim_anchor(row)
    if hi is None and roi is not None:
        hi = _dim_anchor(np.asarray(roi).ravel())
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
        bright = brighten_row(row, roi)
        if bright is not None:
            f2, m2 = _scan_fill(bright)
            # 台阶闸（_row_is_step）对「条画满」这一种情况天然判不过：整行都是白，
            # 行内没有对比度，阈值取在哪儿都会把亮点比例压到 70% 以下。可是
            # 「最右一列就是白的」本来就是主路径（SDR）认满条的判据（_scan_fill 直接
            # 返回 n），没道理在压暗兜底这条路上反而不认。所以 f2 铺满整行时放行，
            # 其余情况照旧要过台阶闸 —— 那道闸防的是「把场景杂色抬成假填充」，
            # 而能走到兜底，说明 ROI 的电平已经确认过是被压暗的游戏 HUD（见 brighten_row）。
            f2 = f2 if (f2 >= row.size or _row_is_step(bright, f2)) else 0
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


def read_sign(roi: np.ndarray, layout: Layout) -> SignReading:
    """读「费用数字左边的减号」—— 局内负费唯一的像素判据。

    为什么必须有它：负费期间每逻辑帧只攒一半，一费从 30 逻辑帧变成 60。不认出来的话
    相位模型会按 1/30 往前推，`_resync` 只能挑到真值一半的 k —— 计数器直接把那一段
    数成一半（不是「多记」也不是「少记一帧」，是整段 2 倍偏差）。见 docs 第 31 节。

    roi 是 geometry.sign_rect 那个框的灰度图（一行一行扫，够小，不用向量化）。
    """
    thr = max(6, int(round(SIGN_MIN_RUN_UNIT * float(getattr(layout, 'scale', 1.0) or 1.0))))
    if roi is None or getattr(roi, 'size', 0) == 0:
        return SignReading(negative=False, run=0, rows=0, thr=thr)
    band = roi.mean(axis=2) if roi.ndim == 3 else roi
    lit = band > WHITE_MIN
    best_run, rows = 0, 0
    for y in range(lit.shape[0]):
        run = 0
        longest = 0
        for value in lit[y]:
            run = run + 1 if value else 0
            if run > longest:
                longest = run
        if longest > best_run:
            best_run = longest
        if longest >= thr:
            rows += 1
    return SignReading(negative=rows >= SIGN_MIN_ROWS, run=int(best_run), rows=rows,
                       thr=thr)


def read_pause(roi: np.ndarray, layout: Layout) -> PauseReading:
    """读暂停键。

    只取字形顶部的条带：单三角（已暂停）顶部很窄，双竖条（运行中）顶部是两个整宽矩形，
    两者亮点数差约 4 倍（实测 0.063 vs 0.24），所以用占比判别很稳。

    除此之外还单独回答「这个键画出来了没有」（drawn）：见 PauseReading.drawn。
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
    mask = band > thr
    ratio = float(mask.sum()) / float(mask.size)
    solid = solid_frac(mask, 1)
    drawn = solid >= PAUSE_GLYPH_MIN
    return PauseReading(ratio=ratio, running=ratio >= RUN_PAUSE_RATIO_MIN,
                        drawn=drawn, solid=solid)


# ---- 倍速键上的「三个三角（>>>）」= 子弹时间 ----
# 子弹时间下倍速键上**没有数字**，画的是三个三角（亮度不是纯白 255：实测块内 p90
# 234~238、峰值 236~239，画 1X / 2X 时才是 255）。真正坑人的是**位置**：三角最亮的
# 那一块落在 ROI 行 40..81，而旧判据只扫「箭头带」行 70..115 —— 只有最下面十来行
# 压进去，3x3 腐蚀一遍剩不下东西，于是同一套阈值在一个录像里靠**场景亮块**顶出
# 1277/1277 命中（实心占比恒 0.044）、在另一个录像里 0/583。见 docs 第 32 节。
#
# 现在改成认**形状**：把这一小块归一化之后跟录下来的模板（templates/bullet.npy，从
# 2026-10-09 那一帧裁的、连当时的场景背景一起）比相关系数（NCC）。模板带着当时的
# 背景，换一段录像、背景和半像素对位一变，固定在原位一个窗口就只有 0.598（余量
# 0.05）；在几档缩放 + 横纵平移里搜一遍是 0.686（余量 0.14）。
#
# 实测（两段录像共 3442 帧逐帧跑完，2026-10-10 复测；这条判据只在「读不到数字」时才走）：
#     三个三角    a 1186 帧 0.597..1.000（t>=7s 的那 1020 帧 >= 0.755）
#                 b  583 帧 0.686..0.698（旧判据在这一段 0/583，见 docs 第 32 节）
#     都没有      a   91 帧 0.360..0.548（全在 1.6~6.7s：进关卡按钮淡入）
#     有数字      a   717 帧 0.274..0.549 / b 865 帧 0.258..0.647
#                 —— 跟三角**重叠**！所以判据只能挂在「读不到数字」那一支上，
#                 它不去否决已经读到并匹配上的数字（那种情况有别的规则兜着）
# 阈值取 0.55：正好夹在 a 的负类最高 0.548 和正类最低 0.597 之间（两边各 ~0.05）。
# 那一段是按钮淡入、类别本身就在过渡，再调阈值也拉不开；稳定段（t>=7s）余量是 0.207。
SPEED_GLYPH_BAND = (30 / 159.0, 100 / 159.0)   # 三角所在条带（占 ROI 高度）
SPEED_GLYPH_COLS = (20 / 199.0, 180 / 199.0)   # 占 ROI 宽度
# 模板缩放的几档。注意条带只占 ROI 宽的 80%（SPEED_GLYPH_COLS），所以窗口比条带还宽
# 的时候（scale > 1.25）一个位置都摆不下 —— 1.3 / 1.45 这两档在 2560x1440 客户区上
# 实测是空的（真正跑起来的只有 1.0 / 1.15）。留着是为了别的 ROI 尺寸，代价只是两次
# 查表。
SPEED_GLYPH_SCALES = (1.0, 1.15, 1.3, 1.45)    # 模板缩放的几档
SPEED_GLYPH_SHIFTS = (-0.125, 0.0, 0.125)   # 横向平移（占条带宽度 = 参照分辨率 20px）
SPEED_GLYPH_ROWSHIFT = (-0.143, 0.0, 0.143)   # 纵向平移（占条带高度 = 参照分辨率 10px）
# 两边都先降采样一半再比：实测 _glyph_score 一次从 1.42ms 掉到 0.30ms（4.7 倍），
# 而形状分只差 +-0.014（三角 0.686 -> 0.672、淡入那一帧 0.548 -> 0.560）。实时链路
# 默认 120Hz 采样、一拍只有 8.3ms 预算，而这条路在子弹时间里是**每拍都走**的。
SPEED_GLYPH_DOWN = True
GLYPH_NCC_MIN = 0.55
# load_templates 给「形状模板」（不是倍速数字）的标记值：read_speed 把它当三角模板用。
BULLET_TEMPLATE_MARK = -1.0


def _glyph_templates(templates):
    """从 templates 里挑出形状模板（值是负数的那几个，见 BULLET_TEMPLATE_MARK）。"""
    return [arr for value, _name, arr in templates or () if value < 0]


def _half2(img: np.ndarray):
    """2x2 取均值降采样一半（奇数边丢掉最后一行 / 列；太小就返回 None）。

    自己写而不是用 PIL：features 只依赖 numpy（和 _resize_nearest 一个理由）。
    为什么要降：这一路在子弹时间里**每拍都走**，而实时链路默认 120Hz 采样（一拍 8.3ms）。
    """
    h, w = img.shape[0] // 2 * 2, img.shape[1] // 2 * 2
    if h < 2 or w < 2:
        return None
    # 四个错位相加再乘 0.25：同样的事 reshape(...,2,2).mean(axis=(1,3)) 要 0.27ms，
    # 这样写 0.03ms（实测；numpy 那两个轴一起规约很慢）。
    return (img[:h:2, :w:2] + img[1:h:2, :w:2]
            + img[:h:2, 1:w:2] + img[1:h:2, 1:w:2]) * 0.25


def _glyph_level(img: np.ndarray) -> float:
    """亮度基准 = 抽稀之后的中位数。

    为什么抽稀：全量 np.median 实测一次 0.2ms，而这条路每拍要算好几遍（一拍 8.3ms）。
    抽 1/4 的像素之后 0.02ms，对「减掉背景亮度」来说精度绰绰有余。
    """
    sample = img[::2, ::2] if img.ndim == 2 else img[::2]
    return float(np.median(sample))


def _glyph_norm(img: np.ndarray):
    """按自己的亮度基准 / 峰值归一化到 0..1（不借整个 ROI 的亮度，换 HDR 档也不用改）。"""
    med = _glyph_level(img)
    span = max(1.0, float(img.max()) - med)
    return np.clip((img - med) / span, 0.0, 1.0)


def _glyph_score(roi: np.ndarray, templates) -> float:
    """「三个三角（>>>）」的形状分：这一小块跟模板的最大相关系数（NCC）。

    为什么不能只看亮度 / 面积：倍速键是半透明的，三种状态都透场景 —— 实测「内容区
    实心占比」在有数字（p50 0.096）和三个三角（p50 0.102）之间根本分不开，场景亮块
    还能顶到 0.478。形状是唯一分得开的东西（见上面那张实测表）。

    模板只录了一个尺寸，所以要在几档缩放 + 横纵平移里取最大。实测（见 tools/fixtures
    的 7 张真帧）：只比原位一个窗口时 2026-10-06 那段三角是 0.598（余量 0.05），
    搜完 0.686（余量 0.14）；2026-10-09 那段一直是 0.755（模板就是从它身上裁的）。
    模板存的是**原始灰度**，跟这一拍一样在比对时按自己归一化 —— 免得出现「模板是按
    ROI 归一化的、这一拍是按条带归一化的」这种不对称。

    模板那一侧的归一化结果是常量，缓存在 _glyph_tpl_flat 里 —— 实测这一步本来就占
    一半（缓存前一次 1.9ms、缓存后 0.3ms）。
    """
    if not templates or roi is None or getattr(roi, 'size', 0) == 0:
        return float('nan')
    band = roi.mean(axis=2) if roi.ndim == 3 else roi
    band = band.astype(np.float32)
    if SPEED_GLYPH_DOWN:
        band = _half2(band)
        if band is None:
            return float('nan')
    h, w = band.shape
    r0 = int(round(h * SPEED_GLYPH_BAND[0]))
    r1 = max(r0 + 2, int(round(h * SPEED_GLYPH_BAND[1])))
    c0 = int(round(w * SPEED_GLYPH_COLS[0]))
    c1 = max(c0 + 2, int(round(w * SPEED_GLYPH_COLS[1])))
    band_h, band_w = r1 - r0, c1 - c0
    best = -2.0
    for tpl in templates:
        for scale in SPEED_GLYPH_SCALES:
            th = max(4, int(round(band_h * scale)))
            tw = max(4, int(round(band_w * scale)))
            # 降采样 / 缩放 / 归一化只跟 (模板, 窗口尺寸) 有关，跟这一拍的像素无关：
            # 第一次用到这个尺寸时算一遍存下来，之后每拍都是查表（见 _glyph_tpl_flat）。
            flat = _glyph_tpl_flat(tpl, th, tw)
            if flat is None:
                continue
            windows = []
            for dy in SPEED_GLYPH_ROWSHIFT:
                y0 = r0 + int(round(band_h * dy))
                if y0 < 0 or y0 + th > h:
                    continue
                for dx in SPEED_GLYPH_SHIFTS:
                    x0 = c0 + int(round(band_w * dx))
                    if x0 < 0 or x0 + tw > w:
                        continue
                    windows.append(band[y0:y0 + th, x0:x0 + tw].ravel())
            if not windows:
                continue
            # 一档里的几个平移**批量**算完，不逐个调用 _ncc：实测单次 _ncc 的调用开销
            # 就要 0.034ms，9 个平移 x 4 档缩放 = 36 次 = 1.2ms，而一拍只有 8.3ms。
            stack = _glyph_norm_flat(np.stack(windows))
            den = np.sqrt((stack * stack).sum(axis=1)) * float(np.sqrt((flat * flat).sum()))
            corr = np.zeros(stack.shape[0], dtype=np.float32)
            ok = den > 0
            corr[ok] = (stack[ok] @ flat) / den[ok]
            top = float(corr.max())
            if top > best:
                best = top
    return best


def _glyph_norm_flat(rows: np.ndarray):
    """按行归一化：每行减自己的亮度基准、除以自己的峰值-基准（给批量 NCC 用）。

    基准同样抽稀（每 4 个像素取一个）—— 见 _glyph_level。
    """
    med = np.median(rows[:, ::4], axis=1, keepdims=True)
    span = np.maximum(1.0, rows.max(axis=1, keepdims=True) - med)
    return np.clip((rows - med) / span, 0.0, 1.0)


# 模板那一侧的归一化结果缓存。键里用的是 id()，所以**值里必须回指模板对象本身** ——
# 否则对象被回收之后 id 会被复用，缓存就串到别的模板上去了（ndarray 不可哈希，不能
# 拿它本身当键）。模板是启动时从 templates/ 读一次、之后只读，缓存不会失效。
_GLYPH_TPL_CACHE = {}


def _glyph_tpl_flat(tpl, th: int, tw: int):
    """模板一侧归一化之后的扁平向量（给批量 NCC 用）；同一个 (模板, 尺寸) 只算一次。"""
    key = (id(tpl), th, tw)
    hit = _GLYPH_TPL_CACHE.get(key)
    if hit is not None and hit[0] is tpl:
        return hit[1]
    base = np.asarray(tpl, dtype=np.float32)
    if SPEED_GLYPH_DOWN:
        base = _half2(base)
        if base is None:
            return None
    base = _glyph_norm(base)
    if base.shape != (th, tw):
        base = _resize_nearest(base, th, tw)
        if base is None:
            return None
    flat = _glyph_norm_flat(base.reshape(1, -1))[0]
    _GLYPH_TPL_CACHE[key] = (tpl, flat)
    return flat


def read_speed(roi: np.ndarray, layout: Layout, templates=()) -> SpeedReading:
    """读倍速键。

    三种形态：
        2X + 双箭头   粗白、不透明
        1X + 单箭头   细、半透明
        >>>          只有**三个三角**，不显示数字（子弹时间）

    子弹时间下屏幕上根本没画倍速数字（只有那三个三角），读不到就只能沿用上一次已知值 ——
    这是信息缺失，不是实现问题。同理，字形处于过渡/异形状态（匹配不上任何模板）时也沿用
    上次值，并标记 conf='low'，绝不瞎猜一个倍速。

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
        # 读不到数字有三条岔路，必须分开（这一段 2026-10-10 按两段录像的量重写过）：
        #   (a) 真子弹时间（>>>）：键上画着**三个三角**（半透明，峰值 236~239），没有数字
        #       —— 用户说的「这里有三个三角」。它扫不到的原因是**位置**：三角在 ROI
        #       行 40..81，而下面那条箭头带是行 70..115。得认形状（_glyph_score）。
        #   (b) 按钮淡出 / 整块变暗（进关卡淡入、过场、暂停遮罩）：什么都没画。
        #   (c) 旧判据「箭头带里有实心白块」在亮背景关卡上会被场景顶过去：同一套阈值
        #       在 2026-10-09 那段里 1277/1277 命中，在 2026-10-06 那段里 0/583 命中。
        #       现在它只用来判 drawn（这颗键画出来没有）。
        # 箭头带里「亮」的判据必须是相对的：绝对阈值 200 在 HDR 录像上命中率 0.0000
        # （实测 UI 白只有 122、箭头带 p99 只有 91），那样什么都认不出来。
        ar0, ar1 = layout.speed.band_rows(*SPEED_ARROW_BAND)
        arrow = roi[ar0:ar1 + 1, :]
        hi = float(np.percentile(roi, 99.0))
        med = float(np.median(roi))
        if hi - med >= ARROW_MIN_CONTRAST:
            thr_arrow = med + ARROW_REL * (hi - med)
            arrow_mask = arrow >= thr_arrow
            arrow_solid = solid_frac(arrow_mask, 1)
        else:
            # 整块 ROI 没有对比度（按钮淡出 / 被遮罩糊平）：画面上根本没画东西
            arrow_solid = 0.0
        # 子弹时间的判据：这三个三角的形状分（对缩放 / 平移不敏感）。
        glyph = _glyph_score(roi, _glyph_templates(templates))
        if glyph >= GLYPH_NCC_MIN:
            return SpeedReading(digit_ratio=digit_ratio, corr_2x=corr, has_digit=False,
                                m=float('nan'), bullet=True, conf='ok',
                                solid=arrow_solid, glyph=glyph)
        # 倍速信息这一刻根本不存在于画面上：沿用上次已知倍速，也不按 1/5 打折。
        # drawn 必须用**实心**判据，不能用占比：关卡外那些细笔画占比能到 0.09（见
        # ARROW_SOLID_MIN），拿占比当「按钮画着」等于给菜单开绿灯 —— 关卡判定就废了。
        return SpeedReading(digit_ratio=digit_ratio, corr_2x=corr, has_digit=False,
                            m=float('nan'), bullet=False, conf='low',
                            drawn=arrow_solid >= ARROW_SOLID_MIN, solid=arrow_solid,
                            glyph=glyph)

    # 归一化：按钮有亮/暗两种渲染，绝对值阈值和直接比较都会失效，
    # 用 (值 - 中位数) / (峰值 - 中位数) 归一化后再比形状。
    med = float(np.median(roi))
    span = max(1.0, float(roi.max()) - med)
    norm = np.clip((band.astype(np.float32) - med) / span, 0.0, 1.0)

    best_m = None
    best_corr = -2.0
    for m_val, _name, arr in templates:
        if m_val < 0:
            # 形状模板（三个三角）：形状跟数字条带完全不同，别拿它比数字。
            continue
        # 模板是在某个特定窗口尺寸下采集的，形状不一定和当前 ROI 一致。
        # 直接按最近邻缩放过去再比：UI 是等比缩放的，这样换分辨率也能用同一批模板。
        tpl = arr if arr.shape == norm.shape else _resize_nearest(arr, *norm.shape)
        if tpl is None:
            continue
        c = _ncc(norm, tpl)
        if c > best_corr:
            best_m, best_corr = m_val, c
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
