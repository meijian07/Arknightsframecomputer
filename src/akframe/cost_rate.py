"""回费速率识别：一费到底是多少个逻辑帧。

## 为什么要单独识别

费用条推进的**像素**永远是真的（条走多少像素就过去多少个逻辑帧），但状态机要把
「像素」翻译成「帧」得先知道一费对应多少相位。这个映射由 fp24 的 `required` 定死：

    required = 一费需要多少逻辑帧 / 30

普通关卡（回费速度 100%）一费 30 逻辑帧；带了「回费速度 -50%」这类**战前 tag**
（突袭 / 磨难 / 合约词条 / 关卡描述红字）之后，一费变成 60 / 120 帧。要是按 30 去
解释一条 60 帧/费的条，模型每帧该涨的宽度就翻了一倍，重同步只能两帧并一帧地凑 ——
一费被数成 15 帧，整段录像的绝对帧号直接减半。反过来（按 60 解释 30）会翻倍。
所以「一费几帧」必须先认准。

## 怎么认：数「一个周期里条出现过多少种不同宽度」

这是唯一不依赖几何常数、也不依赖挂钟的观测量：

  - 条的宽度是相位的函数，且**单调**（相位涨、宽度只会涨）。一费 n 帧，就意味着
    一个周期里条最多能画出 n 种不同的宽度（取整会让某些相邻帧撞成同一个值，所以
    实际是 n 减去少数几次撞车）。
  - 挂钟不能用来量它：录像帧率不可信（实测采集软件会按标称帧率打时间戳，某录像快了
    4%），而且玩家可能像本项目的测试录像那样**暂停着逐帧推进** —— 那时挂钟和逻辑帧
    完全没有关系，只有「条出现过几种宽度」还有意义。
  - 条槽宽、隐藏长度这些几何常数会有 ±1~2px 的出入，但它们只影响「撞了几次车」，
    对「一个周期里有多少种宽度」几乎没有影响（本机实测：60 帧/费给 58~60 种，
    120 帧/费给 116~118 种）。

于是识别流程：切出一个完整周期里条宽度的**单调台阶**（运行最大值，见下方 collect），
数出台阶上不同宽度的个数 L，再去找「哪个候选速率 c 的模型也会给出差不多个数」。
最后**落到已知档位上**（30 / 39 / 40 / 45 / 60 / 90 / 120）：L 的模型偏差有 ±2，
不收敛到档位上就分不开 60 和 61 —— 而差 1 帧会随着每个周期累积成漂移，打轴器要的是
绝对帧号，必须准。

## 为什么不用前人那套「精确集合匹配」

前人的标定（ArknightsCostBarRuler 的 calibration.rs）要求「观测到的宽度集合 ⊆ 模型
宽度集合」，做法本身没问题，但前提是它有专用的读条通道、宽度是干净的。本项目读的是
任意分辨率下的录屏 / 窗口截图，宽度带 ±1px 抖动与偶发跳变，集合里多出一个像素就
判定失败，实测会挑出 175 / 130 这种垃圾值。这里的做法（数种类数 + 落到档位）对抖动
免疫，代价是分辨率不如前人精细 —— 但本项目只需要区分档位。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .synthesis import synthesized_width

# 明日方舟已知的回费速率档位（单位：逻辑帧 / 费）。
#   30 = 1.0 秒/费（普通关卡）
#   39 = 1.3 秒/费（沙盘 15-17 等，不是整秒档）
#   45 = 1.5 秒/费（磨难 10-4 等）
#   60 = 2.0 秒/费（回费速度 -50%：突袭 / 磨难 / 合约词条）
#   90 = 3.0 秒/费
#  120 = 4.0 秒/费（回费速度 -75%）
# 除了 39，其余都是「整秒档」；游戏里没出现过别的值。真出现别的值（或者这几档贴得
# 太近分不开时），用 --cost-rate 手工指定。
#
# 为什么不放 40（1.33 秒/费）进来：它和 39 只差 1 帧，而「数宽度种类」的观测误差就有
# ±2 种，两者根本分不开，放进来只会变成硬币抛掷。真要支持它请手工指定。
CANDIDATE_RATES = (30.0, 39.0, 45.0, 60.0, 90.0, 120.0)

MIN_CANDIDATE = 15.0          # 模型反推的搜索下限（比 0.5 倍速还慢就不像方舟了）
MAX_CANDIDATE = 180.0         # 上限：再慢每个周期也凑不出 8 种可分辨宽度，认不出来

WRAP_GAP_FRAC = 0.3           # 条宽从峰值掉下这么多 = 回费了（正常回费是一下掉到 0）
WRAP_GAP_MIN = 6              # 掉这么多个像素才算回费（±1px 抖动不会触发）
MIN_LEVELS = 6                # 一个周期至少要有这么多种宽度才拿去参评
# 小于「典型步长 × 这个比例」的小台阶一律当噪声并进前一个宽度（见 _denoise_stair）。
# 为什么不能取 0.5：一费 100 帧那种档位的真步长就是 2~3px，取 0.5 会把真台阶当噪声
# 并掉一半（实测量过：一周期从 97 种掉到 87 种），于是被误认成 90 帧/费。
STEP_MERGE_FRAC = 0.4

_model_cache: dict = {}


def model_levels(bar_w: int, bar_width_frac: float, frames_per_cost: float,
                 visible_w: int = None) -> list:
    """模型给出的「一个完整周期里能画出哪些宽度」（升序、去重、不含 0）。

    就是照游戏那条相位->宽度曲线（synthesis.synthesized_width）逐逻辑帧走一遍。
    条宽为 0 的那几帧画面上什么都没有（和关卡外没画条长得一样），所以不计入 ——
    观测时也读不到它们。
    """
    key = (int(bar_w), round(float(bar_width_frac or bar_w), 4),
           round(float(frames_per_cost), 4), int(visible_w) if visible_w else 0)
    hit = _model_cache.get(key)
    if hit is not None:
        return hit
    n = int(round(frames_per_cost))
    seen = set()
    for j in range(1, n + 1):
        w = synthesized_width(bar_w, bar_width_frac or bar_w, j / float(n))
        if visible_w:
            w = min(w, visible_w)
        if w > 0:
            seen.add(w)
    out = sorted(seen)
    _model_cache[key] = out
    return out


def _denoise_stair(stair: list) -> list:
    """去掉台阶上那些「小碎步」—— 它们是渲染过渡和 ±1px 抖动，不是真的逻辑帧。

    怎么做：先算出台阶的「典型步长」（相邻宽度差的中位数），再把不到它
    `STEP_MERGE_FRAC`（40%）的小台阶全部并进前一个宽度。真台阶的步长是 ~条槽宽/n
    （30 帧/费时 8px、120 帧/费时 2px），而噪声碎步是 1~3px；取中位数当典型步长是
    因为大部分相邻差都是真台阶。

    为什么不用「停留时间太短就丢掉」（按 run 长度过滤）：停留时间取决于**录制/采样
    方式**，不是一个可靠判据。实测两段录像就正好相反 ——
      - 逐帧推进（暂停着一格一格推）时每个逻辑帧停留 20~180 拍，过渡碎步只停 1~6 拍，
        「按停留时间过滤」好用；
      - 正常 1 倍速播放时每个逻辑帧只被采到 1~2 拍，和过渡碎步一样长，
        「按停留时间过滤」会把真台阶一起丢掉（实测一周期从 29 种掉到 23 种）。
    按「步长」过滤和采样方式无关，两段都能用。
    """
    if len(stair) < 4:
        return stair
    steps = [stair[i + 1] - stair[i] for i in range(len(stair) - 1)]
    thr = max(1, int(round(statistics.median(steps) * STEP_MERGE_FRAC)))
    out = [stair[0]]
    for w in stair[1:]:
        if w - out[-1] >= thr:
            out.append(w)
    return out


def collect_cycles(fills, states, bar_w: int, min_levels: int = MIN_LEVELS) -> list:
    """把逐帧读数切成「回费周期」，每个周期取条宽度的单调台阶。

    fills / states 是逐帧的条宽与条状态（features.BarReading 的那两个字段）。
    返回 [[宽度, ...], ...]，每个内层列表严格递增、只保留长度 >= min_levels 的周期。

    为什么取「运行最大值」：条的宽度带 ±1px 抖动，偶发读到偏小的一帧（还有整幅画面
    被压暗时短暂读空），这些都会在台阶上留下向下的凹口。运行最大值把它抹掉 ——
    真实的条是单调的，向下的东西一律当噪声。

    为什么「掉下来一大截」才算回费：回费时条会从画满（240）一下掉到 0，幅度远大于
    抖动。用相对 + 绝对双阈值卡住，就不会把一次抖动误判成新周期。
    """
    gap = max(WRAP_GAP_MIN, int(bar_w * WRAP_GAP_FRAC))
    cycles = []
    stair = []
    peak = -1

    def close():
        s_ = _denoise_stair(stair)
        if len(s_) >= min_levels:
            cycles.append(s_)

    for fill, state in zip(fills, states):
        if state == 'TRACK' and fill > 0:
            if fill > peak:
                stair.append(fill)
                peak = fill
            elif fill < peak - gap:
                # 回费了：把上一个周期收工，从这一帧起重新爬台阶
                close()
                stair = [fill]
                peak = fill
        else:
            # 条没画着（EMPTY / 满费换灰底）：只有「刚爬得很高」才当成周期结束，
            # 免得把关卡外那一长串读不到条的空档也算成周期
            if stair and peak > gap * 2:
                close()
                stair = []
                peak = -1
    if stair:
        close()
    return cycles


@dataclass
class RateEstimate:
    """识别结果。

    frames_per_cost  判定的速率（没把握时是 None）
    level_count     各完整周期数出来的宽度种类数（观测值）
    expected        判定档位在模型下应有的种类数
    raw             模型反推的连续估计（未落到档位前），用来核对手工指定
    scores          [(档位, 偏差), ...]，升序，方便看分不分得开
    conf            high / low / unknown
    """
    frames_per_cost: float = None
    level_count: list = field(default_factory=list)
    expected: int = 0
    raw: float = 0.0
    scores: list = field(default_factory=list)
    conf: str = 'unknown'

    @property
    def median_levels(self) -> float:
        if not self.level_count:
            return 0.0
        s = sorted(self.level_count)
        return float(s[len(s) // 2])


def infer_rate(cycles, bar_w: int, bar_width_frac: float, visible_w: int = None,
               candidates=CANDIDATE_RATES) -> RateEstimate:
    """从周期台阶反推速率档位。

    两步：
      1. 先不设档位，在 15..180 里找「模型种类数最接近观测中位数」的那个连续值 raw。
         这一步给出「大概是多少」，也方便手工核对（--cost-rate 就是照它填的）。
      2. 再把 raw 落到最近的已知档位上。档位之间至少差 6，raw 的偏差只有 ±2，
         落档是稳的。不落档不行：观测误差 ±2 种会让 raw 落在 61 / 122 这种值上
         （真值 60 / 120），差 1~2 帧会随着每个费用周期累积成漂移。
    """
    est = RateEstimate()
    if not cycles:
        return est
    est.level_count = [len(c) for c in cycles]
    obs = est.median_levels
    if obs < MIN_LEVELS:
        return est
    if visible_w is None:
        visible_w = bar_w

    def expected(c):
        return len(model_levels(bar_w, bar_width_frac, c, visible_w))

    # 1) 连续反推
    best_raw, best_gap = None, None
    c = MIN_CANDIDATE
    while c <= MAX_CANDIDATE:
        gap = abs(expected(c) - obs)
        if best_gap is None or gap < best_gap:
            best_gap, best_raw = gap, c
        c += 1
    est.raw = float(best_raw)

    # 2) 落到档位：取离 raw 最近的档位（不是离 obs 最近的 —— 见函数注释）
    est.frames_per_cost = min(candidates, key=lambda c: (abs(c - est.raw), c))
    est.expected = expected(est.frames_per_cost)
    est.scores = sorted(((abs(expected(c) - obs), float(c)) for c in candidates))
    best, second = est.scores[0][0], (est.scores[1][0] if len(est.scores) > 1 else None)
    # 容差怎么定的：种类数只会被两件事带偏 —— 漏采（少几种）和 ±1px 抖动（多几种）。
    # 实测一段逐帧推进的录像（每格停留 20+ 拍）偏差是 ±2 种；采样更稀、漏采更多时
    # 能到 ±4 种（合成信号上量过），所以取 4% 且至少 3 种。再宽就会把 45 认成 50。
    worst = max(3.0, 0.04 * est.frames_per_cost)
    if best <= 2 and (second is None or second - best >= 3):
        est.conf = 'high'
    elif best <= worst:
        est.conf = 'low'
    else:
        # 和哪个档位都对不上：诚实地说「不知道」，让调用方保留手填的速率
        est.conf = 'unknown'
        est.frames_per_cost = None
    return est


class RateEstimator:
    """在线收集器：喂逐帧的 (fill, state)，攒够一个周期就更新判定。

    给实时链路用（live.py）。离线链路直接 collect_cycles + infer_rate 就行。
    """

    def __init__(self, bar_w: int, bar_width_frac: float, visible_w: int = None,
                 candidates=CANDIDATE_RATES, min_levels: int = MIN_LEVELS,
                 max_cycles: int = 6):
        self.bar_w = int(bar_w)
        self.bar_width_frac = bar_width_frac
        self.visible_w = int(visible_w) if visible_w else self.bar_w
        self.candidates = candidates
        self.min_levels = min_levels
        self.gap = max(WRAP_GAP_MIN, int(self.bar_w * WRAP_GAP_FRAC))
        self.max_cycles = max_cycles
        self._cycle = []          # 当前周期的台阶
        self._peak = -1
        self._cycles = []         # 已完成的周期（最近 max_cycles 个）
        self.estimate = RateEstimate()

    def feed(self, fill: int, state: str) -> RateEstimate:
        """喂一帧。返回当前判定（会随周期增加而变准）。"""
        gap = self.gap
        if state == 'TRACK' and fill > 0:
            if fill > self._peak:
                self._cycle.append(fill)
                self._peak = fill
            elif fill < self._peak - gap:
                self._close()
                self._cycle = [fill]
                self._peak = fill
        else:
            if self._cycle and self._peak > gap * 2:
                self._close()
        return self.estimate

    def _close(self):
        s_ = _denoise_stair(self._cycle)
        if len(s_) >= self.min_levels:
            self._cycles.append(s_)
            if len(self._cycles) > self.max_cycles:
                self._cycles.pop(0)
            self.estimate = infer_rate(self._cycles, self.bar_w, self.bar_width_frac,
                                       self.visible_w, self.candidates)
        self._cycle = []
        self._peak = -1

    def reset(self):
        self._cycle = []
        self._peak = -1
        self._cycles = []
        self.estimate = RateEstimate()
