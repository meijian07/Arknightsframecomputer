#!/usr/bin/env python
"""录像链路自检：打开一段录像之后，播放 / 暂停 / 拖进度条 / 挪 ROI 对不对。

为什么单独一个检查：录像输入（第 3 条需求「多样接口」）和实时链路共用**同一个**状态机，
但「帧从哪来」是另一套逻辑 —— 每拍算出「现在该播到第几帧」、往回拖要把 0..k 重喂一遍、
换了 ROI 要把特征缓存丢掉从头重喂。这些都不依赖真实像素，所以这里用一个「帧是算出来的」
假录像源把整条链路（真 VideoMonitor + 真 Counter + 真布局换算 + 真线程锁）跑一遍，
不碰 ffmpeg、不碰文件，跑完只要一两秒。

检查什么：
    1. 一费就是 30 帧（像素通道，和播放快慢无关）；
    2. 播放倍速只决定**看得多快**：4X 跑 0.5 秒和 0.5X 跑 4 秒停在同一帧，帧号一模一样；
    3. 播放暂停时位置和帧号都不动；「全速」档一拍跑完整段；
    4. 进度条往回拖 = 从 0 重喂一遍，帧号可复现（拖回同一帧拿到的数和第一次一样）；
    5. 换 ROI：特征缓存丢掉、位置回到 0、**帧号也归零**
       （不归零的话重喂一遍会把帧号叠成双倍）；
    6. 手动把 ROI 挪出抽流时外扩的框 -> 夹回框边并给出提示，而不是读到框外去；
    7. 开录像时 CSV 真的在写（表头 + 行数）；
    8. 一边喂帧一边拖进度条（两个线程）不炸，位置始终合法。

用法：python tools/check_video.py
"""

import atexit
import os
import shutil
import sys
import tempfile
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.demo import DEMO_SIZE                                     # noqa: E402
from akframe.features import BarReading, PauseReading, SpeedReading    # noqa: E402
from akframe.geometry import UI_SCALER_DEFAULT, apply_deltas, layout_for  # noqa: E402
from akframe.state import LOGICAL_FPS                                  # noqa: E402
from akframe.synthesis import synthesized_width                        # noqa: E402
from akframe.video import VideoMonitor, VideoSource, pad_layout        # noqa: E402

FAIL = []
RATE = 120.0


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


class FakeClock:
    """假挂钟：推一下走一步。播放推进全从它来，所以几秒钟的活瞬间跑完。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def step(self, dt):
        self.t += dt


class StubSource:
    """假录像源：接口和 VideoSource 一样，但一帧文件都不读。

    adopt_layout **直接调真的** VideoSource.adopt_layout —— 那一段（把面板挪过的 ROI
    夹进抽流时外扩的框）正是要验的东西之一，所以这里只把它依赖的字段摆好就行。
    """

    def __init__(self, size=DEMO_SIZE, frames=1200, fps=60.0):
        lay = layout_for(int(size[0]), int(size[1]), UI_SCALER_DEFAULT)
        self.path = os.path.join('out', 'cache', 'check_video_fake.mp4')
        self.size = (int(size[0]), int(size[1]))
        self.fps = float(fps)
        self.frames = int(frames)
        self.ui_scaler = UI_SCALER_DEFAULT
        self.client = self.size
        self.offset = (0, 0)
        self.layout = lay
        self.auto_layout = lay          # 「刚校准出来」那份：方向键的 delta 加在它上面
        self.buttons_ok = True
        self.notes = ['（假录像：帧是算出来的，不读文件）']
        self.padded, self.pad_off = pad_layout(lay)

    def __len__(self):
        return self.frames

    def frame(self, i):
        raise AssertionError('假录像不该被读像素：这一路的帧是算出来的')

    def adopt_layout(self, layout):
        return VideoSource.adopt_layout(self, layout)

    def save_current(self, layout=None, source='', note=''):
        return os.path.join('out', 'check_video_calib.json')

    def close(self):
        pass

    def describe(self):
        return ('假录像 %dx%d %.1f fps %d 帧'
                % (self.size[0], self.size[1], self.fps, self.frames))


class StubMonitor(VideoMonitor):
    """把「读一帧像素」换成「按帧号直接算一份读数」。

    测的是**录像链路自己**（播放推进 / 拖进度条 / 倒带 / 换 ROI）。特征提取那条路
    （像素 -> 读数）不在这里验：那由 tools/check_state_synth.py 和真录像的端到端负责。
    这里约定「录像第 i 帧 = 本费第 i+1 帧」，于是一帧推一个逻辑帧，断言才有确定的数。
    """

    def _readings(self, i):
        hit = self._read_cache.get(i)
        if hit is not None:
            return (None, None, None) + hit
        lay = self.src.layout
        per = float(self.counter.frames_per_cost if self.counter else LOGICAL_FPS)
        phase = ((i % per) + 1) / per
        fill = synthesized_width(lay.model_bar_w, lay.bar_width_f, phase)
        bar = BarReading(fill=fill, grey146=0, grey88=0,
                         state='TRACK' if fill > 0 else 'EMPTY',
                         row=np.zeros(0, dtype=np.int16))
        pause = PauseReading(ratio=0.241, running=True)
        speed = SpeedReading(digit_ratio=0.30, corr_2x=1.0, has_digit=True, m=1.0,
                             bullet=False, conf='ok')
        out = (bar, pause, speed)
        self._read_cache[i] = out
        return (None, None, None) + out


def make(fake_clock=True, frames=1200, **kw):
    clock = FakeClock() if fake_clock else time.perf_counter
    src = StubSource(frames=frames)
    mon = StubMonitor(src, rate=RATE, clock=clock, **kw)
    if fake_clock:
        mon._t0 = clock()               # 面板的 t 基准跟着假钟，别混两种时钟
    return src, mon, clock


def advance(mon, clock, seconds):
    """按假钟推 seconds 秒（每拍 1/RATE），返回这一段走掉的帧数。"""
    dt = 1.0 / RATE
    before = mon.position
    for _ in range(int(round(seconds * RATE))):
        clock.step(dt)
        mon._sample()
    return mon.position - before


def run_to(mon, clock, frame):
    """放开播跑到第 frame 帧（在当前位置之后就用假钟推，之前就先 seek 回去）。"""
    if frame < mon.position:
        mon.seek(frame)
        return mon.position
    while mon.position < frame:
        clock.step(1.0 / RATE)
        mon._sample()
    return mon.position


# ---- 1. 一费 30 帧，10 秒 60fps 的录像走 600 帧 ----
src, mon, clock = make()
mon.play()
advance(mon, clock, 10.0)
n_cyc, med, lo, hi = mon.counter.cycle_stats()
check('一费 30 帧（实测周期中位）', n_cyc >= 10 and med == 30,
      '周期 %d 个，中位 %g，范围 %g..%g' % (n_cyc, med, lo, hi))
check('10 秒 60fps 的录像喂到第 600 帧（0 起）', mon.position == 599,
      '实得 %d' % mon.position)
check('像素通道数出来 599 帧（起锚那一帧是「本费第 1 帧」，不额外记）',
      mon.counter.elapsed == 599, '实得 %d' % mon.counter.elapsed)
e0 = mon.counter.elapsed
advance(mon, clock, 1.0)
check('再跑 1 秒不多不少 +60 帧', mon.counter.elapsed - e0 == 60,
      '实得 %+d' % (mon.counter.elapsed - e0))

# ---- 2. 播放倍速只决定「看得多快」 ----
_, fast, fc = make()
fast.play()
fast.set_play_speed(4.0)
advance(fast, fc, 0.5)
_, slow, sc = make()
slow.play()
slow.set_play_speed(0.5)
advance(slow, sc, 4.0)
check('4X 跑 0.5 秒 和 0.5X 跑 4 秒停在同一帧',
      fast.position == slow.position, '4X 停在 %d，0.5X 停在 %d'
      % (fast.position, slow.position))
check('播放倍速不进帧号：两种快慢停在同一帧时帧号相同',
      fast.counter.elapsed == slow.counter.elapsed,
      '%d vs %d' % (fast.counter.elapsed, slow.counter.elapsed))

# ---- 3. 播放暂停：位置和帧号都不动；全速档一拍跑完 ----
_, mon, clock = make()
mon.play()
advance(mon, clock, 2.0)
mon.pause()
p1, e1 = mon.position, mon.counter.elapsed
advance(mon, clock, 3.0)
check('播放暂停时位置不动', mon.position == p1, '%d -> %d' % (p1, mon.position))
check('播放暂停时帧号不动', mon.counter.elapsed == e1,
      '%d -> %d' % (e1, mon.counter.elapsed))

# 播放暂停时清零：计数器归零了，但面板大数字读的是**缓存读数**（latest.result.elapsed），
# 而暂停这一拍根本不刷新它 —— 数字就一直挂着清零前的值，用户报的「暂停期间清零没用」
# 就是这里。判据要两条一起看：计数器归零 + 缓存读数也归零。
mon.reset()
check('暂停时清零：计数器归零', mon.counter.elapsed == 0, str(mon.counter.elapsed))
check('暂停时清零：缓存读数也跟着归零（不然面板还挂着旧数字）',
      mon.latest.result is not None and mon.latest.result.elapsed == 0
      and mon.latest.result.delta == 0,
      'latest.elapsed = %s' % (None if mon.latest.result is None
                               else mon.latest.result.elapsed))
mon._sample()
check('暂停期间再采一拍：数字还是 0（没被采样覆盖回去）',
      mon.latest.result.elapsed == 0, str(mon.latest.result.elapsed))

mon.set_play_speed(0.0)                # 全速
mon.play()
clock.step(1.0 / RATE)
mon._sample()
check('「全速」档一拍喂到结尾', mon.position == len(mon.src) - 1,
      '实得 %d / %d' % (mon.position, len(mon.src) - 1))
check('喂到结尾后自己停住（不用手点暂停）', not mon.playing)

# ---- 4. 拖进度条：往回拖 = 从 0 重喂一遍，帧号可复现 ----
_, mon, clock = make()
mon.play()
run_to(mon, clock, 600)
e600 = mon.counter.elapsed
mon.seek(300)
check('往回拖到第 300 帧：位置跟过去了', mon.position == 300, '实得 %d' % mon.position)
e300 = mon.counter.elapsed
_, fresh, fclock = make()
fresh.seek(300)
check('拖回去拿到的帧号 = 从头跑到那一帧的帧号', e300 == fresh.counter.elapsed,
      '拖回去 %d，从头跑 %d' % (e300, fresh.counter.elapsed))
mon.seek(600)
check('再拖回第 600 帧：帧号和第一次一样（可复现）', mon.counter.elapsed == e600,
      '%d vs %d' % (mon.counter.elapsed, e600))
mon.seek(0)
check('拖回第 0 帧：位置归零', mon.position == 0, '实得 %d' % mon.position)

# ---- 5. 换 ROI：丢缓存 + 倒带 + 帧号归零（不归零就会翻倍） ----
_, mon, clock = make()
mon.play()
run_to(mon, clock, 600)
base = mon.base_layout()
moved = apply_deltas(base, {'bar': [1, 0, 0, 0], 'pause': [1, 0, 0, 0],
                            'speed': [1, 0, 0, 0]}, None)
mon.set_layout(moved)
check('换 ROI 之后位置回到 0（要重喂一遍）', mon.position == 0, '实得 %d' % mon.position)
check('换 ROI 之后特征缓存清空了', not mon._read_cache,
      '还剩 %d 条' % len(mon._read_cache))
check('换 ROI 之后帧号归零（重喂不会叠成双倍）', mon.counter.elapsed == 0,
      '实得 %d' % mon.counter.elapsed)
check('换 ROI 之后读的就是新框', mon.layout.bar.x == base.bar.x + 1
      and mon.src.layout.bar.x == base.bar.x + 1,
      'monitor %d / src %d（原来 %d）' % (mon.layout.bar.x, mon.src.layout.bar.x,
                                        base.bar.x))
mon.play()
advance(mon, clock, 5.0)
e_now = mon.counter.elapsed
_, ref, rclock = make()
ref.play()
advance(ref, rclock, 5.0)
check('换 ROI 之后跑 5 秒，帧号和没换过一样', e_now == ref.counter.elapsed,
      '%d vs %d' % (e_now, ref.counter.elapsed))

# ---- 5b. 长录像往回拖：一拍最多喂 MAX_BATCH 帧，喂不完要接着喂 ----
_, mon, clock = make(frames=6000)
mon.play()
run_to(mon, clock, 5500)
mon.seek(4500)
check('长录像往回拖 1000 帧也能拖到位（一拍的上限只有 2000）',
      mon.position == 4500, '实得 %d' % mon.position)
mon.seek(300)
check('往回拖 5000 多帧（要喂好几轮）也拖到位', mon.position == 300,
      '实得 %d' % mon.position)
mon.seek(5999)
check('往前拖到最后一帧也拖到位', mon.position == 5999, '实得 %d' % mon.position)

# ---- 6. 挪出抽流框：夹回框边 + 提示 ----
_, mon, _ = make()
base = mon.base_layout()
# 条 ROI 的外扩框只有 +-PAD_BAR(16) 像素，往下挪 40px 就出框了
far = apply_deltas(base, {'bar': [0, 40, 0, 0], 'pause': [0, 0, 0, 0],
                          'speed': [0, 0, 0, 0]}, None)
mon.set_layout(far)
inside = []
for nm in ('bar', 'pause', 'speed'):
    r = getattr(mon.layout, nm)
    pad = getattr(mon.src.padded, nm)
    inside.append(pad.x <= r.x and r.x2 <= pad.x2 and pad.y <= r.y and r.y2 <= pad.y2)
check('挪出框外被夹在抽流框里（不会读到框外）', all(inside),
      '条 ROI y=%d..%d，可读范围 y=%d..%d'
      % (mon.layout.bar.y, mon.layout.bar.y2,
         mon.src.padded.bar.y, mon.src.padded.bar.y2))
check('夹住之后给了一句提示（告诉用户去按「重解录像」）',
      '重解录像' in mon._roi_warn, mon._roi_warn or '（没有提示）')

# ---- 7. CSV：开录像时真的在写 ----
_csv_dir = tempfile.mkdtemp(prefix='akframe_chk_')
atexit.register(shutil.rmtree, _csv_dir, ignore_errors=True)   # 跑完自己收尾
tmp = os.path.join(_csv_dir, 'video.csv')
_, mon, _ = make(csv_path=tmp)
mon._sync_csv()
mon._feed_to(5)
mon._close_csv()
lines = [l for l in open(tmp, encoding='utf-8').read().splitlines() if l.strip()]
check('录像模式 CSV：写了表头 + 6 行（喂的是第 0..5 帧）', len(lines) == 7,
      '实得 %d 行；表头 %s' % (len(lines), lines[0][:40] if lines else '—'))
check('CSV 表头是那个共享的 CSV_HEADER（不是录像专用的另一套）',
      bool(lines) and lines[0].split(',')[0] == 'idx',
      lines[0][:40] if lines else '—')

# ---- 8. 一边喂帧一边拖进度条（两个线程） ----
_, mon, _ = make(fake_clock=False)
mon.play()
boom = []


def feed():
    try:
        for _ in range(4000):
            mon._sample()
    except BaseException as exc:              # noqa: BLE001
        boom.append('%s: %s' % (type(exc).__name__, exc))


th = threading.Thread(target=feed, daemon=True)
th.start()
for target in (600, 100, 900, 0, 300):
    mon.seek(target)
    time.sleep(0.01)
mon.pause()
th.join(timeout=5.0)
check('一边喂帧一边拖进度条：没有异常', not boom, '；'.join(boom[:2]))
check('拖完之后位置仍然合法（0..末帧）',
      0 <= mon.position <= len(mon.src) - 1,
      'position=%d，共 %d 帧' % (mon.position, len(mon.src)))

print()
if FAIL:
    print('%d 项没过：%s' % (len(FAIL), '；'.join(FAIL)))
    sys.exit(1)
print('全部通过。')