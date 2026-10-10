"""定点（2^-24）费用计时：把游戏内部那个累加器原样搬过来。

为什么要用定点而不是浮点：游戏是每一逻辑帧用累加器算一次进度条宽度、再取整画出来，
取整的边界效应会实打实地产生「第 11 费要 31 帧」这种周期。用浮点累加会把边界抹平，
用定点 + 截断才能复现它 —— 前人项目（ZeroAd-06/ArknightsCostBarRuler）就是从这条
反推出来的，这里照搬结论。

三个量（单位都是 2^-24，SCALE = 1.0）：
    required      回一费的目标量（不带 tag 的普通关卡 = 1.0）
    speed         每逻辑帧的增量（正常回费 = 1/30 费/帧）
    accumulator   已经攒了多少，每逻辑帧 += speed

回费条件：accumulator + EPSILON >= required。回费时 accumulator -= required，
余数可以是负的 —— 这正是「边界周期多一帧」的来源：
30 帧累计的误差 = SCALE - 30*(SCALE//30) = 16，攒到第 11 个周期就是 176 > EPSILON(167)，
于是第 11 费多花一帧。
"""

from __future__ import annotations

LOGICAL_FPS = 30          # 游戏内时钟恒为 30 帧/秒，和画面帧数设置无关
SCALE = 1 << 24           # 1.0 的定点表示
EPSILON_RAW = 167         # 回费判定容差，约 1e-5
# 「回费变慢」在游戏里是**两条不同的路**，不能混：
#   * 战前 tag（突袭 / 磨难 / 合约词条）改 required（一费要攒多少）—— required_from_frames
#     走的就是这条；
#   * 战斗中才生效的（局内负费 / 可露希尔 / 忍冬）改 speed（每逻辑帧攒多快）——
#     负费就是把 speed 减半，一费从 30 逻辑帧变成 60 逻辑帧。
# 前人 fp24 的 NORMAL_SPEED / NEGATIVE_SPEED（SCALE/30 与 SCALE/60）就是这个分法。
# 为什么不能把负费写成「required 翻倍」：一费几帧确实都对，但**边界周期**（第几费会
# 多出 1 帧）落在哪一费上不一样；而且负费是局内随时变的，改 required 就得重新起锚，
# 相位会断（实测转场那一帧会丢 1 帧）。
SPEED_NORMAL = SCALE // 30      # 559240，正常：1 秒 1 费
SPEED_NEGATIVE = SCALE // 60    # 279620，局内负费（费用条上出现减号）：2 秒 1 费


def required_from_frames(frames: float) -> int:
    """按「多少逻辑帧回一费」构造 required。

    这是战前回费速度 tag 该走的那条路（前人 fp24 的说明：
    突袭 / 磨难 / 合约词条这类**战前**就定下来的修正改的是 `required`；
    战斗中才生效的（负费、可露希尔、忍冬）改的是 `speed`）。

        frames = 30  -> required = 1.0 * SCALE   （1 秒 1 费，普通关卡）
        frames = 60  -> required = 2.0 * SCALE   （2 秒 1 费，回费速度 -50%）
        frames = 120 -> required = 4.0 * SCALE   （4 秒 1 费，回费速度 -75%）

    为什么用 required 而不是改 speed：两种写法「一费几帧」都对，但**边界周期**
    （第几费会多出 1 帧）不一样。前人文档给的表是按 required 算的：
    30 -> `30*10, 31`、39 -> `39*8, 40`、45 -> `45*6, 46`、60 -> `60*5, 61`。
    照抄它才能和游戏对得上，回归项 tools/check_cost_rate.py 就是照这张表验的。
    """
    if frames != frames or frames <= 0:
        return 0
    return int(round(frames / float(LOGICAL_FPS) * SCALE))


def frames_from_required(required: int) -> float:
    """反过来：这个 required 相当于多少逻辑帧回一费。"""
    if not required or required <= 0:
        return 0.0
    return required / float(SCALE) * LOGICAL_FPS


class CostTiming:
    """费用累加器。一个逻辑帧调用一次 advance_one_frame()。"""

    __slots__ = ('required', 'accumulator', 'frames_since_cycle_start', 'cycles')

    def __init__(self, required: int = SCALE, accumulator: int = 0,
                 frames_since_cycle_start: int = 0, cycles: int = 0):
        self.required = int(required)
        self.accumulator = int(accumulator)
        self.frames_since_cycle_start = int(frames_since_cycle_start)
        self.cycles = int(cycles)

    def copy(self) -> 'CostTiming':
        return CostTiming(self.required, self.accumulator,
                          self.frames_since_cycle_start, self.cycles)

    def advance_one_frame(self, speed: int) -> bool:
        """前进一个逻辑帧。返回 True 表示这一帧正好回收了 1 费。"""
        self.accumulator += speed
        self.frames_since_cycle_start += 1
        if self.accumulator + EPSILON_RAW >= self.required:
            self.accumulator -= self.required
            self.frames_since_cycle_start = 0
            self.cycles += 1
            return True
        return False

    def phase(self) -> float:
        """当前相位 0..1，也就是费用条应该画到多满。

        **刚好回费的那一帧返回 1.0，不是 0。** 实测一连串录像里，回卷处的可见宽度
        序列是 `... 223, 232, 240, 0, 8 ...`：游戏是在累加器刚好攒满的那一帧把条画满
        （相位 = 1.0 -> 宽度 240），**下一帧**才归零。如果这里返回 0，模型就少了
        「条画满」这个状态，于是每费的回费帧都匹配不上、被判 bar-reject，
        一费之内必然差 1 帧 —— 表现正是「一秒的总数对、一秒内的位置不对」。

        frames_since_cycle_start == 0 只在本帧刚回费（或还没起锚）时成立，
        用 cycles > 0 把「还没起锚」排除掉。
        """
        if self.required <= 0:
            return 0.0
        if self.cycles > 0 and self.frames_since_cycle_start == 0:
            return 1.0
        p = self.accumulator / self.required
        if p < 0.0:
            return 0.0
        if p > 1.0:
            return 1.0
        return p

    def frames_until_recovery(self, speed: int, limit: int = 10000) -> int:
        """从当前状态出发，还要几帧才回一费（用来限定重同步的搜索范围）。"""
        probe = self.copy()
        for n in range(1, limit + 1):
            if probe.advance_one_frame(speed):
                return n
        return 0
