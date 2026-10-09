#!/usr/bin/env python
"""读窗口链路自检：「打开一个录屏置顶 -> 校准 -> 开始计数」这条路，用假屏幕跑一遍。

为什么要单独一个检查：读窗口（第 3 条需求「多样接口」的第二个口子）和读录像文件是**两套
帧来源**，校准表也另开一份（window_calib.json，键 = 进程名 + 客户区尺寸）。里面烧着几条
容易被改坏的假设：

    1. 用户框出来的那条费用条 -> 能反推出「画面里的游戏客户区」落在窗口的哪个位置。
       这是后面所有 ROI 的基准；
    2. 窗口被拉大拉小时按比例缩放（应急路径），ROI 不许越界；
    3. 校准表按 (进程名, 客户区尺寸) 分开存 —— 同一个尺寸下游戏窗口和播放器窗口必须
       互不干扰（它们的三条 ROI 差着几百像素）；
    4. 自动校准（不框选）在窗口里也要能用：播放器的音量条 / 进度条同样是「一条亮线」，
       只有「一个字都不动」能把它筛掉；
    5. 最后一件事：**一费就是 30 帧** —— 假窗口按 30 逻辑帧/秒长条，读窗口链路数出来
       必须也是 30（这是整个项目的根）。

怎么做到不碰真屏幕也不碰真窗口：WindowSource 抓屏只经过 self._grabber.grab_bgra，
窗口位置只经过 win32.client_rect。于是这里把这两个都换成假的：
    window.ScreenGrabber / live.ScreenGrabber -> 从一张「虚拟画面」里切块的假抓屏器
    window.client_rect / live.client_rect     -> 恒返回虚拟客户区矩形
帧内容是合成的（synthesis.synthesized_width 那条相位->宽度曲线），所以真值精确已知。

用法：python tools/check_window.py

（夹具 FakeScreen / install / scene_layout / fake_window 是模块级的，别的脚本 import
进来可以复用来做定点调试；断言都在 main() 里，import 不会顺手跑一遍。）
"""

import atexit
import os
import shutil
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import akframe.live as live                                          # noqa: E402
import akframe.window as W                                           # noqa: E402
from akframe.demo import TRACK_GREY                                  # noqa: E402
from akframe.geometry import (SPEED_DIGIT_BAND, SPEED_DIGIT_COLS,   # noqa: E402
                              UI_SCALER_DEFAULT, Rect, layout_for, reframe_layout)
from akframe.state import LOGICAL_FPS                                # noqa: E402
from akframe.synthesis import synthesized_width                      # noqa: E402
from akframe.win32 import WindowInfo                                 # noqa: E402
from akframe.window import (WindowMonitor, WindowSource,             # noqa: E402
                            build_window_layout, layout_for_window, layout_from_bar_box,
                            load_window_profiles, rescaled_layout, save_window_profile,
                            window_key)

FAIL = []
EXE = 'vlc.exe'

# 实测场景：VLC 播放 2560x1600 的录屏、自己的客户区 1378x844 时，画面里的游戏客户区
# 落在窗口 (85,50) 的 1218x688（见 docs 与 window.py 顶部）。这里按它造一张假画面。
WIN_SIZE = (1378, 844)
CLIENT = (1218, 688)
ORIGIN = (85, 50)


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def tmp_profile_path():
    d = tempfile.mkdtemp(prefix='akframe_win_')
    atexit.register(shutil.rmtree, d, ignore_errors=True)   # 跑完自己收尾
    return os.path.join(d, 'window_calib.json')


def fake_window(size=WIN_SIZE, exe=EXE, pid=4242):
    """假窗口：hwnd 是编的（client_rect 被换掉了，不会真去问系统）。"""
    return WindowInfo(0x1234, pid, exe, 'fake', (0, 0, size[0], size[1]), False)


def phase_of(n, per=LOGICAL_FPS):
    """第 n 个逻辑帧画在什么相位上（和 tools/check_state_synth.py 同一约定）。"""
    return (((n - 1) % per) + 1) / float(per)


class FakeScreen:
    """一张虚拟的窗口客户区画面：按**挂钟**推进逻辑帧，画条 / 暂停键 / 倍速键。

    和 tools/fake_screen.py 的区别：那个把真录像的 ROI 缩放后贴出来，这个的像素是按
    相位->宽度曲线现画的 —— 真值精确已知，不需要另算一条参考轴。
    """

    def __init__(self, size, layout, clock=time.perf_counter, fps=LOGICAL_FPS):
        self.size = (int(size[0]), int(size[1]))
        self.layout = layout            # 渲染用的那份（窗口客户区坐标）
        self._clock = clock
        self.fps = float(fps)
        self.t0 = None
        self.painted = 0                # 画了多少张（证明真的在取新帧）
        self.last_idx = 0

    def idx(self) -> int:
        if self.t0 is None:
            self.t0 = self._clock()
        self.last_idx = int((self._clock() - self.t0) * self.fps) + 1
        return self.last_idx

    def fill(self) -> int:
        lay = self.layout
        return synthesized_width(lay.model_bar_w, lay.bar_width_f, phase_of(self.idx()))

    def canvas(self) -> np.ndarray:
        lay = self.layout
        img = np.zeros((self.size[1], self.size[0]), np.uint8)
        bar = np.full((lay.bar.h, lay.bar.w), TRACK_GREY, np.uint8)
        f = self.fill()
        if f > 0:
            bar[5:13, :min(f, lay.bar.w)] = 255
        img[lay.bar.y:lay.bar.y + lay.bar.h, lay.bar.x:lay.bar.x + lay.bar.w] = bar
        # 暂停键：双竖条 = 运行中（和 demo._pause_image 同一个画法）
        w, h = lay.pause.w, lay.pause.h
        pause = np.zeros((h, w), np.uint8)
        pause[int(h * 0.22):int(h * 0.78), int(w * 0.34):int(w * 0.46)] = 255
        pause[int(h * 0.22):int(h * 0.78), int(w * 0.54):int(w * 0.66)] = 255
        img[lay.pause.y:lay.pause.y + h, lay.pause.x:lay.pause.x + w] = pause
        # 倍速键：数字位置上两段笔画（read_speed 靠占空比判「画着数字」）
        w, h = lay.speed.w, lay.speed.h
        spd = np.zeros((h, w), np.uint8)
        r0, r1 = lay.speed.band_rows(*SPEED_DIGIT_BAND)
        c0, c1 = lay.speed.cols_range(*SPEED_DIGIT_COLS)
        seg = max(1, c1 - c0)
        y0, y1 = r0 + (r1 - r0) // 4, max(r0 + 1, r1 - (r1 - r0) // 4)
        spd[y0:y1, c0 + seg * 5 // 100:c0 + seg * 35 // 100] = 255
        spd[y0:y1, c0 + seg * 60 // 100:c0 + seg * 90 // 100] = 255
        img[lay.speed.y:lay.speed.y + h, lay.speed.x:lay.speed.x + w] = spd
        self.painted += 1
        return img

    def bgra(self, x, y, w, h) -> np.ndarray:
        """假 BitBlt：从虚拟画面里切一块，按 BGRA 返回（to_gray 出来还是原值）。"""
        img = self.canvas()
        out = np.zeros((int(h), int(w), 4), np.uint8)
        xs, ys = max(0, int(x)), max(0, int(y))
        xe = min(self.size[0], xs + int(w))
        ye = min(self.size[1], ys + int(h))
        if xe > xs and ye > ys:
            patch = img[ys:ye, xs:xe]
            for ch in (0, 1, 2):
                out[:patch.shape[0], :patch.shape[1], ch] = patch
        return out


class FakeGrabber:
    def __init__(self, screen):
        self.screen = screen

    def grab_bgra(self, x, y, w, h):
        return self.screen.bgra(x, y, w, h)

    def close(self):
        pass


def install(screen, size=WIN_SIZE):
    """把 window / live 两个模块里的「抓屏」和「窗口在哪」都换成假的。"""
    W.ScreenGrabber = lambda *a, **k: FakeGrabber(screen)
    live.ScreenGrabber = lambda *a, **k: FakeGrabber(screen)
    rect = (0, 0, int(size[0]), int(size[1]))
    W.client_rect = lambda hwnd: rect
    live.client_rect = lambda hwnd: rect


def scene_layout():
    """场景真值：游戏客户区 1218x688 摆在窗口 (85,50) 上，画出来的那三条 ROI。"""
    return reframe_layout(layout_for(CLIENT[0], CLIENT[1], UI_SCALER_DEFAULT),
                          ORIGIN[0], ORIGIN[1], WIN_SIZE[0], WIN_SIZE[1])


def bar_box(truth):
    return Rect(truth.bar.x, truth.bar.y, truth.bar.w, truth.bar.h)


def run_monitor(screen, lay, seconds, profiles_path=None):
    """按真采样循环数 `seconds` 秒（假屏幕按挂钟走同一段时间）。"""
    install(screen)
    src = WindowSource(fake_window(), profile_path=profiles_path, use_profile=True,
                       snap=False, templates=())
    if profiles_path:                      # snap=False：不重量条槽，ROI 直接用表里那份
        save_window_profile(window_key(EXE, WIN_SIZE), WIN_SIZE, ORIGIN, 1.0,
                            source='检查', note='', layout=lay, path=profiles_path)
        src = WindowSource(fake_window(), profile_path=profiles_path, use_profile=True,
                           snap=False, templates=())
    mon = WindowMonitor(fake_window(), source=src, layout=lay,
                        profiles_path=profiles_path, templates=(), rate=200.0)
    t0 = time.perf_counter()
    screen.t0 = t0
    mon._t0 = t0
    while time.perf_counter() - t0 < seconds:
        mon._sample()
    return mon, src


def main() -> int:
    truth = scene_layout()
    box = bar_box(truth)

    # ------------------------------------------------------------ 1. 框选反推客户区
    client, offset, lay = layout_from_bar_box(WIN_SIZE, box)
    check('框选费用条 -> 反推出画面里的客户区尺寸',
          abs(client[0] - CLIENT[0]) <= 4 and abs(client[1] - CLIENT[1]) <= 8,
          '实得 %dx%d（真值 %dx%d）' % (client[0], client[1], CLIENT[0], CLIENT[1]))
    check('框选费用条 -> 反推出客户区原点（框的整数化会带来几个像素）',
          abs(offset[0] - ORIGIN[0]) <= 5 and abs(offset[1] - ORIGIN[1]) <= 5,
          '实得 (%d,%d)（真值 (%d,%d)）' % (offset[0], offset[1], ORIGIN[0], ORIGIN[1]))
    check('费用条 ROI 就是框本身（1px 都不差）',
          (lay.bar.x, lay.bar.w) == (box.x, box.w),
          'x=%d 宽 %d（框 x=%d 宽 %d）' % (lay.bar.x, lay.bar.w, box.x, box.w))
    d_pause = max(abs(lay.pause.x - truth.pause.x), abs(lay.pause.y - truth.pause.y))
    d_speed = max(abs(lay.speed.x - truth.speed.x), abs(lay.speed.y - truth.speed.y))
    check('右上角两个按钮的预测位置够准（粗搜半径只有 40px）',
          d_pause <= 8 and d_speed <= 8, '暂停差 %dpx、倍速差 %dpx' % (d_pause, d_speed))
    check('按钮尺寸也跟着客户区正确缩放（不是拍脑袋来的）',
          abs(lay.bar_width_f - truth.bar_width_f) / truth.bar_width_f < 0.02 and
          abs(lay.pause.w - truth.pause.w) <= 2,
          '条宽 %.1f（真值 %.1f）｜按钮宽 %d（真值 %d）'
          % (lay.bar_width_f, truth.bar_width_f, lay.pause.w, truth.pause.w))
    try:
        layout_from_bar_box(WIN_SIZE, Rect(WIN_SIZE[0] - 40, 100, 100, 17))
        check('框跑到窗口外面 -> 报错而不是算出一份假布局', False, '没有报错')
    except ValueError as exc:
        check('框跑到窗口外面 -> 报错而不是算出一份假布局', True, str(exc)[:36])

    # ------------------------------------------------------------ 2. 窗口改大小（应急）
    base720 = layout_for(1280, 720, UI_SCALER_DEFAULT)
    big = rescaled_layout(base720, 1920, 1080)
    check('窗口被拉大 1.5 倍 -> 三个 ROI 按比例缩放到 1.5 倍',
          abs(big.bar.x - round(base720.bar.x * 1.5)) <= 1 and
          abs(big.pause.y - round(base720.pause.y * 1.5)) <= 1,
          'bar.x %d -> %d（按比例应为 %d）'
          % (base720.bar.x, big.bar.x, round(base720.bar.x * 1.5)))
    check('按比例缩放之后条宽也跟着变（不然「一费 30 帧」的换算就错了）',
          abs(big.bar_width_f - base720.bar_width_f * 1.5) <= 1.0,
          '条宽 %.1f -> %.1f' % (base720.bar_width_f, big.bar_width_f))
    tiny = rescaled_layout(base720, 400, 300)
    inside = all(getattr(tiny, n).x >= 0 and getattr(tiny, n).y >= 0 and
                 getattr(tiny, n).x2 < 400 and getattr(tiny, n).y2 < 300
                 for n in ('bar', 'pause', 'speed'))
    check('缩到很小也不越界（ROI 越界抓屏会直接抛异常）', inside, '400x300 三条 ROI 都在框内')

    # ------------------------------------------------------------ 3. 校准表
    prof = tmp_profile_path()
    check('校准表的键：尺寸给 (w,h) 还是给 (x,y,w,h) 都是同一个键',
          window_key(EXE, (1378, 844)) == window_key(EXE, (0, 0, 1378, 844)),
          window_key(EXE, (0, 0, 1378, 844)))
    check('校准表的键：大小写不影响（同一个进程只有一个键）',
          window_key('VLC.EXE', (1378, 844)) == window_key('vlc.exe', (1378, 844)),
          window_key('VLC.EXE', (1378, 844)))
    save_window_profile(window_key(EXE, WIN_SIZE), WIN_SIZE, ORIGIN, 1.0, source='检查',
                        note='', layout=truth, path=prof)
    got = layout_for_window(EXE, WIN_SIZE, prof)
    check('存进 window_calib.json 的布局能原样读回来',
          got is not None and got.to_dict() == truth.to_dict(), '拿到同一份')
    check('尺寸对不上就不套用（换了窗口大小必须重新校准）',
          layout_for_window(EXE, (1378, 900), prof) is None, '返回 None')
    check('换个进程名就不套用（游戏窗口和播放器窗口不能互相串）',
          layout_for_window('Arknights.exe', WIN_SIZE, prof) is None, '返回 None')
    check('校准表里记了客户区原点（下次开窗口拿它当预测起点）',
          bool(load_window_profiles(prof)) and
          load_window_profiles(prof)[window_key(EXE, WIN_SIZE)]['offset'] == list(ORIGIN),
          str(sorted(load_window_profiles(prof).keys())))

    # ------------------------------------------------------------ 4. 三级取 ROI
    prof2 = tmp_profile_path()
    check('一级：校准表里有 -> 就用它（读同一个窗口的第二次）',
          build_window_layout(EXE, WIN_SIZE[0], WIN_SIZE[1],
                              path=prof).to_dict() == truth.to_dict(), '拿到表里那份')
    fb = layout_for(1280, 720, UI_SCALER_DEFAULT)
    got = build_window_layout(EXE, 1920, 1080, fallback=fb, path=prof2)
    check('二级：没校准过但手上有一份 -> 按比例缩过去顶上',
          abs(got.bar_width_f - fb.bar_width_f * 1.5) <= 1.0,
          '条宽 %.1f -> %.1f' % (fb.bar_width_f, got.bar_width_f))
    got = build_window_layout('Arknights.exe', 1920, 1080, path=prof2)
    ref = layout_for(1920, 1080, UI_SCALER_DEFAULT)
    check('三级：什么都没有 -> 落到游戏分辨率表（读游戏窗口时是对的）',
          got.to_dict() == ref.to_dict(), '条宽 %.1f（分辨率表 %.1f）'
          % (got.bar_width_f, ref.bar_width_f))

    # ------------------------------------------------------------ 5. WindowMonitor
    prof3 = tmp_profile_path()
    win = fake_window()
    mon = WindowMonitor(win, profiles_path=prof3, templates=())
    check('窗口没校准过：先退回分辨率表（读游戏窗口能直接用）',
          mon.layout.to_dict() == layout_for(WIN_SIZE[0], WIN_SIZE[1],
                                             UI_SCALER_DEFAULT).to_dict(),
          '条宽 %.1f' % mon.layout.bar_width_f)
    mon2 = WindowMonitor(win, layout=truth, profiles_path=prof3, templates=())
    check('校准出来的布局能直接交给监视器用', mon2.layout.to_dict() == truth.to_dict(),
          '条宽 %.1f' % mon2.layout.bar_width_f)
    check('面板方向键的基准是「校准那份」，不是被挪过的那份',
          mon2.base_layout().to_dict() == truth.to_dict(), '一致')
    mon2.save_current(source='检查', note='')
    p = load_window_profiles(prof3)[window_key(EXE, WIN_SIZE)]
    check('面板「保存校准」真的写进了 window_calib.json',
          p['layout']['bar'] == truth.bar.to_dict(),
          '%s 里 bar=%s' % (os.path.basename(prof3), p['layout']['bar']))
    resized = mon2._layout_for_size(1920, 1175)
    check('窗口被拉大：算得出一份新布局（应急按比例缩）而不是崩',
          resized.width == 1920 and resized.height == 1175 and resized.bar_width_f > 0,
          '条宽 %.1f' % resized.bar_width_f)

    # ------------------------------------------------------------ 6. 端到端
    screen = FakeScreen(WIN_SIZE, truth)
    install(screen)

    # 6a 自动校准（一张白纸，不框选）
    src = WindowSource(fake_window(), profile_path=tmp_profile_path(), use_profile=True,
                       snap=True, templates=())
    check('自动校准（不框选）自己把费用条找出来、并自检通过', src.verified is True,
          (src.notes[-1] if src.notes else '（没有说明）')[:60])
    check('自动校准反推出的三条 ROI 和真值差在几像素内',
          abs(src.layout.bar.x - truth.bar.x) <= 3 and
          abs(src.layout.bar_width_f - truth.bar_width_f) <= 4 and
          abs(src.layout.pause.x - truth.pause.x) <= 40,
          'bar.x %d（真值 %d）、条宽 %.1f（真值 %.1f）、按钮 dx %+d'
          % (src.layout.bar.x, truth.bar.x, src.layout.bar_width_f, truth.bar_width_f,
             src.layout.pause.x - truth.pause.x))
    src.close()

    # 6b 一费 30 帧（先框选校准，再开始计数 —— 用户的正常流程）
    prof5 = tmp_profile_path()
    save_window_profile(window_key(EXE, WIN_SIZE), WIN_SIZE, ORIGIN, 1.0, source='检查',
                        note='', layout=truth, path=prof5)
    mon3, src3 = run_monitor(screen, truth, 3.4, prof5)
    n_cycles, med, lo, hi = mon3.counter.cycle_stats()
    ideal = 3.4 * LOGICAL_FPS
    got = mon3.counter.elapsed
    check('一费 = 30 帧（逐费用周期实测，不是「一秒凑 30 帧」）',
          n_cycles >= 2 and lo == LOGICAL_FPS and hi == LOGICAL_FPS,
          '%d 个完整周期：中位 %.0f、最小 %.0f、最大 %.0f' % (n_cycles, med, lo, hi))
    check('总帧数和「假窗口走的逻辑帧」对得上',
          abs(got - ideal) <= 3.0,
          '数到 %d 帧，真值约 %.0f 帧（%.2f 秒）' % (got, ideal, 3.4))
    check('采样时真的在一张一张抓新画面（不是抓了一帧反复用）', screen.painted > 50,
          '画了 %d 张，采样 %d 次' % (screen.painted, mon3.stats.samples))
    res = mon3.latest.result
    check('读窗口链路走的是像素通道（在关卡里、条在动）',
          res is not None and res.mode == 'pixel',
          'mode=%s fill=%d' % (res.mode, res.fill) if res else '没有读数')
    check('读窗口链路没有漏采（最大采样间隔 < 一个逻辑帧的显示时长）',
          mon3.stats.gap_max_ms < 1000.0 / LOGICAL_FPS,
          '最大间隔 %.1f ms（门限 %.1f ms）' % (mon3.stats.gap_max_ms,
                                                1000.0 / LOGICAL_FPS))

    # 6c 手动框选
    prof6 = tmp_profile_path()
    src2 = WindowSource(fake_window(), profile_path=prof6, use_profile=True, snap=False,
                        templates=())
    box2 = Rect(truth.bar.x - 3, truth.bar.y, truth.bar.w + 6, truth.bar.h)
    lay2, _note = src2.manual_layout(box2)
    check('手动框选：费用条 ROI 就用框的（左边缘 + 宽度 1px 不差）',
          (lay2.bar.x, lay2.bar.w) == (box2.x, box2.w),
          'x=%d 宽 %d（框 x=%d 宽 %d）' % (lay2.bar.x, lay2.bar.w, box2.x, box2.w))
    check('手动框选：算出来的条宽就是框宽（一费 30 帧靠这个换算）',
          abs(lay2.bar_width_f - box2.w) <= 1.0, '条宽 %.1f，框宽 %d'
          % (lay2.bar_width_f, box2.w))
    check('手动框选：立刻记进 window_calib.json（下次开同尺寸窗口直接套用）',
          layout_for_window(EXE, WIN_SIZE, prof6) is not None,
          os.path.basename(prof6))
    src2.close()

    print()
    if FAIL:
        print('%d 项没过：%s' % (len(FAIL), '；'.join(FAIL)))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())