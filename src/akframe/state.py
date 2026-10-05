"""状态机：把逐帧读数翻译成全局逻辑帧。

三条路子：
    像素通道  条画着填充（含一费开头那两帧的 0 宽平段、以及回费后条被收起来的那一帧）
              -> 用 fp24 累加器 + 相位重同步数出这一拍前进了几个逻辑帧。
              **和倍速完全无关**：1X / 2X / 0.2X 都不用读，条走多少像素就记多少帧。
              回费那一帧（条从满变空）也归这条路：相位是确定的，必须当场记下来，
              具体见 Counter.step 里 wrap_pending 的说明。
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

from .fp24 import (SCALE, SPEED_NORMAL, CostTiming,
                    frames_from_required, required_from_frames)
from .features import BarReading, PauseReading, SpeedReading
from .geometry import PAUSE_RUN_RATIO, PAUSE_STOP_RATIO
from .synthesis import synthesized_width

LOGICAL_FPS = 30.0    # 1 倍速 = 每秒 30 逻辑帧
MAX_SPEED_M = 2.0     # 游戏内最高倍速（2X = 60 逻辑帧/秒），做「不可能更快」的上界用
CLOCK_ARM_FRAMES = 5  # 「条不可读 + 运行中」要连续保持这么多帧，才允许挂钟通道补帧
DFILL_SLACK = 3.0     # 像素通道允许「比期望多」多少逻辑帧，超出只记告警
MATCH_TOLERANCE_PX = 5   # 相位重同步的匹配容差：误差超过它就判这帧读数不可信
WRAP_MIN_PREV_PHASE = 0.75   # 上一拍相位不到这个值，就不许跨过回费边界（防误判绕圈）
ANCHOR_MAX_CATCHUP = 2   # 起锚那一拍最多补回几帧「费用周期开头还没长出像素的帧」。
                         # 实测开战起锚都落在周期第 2 / 第 3 帧（fill=6..15）-> 补 1~2 帧；
                         # 条在别处半路重新出现（fill=129 那类）补不了，标 anchor-late

# 「条读到了，但相位模型匹配不上」连续累积了这么久，就认定模型本身已经不可信，
# 按当前画面重新起锚（delta = 0）：这段时间的帧丢掉，绝不猜。
#
# 为什么必须留这条路：_pixel 只在「上一拍相位 > WRAP_MIN_PREV_PHASE(0.75)」时才允许
# 跨回费边界。万一某一拍漏采得太狠（缺口盖过 1/4 个周期），下一拍相位模型还停在
# 旧周期末尾，而画面已经进到新周期开头 —— 于是每一拍都匹配失败、相位再也不前进，
# 只能等新周期的宽度偶然撞上卡住的旧宽度才恢复（实测整整卡掉一个费用周期）。
# 病根是「模型把自己锁死了」：连续这么久对不上，模型就已经不可信，重锚是唯一诚实的
# 做法（宁可丢掉这段无法证明的帧，也不凭空补）。
# 阈值 0.2 秒的依据：10 段实测录像共 2.7 万拍，「条还画着、只是相位对不上」的连续拒帧
# 最长是 **0** 拍（屏幕上出现过 119 连拒，但那全是 EMPTY，走的是关卡判定那条路，
# 它自己有关卡滞回兜底，不经过这里）。
REJECT_RESYNC_SECONDS = 0.2

# 「在关卡里」的证据：费用条这一段被画出来了。三种状态都算画出来：
#     TRACK     正常推进
#     MAX       满费（整条换成灰底）
#     MAX_FADE  满费退出的过渡
# EMPTY 不算证据 —— 主菜单 / 仓库 / 编队 / 结算 / 切场景都会读成 EMPTY，
# 旧逻辑在这些帧上走挂钟通道，于是「不在关卡里也在一秒 30 帧地涨」。
# 关卡内真正的「条读不到」（被遮挡、满费刚结束）用滞回兜住：只要 BATTLE_HOLD_FRAMES
# 之内见过一次费用条，就还按关卡内处理。
BAR_PRESENT_STATES = ('TRACK', 'MAX', 'MAX_FADE')
# 走**像素通道**的通道名：只有这些拍才在「按条计数」。
# 「距上次采纳读数过去了多久」这个物理上限只在像素通道内部才有意义 —— 一旦这一拍
# 不在这个集合里（关卡外 / 起锚 / 满费挂钟 / 条读不到被冻结），计时就必须清零，
# 否则它会把「整段没在看条的时间」算进上限（见 _pixel 里那段说明）。
PIXEL_TRACK_MODES = ('pixel', 'pixel-jump', 'bar-reject', 'wrap', 'wrap-missed')
BATTLE_HOLD_FRAMES = 120     # 1..2 秒 @60Hz 采样：足够覆盖关卡内的短暂读不到
PROBE_MIN_SECONDS = 2.0      # 实测帧率探针至少要攒这么多「未暂停」等效秒才敢报数


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

    bar_w            合成模型里的条槽宽（= round(真实条槽宽，没被裁的那个）
    bar_width_frac   真实条槽宽（亚像素）。不传就按 bar_w 处理
    visible_w        这一路实际能看到的条槽宽度。ROI 被屏幕/画面裁掉时比 bar_w 小，
                     模型会把「画出来的宽度」截到这个值，否则尾部会一直匹配不上
    frames_per_cost  回一费需要多少个逻辑帧。普通关卡 30（1 秒 1 费）；战前选了
                     减回费速度的 tag 时是 60（2 秒）或 120（4 秒）。
                     见 set_frames_per_cost
    required         回一费的目标量（fp24 定点）。给了它就以它为准，frames_per_cost 反推
    """

    def __init__(self, bar_w: int, fps: float = 60.0, bar_width_frac: float = None,
                 visible_w: int = None, required: int = None,
                 frames_per_cost: float = 30.0):
        self.bar_w = int(bar_w)
        self.visible_w = int(visible_w) if visible_w else self.bar_w
        self.model_w = self.bar_w
        self.bar_width_frac = float(bar_width_frac) if bar_width_frac else float(self.bar_w)
        if required and required > 0:
            self.required = int(required)
            self.frames_per_cost = frames_from_required(self.required) or 30.0
        else:
            self.frames_per_cost = (float(frames_per_cost)
                                    if frames_per_cost and frames_per_cost > 0 else 30.0)
            self.required = required_from_frames(self.frames_per_cost) or SCALE
        self.fps = float(fps) if fps and fps > 0 else 60.0
        self.last_m = 1.0          # 读不到倍速时的沿用值：初值必须是 1X（最常见的档），
                                   # 不能是 2X —— 曾经写成 2.0，加上挂钟兜底之后
                                   # 「一费 30 帧」直接被算成 62 帧（挂钟按 60 帧/秒走）
        self.elapsed = 0
        # 分批记：换回费速率时只有像素通道那部分要按比例换算（挂钟通道数的是
        # 真实逻辑帧，和 required 无关）。elapsed 永远是两者之和。
        self.pixel_frames = 0
        self.clock_frames = 0
        self.timing = None         # fp24 累加器；None 表示还没起锚
        self.last_phase = None     # 上一拍认可的相位（防误判绕圈用）
        self.last_k = 0
        self._crossed = False      # 这一拍的 k 帧里是不是跨过了回费边界
        self._run_prev = None      # 上一拍的运行/暂停判定（滞回用）
        self._unread_run = 0       # 「条不可读 + 运行中」连续了多少帧
        self._need_confirm = True  # 是否刚从暂停回来（是的话挂钟通道要先确认，见 _clock_gated）
        self._clock_wait = 0.0     # 确认之前攒下的挂钟秒数，确认时一次性补上
        self._clock_frac = 0.0     # 挂钟通道余量（单位：逻辑帧）
        self._battle_hold = 0      # 还要多少拍之内算「在关卡里」（见 BAR_PRESENT_STATES）
        self._started = False
        # 清零之后的第一拍「起锚」不许把清零**之前**数过的帧补回来：清零的语义是
        # 「从这一刻起从 0 开始数」。不拦的话数字会停在 1/2 上不动
        # （实测回放 2026-10-05 11:01：清零后游戏暂停，数字停在 1，用户报的就是这个）。
        # 但**不能一律不补**：清零时条是空的（本费第 0 帧）的话，清零之后走到
        # 「第一个像素」那 1 帧必须记（用户报的「第零帧到第一帧记不到」）。
        # 所以还要记住清零那一刻条已经画到第几帧：见 _zero_phase / mark_zero_start。
        self._no_catchup = False
        self._zero_phase = None
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
        # 距离上一次「被采纳的像素读数」过去了多久（秒）。
        # 物理上限必须用它、而不是单帧间隔，理由见 _pixel。
        self._since_accept = 0.0
        # 「条读到了、但相位匹配不上」连续累积了多久（秒），见 REJECT_RESYNC_SECONDS。
        self._reject_s = 0.0
        # ---- 逐「费用周期」报告 ----
        # 每跨过一次回费边界（条回卷）就记一次「这个周期实际走了几个逻辑帧」。
        # 这是唯一能直接回答「一费 = 几帧」的口径：不算任何帧率、也不看倍速读数，
        # 纯粹是「条从空走到下一次空」一共记了几帧。正常关卡 1 倍速应当恒为 30。
        # 为什么不能用「累计逻辑帧 / 挂钟秒」代替：那个比值要乘倍速读数，倍速一旦
        # 读错（暂停菜单 / 拖干员时数字不画）整段就整体偏，而且样本边界一抖就跳数。
        self.cycle_lengths = []
        self._cycle_seen = 0     # 已经认过的回费次数（timing.cycles）
        self._cycle_ref = None   # 上一个周期结束时的累计帧号

    # ---------- 对外 ----------

    def step(self, idx: int, t: float, bar: BarReading,
             pause: PauseReading, speed: SpeedReading) -> FrameResult:
        dt = 1.0 / self.fps if self._prev_t is None else (t - self._prev_t)
        if dt <= 0:
            dt = 1.0 / self.fps
        self._prev_t = t
        # 每帧都累加：像素通道一旦拒帧，这个值就会往上涨，
        # 于是 _pixel 的物理上限自己也跟着放宽，下一帧就能把丢掉的那几帧补回来。
        self._since_accept += dt

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
        empty = bar.state == 'EMPTY'
        # 条这一拍读成「空」有两种完全不同的情况，必须分开处理，判据只有一条：
        # **上一拍认下来的相位是不是贴在 1.0 上（也就是上一拍条是画满的）**。
        #   (a) 贴在 1.0 上 -> 刚过回费。游戏把条收起来一拍做回费动画，这一帧就是
        #       **新一费的第一帧**：相位是确定的（1/30，画出来宽度 0），必须当场记 1 帧。
        #       以前这里和 (b) 走同一条路（冻结），于是「条归零」那一帧不计数、要等条
        #       重新长出来才一次性补 2 帧 —— 实测晚了 55 个视频帧（0.9 秒），打轴对不上，
        #       用户看到的就是「第一帧记不到」。
        #   (b) 不贴 1.0 -> 不在关卡里 / 被遮挡 / 暂停中画面里根本没有条：
        #       读数不含任何位置信息，冻结，绝不猜。
        # 回费之后相位立刻掉回 1/30，所以 (a) 每跨一个回费边界最多命中一次，不会连记。
        # 判据用的是 _pixel 里 allow_wrap 的同一个常数：不会出现「这里认了、那里不认」。
        wrap_pending = (self.timing is not None and self.last_phase is not None
                        and self.last_phase > WRAP_MIN_PREV_PHASE)

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
            k = self._start(bar.fill)
            mode, delta, warn = self._anchor_result(k)
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
        elif empty and not wrap_pending:
            # (b)：条这一拍压根没画出来。冻结，等条重新出现时相位重同步自己接上。
            # 这里**不再**要求「运行中」连续确认（原来那道理由上面 wrap_pending 接管了）：
            # 空条读数要能进像素通道，唯一入口就是「上一拍条是满的」，比原来的门更窄，
            # 而且回费后相位立刻掉下来，同一段暂停里最多只放行一帧。
            mode, delta = ('pause', 0) if paused else ('bar-reject', 0)
        else:
            self._unread_run = 0
            if not paused:
                self._need_confirm = False   # 条能读到 = 游戏在跑的直接证据
            mode, delta, warn = self._pixel(bar.fill, dt, paused)
            if empty:
                # 能走到这儿的空条只可能是「刚回费」（wrap_pending 是唯一入口）：
                # 单独一个通道名，面板 / CSV / 统计里能直接看出「归零那一帧记到了」。
                mode = 'wrap'
            elif self._crossed and delta > 1:
                # 这一拍一口气跨过了回费边界，而读数**不是**空条 —— 说明「归零那一帧」
                # 没被采到，k 帧里有一帧是归零、其余是采样漏掉的帧，全在这一行补上了。
                # 表现就是用户看到的「第 0 帧不读、第一帧一次 +2」。这不是计数错
                # （总数照样对），是采样漏了一拍 —— 采样率够高（≥120Hz）时基本不出现。
                # 单独标一个通道名，方便统计它到底还有多频繁。
                mode = 'wrap-missed'
                warn = warn or 'wrap-missed'

        if paused:
            # 暂停 -> 挂钟通道必须重新「确认」：否则暂停期间图标闪一下就会被当成在跑
            self._unread_run = 0
            self._clock_wait = 0.0
            self._need_confirm = True

        # 注意：这里**不能**在换通道时把余量清零。余量代表「不足一帧的相位」，
        # 每次换通道丢一次，几十次换通道就会累积成几帧的系统性少算（实时链路实测）。
        # 只有 resync()（画面不可读、状态未知）才允许丢掉余量。
        self._prev_mode = mode

        # 不在像素通道里的这一拍要把「距上次采纳读数过去了多久」清零：它只该衡量
        # 「连续采样的缺口」，不该把整段没在看条的时间也算进去。实测清零后游戏在
        # 暂停菜单里待了 2 秒多，_since_accept 涨到 2 秒 -> 物理上限放宽成 120 帧，
        # 条重新出现时 HUD 的淡入位移就被当成真帧数记进来了（一次 +5）。
        if mode not in PIXEL_TRACK_MODES:
            self._since_accept = 0.0

        if mode.startswith('clock'):
            self.clock_frames += delta
        else:
            self.pixel_frames += delta
        self.elapsed = self.pixel_frames + self.clock_frames
        # 探针只统计「游戏确实在跑」的样本：帧操时条会动但游戏是暂停的，
        # 那种样本会把实测帧率拉低，不代表真实逻辑帧率。
        if mode == 'pixel' and not speed.bullet and not paused:
            # 探针只累计「条在动、未暂停、非子弹时间」的样本。
            # 注意这个比值**天生有 ±1 帧/秒的量化误差**（窗口头尾各差半帧，1 秒的窗口
            # 就是 ±3%）：实测同一段 1 倍速录像，1 秒窗口给 29.5、8 秒窗口给 29.9，
            # 真值都是 30.0。所以它只用来「看量级」，精确答案用 cycle_stats()。
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
        帧率 = 像素通道累计逻辑帧 / (Σ m*dt)。样本不足（不到 PROBE_MIN_SECONDS）时
        返回 nan —— 短窗口的边界误差能到 ±1 帧/秒，报出来只会误导。
        这个数只用来「粗看量级」，要核对「一费几帧」请看 cycle_stats()。
        """
        if self.probe_mt < PROBE_MIN_SECONDS:
            return (float('nan'), self.probe_frames, self.probe_mt)
        return (self.probe_frames / self.probe_mt, self.probe_frames, self.probe_mt)

    def cycle_stats(self) -> tuple:
        """逐费用周期统计。

        返回 (完整周期数, 中位数, 最小, 最大)，单位都是逻辑帧。
        没有完整周期（录像太短 / 条一直没回卷）时返回 (0, nan, nan, nan)。
        """
        n = len(self.cycle_lengths)
        if not n:
            return (0, float('nan'), float('nan'), float('nan'))
        s = sorted(self.cycle_lengths)
        return (n, float(s[n // 2]), float(s[0]), float(s[-1]))

    def set_frames_per_cost(self, frames: float, rescale: bool = True) -> None:
        """换回费速率：一费几个逻辑帧。

        required 就是「一费要攒多少」，所以换速率只换 required —— speed 永远是
        每逻辑帧 SCALE//30（逻辑帧率恒为 30，和游戏倍速无关，也和回费速度无关）。
        战前 tag（突袭 / 磨难 / 合约词条）改的正是 required，这一点和前人的 fp24 一致。

        rescale=True 会把**已经数出来的**像素通道帧数按新旧速率换算一遍。为什么必须
        这么做：速率是跑起来之后才认出来的，认出来之前那一段是按 30 帧/费在数；
        不换算的话整段录像的绝对帧号从头就偏（打轴器要的就是绝对帧号）。
        挂钟通道那部分不换算（它数的就是真实逻辑帧）。

        换完速率会重新起锚（相位模型的参数变了），所以下一拍走 anchor、delta = 0，
        数字不会凭空跳一下。
        """
        frames = float(frames)
        if not (frames > 0):
            return
        old = self.frames_per_cost
        ratio = (frames / old) if old > 0 else 1.0
        self.frames_per_cost = frames
        self.required = required_from_frames(frames)
        if rescale and abs(ratio - 1.0) > 1e-9:
            self.pixel_frames = int(round(self.pixel_frames * ratio))
            self.cycle_lengths = [int(round(v * ratio)) for v in self.cycle_lengths]
        self.elapsed = self.pixel_frames + self.clock_frames
        self.resync(keep_battle=True)

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
        self._crossed = False
        self._clock_frac = 0.0
        self._unread_run = 0
        self._need_confirm = True
        self._clock_wait = 0.0
        # 重新起锚 = 中间有一段没读到，周期基准也作废（否则会记出一个超长的假周期）
        self._cycle_ref = None
        self._since_accept = 0.0
        self._reject_s = 0.0
        # 注意：清零那对状态（_no_catchup / _zero_phase）这里**故意不动**。
        # set_layout / set_frames_per_cost 都会走一遍 resync，而真正的清零是在
        # set_layout 之后才调 mark_zero_start —— 清了的话标志就活不到下一拍起锚。
        # 相位本身也不需要作废：它记的是「条画到本费第几帧」，跨不跨一段没读到的
        # 画面都成立，_anchor_result 自己会用 ANCHOR_MAX_CATCHUP 把跳太远的挡掉。
        if not keep_battle:
            self._battle_hold = 0
        self._prev_mode = None

    # ---------- 相位模型 ----------

    def _speed(self) -> int:
        """每逻辑帧的 fp24 增量。

        注意这是「逻辑帧」的速度，和游戏倍速无关：倍速只改变每秒有多少逻辑帧，
        不改变一费需要多少逻辑帧。所以 1X / 2X / 子弹时间都用 SPEED_NORMAL。

        回费速度 tag 走的是**另一条路**：它改的是 required（一费要攒多少），不是
        每帧攒多快。两种写法「一费几帧」都对得上，但**边界周期**（第几费会多 1 帧）
        不一样；前人文档给的表是按 required 算的，照抄它才对得上游戏。
        见 fp24.required_from_frames 和 tools/check_cost_rate.py。
        """
        return SPEED_NORMAL

    def _width(self, phase: float) -> int:
        """这个相位在画面上应该有多少像素宽（截到实际能看到的宽度）。"""
        w = synthesized_width(self.model_w, self.bar_width_frac, phase)
        return w if w <= self.visible_w else self.visible_w

    def _start(self, observed: int) -> int:
        """起锚：把累加器放到一个「画出来正好是 observed 宽」的相位上。

        返回 k：起锚这一拍是**当前费用周期的第几帧**（第 1 帧 = 条刚归零、还没长出
        像素的那一帧）。k 由宽度唯一反推（相位-宽度曲线单调，每帧 8.3px），所以
        「周期开头那几帧」是读数证明过的，可以补；补多少见 _anchor_result。
        """
        self.timing = CostTiming(self.required)
        k, _, timing = self._resync(observed, allow_wrap=True, base=self.timing)
        self.timing = timing
        # 累加器是新建的，cycles 从 0 重新开始数：周期基准必须跟着同步，
        # 否则「上次锚定记过的回费次数」比新累加器大，之后再也不会有周期被记下来。
        self._cycle_seen = timing.cycles
        self._cycle_ref = None
        self.last_phase = timing.phase()
        self._reject_s = 0.0
        return k

    def _phase_index(self, observed: int) -> int:
        """把一次条宽读数反推成「本费第几帧」（相位号，1 起）。

        和 _start() 用的是同一条曲线，只是这里只取相位号、不动状态机。
        读不准（误差超容差）或落在空条上时按 1 算 —— 也就是「本费刚开始」。
        """
        k, err, _ = self._resync(int(observed), allow_wrap=False,
                                 base=CostTiming(self.required))
        if err > MATCH_TOLERANCE_PX:
            return 1
        return max(1, int(k))

    def mark_zero_start(self, bar_fill=None) -> None:
        """清零：下一次起锚只补「清零那一刻之后」才走的帧。

        和 resync() 的区别：resync() 只是「中间丢了一段、按当前读数重新对齐」，
        起锚时的补帧仍然成立；清零是「从这里开始数」。

        bar_fill 是清零那一刻读到的条宽。它决定「本费第 1..几帧」算清零之前：
          - 条是空的（0 宽）-> 本费第 0 帧，第 1 帧还没走，起锚时那 1 帧要补上；
          - 条已经有像素   -> 补帧只能从清零时的相位往后算，不能倒回去补。
        bar_fill=None（界面没读到画面）时保守处理：一律不补（宁可少记也不猜）。
        """
        self._no_catchup = True
        self._zero_phase = self._phase_index(bar_fill) if bar_fill is not None else None

    def _anchor_result(self, k: int) -> tuple:
        """起锚这一拍该记几帧（返回 mode, delta, warn）。

        为什么不能一律记 0：条在「费用周期第 1 帧」是**画不出像素**的
        （相位 1/30 -> 宽度 0，见 synthesis.py 的相位表），所以第一次读到条时，
        画面上那条「空的条槽」很可能就是本费的第 1 帧，而这一拍已经是第 k 帧了。
        用户看到的「费用条空（第 0 帧）到出现第一个像素（第 1 帧）还是不记录」
        就是这里：第 1 帧被起锚吞掉，整段录像的帧号从此差 1。

        为什么卡上限：条如果是在别的地方（切场景 / 关菜单 / 被遮挡）半路重新出现
        的，前面丢了多久无法证明，仍然按 0 起锚 —— 宁可少记也不猜。实测开战起锚
        落在周期第 2 / 第 3 帧（fill=6/7/8 -> k=2；fill=15 -> k=3），半路重现落在
        周期第 16 帧（fill=129 -> k=16），两者分得很开。补不上的标 anchor-late，
        面板 / CSV 里能直接看出来。
        """
        head = int(k) - 1        # 从周期第 1 帧数到这一拍，已经过去了这么多帧
        if self._no_catchup:
            # 清零之后第一次起锚：只补「清零那一刻之后」才走的帧。
            # _zero_phase = 清零时条已经画到第几帧；None = 当时没读到画面，不猜。
            self._no_catchup = False
            base_k = self._zero_phase
            self._zero_phase = None
            if base_k is None:
                return 'anchor', 0, ('anchor-late' if head > ANCHOR_MAX_CATCHUP else '')
            head = int(k) - int(base_k)
        if 0 < head <= ANCHOR_MAX_CATCHUP:
            return 'anchor-head', head, ''
        if head > ANCHOR_MAX_CATCHUP:
            return 'anchor', 0, 'anchor-late'
        return 'anchor', 0, ''

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
        # 相位模型连续这么久都匹配不上 -> 模型已经不可信：按当前读数重新起锚，
        # 这一拍 delta = 0（丢掉的帧无法证明，宁可少记也不猜）。见 REJECT_RESYNC_SECONDS。
        stale = self._reject_s > REJECT_RESYNC_SECONDS
        if stale:
            self._start(observed)
        allow_wrap = self.last_phase is not None and self.last_phase > WRAP_MIN_PREV_PHASE
        base = self.timing
        # 上一拍是不是正好停在「画满」那一帧（= 游戏刚回费那一帧）：
        # 它的下一拍一定是「条归零」那一帧，也就是新一费的第 1 帧。
        was_full = bool(base is not None and base.cycles > 0
                        and base.frames_since_cycle_start == 0)
        k, err, timing = self._resync(observed, allow_wrap)
        if err > MATCH_TOLERANCE_PX:
            # 读数被遮挡 / 条换了渲染 / 相位对不上：这条读数不含位移信息
            self._reject_s += dt
            return 'bar-reject', 0, 'bar-reject'
        # 物理上限：**距上一次被采纳的读数**之间最多能跑多快
        # （游戏内最高 2 倍速 = 60 逻辑帧/秒），再快只能是误读
        # （比如暂停时闪一下 + fill=0，重同步会「绕一圈」）。
        # 这里也**不乘倍速读数**：用物理上限就够，倍速错了也不会把硬判据带偏。
        #
        # 为什么分母是 _since_accept 而不是 dt：用 dt 会**死锁**。
        # 实测一段 60fps 录像：条读数是 23，下一帧变成 65（游戏卡了一下，
        # 一帧里过去了 5 个逻辑帧）。此时 _resync 要的就是 k=5，而
        # bound = 60*dt + 3 = 4 -> 判 pixel-jump、扔掉；扔掉之后相位没前进，
        # 下一帧同样差 5 -> 继续扔 …… 整个费用周期一格都记不到，
        # 表现就是「一秒的总数对、一秒内的位置全错」。
        # 换成「距上次采纳过去了多久」以后，第一帧仍会被拒（上限没有放宽），
        # 但被拒的这一帧把时间攒了起来，下一帧上限就够用了，一帧之内自愈。
        # 保护性不变：连续正常采纳时 gap ≈ dt，上限和以前一样紧。
        #
        # 但**暂停中**不许用那个放宽窗口：暂停时逻辑帧根本不该前进，所以这一拍的上限
        # 只按「这一拍自己的间隔」算。实测清零后游戏停在暂停菜单里 2 秒多，
        # _since_accept 涨到 2 秒 -> 上限放宽成 120 帧，条重新出现时 HUD 的淡入位移
        # 被当成真帧数记了进来（一次 +5；同一段回放里还有一次 +7）。放开门限之后
        # 这两次都变成 bar-reject / 相位重锚，一帧都不多记。
        # 只放宽到 dt 不会误伤帧操：实测暂停键滞后那一刻的位移最多 1~2 帧，
        # 60*dt + DFILL_SLACK 足够放行（见 tools/check_speed_mix.py 的帧操用例）。
        gap = dt if paused else max(dt, self._since_accept)
        bound = LOGICAL_FPS * MAX_SPEED_M * gap
        if k > bound + DFILL_SLACK:
            self._reject_s += dt
            return 'pixel-jump', 0, 'pixel-jump'
        self.timing = timing
        self._since_accept = 0.0
        self._reject_s = 0.0
        self.last_phase = timing.phase()
        self.last_k = k
        # 这 k 帧里跨过回费边界了吗？两条路：
        #   上一拍就是「画满」那一帧 -> 下一拍（k>=1）必跨；
        #   更靠前的位置起步（比如 n=28）走 k 帧时累加器自己回卷 -> cycles 涨了。
        self._crossed = (was_full and k >= 1) or (timing.cycles > self._cycle_seen)
        if timing.cycles > self._cycle_seen:
            # 这一步跨过了回费边界（可能还不止一次，但正常推进时只会跨一次）。
            # 边界在这 k 帧里的位置由 frames_since_cycle_start 反推 —— 它回卷时清零，
            # 所以「边界落在哪一帧」是精确的，周期长度不会一帧一帧地漂。
            end = self.elapsed + k - timing.frames_since_cycle_start
            if self._cycle_ref is not None and timing.cycles == self._cycle_seen + 1:
                self.cycle_lengths.append(end - self._cycle_ref)
            self._cycle_ref = end
            self._cycle_seen = timing.cycles
        return 'pixel', k, ('phase-resync' if stale else '')

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
