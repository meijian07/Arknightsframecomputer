"""状态机：把逐帧读数翻译成全局逻辑帧。

两条互斥通道：
    像素通道  条在动 -> 加整数逻辑帧，精确、免疫倍速与掉帧
    时钟通道  条不可读（满费 / 满费退出过渡 / 空条超时 / 锁费）-> 按挂钟估算
    暂停时两条都关闭

两条通道都用「余量累加 + 向下取整」而不是每次四舍五入：
1 倍速下条每显示帧只走 4px（=半个逻辑帧），四舍五入会来回跳、产生系统性偏差。
"""

from __future__ import annotations

from dataclasses import dataclass

from .features import BarReading, PauseReading, SpeedReading

LOGICAL_FPS = 30.0    # 1 倍速 = 每秒 30 逻辑帧
BULLET_FACTOR = 0.2   # 子弹时间无视当前倍速，把速率压到 1/5
ZERO_RUN_LIMIT = 3    # fill 连续为 0 超过这么多帧就认为条不可读，切时钟通道
CLOCK_ARM_FRAMES = 5  # 「条不可读 + 运行中」要连续保持这么多帧，才允许挂钟通道补帧
DFILL_SLACK = 3.0     # 像素通道允许「比期望多」多少逻辑帧，超出判为读数异常
STALL_SLACK = 1.5     # 比期望少这么多只告警，不改变结果（可能是采集卡顿）

@dataclass
class FrameResult:
    idx: int
    t: float
    fill: int
    bar_state: str
    grey146: int
    grey88: int
    pause_ratio: float
    running: bool
    digit_ratio: float
    corr_2x: float
    has_digit: bool
    m: float
    bullet: bool
    mode: str        # anchor / pause / pixel / clock
    delta: int       # 本帧新增的逻辑帧
    elapsed: int     # 累计逻辑帧
    warn: str

    def as_row(self) -> list:
        return [self.idx, '%.4f' % self.t, self.fill, self.bar_state,
                self.grey146, self.grey88,
                '%.4f' % self.pause_ratio, int(self.running),
                '%.4f' % self.digit_ratio,
                ('%.4f' % self.corr_2x) if self.corr_2x == self.corr_2x else '',
                int(self.has_digit), '%.1f' % self.m, int(self.bullet),
                self.mode, self.delta, self.elapsed, self.warn]


CSV_HEADER = ['idx', 't', 'fill', 'bar_state', 'grey146', 'grey88',
              'pause_ratio', 'running', 'digit_ratio', 'corr_2x', 'has_digit',
              'm', 'bullet', 'mode', 'delta', 'elapsed', 'warn']


class Counter:
    """全局逻辑帧计数器。"""

    def __init__(self, bar_w: int, fps: float = 60.0):
        self.bar_w = int(bar_w)
        self.px_per_frame = self.bar_w / LOGICAL_FPS
        self.fps = float(fps) if fps and fps > 0 else 60.0
        self.last_m = 2.0          # 子弹时间不显示倍速，缓存最后一次已知值
        self.elapsed = 0
        self._fill_prev = 0
        self._zero_run = 0
        self._unread_run = 0       # 「条不可读 + 运行中」连续了多少帧
        self._need_confirm = True  # 是否刚从暂停回来（是的话挂钟通道要先确认，见 _clock_gated）
        self._clock_wait = 0.0     # 确认之前攒下的挂钟秒数，确认时一次性补上
        self._fill_acc = 0.0       # 像素通道余量（单位：px）
        self._clock_frac = 0.0     # 时钟通道余量（单位：逻辑帧）
        self._started = False
        self._prev_t = None
        self._prev_mode = None
        # ---- 实测速率探针 ----
        # 用「像素通道推进的逻辑帧数」除以「挂钟时间（按倍速折算到 1 倍速）」，
        # 反推游戏真实的逻辑帧率。为什么必须这么做：视频的帧率不可信（采集掉帧后
        # 仍按标称帧率打时间戳，实测某录像快了 4.17%），只有挂钟可信。
        # 只统计「条在动、且不在子弹时间」的样本。
        self.probe_frames = 0
        self.probe_mt = 0.0

    def step(self, idx: int, t: float, bar: BarReading,
             pause: PauseReading, speed: SpeedReading) -> FrameResult:
        dt = 1.0 / self.fps if self._prev_t is None else (t - self._prev_t)
        if dt <= 0:
            dt = 1.0 / self.fps
        self._prev_t = t

        # 子弹时间不显示倍速，只能用上一次已知倍速
        if speed.m == speed.m:   # 非 nan
            m = float(speed.m)
            self.last_m = m
        else:
            m = self.last_m
        rate = LOGICAL_FPS * m * (BULLET_FACTOR if speed.bullet else 1.0)

        warn = ''
        if not self._started:
            mode, delta = 'anchor', 0
            self._started = True
        elif not pause.running:
            mode, delta = 'pause', 0
            # 暂停 -> 挂钟通道必须重新「确认」：否则暂停期间图标闪一下就会被当成在跑
            self._unread_run = 0
            self._clock_wait = 0.0
            self._need_confirm = True
        elif self._zero_run >= ZERO_RUN_LIMIT or bar.state in ('MAX', 'MAX_FADE'):
            mode, delta = self._clock_gated(rate, dt)
        else:
            self._unread_run = 0
            self._need_confirm = False     # 条能读到就是「游戏在跑」的直接证据
            mode, delta, warn = self._pixel(rate, dt, bar.fill)

        # 注意：这里**不能**在换通道时把余量清零。余量代表「不足一帧的相位」，
        # 每次换通道丢一次，几十次换通道就会累积成几帧的系统性少算（实时链路实测）。
        # 只有 resync()（画面不可读、状态未知）才允许丢掉余量。
        self._prev_mode = mode

        self.elapsed += delta
        if mode == 'pixel' and not speed.bullet:
            self.probe_frames += delta
            self.probe_mt += m * dt
        self._zero_run = self._zero_run + 1 if bar.fill <= 4 else 0
        self._fill_prev = bar.fill

        if speed.conf == 'low' and not warn:
            warn = 'speed-low-conf'
        if bar.grey146 > 40 and bar.state == 'TRACK' and not warn:
            warn = 'grey-in-track'

        return FrameResult(
            idx=idx, t=t, fill=bar.fill, bar_state=bar.state,
            grey146=bar.grey146, grey88=bar.grey88,
            pause_ratio=pause.ratio, running=pause.running,
            digit_ratio=speed.digit_ratio, corr_2x=speed.corr_2x,
            has_digit=speed.has_digit, m=m, bullet=speed.bullet,
            mode=mode, delta=delta, elapsed=self.elapsed, warn=warn)

    def probe(self) -> tuple:
        """实测逻辑帧率探针。

        返回 (实测 1 倍速帧率, 累计逻辑帧, 累计等效挂钟秒数)。
        帧率 = 像素通道累计逻辑帧 / (Σ m*dt)。样本不足时返回 nan。
        """
        if self.probe_mt <= 0.0:
            return (float('nan'), self.probe_frames, self.probe_mt)
        return (self.probe_frames / self.probe_mt, self.probe_frames, self.probe_mt)

    def _clock_gated(self, rate: float, dt: float) -> tuple:
        """挂钟通道，但先要确认游戏真的在跑。

        为什么必须确认：暂停时暂停键图标会以 2~3 帧为单位闪成「运行中」形状（实测一段
        76 秒的暂停录像里闪了 28 次）。闪的那几帧里条正好不可读（满费），旧逻辑就照挂钟
        补帧 —— 每次闪凭空多 1 帧、总共多 30 帧，肉眼完全看不出来。

        规则：「条不可读 + 运行中」必须连续保持 CLOCK_ARM_FRAMES 帧才承认。确认之前那几帧
        的挂钟时间先攒着（不计数），确认的一刻一次性补上，所以真实的满费/锁费一点都不会少算。

        只在**刚从暂停回来**（_need_confirm）时才要求确认，这一点很关键：
          - 闪烁一定发生在暂停期间（前面有一串暂停帧），所以它一定躲不过确认 -> 被吃掉；
          - 正常打的时候一直在跑，条一旦变成满费就立刻照常补帧，不会因为「等确认」丢帧
            （实测 2 倍速录像里，无条件确认会丢 8 帧，加上这条判据后不再丢）。

        注意：**像素通道（条能读到）完全不经过这里**。暂停后继续时条是能读到的
        （实测暂停中 fill 仍然正常），所以「快速暂停 / 继续 / 再暂停」的识别速度不受影响。
        """
        self._unread_run += 1
        if self._need_confirm and self._unread_run < CLOCK_ARM_FRAMES:
            self._clock_wait += rate * dt
            return 'clock-wait', 0
        self._need_confirm = False
        acc = rate * dt + self._clock_wait + self._clock_frac
        self._clock_wait = 0.0
        delta = int(acc)
        self._clock_frac = acc - delta
        return 'clock', delta

    def _clock(self, rate: float, dt: float) -> tuple:
        """不带确认的挂钟补帧（只给像素通道读数跳变时的兜底用）。"""
        acc = rate * dt + self._clock_frac
        delta = int(acc)
        self._clock_frac = acc - delta
        return 'clock', delta

    def _pixel(self, rate: float, dt: float, fill: int) -> tuple:
        dfill = (fill - self._fill_prev) % self.bar_w
        trial = self._fill_acc + dfill
        k = int(trial / self.px_per_frame)
        expect = rate * dt          # 期望的逻辑帧数（k 也是帧数，两者同量纲才能比）
        if k - expect > DFILL_SLACK:
            # 读数跳变（遮挡、误判、相位重锚），本帧不可信 -> 交给时钟通道
            return self._clock(rate, dt) + ('pixel-jump',)
        self._fill_acc = trial - k * self.px_per_frame
        warn = 'pixel-stall' if k < expect - STALL_SLACK else ''
        return 'pixel', k, warn

    def resync(self) -> None:
        """重新锚定。

        用于「有一段时间读不到画面」之后（窗口最小化、被完全遮挡、切出去了）：
        这段时间里条可能动过，但我们不知道动了多少，只能丢弃增量、以当前读数重新起锚。
        下一帧会走 anchor 模式（delta=0），之后恢复正常。
        """
        self._started = False
        self._fill_acc = 0.0
        self._clock_frac = 0.0
        self._zero_run = 0
        self._unread_run = 0
        self._need_confirm = True
        self._clock_wait = 0.0
        self._prev_mode = None
