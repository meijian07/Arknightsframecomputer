"""状态机：把逐帧读数翻译成全局逻辑帧。

三条路子：
    像素通道  条画着填充（含一费开头那两帧的 0 宽平段、以及回费后条被收起来的那一帧）
              -> 用 fp24 累加器 + 相位重同步数出这一拍前进了几个逻辑帧。
              **和倍速完全无关**：1X / 2X / 0.2X 都不用读，条走多少像素就记多少帧。
              回费那一帧（条从满变空）也归这条路：相位是确定的，必须当场记下来，
              具体见 Counter.step 里 wrap_pending 的说明。
    挂钟通道  **只在满费**（整条换灰底）时用：满费说明条被画出来了 = 一定在关卡里，
              而相位被游戏钉在 0，像素里没有任何时间信息，只能按挂钟估。速率有两个来源：
                子弹时间（>>>）    -> 固定 6 帧/秒（= 30/5），**和倍速设置无关**（实测）
                倍速键上画着数字  -> 30 × 数字（1X 给 30，2X 给 60）
              两个都读不到（按钮淡出 / 被部署遮罩糊掉）就冻结，绝不猜 ——
              猜错一倍整段就多记一倍。
              这条通道**带小数**：满费期间玩家常做帧操（暂停 -> 跑几十毫秒 -> 再暂停），
              那点时间不足 1 个逻辑帧，余量留在 Counter._clock_frac 里接着攒，
              FrameResult.elapsed_f 就是「整数帧 + 余量」（见 Counter._clock）。
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

from .fp24 import (EPSILON_RAW, SCALE, SPEED_NEGATIVE, SPEED_NORMAL, CostTiming,
                    frames_from_required, required_from_frames)
from .features import (EMPTY_MAX_FILL, BarReading, PauseReading, SignReading,
                       SpeedReading)
from .geometry import PAUSE_RUN_RATIO, PAUSE_STOP_RATIO
from .synthesis import synthesized_width

LOGICAL_FPS = 30.0    # 1 倍速 = 每秒 30 逻辑帧
MAX_SPEED_M = 2.0     # 游戏内最高倍速（2X = 60 逻辑帧/秒），做「不可能更快」的上界用
# 子弹时间（倍速键变 >>>，拖干员 / 敌方特殊技能）：**固定的 6 逻辑帧/秒**。
# 「无视当前倍速把游戏速率调至 1/5」调的是 [1 倍速] 的 1/5，不是「当前倍速 ÷ 5」：
# 游戏内 1 秒恒为 30 逻辑帧这一点不变，子弹时间只把时间流速压到 1/5，所以恒为 30/5。
# 用费用条自己当尺子量（条每逻辑帧走 条槽宽/30 px，与倍速无关）：
#   2026-10-05 11.01.51.01（倍速键全程画 2X）：子弹时间段 5.0 / 5.5 / 5.9 / 6.1 帧/秒，
#   同一段视频正常 2X 处恰好 60.00 帧/秒 —— 2X 下也是 6，不是 12（见 docs 3.1）。
# 所以**不能**用 last_m / 5 去算（2X 下那样会整段多记一倍），也不能当「0.2 倍速」。
# 倍速键子弹时间下只画 >>>、不画数字，这条固定速率正好补上这个信息缺失。
BULLET_FPS = LOGICAL_FPS / 5.0
# ---- 满费 + 倍速键读不出数字：按子弹时间兜底（2026-10-10，用户要求）----
# 为什么需要兜底：拖干员进子弹时间时倍速键上只画 >>>、不画数字 —— 而「满费挂钟
# 通道」要靠倍速才能把秒数换成逻辑帧（见 BULLET_FPS）。读不到就等于没信息，
# 那一段会一帧都不记（用户报的「满费下子弹时间过帧无法识别」就是它）。
# 这里记一笔**曾经写错的原因**，免得再被它带偏：2026-10-10 之前这段注释写的是
# 「游戏把那颗键整块收起来、ROI 里只剩场景（最亮 206）」—— 那是因为旧判据锚在
# 「箭头带」上，而三角比那条带高一截：同一套阈值在一个录像里 1277/1277 命中、
# 在另一个里 0/583。**三角一直都在**，只是比数字暗一点（峰值 236~239 vs 255）。
# 现在按形状认（features 里 SPEED_GLYPH_* 那段：两段录像 1186/1186 + 583/583、零误报）。
#
# 规则（用户给的）：**这一局里读到过倍速数字**之后（1X / 2X，模板认不出也算「画着」，
# 见 features.SpeedReading.has_digit），倍速键再变成「什么都没画」就按子弹时间算 ——
# 恒 BULLET_FPS = 6 帧/秒，不再冻结。
# 为什么「读到过数字」是必要条件：关卡外菜单 / 切场景那种空白也是「什么都没画」，
# 但那些画面里 HUD（暂停键）和费用条都不在，关卡判定先挡掉了；关卡内、运行中还
# 能让倍速键整块空白的，实测只有拖干员这一种。
# 代价说清楚：万一某次空白其实是「按钮淡出」而不是子弹时间，这段会按 1/5 记
# （比冻结少记 5 倍）。所以这一路的 warn 单列 `bullet-assumed`，面板 / CSV 上看得见，
# 真出问题一眼认得出。见 docs 第 32 节。
BULLET_ASSUMED_WARN = 'bullet-assumed'
# 【已删除，2026-10-06】「条不可读 + 运行中」连续 5 拍确认门（原 CLOCK_ARM_FRAMES = 5）。
#
# 它原来是防这个的：暂停时右上角继续按钮会以 2~3 个渲染帧为单位闪回「运行中」形状，
# 闪的那几拍走挂钟通道补帧 = 凭空多算（当时实测 76 秒暂停录像 +30 帧，见 commit 00b48e2）。
#
# 但它顺带把**真实的帧操**也吃掉了，而这个代价用户每次都撞上：满费 + 子弹时间
# （恒 6 帧/秒）下，暂停 -> 跑 17~67ms -> 再暂停，每一段都短于 5 拍，于是每一下操作
# 都记 0 帧 —— 用户原话「操作太快记不了」。
# 实测 Desktop 2026.10.06 18.32.40（24.1 秒 / 1448 拍，全程满费）：29 段「运行中」里
# 20 段只有 1~3 拍、全部记 0；而同一段录像里连续暂停 4.3 秒时一次都没闪 —— 用暂停
# 遮罩的整屏变暗交叉验证（暂停时战场亮度 35 / 运行 70），那 262 拍「运行中」全是真的。
# 所以门限取消：读数说在跑就按挂钟记，不足 1 帧的余量留着接着攒（见 Counter._clock）。
# 万一以后哪一版游戏又开始闪：表现是「暂停着数字还在一格一格挪」，面板上直接看得到，
# 那时再上更硬的判据（暂停遮罩的整屏变暗比图标形状可靠得多），别再用拍数门限。
DFILL_SLACK = 3.0     # 像素通道允许「比期望多」多少逻辑帧，超出只记告警
# 物理上限那个比较是浮点的：bound = 30 * 2 * (1/60) 算出来是 0.9999999999999999，
# 加上 DFILL_SLACK 之后「正好顶到上限」的 k=4 会变成 3.9999999999999996 —— k=4
# 被判成超限、整帧扔掉。实测 25% + 局内负费那一档（一费 240 帧）里，累加器的
# 边界周期让第 3 个周期多出 1 帧，k 正好顶到上限，于是每个周期少记 1 帧
# （用户 2026-10-15 报的「25% 到 119 帧的时候只有 117 帧」）。比较留 1e-6。
BOUND_EPS = 1e-6
# 挂钟取整用的极小量：60 * (1/60) 在浮点下等于 0.9999999999999999，直接 int() 会少算
# 一帧、把这一帧推到下一拍（满费挂钟通道踩得到）。加一个比任何真实帧长（33.3ms）
# 小 7 个数量级的量再取整，只在「本来就正好是整数」时起作用。
CLOCK_ROUND_EPS = 1e-9
MATCH_TOLERANCE_PX = 5   # 相位重同步的匹配容差：误差超过它就判这帧读数不可信
WRAP_MIN_PREV_PHASE = 0.75   # 上一拍相位不到这个值，就不许跨过回费边界（防误判绕圈）
# 起锚那一拍最多补回几帧「费用周期开头读不出来的帧」——这是**下限**，
# 真正的上限跟着回费速率走，见 Counter._unreadable_head_frames：
#     30 帧/费 -> 开头 1 帧读不出来，条最早在第 2 帧读到，上限 3
#     60 帧/费 -> 2 帧，最早第 3 帧读到，上限 4
#     120 帧/费 -> 4 帧，最早第 5 帧读到，上限 6
# 实测开战起锚落在周期第 2 / 第 3 帧（fill=6..15）-> 补 2~3 帧；条在别处半路重新
# 出现（fill=129 那类，周期第 16 帧）补不了，标 anchor-late。
ANCHOR_MAX_CATCHUP = 2

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

# ---- 满费锚点（长期漂移校正）----
# 满费（MAX）是画面里唯一一个「与像素推进无关、但时间位置确定」的事件：
# 正常回费时它每隔 frames_per_cost 个逻辑帧出现一次（负费再翻倍）。
# 像素通道每拍只走整数帧，满费挂钟通道还会把不足一帧的余量四舍五入，
# 几十秒下来会攒出 1 帧的偏差。这里把每次 MAX 的**上升沿**记成锚点：
# 如果这一拍离上次锚点接近 n 个完整周期，就把累计帧号轻轻拨回 n 个周期，
# 让误差不跨周期累积。
#
# 只在 |偏差| <= FULL_ANCHOR_TOL 时才拨：玩家在满费时用费后重新满费、
# 或者靠技能提前回满，间隔本来就不是完整周期；差得远说明这不是「同一段
# 连续回费」，只重新起锚，绝不硬改成 +30 —— 那会把真实操作算错。
FULL_ANCHOR_TOL = 2.0

# ---- 费用周期边界锚点（每一条满/空都对齐）----
# 满费锚点只在 MAX 出现时生效；玩家一直在 99 以下用费时可能很久都碰不到 MAX。
# 费用条每走完一个周期必然回卷（条满 -> 条空），这个边界的帧位置是确定的：
# 上一个边界 + frames_per_cost（负费再翻倍）。每次回卷都把累计帧号钉回这个值，
# 误差就不会跨费用周期累积（用户 2026-10-18 实测 40 秒漂 3 帧就是跨周期攒出来的）。
# 只修「离整数周期不超过几帧」的偏差：中途被第 15 章机制停条、切场景漏采时，
# 周期长度本来就不是标准值，这时只重新起锚，不硬改。
CYCLE_ANCHOR_TOL = 4.0

# ---- 局内负费（费用条上出现减号）----
# 负费期间**每逻辑帧只攒一半**（1/60 费/帧），一费从 30 逻辑帧变成 60 逻辑帧。
# 照抄前人：fp24 的 NEGATIVE_SPEED = SCALE/60、engine.rs 的 speed_for_cost_state。
# 相位本身**不重置** —— 进场 / 离场都只是换速度，累加器接着走。
TRANS_ENTER_NEG = 'enter'
TRANS_EXIT_NEG = 'exit'


def speed_for_cost_state(neg: bool) -> int:
    """这一拍该用哪个每逻辑帧增量：负费减半，其余 SCALE/30。

    只和「画面里有没有减号」有关，和倍速、画面帧率都无关（它们不改变逻辑帧率，
    也不改变一费要攒多少）。
    """
    return SPEED_NEGATIVE if neg else SPEED_NORMAL

# 「在关卡里」的证据：费用条这一段被画出来了。三种状态都算画出来：
#     TRACK     正常推进
#     MAX       满费（整条换成灰底）
#     MAX_FADE  满费退出的过渡
#     MAX_COVER 条槽右半段被部署 / 拖拽遮罩压成灰，左端白条还在（不是满费）
# EMPTY 不算证据 —— 主菜单 / 仓库 / 编队 / 结算 / 切场景都会读成 EMPTY，
# 旧逻辑在这些帧上走挂钟通道，于是「不在关卡里也在一秒 30 帧地涨」。
# 关卡内真正的「条读不到」（被遮挡、满费刚结束）用滞回兜住：只要 BATTLE_HOLD_FRAMES
# 之内见过一次费用条，就还按关卡内处理。
BAR_PRESENT_STATES = ('TRACK', 'MAX', 'MAX_FADE', 'MAX_COVER')
# 走**像素通道**的通道名：只有这些拍才在「按条计数」。
# 「距上次采纳读数过去了多久」这个物理上限只在像素通道内部才有意义 —— 一旦这一拍
# 不在这个集合里（关卡外 / 起锚 / 满费挂钟 / 条读不到被冻结），计时就必须清零，
# 否则它会把「整段没在看条的时间」算进上限（见 _pixel 里那段说明）。
PIXEL_TRACK_MODES = ('pixel', 'pixel-jump', 'bar-reject', 'wrap', 'wrap-missed',
                     'max-cover')
BATTLE_HOLD_FRAMES = 120     # 1..2 秒 @60Hz 采样：足够覆盖关卡内的短暂读不到

# ---- 关卡判定的第二道闸：游戏 HUD 还在不在 ----
# 只有「费用条被画出来」不够，两层理由都有实测：
#   ① 菜单 / 仓库 / 切场景的条 ROI 会压到别的高亮 UI，读出一条**死的**假白条
#      （2026-10-05 11:02 那段会话的 CSV：fill=154 连着 3.1 秒、fill=215 连着 1.5 秒，
#      条状态是 TRACK）—— 只按「条在不在」判就会一路放行；
#   ② 换一条仿真更狠：关卡外的画面（桌面 / OBS 窗口 / 加载 / 选关）在**倍速键**的
#      箭头带里也有 0.028~0.093 的亮点占比，旧代码据此判成子弹时间。
# 关卡里 HUD 一定画着：暂停键的 ▶ / ❚❚ 字形（实测两段共 3092 帧，0 帧缺失，
# 最弱一帧的实心占比 0.031），或者倍速键的数字 / 实心箭头。
# 所以「从关卡外进关卡」只认 HUD；已经在关卡里才放宽成「HUD 或者条在动」。
BAR_MOVE_HOLD = 60           # 条「动过」之后还算在动的拍数（1 秒 @60Hz）

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
    mode: str        # anchor / pause / pixel / clock / bar-reject / manual-pause ...
    delta: int       # 本帧新增的逻辑帧
    elapsed: int     # 累计逻辑帧（整数，打轴器 / CSV 的绝对帧号用这个）
    warn: str
    phase: float = 0.0   # 本帧相位（调试用，不进 CSV）
    err: int = 0         # 相位重同步的匹配误差 px（调试用，不进 CSV）
    elapsed_f: float = 0.0   # 累计逻辑帧（带小数）：满费挂钟攒下的不足 1 帧的余量
    pause_drawn: bool = True   # 暂停键画出来了没有（面板要能说「按钮 未画出」）
    pause_solid: float = 0.0   # 上面那个判定用的实心占比（面板显示，方便对着调阈值）
    negative: bool = False     # 这一拍在按「局内负费」算（速度减半）
    sign_run: int = 0          # 减号扫框里实测的最长纯白连续段（调框用，不进 CSV）
    sign_thr: int = 0          # 上面那条的判定阈值（调框用，不进 CSV）
    glyph: float = float('nan')   # 「三个三角」形状分（面板显示，方便对着调阈值）
    bar_fill_count: int = 0       # 从清零/开始算起，费用条「满 -> 空」回卷过几次
    bar_fill_frames: int = 0      # 上一个回卷边界所在的累计帧号（面板算本费余量用）

    def as_row(self) -> list:
        return [self.idx, '%.4f' % self.t, self.fill, self.bar_state,
                self.grey146, self.grey88,
                '%.4f' % self.pause_ratio, int(self.running),
                '%.4f' % self.digit_ratio,
                ('%.4f' % self.corr_2x) if self.corr_2x == self.corr_2x else '',
                int(self.has_digit), '%.1f' % self.m, int(self.bullet),
                self.mode, self.delta, self.elapsed, self.warn,
                '%.3f' % self.elapsed_f]


CSV_HEADER = ['idx', 't', 'fill', 'bar_state', 'grey146', 'grey88',
              'pause_ratio', 'running', 'digit_ratio', 'corr_2x', 'has_digit',
              'm', 'bullet', 'mode', 'delta', 'elapsed', 'warn', 'elapsed_f']


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
        # 局内负费：画面里出现减号时每逻辑帧只攒一半（一费 30 -> 60 帧）。
        # _neg 是「已采纳」的状态（只有读数被采纳时才更新），_neg_override 是面板上的
        # 手动档（None = 按画面判）。见 set_negative_override / _pixel。
        self._neg = False
        self._neg_override = None
        self.elapsed = 0
        # 带小数的累计值（满费挂钟的余量）。__init__ 里就要有：第一拍就开着
        # 「暂停计数」时，手动暂停分支会读它、而那时还没走过任何一次赋值。
        self.elapsed_f = 0.0
        # 分批记：换回费速率时只有像素通道那部分要按比例换算（挂钟通道数的是
        # 真实逻辑帧，和 required 无关）。elapsed 永远是两者之和。
        self.pixel_frames = 0
        self.clock_frames = 0
        self.timing = None         # fp24 累加器；None 表示还没起锚
        self.last_phase = None     # 上一拍认可的相位（防误判绕圈用）
        self.last_k = 0
        self._crossed = False      # 这一拍的 k 帧里是不是跨过了回费边界
        self._run_prev = None      # 上一拍的运行/暂停判定（滞回用）
        self._clock_frac = 0.0     # 满费挂钟通道余量（单位：逻辑帧，跨暂停接着攒）
        # 部署 / 拖拽遮罩期间：条被盖住，但游戏按 1/5 速继续走（恒 6 逻辑帧/秒）。
        # 这里单独攒不足 1 帧的余量，按模型把相位往前推 —— 面板数字在遮罩期间
        # 就能一帧一帧地涨，而不是等遮罩消失再一次性跳过去。
        self._cover_frac = 0.0
        self._cover_active = False
        # 上一拍是不是「满费」（MAX / MAX_FADE）那一档。满费那段条被游戏钉住、
        # 像素里没有任何位置信息，帧数只能靠挂钟通道数；条重新画出来时必须
        # **只把相位对上去、一帧都不再补**，否则那段帧会被数两遍 —— 见 _max_reanchor。
        self._after_max = False
        # 满费段用的挂钟速率（_clock_rate 的最后一次结果）。条重新长出来的那一拍也归
        # 挂钟（见 _max_reanchor），要用它接着算这一拍自己走了几帧。
        self._max_rate = 0.0
        self._battle_hold = 0      # 还要多少拍之内算「在关卡里」（见 BAR_PRESENT_STATES）
        self._move_hold = 0        # 「条在动」还剩多少拍（见 BAR_MOVE_HOLD）
        self._last_fill = None     # 上一拍的条宽 / 状态：用来判「条这一拍动没动」
        self._last_state = None
        self._bullet = False       # 这一拍的子弹时间判定（判据在 read_speed 里）
        # 「这一局里读到过倍速数字」：拖干员时倍速键整块空白，靠它判子弹时间
        # （见 BULLET_ASSUMED_WARN 上面那段）。重新起锚 / 出关卡会作废。
        self._digit_seen = False
        self._started = False
        # 清零之后的第一拍「起锚」不许把清零**之前**数过的帧补回来：清零的语义是
        # 「从这一刻起从 0 开始数」。不拦的话数字会停在 1/2 上不动
        # （实测回放 2026-10-05 11:01：清零后游戏暂停，数字停在 1，用户报的就是这个）。
        # 但**不能一律不补**：清零时条是空的（本费第 0 帧）的话，清零之后走到
        # 「第一个像素」那 1 帧必须记（用户报的「第零帧到第一帧记不到」）。
        # 所以还要记住清零那一刻条已经画到第几帧：见 _zero_phase / mark_zero_start。
        self._no_catchup = False
        self._zero_phase = None
        # 第一次条空是否可以直接当成一个完整周期：正常起锚补过头的算 clean；
        # 清零时如果条已经走了几帧，则只允许在“离整周期很近”时补平（见 _pixel）。
        self._cycle_origin_clean = False
        self._snap_first_cycle = False
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
        # ---- 满费锚点 ----
        # full_anchor 是上一次「MAX 上升沿」时的累计帧号（带小数）；
        # _full_active 让连续的 MAX 只算一次事件，MAX_FADE 也不会重复触发。
        self.full_anchor = None
        self._full_active = False
        self.full_anchor_events = 0       # 认过几次满费上升沿（调试用）
        self.full_anchor_corrections = 0  # 其中真正拨过帧号的次数（调试用）
        self.full_anchor_last_diff = 0.0  # 最近一次拨了多少帧（调试用）
        # 费用条「满 -> 空」回卷过多少次（从开始计数 / 清零算起）。
        # 面板的绝对帧号每跨一个周期就以它为整数周期基准。
        self.bar_fill_count = 0
        # 上一个回卷边界所在的累计帧号。和 _cycle_ref 的区别：_cycle_ref 会在
        # 重新起锚 / 满费之后清空，这里**不清**，专门给面板算「本费已走 x 帧」。
        self.bar_fill_frames = 0
        # 本费周期里出现过哪几种回费速度。负费每帧只攒一半，一费本身就是
        # 30 + n/2 帧；正负混合的周期再做「整数周期」补偿会把真实帧数硬掰回
        # 30 / 60。用户 2026-10-18 实测：正费中途切负费，8 帧负费的那个周期
        # 真值 34 帧，被边界锚点强制成 30。只有**单一速度**的周期才允许补偿
        # （全程正费 -> 30，全程负费 -> 60）。
        self._cycle_had_neg = False
        self._cycle_had_pos = False
        # 上一次满费锚点之后出现过哪几种速度；正负混合就跳过满费锚点的校正。
        self._anchor_had_neg = False
        self._anchor_had_pos = False

    # ---------- 对外 ----------

    def step(self, idx: int, t: float, bar: BarReading,
             pause: PauseReading, speed: SpeedReading,
             sign: SignReading = None) -> FrameResult:
        dt = 1.0 / self.fps if self._prev_t is None else (t - self._prev_t)
        if dt <= 0:
            dt = 1.0 / self.fps
        self._prev_t = t
        # 每帧都累加：像素通道一旦拒帧，这个值就会往上涨，
        # 于是 _pixel 的物理上限自己也跟着放宽，下一帧就能把丢掉的那几帧补回来。
        self._since_accept += dt

        # 倍速读数**只**给满费挂钟通道定速率，不参与像素通道（像素通道天然免疫倍速）。
        # 三种情况分得很开，判据全在 read_speed 里（都用**实心**占比，不用亮点占比）：
        #   画着数字      -> 30 x 数字
        #   只有实心箭头  -> 子弹时间，恒 6 帧/秒，与倍速设置无关
        #   什么都没有    -> 不知道速率，冻结不猜
        # 「倍速键上没画数字」**不等于**子弹时间：暂停菜单里也只剩三条箭头（实测 23.53
        # 那段录像全程如此），按 1/5 打折就是 5 倍的系统偏差，比不计数严重得多。
        # 这里不加「数字消失够久才算子弹时间」那种时间闸：实测两段录像里子弹时间的
        # 进出都是**一拍之内**完成的（22-20-20 第 100 帧 2X->>>>、第 1091 帧 >>>->2X：
        # 数字带占比 0.2721 -> 0.0014、实心箭头 0.0499 -> 0.0316，同一拍切换）。
        # 加时间闸只会把每次进子弹时间的前 0.25 秒（1 倍速 7.5 帧、2 倍速 15 帧）
        # 按正常倍速记错 —— 那正是「一费 30 帧变成 62 帧」这一类偏差的来源。
        # 局内负费：费用数字左边出现减号时，每逻辑帧只攒一半（一费 30 -> 60 帧）。
        # 手动档优先（认不出减号时能强开强关，也是面板上的测试开关），否则看像素。
        # 传 None 的老调用方（录像链路 / 各种自检）行为完全不变。
        neg = (self._neg_override if self._neg_override is not None
               else bool(sign is not None and sign.negative))
        sign_run = int(sign.run) if sign is not None else 0
        sign_thr = int(sign.thr) if sign is not None else 0
        self._bullet = bool(speed.bullet)
        if speed.has_digit:
            # 倍速键上画着数字 —— 把这件事记下来（模板认不出也没关系，只看「画没画」）。
            self._digit_seen = True
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
        # 条槽被部署 / 拖拽网格压成灰底、但左端白条还在：这不是真满费。
        # 遮罩期间条仍然在按逻辑帧前进，只是右半段看不见；必须把
        # _since_accept 留着，遮罩消失那一拍才能按真实位移一次补回
        # （见下面 MAX_COVER 分支和 _pixel 的物理上限）。
        covered = bar.state == 'MAX_COVER'
        if not covered:
            self._cover_active = False
            self._cover_frac = 0.0
        # 暂停期间不累计“等待补帧”的时间，也不保留误判残留：
        # 否则暂停遮罩让条读数抖一下，恢复后会按这段挂钟时间一次补回来。
        # 唯一例外：遮罩把暂停键也压没了（drawn=False），这时既不能
        # 断言游戏在跑、也不能断言暂停；保留窗口给条位移自己说话。
        # 能明确读到「已暂停」（drawn=True）时仍按暂停清窗口，
        # 防止暂停界面的 HUD 淡入位移被当成补帧一次吃进来。
        pause_unreadable = covered and not pause.drawn
        if paused and not pause_unreadable:
            self._since_accept = 0.0
            self._reject_s = 0.0
        empty = bar.state == 'EMPTY'
        # 拖干员 / 部署时游戏进入子弹时间，右上角暂停键同时被压暗
        # （read_pause 给 drawn=False），费用槽还会被部署网格压成 146 灰 ——
        # 看起来和真满费一模一样。条其实没满，这时绝不能走满费挂钟通道。
        # 注意用 pause.running（原始读数），不是上面那个带滞回的 running。
        # 拖干员时暂停键被压暗，ratio 会掉到 0~0.1；带滞回的 running 还可能
        # 沿用上一拍的 True，于是 MAX 分支会继续走挂钟。原始读数不明确是
        # 运行中，就不许挂钟。
        drag_max = bool(self._bullet and not pause.running)
        if drag_max:
            # 拖拽期间即使上一拍是真满费，也不要把满费挂钟锚点带进这一拍。
            self._after_max = False
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
                has_digit=speed.has_digit, m=m, bullet=self._bullet,
                glyph=speed.glyph,
                mode='manual-pause', delta=0, elapsed=self.elapsed,
                elapsed_f=self.elapsed_f, warn='',
                bar_fill_count=self.bar_fill_count,
                bar_fill_frames=self.bar_fill_frames,
                phase=(self.timing.phase() if self.timing else 0.0), err=0,
                pause_drawn=pause.drawn, pause_solid=pause.solid,
                negative=neg, sign_run=sign_run, sign_thr=sign_thr)

        # ---- 关卡判定 ----
        #   ① 费用条被画出来（BAR_PRESENT_STATES）；
        #   ② 游戏 HUD 画着（暂停键的 ▶/❚❚ 字形，或倍速键的数字 / 实心箭头）。
        # 「从关卡外**进**关卡」只认 ②：菜单 / 仓库 / 切场景压出来的亮块是死的 ——
        # 它可能让条读数动起来，但画不出游戏 HUD，所以永远进不来。这条就是根治
        # 「在别的界面也计数」的那条规则。进了关卡之后放宽成「② 或条这一拍确实在动」，
        # 兜住关卡内 HUD 偶尔读不到的那么一两拍（实测 22-20-20 第 55~56 帧就是）。
        bar_moving = (bar.state != self._last_state) or (bar.fill != self._last_fill)
        self._last_state, self._last_fill = bar.state, bar.fill
        if bar_moving:
            self._move_hold = BAR_MOVE_HOLD
        elif self._move_hold > 0:
            self._move_hold -= 1
        hud = bool(pause.drawn or speed.drawn)
        keep = hud or (self._battle_hold > 0 and self._move_hold > 0)
        if bar.state in BAR_PRESENT_STATES and keep:
            self._battle_hold = BATTLE_HOLD_FRAMES
        elif self._battle_hold > 0:
            self._battle_hold -= 1
        in_battle = self._battle_hold > 0
        if in_battle:
            # 记下这一费里出现过哪几种回费速度。正负混合的周期不做
            # 「整数周期」补偿（见 _cycle_had_neg / _anchor_had_neg）。
            if neg:
                self._cycle_had_neg = True
                self._anchor_had_neg = True
            else:
                self._cycle_had_pos = True
                self._anchor_had_pos = True

        if not in_battle:
            # 关卡外：不计数，并且重新锚定，等真进关卡时干净起锚（不把两段接起来）
            if self._prev_mode != 'idle':
                self.resync()
            # 出了关卡，上一局的满费锚点就没有意义了（下一局可能从任意费用开始）。
            self._reset_full_anchor()
            mode, delta = 'idle', 0
            if bar.state in BAR_PRESENT_STATES:
                # 画面里「有东西」但不是关卡（菜单 / 仓库 / 切场景压到高亮 UI）。
                # 单独标一下：面板能直接说「不在关卡里」，而不是让人以为条真读到了。
                warn = 'not-battle'
        elif not self._started:
            px_speed = speed_for_cost_state(neg)
            k = self._start(bar.fill, speed=px_speed)
            visible = bar.fill > EMPTY_MAX_FILL      # 条真画出来了（TRACK 读数）
            if paused and self._no_catchup:
                # 暂停中点「开始」：只把相位对齐，不把开始前的本费进度补进来。
                # 用户录像里首拍 running=0，却从 0 直接跳到 2，就是这里漏判暂停。
                mode, delta, warn = 'pause', 0, ''
                self._no_catchup = False
                self._zero_phase = None
            else:
                mode, delta, warn = self._anchor_result(k, px_speed, visible)
            self._started = True
        elif bar.state in ('MAX', 'MAX_FADE'):
            # 满费：整条换成灰底，相位被游戏钉在 0 —— 像素里一点时间信息都没有。
            # 但「条被画出来」本身就是在关卡里的硬证据，所以这段时间按挂钟补。
            # 速率怎么取（优先级、以及每一步为什么不能换）都在 _clock_rate 里；
            # 读得到就**一拍都不放过**：满费期间的帧操很短（17~67ms），不足 1 个
            # 逻辑帧，靠 _clock() 里的余量接着攒（见 _clock 的说明）。
            if drag_max or not pause.running:
                # 暂停键不是明确「运行中」：假满费 / 拖拽 / 过渡帧，只冻结。
                # 不锚满费、不进挂钟，避免部署网格把条压成 146 灰时凭空涨帧。
                mode, delta = 'bar-reject', 0
                warn = 'drag-max-ignore' if drag_max else ''
                self._after_max = False
            elif paused:
                mode, delta = 'pause', 0
                self._after_max = True
            else:
                rate, rw = self._clock_rate(m_now, speed.has_digit)
                if rate > 0:
                    mode, delta = self._clock(rate, dt)
                    self._max_rate = rate
                else:
                    # 从没读到过倍速数字（刚进关卡、或一直在别的界面）：不知道速率，冻结不猜。
                    mode, delta = 'bar-reject', 0
                warn = rw
                # 满费这一档的帧数是**挂钟**数出来的（条上没有任何位置信息）。
                # 条重新画出来的那一拍要按它重新对相位、并且不再补帧 —— 见 _max_reanchor。
                self._after_max = True
        elif covered:
            # 部署 / 拖拽遮罩：条槽右半段被压成 146 灰，左端白条还在。
            # 相位信息没丢，但右半段看不见；拖拽期间游戏固定 1/5 速
            # （6 逻辑帧/秒），所以按这个速率把**相位模型**往前推，
            # 读数用模型合成 —— 面板数字在遮罩期间就能连续上涨。
            # 遮罩消失后，真实条读数会和模型差 0~1 帧，_pixel 自己会拨平，
            # 不会再把整段位移重算一遍。
            if not self._cover_active:
                self._cover_active = True
                self._cover_frac = 0.0
            if not pause.running:
                # 暂停键明确不是运行中：可能真的暂停了，冻结。
                mode, delta = 'max-cover', 0
                warn = 'max-cover'
            else:
                acc = BULLET_FPS * dt + self._cover_frac
                n = int(acc + CLOCK_ROUND_EPS)
                if n <= 0 or self.timing is None:
                    self._cover_frac = acc
                    mode, delta = 'max-cover', 0
                    warn = 'max-cover'
                else:
                    self._cover_frac = acc - n
                    px_speed = speed_for_cost_state(neg)
                    trial = self.timing.copy()
                    for _ in range(n):
                        trial.advance_one_frame(px_speed)
                    observed = self._width(trial.phase())
                    self._after_max = False
                    mode, delta, warn = self._pixel(observed, dt, False, neg)
                    if mode in ('bar-reject', 'pixel-jump'):
                        # 这一拍没被采纳（模型对不上）：把帧还回去，下一拍再试。
                        self._cover_frac += n
                        mode, delta, warn = 'max-cover', 0, 'max-cover'
        elif empty and not wrap_pending:
            # (b)：条这一拍压根没画出来。冻结，等条重新出现时相位重同步自己接上。
            # 这里**不再**要求「运行中」连续确认（原来那道理由上面 wrap_pending 接管了）：
            # 空条读数要能进像素通道，唯一入口就是「上一拍条是满的」，比原来的门更窄，
            # 而且回费后相位立刻掉下来，同一段暂停里最多只放行一帧。
            #
            # 2026-10-14 这里曾经按挂钟把「本费开头那 1~4 帧」逐帧垫上（_head_step）、
            # 2026-10-15 回退：那几个帧数垫的是**账**、相位一步没动，读数回来时要扣掉，
            # 只要有一条路（resync / 起锚 / 拒帧）把账丢了就是多记 —— 用户实测 50% 一费
            # 被垫成 62 帧、25% 又反过来少 2 帧。像素通道不碰挂钟这条老规矩不能破：
            # 那几帧宁可晚一拍、由读数一次补上（总数照样对），也不按挂钟猜。
            mode, delta = ('pause', 0) if paused else ('bar-reject', 0)
        else:
            mode, delta, warn = self._pixel(bar.fill, dt, paused, neg)
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
            # 暂停：满费分支已经在上面直接给了 delta=0，这里只需要**保住余量** ——
            # 帧操一下只跑零点几帧，余量就是那零点几帧，清了等于把这一下操作删掉。
            pass

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
            # 挂钟通道数的是**绝对逻辑帧**：不能跟着回费速率的换算走
            # （set_frames_per_cost 只该换算「按费用条像素」推出来的那部分）。
            self.clock_frames += delta
        else:
            self.pixel_frames += delta
        self.elapsed = self.pixel_frames + self.clock_frames
        # 带小数的累计值：像素通道数出来的永远是整数，小数只可能来自满费挂钟那点余量。
        # 面板在满费时印这个（用户要「精确到小数点后一到两位」），打轴器/CSV 用整数。
        self.elapsed_f = self.pixel_frames + self.clock_frames + self._clock_frac
        # ---- 满费锚点 ----
        # 只在「MAX 上升沿」记一次；MAX 连续亮很多拍、以及 MAX -> MAX_FADE 都不算新事件。
        # 暂停中的 MAX 不记锚点：暂停那段本来就不走逻辑帧，等恢复运行后再记。
        full_now = bar.state == 'MAX'
        if in_battle and bar.state in ('MAX', 'MAX_FADE') and self._full_active:
            pass                      # 同一次满费还在继续（或正在淡出）
        elif in_battle and full_now and not paused:
            self._full_anchor_event(self.frames_per_cost * (2.0 if neg else 1.0))
            self._full_active = True
        else:
            self._full_active = False
        # 探针只统计「游戏确实在跑」的样本：帧操时条会动但游戏是暂停的，
        # 那种样本会把实测帧率拉低，不代表真实逻辑帧率。
        if mode == 'pixel' and not self._bullet and not paused:
            # 探针只累计「条在动、未暂停、非子弹时间」的样本。
            # 注意这个比值**天生有 ±1 帧/秒的量化误差**（窗口头尾各差半帧，1 秒的窗口
            # 就是 ±3%）：实测同一段 1 倍速录像，1 秒窗口给 29.5、8 秒窗口给 29.9，
            # 真值都是 30.0。所以它只用来「看量级」，精确答案用 cycle_stats()。
            self.probe_frames += delta
            self.probe_mt += m * dt

        if speed.conf == 'low' and not warn:
            warn = 'speed-low-conf'
        if bar.grey146_core > 40 and bar.state == 'TRACK' and not warn:
            warn = 'grey-in-track'
        if not bar.gray_ok and not warn:
            warn = 'bar-not-gray'

        return FrameResult(
            idx=idx, t=t, fill=bar.fill, bar_state=bar.state,
            grey146=bar.grey146, grey88=bar.grey88,
            pause_ratio=pause.ratio, running=running,
            digit_ratio=speed.digit_ratio, corr_2x=speed.corr_2x,
            has_digit=speed.has_digit, m=m, bullet=self._bullet,
            glyph=speed.glyph,
            mode=mode, delta=delta, elapsed=self.elapsed, elapsed_f=self.elapsed_f,
            warn=warn,
            phase=(self.timing.phase() if self.timing else 0.0),
            err=self.last_k, pause_drawn=pause.drawn, pause_solid=pause.solid,
            negative=neg, sign_run=sign_run, sign_thr=sign_thr,
            bar_fill_count=self.bar_fill_count,
            bar_fill_frames=self.bar_fill_frames)

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

    def reset_cycle_stats(self) -> None:
        """把「满条次数 / 上一个条空位置」清零。

        清零计数、换回费速率时用。面板的「满条 n ｜ +x 帧」靠这两个数算：
        x = 累计帧号 - bar_fill_frames，所以条空时 x 一定是 0。
        """
        self.bar_fill_count = 0
        self.bar_fill_frames = 0
        self._cycle_had_neg = False
        self._cycle_had_pos = False

    def set_frames_per_cost(self, frames: float, rescale: bool = True) -> None:
        """换回费速率：一费几个逻辑帧。

        required 就是「一费要攒多少」，所以换速率只换 required —— speed 就是每逻辑帧
        SCALE//30（负费时才减半，见 _speed；逻辑帧率恒为 30，和游戏倍速无关）。
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
        self._cycle_origin_clean = False
        self._snap_first_cycle = False
        if abs(ratio - 1.0) > 1e-9:
            # 换周期后旧的「满条次数 n」不能再和新周期相乘；周期基准也在
            # resync() 里作废了，下一次条空重新从 n = 1 开始对齐。
            self.reset_cycle_stats()
        if rescale and abs(ratio - 1.0) > 1e-9:
            self.pixel_frames = int(round(self.pixel_frames * ratio))
            self.cycle_lengths = [int(round(v * ratio)) for v in self.cycle_lengths]
        self.elapsed = self.pixel_frames + self.clock_frames
        self.resync(keep_battle=True)
        # 周期长度变了，旧锚点对应的「+30 / +60」已经不成立，重新建立。
        self._reset_full_anchor()

    def set_negative_override(self, value) -> None:
        """手动指定「局内负费」：True 强开 / False 强关 / None 按画面判（默认）。

        为什么留这一手：减号那个框是照抄前人的（见 geometry.sign_rect），本机还没有
        一段真实的负费录像能对着钉它。认不出来时用户至少能强制开一格，把计数救回来；
        画面判据本身也能用面板上那行「减号 N/thr」当场核对。

        换速度**不重新起锚** —— 相位是连续的，_pixel 会在下一拍按新速度走，
        并且把这一拍当转场帧做亚帧修正（见 _boundary_correction）。
        """
        value = None if value is None else bool(value)
        self._neg_override = value

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
            # 不该把关卡判定一起清掉（否则恢复后要等费用条再出现一次才开始计数）。
            # mark_zero_start(None)：恢复时只对相位、不补暂停期间条走过的帧 ——
            # 否则条恢复时正好在本费第 2 帧，数字会从 0 直接跳到 2。
            self.resync(keep_battle=True)
            self.mark_zero_start(None)

    def adopt(self, other: 'Counter') -> None:
        """把另一个 Counter 已经数出来的帧数接过来（换布局 / 重新校准时用）。

        为什么不能只搬 elapsed：step() 每一拍都用 pixel_frames + clock_frames **重算**
        elapsed（见 step 末尾）。只赋一个 elapsed 的话，下一拍立刻被 0 + 0 覆盖 ——
        表现就是「换个窗口 / 重新校准一下，数字从 0 重新数」。
        实测（2026-10-07）：LiveMonitor.set_layout(keep_elapsed=True) 原来只写了
        self.counter.elapsed = elapsed，所以这个「保留已数帧数」一直是假的。

        逐帧的费用周期长度也一起带过去：它是按**帧**记的，和像素几何无关（换分辨率
        不该把「一费 N 帧（中位）」这个统计清空）。last_m 同理 —— 读不到倍速时靠它沿用，
        换布局时清回 1.0 会在满费挂钟通道上按错误的倍速算一拍。
        """
        self.pixel_frames = other.pixel_frames
        self.clock_frames = other.clock_frames
        self._clock_frac = other._clock_frac
        self._cover_frac = other._cover_frac
        self._cover_active = other._cover_active
        self.elapsed = self.pixel_frames + self.clock_frames
        self.elapsed_f = self.elapsed + self._clock_frac
        self.cycle_lengths = list(other.cycle_lengths)
        self._cycle_seen = other._cycle_seen
        self.last_m = other.last_m
        self._digit_seen = other._digit_seen
        self.full_anchor = other.full_anchor
        self._full_active = other._full_active
        self.full_anchor_events = other.full_anchor_events
        self.full_anchor_corrections = other.full_anchor_corrections
        self.full_anchor_last_diff = other.full_anchor_last_diff
        self.bar_fill_count = other.bar_fill_count
        self.bar_fill_frames = other.bar_fill_frames
        self._cycle_had_neg = other._cycle_had_neg
        self._cycle_had_pos = other._cycle_had_pos
        self._anchor_had_neg = other._anchor_had_neg
        self._anchor_had_pos = other._anchor_had_pos

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
        self._cover_frac = 0.0
        self._cover_active = False
        self._after_max = False
        self._max_rate = 0.0
        # 重新起锚 = 中间有一段没读到，周期基准也作废（否则会记出一个超长的假周期）
        self._cycle_ref = None
        self._since_accept = 0.0
        self._reject_s = 0.0
        # 「条动没动」的历史也作废：重新起锚之后的第一拍不该因为旧读数而算「在动」。
        self._last_state = None
        self._last_fill = None
        self._move_hold = 0
        # 子弹时间的判定也作废：重新拿到画面后，第一拍重新读倍速键。
        self._bullet = False
        # 「读到过倍速数字」也一起作废：它是「这一局里」的事实，中间断了一段就得重新认。
        self._digit_seen = False
        # 负费状态也**故意不动**：它记的是「画面里有没有减号」这个事实，和「相位模型
        # 可不可信」无关；重新起锚后的第一拍会按新读数重新判（且不判转场，见 _transition）。
        # 满费锚点这里也**故意不动**：重新校准 / 换布局 / 手动暂停都不代表
        # 中间真的走过了新的一段逻辑时间；下一次 MAX 仍可以拿它做小偏差校正。
        # 真正的重置只有三处：出关卡、清零、换回费速率（见 _reset_full_anchor）。
        # 注意：清零那对状态（_no_catchup / _zero_phase）这里**故意不动**。
        # set_layout / set_frames_per_cost 都会走一遍 resync，而真正的清零是在
        # set_layout 之后才调 mark_zero_start —— 清了的话标志就活不到下一拍起锚。
        # 相位本身也不需要作废：它记的是「条画到本费第几帧」，跨不跨一段没读到的
        # 画面都成立，_anchor_result 自己会用 ANCHOR_MAX_CATCHUP 把跳太远的挡掉。
        if not keep_battle:
            self._battle_hold = 0
        self._prev_mode = None

    @property
    def negative(self) -> bool:
        """最近一次被采纳的读数说「局内负费」（面板要显示这个）。

        不是 _neg_override：手动档只是「下一拍按什么速度算」，真正的状态要等读数被
        采纳（和前人 engine.rs 的 last_cost_is_negative 一个语义）。
        """
        return bool(self._neg)

    # ---------- 相位模型 ----------

    def _speed(self) -> int:
        """每逻辑帧的 fp24 增量（默认值；_pixel 会按**这一拍读到的**负费状态传进来）。

        这是「逻辑帧」的速度，和游戏倍速无关：倍速只改变每秒有多少逻辑帧，
        不改变一费需要多少逻辑帧。所以 1X / 2X / 子弹时间都用同一个速度。

        回费变慢有**两条路**，都通向「一费更多帧」，但改的量不一样：
          * 战前 tag（突袭 / 磨难 / 合约词条）改 required（一费要攒多少）——
            见 fp24.required_from_frames 和 tools/check_cost_rate.py；
          * 局内负费改 speed（每逻辑帧攒多快）—— 就是这里的 SPEED_NEGATIVE。
        两种写法「一费几帧」都对，但**边界周期**（第几费会多 1 帧）不一样，
        照抄前人各自的那一张表才对得上游戏。
        """
        return SPEED_NEGATIVE if self._neg else SPEED_NORMAL

    def _width(self, phase: float) -> int:
        """这个相位在画面上应该有多少像素宽（截到实际能看到的宽度）。"""
        w = synthesized_width(self.model_w, self.bar_width_frac, phase)
        return w if w <= self.visible_w else self.visible_w

    def _start(self, observed: int, speed: int = None) -> int:
        """起锚：把累加器放到一个「画出来正好是 observed 宽」的相位上。

        返回 k：起锚这一拍是**当前费用周期的第几帧**（第 1 帧 = 条刚归零、还没长出
        像素的那一帧）。k 由宽度唯一反推（相位-宽度曲线单调，每帧 8.3px），所以
        「周期开头那几帧」是读数证明过的，可以补；补多少见 _anchor_result。
        """
        self.timing = CostTiming(self.required)
        k, _, timing = self._resync(observed, allow_wrap=True, base=self.timing, speed=speed)
        self.timing = timing
        # 累加器是新建的，cycles 从 0 重新开始数：周期基准必须跟着同步，
        # 否则「上次锚定记过的回费次数」比新累加器大，之后再也不会有周期被记下来。
        self._cycle_seen = timing.cycles
        self._cycle_ref = None
        self.last_phase = timing.phase()
        self._reject_s = 0.0
        return k

    def _phase_index(self, observed: int) -> int:
        """把一次条宽读数反推成「本费第几帧」（相位号）。

        和 _start() 用的是同一条曲线，只是这里只取相位号、不动状态机。
        **条是空的（0 宽）返回 0** —— 那一刻本费一帧都还没走，正是「第 0 帧」。
        从前这里用 max(1, k) 兜底，等于把「空条」记成已经走了 1 帧：清零 / 起锚
        的账跟着差 1（和 _anchor_result 里那个 k - 1 是同一个错，见那里的说明）。
        读不准（误差超容差）时仍按 1 算 —— 宁可少记，也不猜。
        """
        k, err, _ = self._resync(int(observed), allow_wrap=False,
                                 base=CostTiming(self.required))
        if err > MATCH_TOLERANCE_PX:
            return 1
        return int(k)

    def mark_zero_start(self, bar_fill=None) -> None:
        """清零：下一次起锚只补「清零那一刻之后」才走的帧。

        和 resync() 的区别：resync() 只是「中间丢了一段、按当前读数重新对齐」，
        起锚时的补帧仍然成立；清零是「从这里开始数」。

        bar_fill 是清零那一刻读到的条宽。它决定「本费第 1..几帧」算清零之前：
          - 条是空的（0 宽）-> 本费第 0 帧，第 1 帧还没走，起锚时那 1 帧要补上；
          - 条已经有像素   -> 补帧只能从清零时的相位往后算，不能倒回去补。
        bar_fill=None（界面没读到画面）时保守处理：一律不补（宁可少记也不猜）。
        """
        already = self._no_catchup
        self._no_catchup = True
        if bar_fill is not None:
            self._zero_phase = self._phase_index(bar_fill)
            # 清零是从本费中间开始的：第一次条空只差几帧时补平，别把它记成
            # “满条 1 次”又留下 -2（用户 2026-10-18 看到的第一费 28 帧）。
            self._snap_first_cycle = True
        elif not already:
            # begin_counting() 会在 resync() 后再调一次 mark_zero_start(None)；
            # 如果前面 reset() 已经记下清零相位，这次不能把它擦掉。
            self._zero_phase = None
            self._snap_first_cycle = False
        # 清零后帧号从 0 重新起算，旧锚点必须作废，否则第一次满费会把帧号拨回去。
        self._reset_full_anchor()

    # ---------- 满费锚点 ----------

    def _reset_full_anchor(self) -> None:
        """作废满费锚点（出关卡 / 清零 / 换回费速率时）。"""
        self.full_anchor = None
        self._full_active = False
        self._anchor_had_neg = False
        self._anchor_had_pos = False

    def _full_anchor_event(self, period: float) -> None:
        """MAX 上升沿：把累计帧号对齐到「上次满费 + n 个完整周期」。

        为什么要锚在满费：像素通道一费固定走 30 / 60 / 120 个逻辑帧，
        正常回费时相邻两次满费之间就应当是整数个周期。逐拍取整、满费挂钟
        余量的四舍五入都会留下不到 1 帧的误差，采样几十分钟后才显形；
        每次满费把误差拨回去，误差就不会跨周期累积。

        只拨「离某个整数周期不超过 FULL_ANCHOR_TOL 帧」的偏差：
        差得远说明满费间隔本来就不是完整周期（中途用费 / 技能提前回满），
        这时只重新起锚，不能硬算成 +30 —— 那会把真实操作算错。
        """
        period = float(period)
        if not (period > 0):
            return
        cur = self.elapsed_f
        self.full_anchor_events += 1
        # 上一次满费之后只要正负费混合过，这段间隔就不是整数个同速周期，
        # 不能拿它做帧数校正（同 _pixel 里的 _cycle_had_neg）。
        mixed = self._anchor_had_pos and self._anchor_had_neg
        if self.full_anchor is not None and not mixed:
            gap = cur - self.full_anchor
            ratio = gap / period
            cycles = int(ratio + 0.5) if ratio >= 0 else int(ratio - 0.5)
            if cycles >= 1:
                target = self.full_anchor + cycles * period
                diff = target - cur
                if 1e-9 < abs(diff) <= FULL_ANCHOR_TOL:
                    # 整数部分记进挂钟通道（漂移正是它产生的），小数部分放回余量；
                    # 这样 elapsed / elapsed_f / 下一拍时钟推进三者保持一致。
                    whole = int(round(diff))
                    self.clock_frames += whole
                    self._clock_frac += diff - whole
                    # 余量保持在 [0, 1)，和 _clock 的不变量一致：面板上的整数帧
                    # 永远等于 elapsed_f 的整数部分，不会出现「.4 却跳了两帧」。
                    carry = int(self._clock_frac // 1.0)
                    if carry:
                        self.clock_frames += carry
                        self._clock_frac -= carry
                    self.elapsed = self.pixel_frames + self.clock_frames
                    self.elapsed_f = self.elapsed + self._clock_frac
                    if self._cycle_ref is not None:
                        self._cycle_ref += whole + carry
                    self.full_anchor_corrections += 1
                    self.full_anchor_last_diff = diff
                    cur = self.elapsed_f
        self.full_anchor = cur
        self._anchor_had_neg = False
        self._anchor_had_pos = False

    def _unreadable_head_frames(self, speed: int = None) -> int:
        """费用周期开头「读不出来」的帧数：这几帧画出来太窄，read_bar 只会给 EMPTY。

        两个原因叠在一起，都跟着回费速率一起变：
          * 条身比可见条槽宽 3.892%（synthesis.BAR_LENGTH_RATIO），左端那一小截藏在
            条槽外面 —— 一费开头的几帧条其实已经在走，画出来却是 0 宽；
          * read_bar 把 fill <= EMPTY_MAX_FILL（现在是 1px）一律读成 EMPTY，和「压根没画条」
            长得一模一样（见 features.read_bar 末段的判据）。
        所以「一费开头能补回几帧」= 宽度还没超过 EMPTY_MAX_FILL 的那几帧：

            30 帧/费 -> 1 帧，60 帧/费 -> 2 帧，120 帧/费 -> 4 帧

        为什么不能写死：30 帧/费时它恰好等于老代码那个常数 2 - 1，减费档位上就
        远远不够。写死 2 的那阵子（到 2026-10-13）：25%（120 帧/费）与 50%（60 帧/费）的开战起锚都落在
        「读得出来的第一帧」，两个都被上限挡掉、判 anchor-late，一开局就白丢好几帧
        —— 用户报的「开头几帧识别不到」
        「25% 完全不准」就是它（回归项 check_state_synth.reduced_rate_anchor_head）。
        2026-10-14 又把 EMPTY_MAX_FILL 从 4 收到 1（见 features.py）：一费开头那几帧
        以前是「读不到，只能靠起锚补」，现在是真读得到（前人 25% 回费的实测宽度序列
        是 0 2 3 4 5…，2px 就是画出来了）。这张上限表因此也变小，只负责兜住「条第一次
        出现就落在周期中段」那种半路重现。
        """
        speed = self._speed() if speed is None else int(speed)
        trial = CostTiming(self.required)
        for n in range(1, int(self.frames_per_cost) + 2):
            trial.advance_one_frame(speed)
            if self._width(trial.phase()) > EMPTY_MAX_FILL:
                return n - 1
        return 0

    def _anchor_result(self, k: int, speed: int = None, visible: bool = False) -> tuple:
        """起锚这一拍该记几帧（返回 mode, delta, warn）。

        为什么不能一律记 0：条在「费用周期第 1 帧」是**画不出像素**的
        （相位 1/30 -> 宽度 0，见 synthesis.py 的相位表），所以第一次读到条时，
        画面上那条「空的条槽」很可能就是本费的第 1 帧，而这一拍已经是第 k 帧了。
        用户看到的「费用条空（第 0 帧）到出现第一个像素（第 1 帧）还是不记录」
        就是这里：第 1 帧被起锚吞掉，整段录像的帧号从此差 1。

        上限不是常数：周期开头「读不出来」的帧数随回费速率变多（见
        _unreadable_head_frames），条最早能在第 (那个数 + 1) 帧被读到，上限取到
        再往后一帧（那个数 + 2）—— 够放行开战起锚，再放宽就把「半路重现」也放进来了。

        **为什么是 k，不是 k - 1**（2026-10-15 修）：k 是 _start 反推出来的相位号，
        也就是「本费第几帧」，而 _start 同时把**累加器停在第 k 帧上**。报出去的帧数
        必须和累加器一致 —— 报 k - 1 的话，面板说「已经走了 2 帧」、累加器却停在
        第 3 帧，而之后每一拍的 delta 都是**相对累加器**算的，于是整个费用周期从头
        到尾都少 1 帧。用户报的三条都是它：
            30 帧/费  「费用条空（第 0 帧）到出现第一个像素（第 1 帧）还是不记录」；
            25%(120)「第 6 帧开始识别，但是记的是第 4 帧」（模型第 5 帧起锚、报成 4）；
            25%+负费(240)「到 119 帧的时候只有 117 帧」（实测起锚报 10、累加器在第 11 帧）。
        真值就是 k：第 n 帧的画面读数 = 相位 n/一费帧数，本费已经走了 n 帧。

        为什么卡上限：条如果是在别的地方（切场景 / 关菜单 / 被遮挡）半路重新出现
        的，前面丢了多久无法证明，仍然按 0 起锚 —— 宁可少记也不猜。实测开战起锚
        落在周期第 2 / 第 3 帧（fill=6/7/8 -> k=2；fill=15 -> k=3），半路重现落在
        周期第 16 帧（fill=129 -> k=16），两者分得很开。补不上的标 anchor-late，
        面板 / CSV 里能直接看出来。
        """
        self._cycle_origin_clean = False
        blind = self._unreadable_head_frames(speed)
        first_visible = blind + 1    # 条第一次读得出来，最早落在本费第几帧
        cap = max(ANCHOR_MAX_CATCHUP, first_visible + 1)
        head = int(k)                # 含这一拍读到的那一帧，本费已经走了几帧
        if visible and head < first_visible and not self._no_catchup:
            # 走到起锚说明条是**看得见**的（TRACK 读数），而本费开头那 blind 帧画面上
            # 根本画不出像素 —— 所以反推出来的相位不可能小于 first_visible。小于只可能是
            # 「模型这一档最小可画宽度」和实测差 1~2px 造成的反推误差：60 帧/费那一档
            # 的模型宽度只有 0 / 0 / 4 / 8…，实测给出 2，反推落回第 0 帧，于是那一两帧
            # 被吞掉（用户实测「50% 从 3 帧才开始计数」）。按 first_visible 记，和 120
            # 帧/费那一档（实测 2px 正好对上第 5 帧）行为一致。
            head = first_visible
            # 报了几帧，**累加器必须跟着挪到第 head 帧**（见 _shift_to_frame）。
            self._shift_to_frame(head, speed)
        if self._no_catchup:
            # 清零之后第一次起锚：只补「清零那一刻之后」才走的帧。
            # _zero_phase = 清零时条已经画到第几帧；None = 当时没读到画面，不猜。
            self._no_catchup = False
            base_k = self._zero_phase
            self._zero_phase = None
            if base_k is None:
                return 'anchor', 0, ('anchor-late' if head > cap else '')
            head = int(k) - int(base_k)
        if 0 < head <= cap:
            self._cycle_origin_clean = True
            return 'anchor-head', head, ''
        if head > cap:
            return 'anchor', 0, 'anchor-late'
        return 'anchor', 0, ''

    def _shift_to_frame(self, frame: int, speed: int = None) -> None:
        """把累加器挪到「费用周期第 frame 帧」的相位上（起锚垫帧之后用）。

        为什么非挪不可：起锚那一拍给出去的是 head 帧（_anchor_result），而累加器是
        _resync 反推出来的（这一步可能是**小**的：60 帧/费那一档的模型宽度只有
        0 / 0 / 4 / 8…，实测给 2，反推落回第 0 帧，head 却按 blind 记成 2）。显示说
        「已经走了 2 帧」、累加器却停在 0 帧，两者差多少，下一拍 resync 就会补多少 ——
        而那几帧起锚时**已经算过了**，于是同一段时间被记两遍。

        用户 2026-10-15 实测到的就是它：50%（60 帧/费）起锚显示 2，下一拍读数一次 +4
        （本该 +1），条走完一费是 63 帧而不是 60（那个 3 = 垫的 2 帧 + 反推少 1 帧）。

        只往前走（frame 比当前相位大才挪）；挪完之后 last_phase 也要跟着刷新，
        否则「上一拍相位」和累加器对不上，跨回费边界的判据会被带偏。
        """
        speed = self._speed() if speed is None else int(speed)
        if self.timing is None:
            self.timing = CostTiming(self.required)
        while self.timing.frames_since_cycle_start < int(frame):
            if self.timing.accumulator + speed + EPSILON_RAW >= self.required:
                break                      # 不许跨回费边界：跨了就不是起锚该管的事
            self.timing.advance_one_frame(speed)
        self.last_phase = self.timing.phase()

    def _resync(self, observed: int, allow_wrap: bool, base: CostTiming = None,
                speed: int = None, transition: str = '') -> tuple:
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
        speed = self._speed() if speed is None else int(speed)
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
        if transition:
            best_timing, best_err = self._boundary_correction(best_timing, best_err,
                                                              observed, transition)
        return best_k, best_err, best_timing

    def _boundary_correction(self, timing: CostTiming, err: int, observed: int,
                             transition: str) -> tuple:
        """负费转场那一帧的**亚帧**修正：相位挪半格，帧数一帧不动。

        为什么需要：转场那一帧的速度到底算旧的还是新的，取决于游戏是在「这一拍的画面
        取到之前」还是「之后」换的速 —— 我们看到的只有一帧像素，分不出来，于是累加器
        可能比真值多 / 少**半个负费增量**（= 半个正常帧，约 4px）。它不影响已经数出来
        的帧数（k 不变），只会让**之后**每一帧的相位偏一点，越攒越对不上。

        做法（照抄前人 engine.rs 的 apply_boundary_subframe_correction）：两个方向都试
        一遍，谁让**这一拍**的相位像素误差更小就用谁；误差打平时按转场方向挑 ——
        负费 -> 正常、且读数就是 0 宽时偏向 +（真值是 1/60 相位，画出来也是 0 宽，
        取 + 才能让后面每帧差 1px 的可见宽度序列对上）。
        """
        half = int(SPEED_NEGATIVE)
        preferred = half if (transition == TRANS_EXIT_NEG and observed == 0) else None
        best_timing, best_err = timing, err
        for delta in (half, -half):
            nudged = timing.copy()
            nudged.accumulator += delta
            e = abs(self._width(nudged.phase()) - observed)
            if e < best_err or (e == best_err and preferred is not None
                                and delta == preferred):
                best_timing, best_err = nudged, e
        return best_timing, best_err

    def _max_reanchor(self, observed: int, speed: int, dt: float) -> tuple:
        """满费（MAX / MAX_FADE）之后的第一拍读数：只对相位，帧数交给挂钟。

        为什么非这样不可：满费时条被游戏钉住（相位停在画满那一帧），像素里一点位置
        信息都没有，那一段的帧数**只能**由挂钟通道数（见 _clock）。可条重新长出来的
        时候，_resync 会从「上一拍认下的相位」——也就是满费前最后一根还看得见的条
        （周期末尾）——一路推到当前读数，于是满费那几帧**又被数了一遍**。

        一费 60 帧（50% 回费）那一档的末尾恰好有 1~2 帧是画满的（合成宽度顶到条槽），
        所以条走完一费会变成 61~62 帧、120 帧那一档变成 123 帧（用户 2026-10-15 报的
        「50% 条走完一费还是 61 帧或者 62 帧」就是它）。

        做法：用 _start() 按当前读数重新起锚（和「有一段时间读不到画面」之后走同一条
        路），像素通道这一拍一帧都不出；**这一拍自己的帧数接着由挂钟给** —— 上一拍
        就是挂钟给的，通道不该在这里断一拍（断了就少 1 帧，60 多轮就是几十帧）。
        但只在相位读得出来时才给：

            宽度 > EMPTY_MAX_FILL   _start 锚得准，挂钟给几帧就记几帧
            宽度落在一费开头的死区 相位定不出来，_start 会把累加器放在第 0 帧，
                                   下一拍的 _resync 会把这一帧一次补上 —— 这里再记
                                   就重复了，delta 保持 0

        周期统计（_cycle_ref）在这里**归零**（_start 自己就会清掉）：满费把回费周期
        从中间截断了，「上一次回费 -> 这一次回费」之间混着一段相位被钉住的满费时间，
        拿它当「一费几帧」本身就是错的（试过按 elapsed 补一个边界，量出来每个周期
        稳定多 1 帧 —— 那正是用户看到的「61 帧」）。下一个完整周期照常量得准。
        """
        self._after_max = False
        self._start(observed, speed=speed)
        if observed > EMPTY_MAX_FILL and self._max_rate > 0.0:
            _mode, delta = self._clock(self._max_rate, dt)
            return 'max-reanchor', delta, ''
        return 'max-reanchor', 0, ''

    # ---------- 计数通道 ----------

    def _pixel(self, observed: int, dt: float, paused: bool, neg: bool = False) -> tuple:
        """像素通道：这一拍前进了几个逻辑帧，只由条画出来的宽度决定。

        读不准就**冻结**（delta = 0），绝不用挂钟补帧：挂钟要乘倍速，
        倍速一读错就整段几倍地错（实测真值 212 帧被算成 372 帧）。
        冻结的代价是这段时间的帧丢掉，但那本来就是「条看不见」的时间
        （切场景 / 被遮挡 / 暂停），宁可少记也不要凭空多记。
        """
        # 相位模型连续这么久都匹配不上 -> 模型已经不可信：按当前读数重新起锚，
        # 这一拍 delta = 0（丢掉的帧无法证明，宁可少记也不猜）。见 REJECT_RESYNC_SECONDS。
        speed = speed_for_cost_state(neg)
        if self._after_max:
            # 上一拍还是满费：满费那几帧是挂钟记的，这一拍接着由挂钟给（见 _max_reanchor）。
            return self._max_reanchor(observed, speed, dt)
        stale = self._reject_s > REJECT_RESYNC_SECONDS
        # 负费的转场判据必须在重新起锚**之前**取：起锚会把 last_phase 重新写上，
        # 之后就没法和「上一拍」比了（前人 engine.rs 比的也是 last_accepted_phase）。
        transition = self._transition(neg, self.last_phase is not None)
        if stale:
            self._start(observed, speed=speed)
        allow_wrap = self.last_phase is not None and self.last_phase > WRAP_MIN_PREV_PHASE
        base = self.timing
        # 上一拍是不是正好停在「画满」那一帧（= 游戏刚回费那一帧）：
        # 它的下一拍一定是「条归零」那一帧，也就是新一费的第 1 帧。
        was_full = bool(base is not None and base.cycles > 0
                        and base.frames_since_cycle_start == 0)
        k, err, timing = self._resync(observed, allow_wrap, speed=speed,
                                      transition=transition)
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
        #
        # 上限还要再加上「一费开头那几帧画不出像素」的死区（_unreadable_head_frames）：
        # 死区里读数恒为 0，相位定不出来，模型只能先停在原地，等条重新长出来的那一拍
        # **一次跨过整段死区** —— 那是合法位移，不是卡顿。死区宽度跟着回费速率变
        # （30 帧/费 1 帧、60 帧/费 2 帧、120 帧/费 **4 帧**），比 DFILL_SLACK(3) 还宽：
        # 不加这一项时，-75% 那一档开局每一拍都被判 pixel-jump，整个费用周期一帧都
        # 记不到（回归项 check_state_synth.reduced_cost_rate_cycles 的 per=120 就是它）。
        # 保护性不变：误判「绕一圈」是几十上百帧的跳，仍然远超这个上限。
        gap = dt if paused else max(dt, self._since_accept)
        bound = LOGICAL_FPS * MAX_SPEED_M * gap + self._unreadable_head_frames(speed)
        if k > bound + DFILL_SLACK + BOUND_EPS:
            self._reject_s += dt
            return 'pixel-jump', 0, 'pixel-jump'
        self.timing = timing
        self._since_accept = 0.0
        self._reject_s = 0.0
        self.last_phase = timing.phase()
        self.last_k = k
        # 负费状态**只在采纳之后**才更新（和前人 engine.rs 的 last_cost_is_negative
        # 一样）：被拒的那一拍读数本身不可信，不该让它把速度改掉。
        self._neg = neg
        # 这 k 帧里跨过回费边界了吗？两条路：
        #   上一拍就是「画满」那一帧 -> 下一拍（k>=1）必跨；
        #   更靠前的位置起步（比如 n=28）走 k 帧时累加器自己回卷 -> cycles 涨了。
        self._crossed = (was_full and k >= 1) or (timing.cycles > self._cycle_seen)
        if timing.cycles > self._cycle_seen:
            # 这一步跨过了回费边界（可能还不止一次，但正常推进时只会跨一次）。
            # 边界在这 k 帧里的位置由 frames_since_cycle_start 反推 —— 它回卷时清零，
            # 所以「边界落在哪一帧」是精确的，周期长度不会一帧一帧地漂。
            crossed = timing.cycles - self._cycle_seen
            end = self.elapsed + k - timing.frames_since_cycle_start
            period = int(round(self.frames_per_cost * (2.0 if neg else 1.0)))
            # 这一费里只要正负费混合过，周期长度就不是整数个同速周期
            # （负费每帧只攒一半），任何「钉回 30 / 60」的补偿都会把真实帧数改错。
            mixed = self._cycle_had_pos and self._cycle_had_neg
            counted = 0
            if self._cycle_ref is None:
                # 清零 / 点开始之后的**第一个**边界有两种可能：
                #   a) 从条空开始（或起锚时已把开头几帧补回来）：这就是一个完整周期；
                #   b) 从本费中间开始（录像里从第 2 帧清零）：到条空只剩 28 帧。
                # 以前 b 也被算成「满条 1 次」，面板就出现 `满条 1 ｜ -2`，看起来
                # 像第一费只有 28 帧。只有起锚确实补过头、或清零后离整周期只差
                # 几帧时才认第一费；差得远就只把它当周期基准，n 保持 0。
                # 首费补平窗口跟着周期走：30 帧仍用 ±4，60 帧 ±7，120 帧 ±15。
                # 25% 回费从本费第 11 帧启动时，首费真值 109；用户要的是
                # 「一费 120」，所以在周期 1/8 以内的起步都拨到整周期。
                first_tol = max(CYCLE_ANCHOR_TOL, period // 8)
                near_full = (period > 0
                             and abs(period - end) <= first_tol)
                if near_full and (self._cycle_origin_clean
                                  or self._snap_first_cycle):
                    if self._snap_first_cycle and not mixed:
                        diff = period - end
                        self.pixel_frames += int(diff)
                        if self.full_anchor is not None:
                            self.full_anchor += diff
                        end = period
                    counted = 1
                self._cycle_origin_clean = False
                self._snap_first_cycle = False
            elif crossed >= 1 and period > 0:
                expected = self._cycle_ref + crossed * period
                diff = expected - end
                # 除了几帧的小漂移，也要识别「整整多算 / 少算 n 个周期」：
                # 同一回费边界之间实际只有一个完整周期；如果相位模型把这段
                # 数成了 58 帧（应为 30），diff 会是 -28，旧判据 |diff|<=4
                # 会放弃校正，之后整段永久多 28 帧。
                n_periods = int(round(diff / period)) if period > 0 else 0
                residual = diff - n_periods * period
                whole_cycle = (0 < abs(n_periods) <= 2
                               and abs(residual) <= CYCLE_ANCHOR_TOL * crossed)
                small_drift = (n_periods == 0
                               and abs(diff) <= CYCLE_ANCHOR_TOL * crossed)
                if not mixed and (small_drift or whole_cycle):
                    # 把「这一周期多算/少算的几帧」从这里拨回去。像素通道的余量
                    # 不用动：diff 是整数帧，直接改累计像素帧数即可；elapsed /
                    # elapsed_f 会在 step 末尾按 pixel_frames + clock_frames 重算。
                    self.pixel_frames += int(diff)
                    if self.full_anchor is not None:
                        self.full_anchor += diff
                    end = expected
                counted = crossed
            if self._cycle_ref is not None and crossed == 1:
                self.cycle_lengths.append(end - self._cycle_ref)
            self.bar_fill_count += counted
            # 面板「满条 n ｜ +x 帧」里的 x 直接由它算：条空时 x 一定是 0，
            # 不管这一费是 30、60 还是中途切过负费的 34 帧。
            self.bar_fill_frames = end
            self._cycle_ref = end
            self._cycle_seen = timing.cycles
            self._cycle_had_neg = False
            self._cycle_had_pos = False
        return 'pixel', k, ('phase-resync' if stale else '')

    def _transition(self, neg: bool, had_phase: bool) -> str:
        """这一拍是不是「局内负费」的转场帧（进场 / 离场）。

        had_phase=False（上一拍还没认下过任何相位，比如刚起锚 / 刚重新校准）时不判 ——
        没有可比的对象，也就没有「转场」这回事。
        """
        if not had_phase:
            return ''
        if self._neg and not neg:
            return TRANS_EXIT_NEG
        if neg and not self._neg:
            return TRANS_ENTER_NEG
        return ''

    def _clock_rate(self, m_now, has_digit: bool) -> tuple:
        """这一拍按多少逻辑帧/秒推（满费挂钟通道用）。

        返回 (每秒逻辑帧, warn)；读不出速率时返回 (0.0, warn) —— 调用方自己决定
        「冻结」还是「退回旧的通道名」。

        优先级（顺序不能换，子弹时间下倍速键上是没有数字的）：
          1) 子弹时间：>>> 是**确定**的读数 —— 恒 BULLET_FPS = 6 帧/秒，与倍速设置无关
             （实测见 BULLET_FPS 上面那段）；
          2) 倍速键上画着数字、模板也认得出：30 x 数字；
          3) 数字**画着**、模板认不出（corr 低、m 读成 nan）：沿用上次的倍速。按钮确实
             画着、画的又不是子弹时间的箭头，只是这一拍字形糊了 —— 沿用比冻结少丢帧。
             实测这种读数占真实会话 2898/20436 拍（docs 第 23 节）。last_m 的初值是 1X
             （最常见的档），所以「从没读到过倍速」也不会瞎猜 2X；
          4) 数字没画、箭头也不实心，但**这一局里刚刚还读到过数字**：按子弹时间兜底。
             关卡内、运行中还能让那颗键整块空白的只有「拖干员」（见 BULLET_ASSUMED_WARN）；
          5) 从没读到过倍速数字：不知道速率，返回 0。
        """
        if self._bullet:
            return BULLET_FPS, ''
        if m_now is not None:
            return LOGICAL_FPS * m_now, ''
        if has_digit:
            return LOGICAL_FPS * self.last_m, 'speed-memory'
        if self._digit_seen:
            return BULLET_FPS, BULLET_ASSUMED_WARN
        return 0.0, 'speed-unknown'

    def _clock(self, rate: float, dt: float) -> tuple:
        """满费挂钟通道：这一拍的墙钟时间折成逻辑帧，**带小数余量**。

        原来这里有一道「连续 5 拍确认」门（CLOCK_ARM_FRAMES），理由是暂停时暂停键图标
        会闪回「运行中」形状、闪的那几拍会凭空多记。2026-10-06 删掉了，代价不对等：

          * 门限吃掉的都是**真实帧操** —— 满费 + 子弹时间（6 帧/秒）下一次操作只跑
            17~67ms（1~4 拍 @60fps），永远够不到 5 拍，于是每一下都记 0 帧。
            实测 Desktop 2026.10.06 18.32.40（全程满费）：29 段「运行中」里 20 段
            只有 1~3 拍、全部记 0；同一段录像连续暂停 4.3 秒时**一次都没闪**。
          * 那些读数还拿暂停遮罩的整屏变暗交叉验证过：暂停时战场亮度 35、运行 70，
            262 拍「运行中」全部落在亮的一侧 —— 读数是真的，不是闪烁。

        所以规则改成：读数说在跑，就按 rate * dt 记，**一拍都不扔**。
        不足 1 个逻辑帧的余量留在 _clock_frac 里跨暂停、跨通道接着攒 ——
        帧操一下只跑 0.3 帧，十下就是 3 帧，余量就是那 3 帧。
        只有 resync()（画面整段读不到、状态未知）才允许把余量丢掉。
        """
        acc = rate * dt + self._clock_frac
        delta = int(acc + CLOCK_ROUND_EPS)
        self._clock_frac = acc - delta
        return 'clock', delta
