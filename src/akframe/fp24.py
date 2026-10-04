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
SPEED_NORMAL = SCALE // 30      # 559240，正常：1 秒 1 费
SPEED_HALF = SCALE // 60        # 279620，回费速度减半（2 秒 1 费）
SPEED_QUARTER = SCALE // 120    # 139810，回费速度 25%（4 秒 1 费）


def speed_for_frames(frames: int) -> int:
    """按「多少逻辑帧回一费」构造 speed。"""
    if frames <= 0:
        return 0
    return SCALE // frames


def required_from_ratio(ratio: float) -> int:
    """按「一费相当于几个正常周期的量」构造 required（战前减速 tag = 2.0 / 4.0）。"""
    if ratio != ratio or ratio <= 0:
        return 0
    return int(round(ratio * SCALE))


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
        """当前相位 0..1，也就是费用条应该画到多满。"""
        if self.required <= 0:
            return 0.0
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
