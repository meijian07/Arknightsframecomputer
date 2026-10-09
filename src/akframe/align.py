"""自动对齐 ROI：录整个桌面 / 窗口时，游戏客户区原点在画面里的位置是未知的。

背景：三个 ROI 的坐标都是「客户区坐标」（geometry.layout_for(客户区宽高)）。
实时抓屏时客户区就是画面，原点一致；但录屏（OBS / NVIDIA 回放）里画面是整块桌面，
客户区原点取决于当时窗口被拖到哪 —— 每次录都不一样。靠人肉试偏移量既麻烦又容易
错一整段：实测同一段录像 16,45 是对的、换成 15,45 就整段读不到条，计数直接崩掉。

原理只用一条硬事实：费用「填充白块」的左边缘 **恒等于条槽左边缘**。
把视频每 K 帧抽一帧、只看条 ROI 周围一块（左右 ±40px、上下 ±120px），统计每一列
的「亮点出现频率」，正确答案上会看到一条干净的阶跃（实测一段 50 秒录像）：

    x <  条槽左边缘   频率 0.033   （游戏背景，几乎不亮）
    x >= 条槽左边缘   频率 0.284   （条槽被填充覆盖的那段时间）

阶跃最陡的那一列就是条槽左边缘 -> dx；再让 8 行判别条带压在条槽上 -> dy。

数据依据记录：
    录像 2026-10-04 15-54-49.mp4（OBS，2560x1600，客户区 2558x1440）
    实测条槽 x 2325..2563；layout_for(2558,1440).bar.x = 2318 -> dx = +7
    实测条槽 y 1127..1134；layout.bar.y = 1075，条带取 ROI 第 5..12 行 -> dy = +48
用 7,48 跑出来的读数与直接从原始画面数出来的填充宽度逐帧一致（200px vs 199.8px）。

为什么不用「找游戏窗口的边框」：窗口位置每次都可能不同，边框在暗场景里几乎不可见，
而填充白块的左边缘是整幅画面里最锐利、最稳定的水平结构。

不依赖绝对亮度：阈值取抽出来的这一块的 p10 / p99 中点，SDR（白 255）和 HDR10/PQ
（白 ~122）都能用。

代价：多跑一次 ffmpeg，只抽 ~160 帧的一小块，几百毫秒。
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import numpy as np

from .capture import probe_video
from .geometry import BAR_BAND_BOTTOM, BAR_BAND_TOP, Rect, layout_for

DX_RANGE = 40        # 左右各搜这么多像素
DY_RANGE = 120       # 上下各搜这么多像素（窗口标题栏 + 任务栏都在这个量级内）
SAMPLES = 160        # 抽多少帧做统计
MIN_CONTRAST = 25.0  # 这一块的亮暗差不到这个数就没什么可看的
MIN_STEP = 0.10      # 频率阶跃小于这个就不敢认（说明这一块根本没有费用条）
# 「亮点」阈值在 p10..p99 之间按这几个比例各试一遍，取分数最高的那一个候选。
# 为什么要试多个：费用条有两种完全不同的样子 ——
#     正常推进：填充是整幅画面里最亮的东西，阈值必须贴白电平（0.8），否则游戏场景
#               的成片中灰会凑出比真条还宽的假平台（实测背景 141 列 vs 真条 125 列）；
#     满费(MAX)：整条换成灰底，比场景亮但远不及白，0.8 根本够不着（实测灰底 97、
#               场景高光 124）—— 只有降到 0.5 才能把它认出来。
# 单阈值必然二选一错一边，所以两个都跑，谁的自信心（阶跃 × 填充频率 × 平台占比）高听谁的。
THR_FRACS = (0.8, 0.65, 0.5)
SUSTAIN_LEVEL = 0.5  # 判定「填充覆盖到了这一列」用峰值频率的几成
MIN_SUSTAIN_FRAC = 0.4   # 边缘右边连续这么多列都得被填充覆盖过（占条槽宽的比例）


def extract_strip(ffmpeg, video, rect, stride=1, frames=None, seek=None):
    """抽 rect 这一块的灰度原始流。stride > 1 时每 stride 帧取一帧。

    frames 给一个整数就只抽前 frames 帧（校准工具用：它只统计白块出现过的最左 /
    最右列，多解几十秒只是白花时间）。
    seek 给秒数就从那儿附近开抽（放在 -i 前面 = 快速定位）：只判「这一段时间里画面对不
    对」时用它，省掉从头解到那儿的时间；stride 仍按**从头算**的帧号取模，所以抽样点的
    相位和整段一致，不会跟着 seek 跑。
    """
    vf = 'format=gray,%s' % rect.crop_filter()
    if stride > 1:
        vf += ",select=not(mod(n\\,%d))" % stride
    path = tempfile.mktemp(suffix='.gray', dir=tempfile.gettempdir())
    tail = ['-vsync', '0', '-f', 'rawvideo', '-pix_fmt', 'gray', '-y', path]
    if frames:
        tail = ['-frames:v', str(int(frames))] + tail
    head = [ffmpeg, '-hide_banner', '-loglevel', 'error']
    if seek:
        head += ['-ss', '%.3f' % float(seek)]
    try:
        subprocess.run(head + ['-i', video, '-vf', vf] + tail, check=True)
        data = np.fromfile(path, dtype=np.uint8)
    finally:
        if os.path.isfile(path):
            os.remove(path)
    per = rect.pixels
    n = data.size // per
    if n <= 0:
        return np.zeros((0, rect.h, rect.w), np.uint8)
    return data[:n * per].reshape(n, rect.h, rect.w)


def offset_from_frames(frames, base, x0: int, y0: int,
                       dx_range: int = DX_RANGE, dy_range: int = DY_RANGE,
                       min_sustain_frac: float = MIN_SUSTAIN_FRAC):
    """在一块**已经抽好**的灰度原始流里找 (dx, dy)。

    frames = (n, h, w)，左上角在画面 (x0, y0)；base 是客户区坐标下的预测 layout。
    返回 (dx, dy, 阶跃强度, 填充出现频率, 平台列数)；认不出来返回 None。

    第五个数是「白块平台」量到的列数（右端上限是搜索框右边缘）。录像那条链路不用它，
    窗口那条链路要靠它定「量条槽」的探针框该有多宽 —— 播放器把画面缩放多少倍是未知量。

    把这一段从「怎么拿到帧」里拆出来，是为了让**读窗口**那条链路复用同一份判据：
    录像那边喂 ffmpeg 抽的帧，窗口那边喂屏幕抓的帧，算法一个字都不用改。

    白电平（hi）取两个口径里**高**的那一个：
        * 所有像素的 p99 —— 填充在一小块里占多数时（录像链路抽的就是条周围那一小块）
          就是它；
        * **每一列最亮值的 p99.9** —— 填充只占这一块百分之几的时候只有它认得出来：
          窗口链路要在 ±120 行的框里搜，块里绝大多数是背景，实测整窗 p99 = 0（阈值
          掉到背景上，阶跃整个消失）。为什么是「每列最亮」而不是「所有像素」：
          填充在时间上是稀疏的（条一格一格长，每一列只在部分帧里是白的），按所有像素
          取分位会被自己的稀疏性稀释掉。
    为什么还要取 99.9 而不是 99：块被搜索框切掉一部分时（条只露出一截）「条槽底色
    （灰 55）」会占掉 top 1%，于是 hi 落到 55 —— 灰轨道也被当成亮点，整块变成一个
    假的「完美阶跃」，而且**纵向位置完全不受约束**（实测：这样找出来的 dy 偏 5px，
    后面读条读到 0）。取 99.9 之后白填充（占 0.98%）重新成为最亮的那一档。

    min_sustain_frac 是「平台至少要占条槽宽的几成」。默认值照录像链路标定过的那份；
    窗口链路把预测的条宽当分母，而播放器把画面缩放多少倍是未知量（实测预测 129px、
    真条 114px），所以那边会放宽 —— 真正的判据是第二级的「条在不在动」。
    """
    if frames.ndim != 3 or frames.shape[0] < 8:
        return None
    fh = int(frames.shape[1])

    # 暗电平必须取「所有像素的 p10」：换成 colmax 之后，凡是每一列都被填过的块
    # （录像链路那种薄条块就是这样）lo 也会升到白电平，hi-lo = 0 反而把整块判成
    # 「没对比度」——那是真坏，实测过。
    lo = float(np.percentile(frames, 10.0))
    hi = max(float(np.percentile(frames, 99.0)),
             float(np.percentile(frames.max(axis=0), 99.9)))
    if hi - lo < MIN_CONTRAST:
        return None
    band_h = BAR_BAND_BOTTOM - BAR_BAND_TOP
    min_sustain = max(8, int(round(min_sustain_frac * base.bar.w)))
    span = float(base.bar.w) if base.bar.w > 0 else 1.0
    best = None
    for frac in THR_FRACS:
      bright = frames > (lo + frac * (hi - lo))
      # 每一列「被亮点覆盖过」的频率。原来是对每个 dy 重算
      # bright[:, r0:r1, :].mean(axis=(0, 1))，数学上完全等价（先按帧平均、再按行平均），
      # 但把代价从「每个 dy 一遍 O(n x band_h x w)」降成只算一次 —— 窗口那条链路要把
      # 搜索半径开到几百像素（播放器缩放过后 ROI 会偏很远），不这么改根本跑不动。
      freq = bright.mean(axis=0)                  # (h, w)：每一列的覆盖频率
      for dy in range(-dy_range, dy_range + 1):
        r0 = (base.bar.y + BAR_BAND_TOP + dy) - y0
        r1 = r0 + band_h
        if r0 < 0 or r1 > fh:
            continue
        prof = freq[r0:r1].mean(axis=0)
        if prof.size < 8:
            continue
        jump = np.diff(prof)
        j = int(np.argmax(jump))
        step = float(jump[j])
        if step < MIN_STEP:
            continue
        # 边缘右边的一小段必须确实被填充覆盖过（否则这个「阶跃」只是噪声）
        after = float(prof[j + 1:j + 1 + 20].mean())
        ref = float(prof[j + 1:].max())
        if after < 0.05 or ref < 0.05:
            continue
        # 费用条独有的形状：边缘右边有一整条「平台」（每列都被填充覆盖过一段时间）。
        # HUD 文字/图标只会给出很窄的一小簇列（实测宽度 ~30），所以这一条能把它们筛掉。
        seg = prof[j + 1:j + 1 + int(base.bar.w)] >= (SUSTAIN_LEVEL * ref)
        run = longest = 0
        for ok in seg:
            run = run + 1 if ok else 0
            if run > longest:
                longest = run
        if longest < min_sustain:
            continue
        # 平台到底铺了多宽：上面那一段只看 base.bar.w 列（判据用），这里放到整个右半边
        # 再数一遍 —— 给窗口链路一个「实测平台宽」的旁证。多跑一趟很便宜。
        wide = prof[j + 1:] >= (SUSTAIN_LEVEL * ref)
        w_run = w_wide = 0
        for ok in wide:
            w_run = w_run + 1 if ok else 0
            if w_run > w_wide:
                w_wide = w_run
        score = step * after * (longest / span)
        if best is None or score > best[0]:
            best = (score, dy, j, step, after, w_wide)
    if best is None:
        return None

    _, dy, j, step, after, platform = best
    dx = (x0 + j + 1) - base.bar.x
    return (int(dx), int(dy), float(step), float(after), int(platform))


def detect_offset(video, ffmpeg, client_size=None, ui_scaler=1.0, size=None,
                  samples=SAMPLES, dx_range=DX_RANGE, dy_range=DY_RANGE,
                  strip_fn=None, info=None):
    """找一个 (dx, dy)，让 layout_for(客户区) 的三个 ROI 落到画面里正确的位置。

    返回 (dx, dy, 阶跃强度, 填充出现频率, 平台列数)，认不出来时返回 None。

    strip_fn(rect, stride, frames) -> (n, h, w) 灰度栈：换帧来源用（录像 = ffmpeg 抽，
    窗口 = 屏幕抓）。info 传 probe_video 的结果可以省一次探测 —— 窗口没有文件可探，
    这时 stride / frames 按「抓 samples 张连续画面」解释。
    """
    given_info = info is not None
    if info is None:
        info = probe_video(ffmpeg, video)
    if size is None:
        size = info.size
    if not size:
        return None
    vw, vh = int(size[0]), int(size[1])
    cw, ch = (int(client_size[0]), int(client_size[1])) if client_size else (vw, vh)
    base = layout_for(cw, ch, ui_scaler)

    # 抽一块把整个搜索范围都包进去的小窗
    x0 = max(0, base.bar.x - dx_range)
    y0 = max(0, base.bar.y - dy_range)
    x1 = min(vw, base.bar.x + base.bar.w + dx_range)
    y1 = min(vh, base.bar.y + base.bar.h + dy_range)
    if x1 - x0 < 8 or y1 - y0 < 16:
        return None
    rect = Rect(x0, y0, x1 - x0, y1 - y0)

    strip = strip_fn or (lambda r, st, n: extract_strip(ffmpeg, video, r, st, n))
    if given_info:
        frames = strip(rect, 1, samples)          # 没有「整段时长」：抓 samples 张连着看
    else:
        total = int(round((info.duration or 0.0) * (info.fps or 60.0)))
        stride = max(1, total // samples) if total > 0 else 1
        frames = strip(rect, stride, None)
    return offset_from_frames(frames, base, x0, y0, dx_range, dy_range)
