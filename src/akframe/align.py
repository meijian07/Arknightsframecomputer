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


def extract_strip(ffmpeg, video, rect, stride=1, frames=None):
    """抽 rect 这一块的灰度原始流。stride > 1 时每 stride 帧取一帧。

    frames 给一个整数就只抽前 frames 帧（校准工具用：它只统计白块出现过的最左 /
    最右列，多解几十秒只是白花时间）。
    """
    vf = 'format=gray,%s' % rect.crop_filter()
    if stride > 1:
        vf += ",select=not(mod(n\\,%d))" % stride
    path = tempfile.mktemp(suffix='.gray', dir=tempfile.gettempdir())
    tail = ['-vsync', '0', '-f', 'rawvideo', '-pix_fmt', 'gray', '-y', path]
    if frames:
        tail = ['-frames:v', str(int(frames))] + tail
    try:
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', video,
                        '-vf', vf] + tail, check=True)
        data = np.fromfile(path, dtype=np.uint8)
    finally:
        if os.path.isfile(path):
            os.remove(path)
    per = rect.pixels
    n = data.size // per
    if n <= 0:
        return np.zeros((0, rect.h, rect.w), np.uint8)
    return data[:n * per].reshape(n, rect.h, rect.w)


def detect_offset(video, ffmpeg, client_size=None, ui_scaler=1.0, size=None,
                  samples=SAMPLES, dx_range=DX_RANGE, dy_range=DY_RANGE):
    """找一个 (dx, dy)，让 layout_for(客户区) 的三个 ROI 落到画面里正确的位置。

    返回 (dx, dy, 阶跃强度, 填充出现频率)，认不出来时返回 None。
    """
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

    total = int(round((info.duration or 0.0) * (info.fps or 60.0)))
    stride = max(1, total // samples) if total > 0 else 1
    frames = extract_strip(ffmpeg, video, rect, stride)
    if frames.shape[0] < 8:
        return None

    # 阈值必须贴着这一块的白电平取（0.8），不能取中点：取中点会把游戏场景里
    # 一片一片的中灰也当成「亮」，于是背景也能凑出很宽的「平台」，判据就废了。
    # 实测（2026-10-04 15-54-49.mp4）：阈值取中点时场景行 y 990..1022 的假平台
    # 宽 141 列、比真费用条的 125 列还宽；取 0.8 之后场景行的阶跃只有 0.04，
    # 真条仍然是 0.28 —— 两者彻底分开。
    lo = float(np.percentile(frames, 10.0))
    hi = float(np.percentile(frames, 99.0))
    if hi - lo < MIN_CONTRAST:
        return None
    band_h = BAR_BAND_BOTTOM - BAR_BAND_TOP
    min_sustain = max(8, int(round(MIN_SUSTAIN_FRAC * base.bar.w)))
    span = float(base.bar.w) if base.bar.w > 0 else 1.0
    best = None
    for frac in THR_FRACS:
      bright = frames > (lo + frac * (hi - lo))
      for dy in range(-dy_range, dy_range + 1):
        r0 = (base.bar.y + BAR_BAND_TOP + dy) - y0
        r1 = r0 + band_h
        if r0 < 0 or r1 > rect.h:
            continue
        prof = bright[:, r0:r1, :].mean(axis=(0, 1))
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
        score = step * after * (longest / span)
        if best is None or score > best[0]:
            best = (score, dy, j, step, after, frac)
    if best is None:
        return None

    _, dy, j, step, after, offset_frac = best
    dx = (x0 + j + 1) - base.bar.x
    return (int(dx), int(dy), float(step), float(after))
