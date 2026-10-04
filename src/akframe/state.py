"""状态机：把逐帧读数翻译成全局逻辑帧。

三条路子：
    像素通道  条画着填充（含一费开头那两帧的 0 宽平段）-> 用 fp24 累加器 + 相位重同步
              数出这一拍前进了几个逻辑帧。**和倍速完全无关**：1X / 2X / 0.2X 都不用读，
              条走多少像素就记多少帧。
    挂钟通道  **只在满费**（整条换灰底）时用：满费说明条被画出来了 = 一定在关卡里，
              而相位被游戏钉在 0，像素里没有任何时间信息，只能按 挂钟 × 倍速 估。
              倍速取自倍速键上那个数字；数字没画出来（暂停菜单 / 拖干员时的按钮）
              就冻结，绝不猜 —— 猜错一倍整段就多记一倍。
    冻结      以上都不成立（条没画出来、匹配失败、暂停）-> 原地不动，绝不猜

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
from .geometry import PAUSE_RUN_RATIO, PAUSE_STOP_RATIO
from .synthesis import synthesized_width

LOGICAL_FPS = 30.0    # 1 倍速 = 每秒 30 逻辑帧
MAX_SPEED_M = 2.0     # 游戏内最高倍速（2X = 60 逻辑帧/秒），做「不可能更快」的上界用
CLOCK_ARM_FRAMES = 5  # 「条不可读 + 运行中」要连续保持这么多帧，才允许挂钟通道补帧
DFILL_SLACK = 3.0     # 像素通道允许「比期望多」多少逻辑帧，超出只记告警
MATCH_TOLERANCE_PX = 5   # 相位重同步的匹配容差：误差超过它就判这帧读数不可信
WRAP_MIN_PREV_PHASE = 0.75   # 上一拍相位不到这个值，就不许跨过回费边界（防误判绕圈）

# 「在关卡里」的证据：费用条这一段被画出来了。三种状态都算画出来：
#     TRACK     正常推进
#     MAX       满费（整条换成灰底）
#     MAX_FADE  满费退出的过渡
# EMPTY 不算证据 —— 主菜单 / 仓库 / 编队 / 结算 / 切场景都会读成 EMPTY，
# 旧逻辑在这些帧上走挂钟通道，于是「不在关卡里也在一秒 30 帧地涨」。
# 关卡内真正的「条读不到」（被遮挡、满费刚结束）用滞回兜住：只要 BATTLE_HOLD_FRAMES
# 之内见过一次费用条，就还按关卡内处理。
BAR_PRESENT_STATES = ('TRACK', 'MAX', 'MAX_FADE')
BATTLE_HOLD_FRAMES = 120     # 1..2 秒 @60Hz 采样：足够覆盖关卡内的短暂读不到


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
        self.last_m = 1.0          # 读不到倍速时的沿用值：初值必须是 1X（最常见的档），
                                   # 不能是 2X —— 曾经写成 2.0，加上挂钟兜底之后
                                   # 「一费 30 帧」直接被算成 62 帧（挂钟按 60 帧/秒走）
        self.elapsed = 0
        self.timing = None         # fp24 累加器；None 表示还没起锚
        self.last_phase = None     # 上一拍认可的相位（防误判绕圈用）
        self.last_k = 0
        self._run_prev = None      # 上一拍的运行/暂停判定（滞回用）
        self._run_run = 0          # 连续「运行中」的拍数（用来给 fill=0 的读数要求背书）
        self._unread_run = 0       # 「条不可读 + 运行中」连续了多少帧
        self._need_confirm = True  # 是否刚从暂停回来（是的话挂钟通道要先确认，见 _clock_gated）
        self._clock_wait = 0.0     # 确认之前攒下的挂钟秒数，确认时一次性补上
        self._clock_frac = 0.0     # 挂钟通道余量（单位：逻辑帧）
        self._battle_hold = 0      # 还要多少拍之内算「在关卡里」（见 BAR_PRESENT_STATES）
        self._started = False
        self._prev_t = None
        self._prev_mode = None
        # 「暂停计数」开关（手动，给测试用）：按住时数字一动不动，其它读数照常刷新。
        self.manual_pause = False
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

        # 倍速读数**只**给满费挂钟通道定速率，不参与像素通道（像素通道天然免疫倍速）。
        # 注意「倍速键上没画数字」只能推出「这一刻不知道倍速」，不能据此当成 0.2 倍速：
        # 暂停菜单里也只剩三条箭头（实测 23.53 那段录像全程如此），
        # 按 1/5 打折就是 5 倍的系统偏差，比不计数严重得多。
        m_now = float(speed.m) if speed.m == speed.m else None
        if m_now is not None:
            self.last_m = m_now
        m = self.last_m

        warn = ''
        # 暂停键读数带滞回：占比落在中间那条带里就沿用上一拍。
        # 实测「已暂停 0.078 / 运行中 0.245」分得很开，中间带里全是按钮淡入淡出的过渡帧；
        # 单门限会让状态在那儿来回翻，帧操（快速暂停/继续）时尤其明显。
        if pause.ratio >= PAUSE_RUN_RATIO:
            running = True
        elif pause.ratio <= PAUSE_STOP_RATIO:
            running = False
        else:
            running = pause.running if self._run_prev is None else self._run_prev
        self._run_prev = running
        paused = not running
        self._run_run = self._run_run + 1 if not paused else 0
        empty = bar.state == 'EMPTY'
        # 空条（fill <= 4）的读数不含位置信息（一费开头两帧本来就没画出来），
        # 这时**必须**要求「运行中」是稳定信号（连续 CLOCK_ARM_FRAMES 拍）才交给像素通道。
        # 为什么：暂停时暂停键图标会闪成「运行中」2~3 拍，那几拍里 fill=0 又被当成
        # 「条在轨道上」，相位重同步就可能凭空跨一整个周期 —— 实测一段 76 秒的暂停录像
        # 被这样多记了 61 帧（其中一拍一次跳 27 帧）。
        readable = (not empty) or self._run_run >= CLOCK_ARM_FRAMES

        if self.manual_pause:
            # 手动「暂停计数」：数字按住不动，方便逐段核对（对着录像/轴一格一格比）。
            # 别的读数（条 / 按钮 / 通道判定）照常刷新，只是不往 total 上加。
            # 恢复时重新起锚，所以按住的那一段既不会事后补进来、也不会让数字跳。
            self._prev_mode = 'manual-pause'
            return FrameResult(
                idx=idx, t=t, fill=bar.fill, bar_state=bar.state,
                grey146=bar.grey146, grey88=bar.grey88,
                pause_ratio=pause.ratio, running=running,
                digit_ratio=speed.digit_ratio, corr_2x=speed.corr_2x,
                has_digit=speed.has_digit, m=m, bullet=speed.bullet,
                mode='manual-pause', delta=0, elapsed=self.elapsed, warn='',
                phase=(self.timing.phase() if self.timing else 0.0), err=0)

        # ---- 关卡判定 ----
        # 只有「费用条被画出来」才算在关卡里。关卡外一帧都不记（见 BAR_PRESENT_STATES）。
        if bar.state in BAR_PRESENT_STATES:
            self._battle_hold = BATTLE_HOLD_FRAMES
        elif self._battle_hold > 0:
            self._battle_hold -= 1
        in_battle = self._battle_hold > 0

        if not in_battle:
            # 关卡外：不计数，并且重新锚定，等真进关卡时干净起锚（不把两段接起来）
            if self._prev_mode != 'idle':
                self.resync()
            mode, delta = 'idle', 0
        elif not self._started:
            self._start(bar.fill)
            mode, delta = 'anchor', 0
            self._started = True
        elif bar.state in ('MAX', 'MAX_FADE'):
            # 满费：整条换成灰底，相位被游戏钉在 0 —— 像素里一点时间信息都没有。
            # 但「条被画出来」本身就是在关卡里的硬证据，所以这段时间按 挂钟 × 倍速 补。
            # 速率只能来自倍速键上那个数字：数字没画出来（暂停菜单 / 拖干员时的按钮）
            # 就**冻结**，绝不猜一个倍速 —— 实测猜错一倍，整段就多记一倍。
            if paused:
                mode, delta = 'pause', 0
            elif m_now is None or not speed.has_digit:
                # 倍速键上这一刻没画数字（暂停菜单 / 拖干员时的按钮）：不知道速率，冻结。
                mode, delta = 'bar-reject', 0
                warn = 'speed-unknown'
            else:
                mode, delta = self._clock_gated(LOGICAL_FPS * m_now, dt)
        elif paused and empty:
            # 条没画出来 + 暂停：不计数。（条画出来但暂停时仍然走像素通道 ——
            # 帧操时两次采样之间游戏已经跑过、条已经动过，见 _pixel 的说明。）
            mode, delta = 'pause', 0
        elif not readable:
            mode, delta = 'bar-reject', 0
        else:
            self._unread_run = 0
            if not paused:
                self._need_confirm = False   # 条能读到 = 游戏在跑的直接证据
            mode, delta, warn = self._pixel(bar.fill, dt, paused)

        if paused:
            # 暂停 -> 挂钟通道必须重新「确认」：否则暂停期间图标闪一下就会被当成在跑
            self._unread_run = 0
            self._clock_wait = 0.0
            self._need_confirm = True

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

        if speed.conf == 'low' and not warn:
            warn = 'speed-low-conf'
        if bar.grey146 > 40 and bar.state == 'TRACK' and not warn:
            warn = 'grey-in-track'
        if not bar.gray_ok and not warn:
            warn = 'bar-not-gray'

        return FrameResult(
            idx=idx, t=t, fill=bar.fill, bar_state=bar.state,
            grey146=bar.grey146, grey88=bar.grey88,
            pause_ratio=pause.ratio, running=running,
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

    def set_manual_pause(self, on: bool) -> None:
        """开关「暂停计数」（手动，给测试用）。

        按住时数字一动不动；放开时重新起锚，所以按住的那一段不会被事后补进来、
        放开后数字也不会跳一下。和游戏自己的暂停无关（那条路一直是按费用条算的）。
        """
        on = bool(on)
        if on == self.manual_pause:
            return
        self.manual_pause = on
        if not on:
            # keep_battle=True：只是手动按住，不代表「人从游戏里切出去了」，
            # 不该把关卡判定一起清掉（否则恢复后要等费用条再出现一次才开始计数）
            self.resync(keep_battle=True)

    def resync(self, keep_battle: bool = False) -> None:
        """重新锚定。

        用于「有一段时间读不到画面」之后（窗口最小化、被完全遮挡、切出去了）：
        这段时间里条可能动过，但我们不知道动了多少，只能丢弃增量、以当前读数重新起锚。
        下一帧会走 anchor 模式（delta=0），之后恢复正常。

        keep_battle=True 时保留「在关卡里」的判定（手动暂停恢复时用）。
        """
        self._started = False
        self.timing = None
        self.last_phase = None
        self.last_k = 0
        self._clock_frac = 0.0
        self._run_run = 0
        self._unread_run = 0
        self._need_confirm = True
        self._clock_wait = 0.0
        if not keep_battle:
            self._battle_hold = 0
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
        _, _, timing = self._resync(observed, allow_wrap=True, base=self.timing)
        self.timing = timing
        self.last_phase = timing.phase()

    def _resync(self, observed: int, allow_wrap: bool, base: CostTiming = None) -> tuple:
        """相位重同步：找出「从 base 往前推 k 个逻辑帧」里画出来最接近 observed 的 k。

        返回 (k, 误差px, 推完之后的累加器)。

        规则（照抄前人 ArknightsCostBarRuler 的 resync）：k = 0 起步，只在
        「往前推 k 帧之后误差**严格更小**」时才采纳 k。**没有任何倍速先验。**

        为什么不能要先验：1 倍速下每拍只前进 0.5 帧、0.2 倍速（部署慢动作 / 子弹时间）
        每拍只前进 0.1 帧，宽度差都落在 ±5px 容差里，谁都对得上 —— 这时就是先验说了算，
        而先验要用倍速。倍速一读错（实测一段 50 秒录像 2822/2987 帧读不出倍速），
        整段就按错的倍速走：真值 212 逻辑帧被算成 372 帧，「一费 30 帧」变成 62 帧。
        换成「最小 k 严格改善」之后完全不需要倍速：条不动就不动，条动了才往前推。

        平段（一费开头 1~2 帧画出来都是 0 宽）不会丢帧：k 是**从上一拍认可的相位**
        往前推的量，平段里先攒着，宽度一变立刻一次补上。

        allow_wrap: 允不允许跨过回费边界（上一拍相位 > 0.75 才允许）。跨边界 = 绕圈，
        会让计数凭空多一整个周期，所以只在这条硬判据下才放开。
        """
        base = base if base is not None else self.timing
        if base is None:
            return 0, 0, CostTiming(self.required)
        speed = self._speed()
        period = CostTiming(base.required).frames_until_recovery(speed) or int(LOGICAL_FPS)
        max_advance = period + 1

        best_err = abs(self._width(base.phase()) - observed)
        best_k, best_timing = 0, base
        trial = base.copy()
        for k in range(1, max_advance + 1):
            trial.advance_one_frame(speed)
            if trial.cycles > base.cycles and not allow_wrap:
                # 不许可跨边界；再往前只会跨得更多，直接停
                break
            err = abs(self._width(trial.phase()) - observed)
            if err < best_err:
                best_err, best_k, best_timing = err, k, trial.copy()
        return best_k, best_err, best_timing

    # ---------- 计数通道 ----------

    def _pixel(self, observed: int, dt: float, paused: bool) -> tuple:
        """像素通道：这一拍前进了几个逻辑帧，只由条画出来的宽度决定。

        读不准就**冻结**（delta = 0），绝不用挂钟补帧：挂钟要乘倍速，
        倍速一读错就整段几倍地错（实测真值 212 帧被算成 372 帧）。
        冻结的代价是这段时间的帧丢掉，但那本来就是「条看不见」的时间
        （切场景 / 被遮挡 / 暂停），宁可少记也不要凭空多记。
        """
        allow_wrap = self.last_phase is not None and self.last_phase > WRAP_MIN_PREV_PHASE
        k, err, timing = self._resync(observed, allow_wrap)
        if err > MATCH_TOLERANCE_PX:
            # 读数被遮挡 / 条换了渲染 / 相位对不上：这条读数不含位移信息
            return 'bar-reject', 0, 'bar-reject'
        # 物理上限：两次采样之间最多能跑多快（游戏内最高 2 倍速 = 60 逻辑帧/秒），
        # 再快只能是误读（比如暂停时闪一下 + fill=0，重同步会「绕一圈」）。
        # 这里也**不乘倍速读数**：用物理上限就够，倍速错了也不会把硬判据带偏。
        bound = LOGICAL_FPS * MAX_SPEED_M * dt
        if k > bound + DFILL_SLACK:
            return 'pixel-jump', 0, 'pixel-jump'
        self.timing = timing
        self.last_phase = timing.phase()
        self.last_k = k
        return 'pixel', k, ''

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
