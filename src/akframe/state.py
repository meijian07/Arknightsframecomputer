"""状态机：把逐帧读数翻译成全局逻辑帧。

三条路子：
    像素通道  条能读到 -> 用 fp24 累加器 + 相位重同步数出这一拍前进了几个逻辑帧
    挂钟通道  条不可读（满费 / 被换渲染 / 真的读不到）-> 按挂钟 × 倍速估算
    冻结      两条都不成立（匹配失败、真暂停）-> 原地不动，绝不猜

为什么像素通道要重做：以前是「位移 / (条宽/30)」的线性除法和取整。它有三个硬伤 ——
条宽 240 时每逻辑帧其实是 8.31px 而不是 8.00px（真实条长比可见条槽宽 3.892%，见
synthesis.py）；一费的头 1~2 帧条其实还没动（渲染宽度是 0）；条被屏幕裁掉一截时
「条宽」根本不是分母。三条叠起来就是实测到的「一费只记到 22~28 帧」。
现在改成：把「累加器相位 -> 应该画多宽」的曲线和实测宽度对齐，取前进帧数 k，
所以 31 帧的那一费、被打断的读数、被裁的条，都能自己吸收掉。

两条通道共用同一个 fp24 累加器：走挂钟的时候也按同样的帧数把累加器往前推，
这样从满费回到正常推进时相位是接得上的，不用重新起锚。
"""

from __future__ import annotations

from dataclasses import dataclass

from .fp24 import SCALE, SPEED_NORMAL, CostTiming
from .features import BarReading, PauseReading, SpeedReading
from .synthesis import synthesized_width

LOGICAL_FPS = 30.0    # 1 倍速 = 每秒 30 逻辑帧
MAX_SPEED_M = 2.0     # 游戏内最高倍速（2X = 60 逻辑帧/秒），做「不可能更快」的上界用
BULLET_FACTOR = 0.2   # 子弹时间无视当前倍速，把速率压到 1/5
ZERO_RUN_LIMIT = 3    # fill 连续为 0 超过这么多帧就认为条不可读，切时钟通道
CLOCK_ARM_FRAMES = 5  # 「条不可读 + 运行中」要连续保持这么多帧，才允许挂钟通道补帧
DFILL_SLACK = 3.0     # 像素通道允许「比期望多」多少逻辑帧，超出只记告警
STALL_SLACK = 1.5     # 比期望少这么多只告警，不改变结果（可能是采集卡顿）
MATCH_TOLERANCE_PX = 5   # 相位重同步的匹配容差：误差超过它就判这帧读数不可信
WRAP_MIN_PREV_PHASE = 0.75   # 上一拍相位不到这个值，就不许跨过回费边界（防误判绕圈）


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
    mode: str        # anchor / pause / pixel / clock / clock-wait / bar-reject
    delta: int       # 本帧新增的逻辑帧
    elapsed: int     # 累计逻辑帧
    warn: str
    phase: float = 0.0   # 本帧相位（调试用，不进 CSV）
    err: int = 0         # 相位重同步的匹配误差 px（调试用，不进 CSV）

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
    """全局逻辑帧计数器。

    bar_w          合成模型里的条槽宽（= round(真实条槽宽，没被裁的那个）
    bar_width_frac 真实条槽宽（亚像素）。不传就按 bar_w 处理
    visible_w      这一路实际能看到的条槽宽度。ROI 被屏幕/画面裁掉时比 bar_w 小，
                   模型会把「画出来的宽度」截到这个值，否则尾部会一直匹配不上
    required       回一费的目标量（fp24 定点）。普通关卡 = SCALE
    """

    def __init__(self, bar_w: int, fps: float = 60.0, bar_width_frac: float = None,
                 visible_w: int = None, required: int = SCALE):
        self.bar_w = int(bar_w)
        self.visible_w = int(visible_w) if visible_w else self.bar_w
        self.model_w = self.bar_w
        self.bar_width_frac = float(bar_width_frac) if bar_width_frac else float(self.bar_w)
        self.required = int(required) if required and required > 0 else SCALE
        self.fps = float(fps) if fps and fps > 0 else 60.0
        self.last_m = 2.0          # 子弹时间不显示倍速，缓存最后一次已知值
        self.elapsed = 0
        self.timing = None         # fp24 累加器；None 表示还没起锚
        self.last_phase = None     # 上一拍认可的相位（防误判绕圈用）
        self.last_k = 0
        self._zero_run = 0
        self._run_run = 0          # 连续「运行中」的拍数（用来给 fill=0 的读数要求背书）
        self._unread_run = 0       # 「条不可读 + 运行中」连续了多少帧
        self._need_confirm = True  # 是否刚从暂停回来（是的话挂钟通道要先确认，见 _clock_gated）
        self._clock_wait = 0.0     # 确认之前攒下的挂钟秒数，确认时一次性补上
        self._clock_frac = 0.0     # 挂钟通道余量（单位：逻辑帧）
        self._expect_frac = 0.0    # 像素通道「期望帧数」的不足一帧余量
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

    # ---------- 对外 ----------

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
        # 「条能不能读」只看条自己（空条超时 / 满费灰底），和暂停与否无关。
        #
        # 为什么暂停时还要按条计数 —— 帧操（快速暂停、继续、再暂停）时，采样点几乎总是
        # 正好落在「暂停」的那一瞬，可两次采样之间游戏已经跑过、条已经动过了。旧逻辑在
        # 暂停帧直接 delta=0，这段位移就整段丢掉。实测一段帧操录像：条总共走了 422px
        # （= 52.8 逻辑帧），旧逻辑只给 44 帧，少算 17%；放开这道门后是 53 帧。
        # 真实的暂停里条是不动的（相位不动），所以放开不会凭空多算。
        paused = not pause.running
        self._run_run = self._run_run + 1 if not paused else 0
        # 条宽度有没有信息量：fill > 4 时读数本身就指明了条画到哪；
        # fill 0/极小的时候读数不含位置信息（一费开头两帧本来就没画出来），
        # 这时**必须**要求「运行中」是稳定信号（连续 CLOCK_ARM_FRAMES 拍）。
        # 为什么：暂停时暂停键图标会闪成「运行中」2~3 拍，那几拍里 fill=0 又被当成
        # 「条在轨道上」，相位重同步就可能凭空跨一整个周期 —— 实测一段 76 秒的暂停录像
        # 被这样多记了 61 帧（其中一拍一次跳 27 帧）。
        informative = bar.fill > 4 or self._run_run >= CLOCK_ARM_FRAMES
        readable = (self._zero_run < ZERO_RUN_LIMIT
                    and bar.state not in ('MAX', 'MAX_FADE')
                    and informative)
        if not self._started:
            self._start(bar.fill)
            mode, delta = 'anchor', 0
            self._started = True
        elif readable:
            self._unread_run = 0
            if not paused:
                self._need_confirm = False   # 条能读到 = 游戏在跑的直接证据
            mode, delta, warn = self._pixel(bar.fill, rate, dt, paused)
        elif paused:
            mode, delta = 'pause', 0
        else:
            mode, delta = self._clock_gated(rate, dt)
            self._advance_accumulator(delta)
            self._expect_frac = 0.0

        if paused:
            # 暂停 -> 挂钟通道必须重新「确认」：否则暂停期间图标闪一下就会被当成在跑
            self._unread_run = 0
            self._clock_wait = 0.0
            self._need_confirm = True
            # 暂停时条读空是正常的（暂停菜单遮住/压暗），不能算作「条不可读」的证据，
            # 否则暂停结束后前几拍会被 ZERO_RUN_LIMIT 挡在像素通道外面，
            # 那几帧被挂钟记一次、像素通道回头又记一次 —— 实测合成信号多记 112 帧。
            self._zero_run = 0
            self._expect_frac = 0.0

        # 注意：这里**不能**在换通道时把余量清零。余量代表「不足一帧的相位」，
        # 每次换通道丢一次，几十次换通道就会累积成几帧的系统性少算（实时链路实测）。
        # 只有 resync()（画面不可读、状态未知）才允许丢掉余量。
        self._prev_mode = mode

        self.elapsed += delta
        # 探针只统计「游戏确实在跑」的样本：帧操时条会动但游戏是暂停的，
        # 那种样本会把实测帧率拉低，不代表真实逻辑帧率。
        if mode == 'pixel' and not speed.bullet and not paused:
            self.probe_frames += delta
            self.probe_mt += m * dt
        self._zero_run = self._zero_run + 1 if bar.fill <= 4 else 0

        if speed.conf == 'low' and not warn:
            warn = 'speed-low-conf'
        if bar.grey146 > 40 and bar.state == 'TRACK' and not warn:
            warn = 'grey-in-track'
        if not bar.gray_ok and not warn:
            warn = 'bar-not-gray'

        return FrameResult(
            idx=idx, t=t, fill=bar.fill, bar_state=bar.state,
            grey146=bar.grey146, grey88=bar.grey88,
            pause_ratio=pause.ratio, running=pause.running,
            digit_ratio=speed.digit_ratio, corr_2x=speed.corr_2x,
            has_digit=speed.has_digit, m=m, bullet=speed.bullet,
            mode=mode, delta=delta, elapsed=self.elapsed, warn=warn,
            phase=(self.timing.phase() if self.timing else 0.0),
            err=self.last_k)

    def probe(self) -> tuple:
        """实测逻辑帧率探针。

        返回 (实测 1 倍速帧率, 累计逻辑帧, 累计等效挂钟秒数)。
        帧率 = 像素通道累计逻辑帧 / (Σ m*dt)。样本不足时返回 nan。
        """
        if self.probe_mt <= 0.0:
            return (float('nan'), self.probe_frames, self.probe_mt)
        return (self.probe_frames / self.probe_mt, self.probe_frames, self.probe_mt)

    def resync(self) -> None:
        """重新锚定。

        用于「有一段时间读不到画面」之后（窗口最小化、被完全遮挡、切出去了）：
        这段时间里条可能动过，但我们不知道动了多少，只能丢弃增量、以当前读数重新起锚。
        下一帧会走 anchor 模式（delta=0），之后恢复正常。
        """
        self._started = False
        self.timing = None
        self.last_phase = None
        self.last_k = 0
        self._clock_frac = 0.0
        self._expect_frac = 0.0
        self._zero_run = 0
        self._run_run = 0
        self._unread_run = 0
        self._need_confirm = True
        self._clock_wait = 0.0
        self._prev_mode = None

    # ---------- 相位模型 ----------

    def _speed(self) -> int:
        """每逻辑帧的 fp24 增量。

        注意这是「逻辑帧」的速度，和游戏倍速无关：倍速只改变每秒有多少逻辑帧，
        不改变一费需要多少逻辑帧。所以 1X / 2X / 子弹时间都用 SPEED_NORMAL。
        （回费速度被 tag 减半时才是 1/60，这里先不做。）
        """
        return SPEED_NORMAL

    def _width(self, phase: float) -> int:
        """这个相位在画面上应该有多少像素宽（截到实际能看到的宽度）。"""
        w = synthesized_width(self.model_w, self.bar_width_frac, phase)
        return w if w <= self.visible_w else self.visible_w

    def _start(self, observed: int) -> None:
        """起锚：把累加器放到一个「画出来正好是 observed 宽」的相位上。

        这一步前进的帧数**不计入总数**：我们不知道条在这个宽度上已经待了多久。
        """
        self.timing = CostTiming(self.required)
        _, _, timing = self._resync(observed, allow_wrap=True, base=self.timing, goal=0.0)
        self.timing = timing
        self.last_phase = timing.phase()

    def _resync(self, observed: int, allow_wrap: bool, base: CostTiming = None,
                goal: float = 0.0) -> tuple:
        """相位重同步：找出「从 base 往前推 k 个逻辑帧」里画出来最接近 observed 的 k。

        返回 (k, 误差px, 推完之后的累加器)。
        选法分两步 —— 这一步是计数准不准的关键：
            1. 先划出一条线：只有误差 <= MATCH_TOLERANCE_PX 的 k 才算「画面对得上」；
            2. 这些候选里再挑**离期望帧数 goal 最近**的那个。
        为什么不是一律挑最小的 k：一费的头 1~2 帧条画出来都是 0 宽度（见 synthesis.py），
        最小 k 会在这种平段上一直选 0，把头两帧吃掉 —— 实测每 30 帧少记 1 帧（就是
        「一费只记到 28~29 帧」那个现象）。有了期望帧数这个先验，平段也能正确分开。
        allow_wrap: 允不允许跨过回费边界（上一拍相位 > 0.75 才允许，否则宁可不动也不
        让条「绕一圈」——绕圈会让计数凭空多一整个周期）。
        """
        base = base if base is not None else self.timing
        if base is None:
            return 0, 0, CostTiming(self.required), 
        speed = self._speed()
        period = CostTiming(base.required).frames_until_recovery(speed) or int(LOGICAL_FPS)
        max_advance = period + 1

        best = None          # (|k-goal|, k, 误差, 累加器)
        trial = base
        for k in range(0, max_advance + 1):
            if k:
                trial.advance_one_frame(speed)
                if trial.cycles > base.cycles and not allow_wrap:
                    # 不许可跨边界；再往前只会跨得更多，直接停
                    break
            err = abs(self._width(trial.phase()) - observed)
            if err > MATCH_TOLERANCE_PX:
                continue
            key = abs(k - goal)
            if best is None or key < best[0]:
                best = (key, k, err, trial.copy())
        if best is None:
            # 一个都对不上：把误差报出去，让调用方冻结/兜底
            return 0, abs(self._width(base.phase()) - observed), base
        return best[1], best[2], best[3]

    def _advance_accumulator(self, frames: int) -> None:
        """按帧数把累加器往前推（挂钟通道用），保持相位和读数一致。"""
        if frames <= 0 or self.timing is None:
            return
        speed = self._speed()
        for _ in range(int(frames)):
            self.timing.advance_one_frame(speed)
        self.last_phase = self.timing.phase()

    # ---------- 计数通道 ----------

    def _pixel(self, observed: int, rate: float, dt: float, paused: bool) -> tuple:
        """像素通道：用相位重同步数出这一拍前进了几个逻辑帧。"""
        allow_wrap = self.last_phase is not None and self.last_phase > WRAP_MIN_PREV_PHASE
        # 期望帧数（先验）：正常跑就是 rate*dt；暂停时一律 0 ——
        # 暂停时条但凡真的动过，宽度会把它顶到正确的 k 上去，不需要先验帮忙，
        # 而给先验反而会让「暂停键闪一下」凭空多记帧（实测 76 秒暂停录像多 28 帧）。
        goal = 0.0 if paused else self._expect_frac + rate * dt
        k, err, timing = self._resync(observed, allow_wrap, goal=goal)
        if err > MATCH_TOLERANCE_PX:
            # 读数被遮挡 / 条换了渲染 / 相位对不上：不把它当位移，改走挂钟兜底
            self._expect_frac = 0.0
            if paused:
                return 'bar-reject', 0, 'bar-reject'
            mode, delta = self._clock(rate, dt)
            self._advance_accumulator(delta)
            return mode, delta, 'bar-reject'
        # 余下的不足一帧的期望带下去，下一拍接着用（1 倍速下每个采样只有 0.5 帧）
        self._expect_frac = 0.0 if paused else goal - k
        expect = rate * dt
        # 物理上限：两次采样之间游戏最多能跑这么快（最高 2 倍速 = 60 逻辑帧/秒），
        # 再快就只能是误读（比如暂停时闪一下 + fill=0，重同步会「绕一圈」）。
        # 这一条是硬判据：超了就当作这帧读不准，交给挂钟兜底，绝不把那几十帧记进去。
        bound = (LOGICAL_FPS * MAX_SPEED_M if paused else rate) * dt
        if k > bound + DFILL_SLACK:
            self._expect_frac = 0.0
            if paused:
                return 'bar-reject', 0, 'pixel-jump'
            mode, delta = self._clock(rate, dt)
            self._advance_accumulator(delta)
            return mode, delta, 'pixel-jump'
        self.timing = timing
        self.last_phase = timing.phase()
        self.last_k = k
        warn = 'pixel-stall' if k < expect - STALL_SLACK else ''
        return 'pixel', k, warn

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
