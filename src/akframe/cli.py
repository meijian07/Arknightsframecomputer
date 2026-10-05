"""命令行入口：跑一遍离线读取，输出逐帧 CSV 和统计摘要。"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import os
import re
import sys

import numpy as np

from .capture import find_ffmpeg, open_images, open_video
from .features import read_bar, read_pause, read_speed
from .cost_rate import collect_cycles, infer_rate
from .settings import DEFAULT_FRAMES_PER_COST
from .state import CSV_HEADER, PROBE_MIN_SECONDS, Counter

IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.bmp')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TEMPLATES = os.path.join(PROJECT_ROOT, 'templates')


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='run_reader.py',
        description='明日方舟全局逻辑帧计数器 —— 离线读取（视频 / 图片序列）')
    p.add_argument('input', help='视频文件，或图片序列所在目录 / 通配符')
    p.add_argument('-o', '--output', default=os.path.join('out', 'frames.csv'),
                   help='逐帧 CSV 输出路径（默认 out/frames.csv）')
    p.add_argument('--ffmpeg', default=None, help='ffmpeg 可执行文件路径')
    p.add_argument('--cache', default=None,
                   help='抽流缓存目录（默认 out/cache/<视频名>）')
    p.add_argument('--force', action='store_true', help='强制重新抽流，不用缓存')
    p.add_argument('--fps', type=float, default=None,
                   help='图片序列的帧率，或覆盖探测到的视频帧率')
    p.add_argument('--size', default=None, help='手动指定视频分辨率，如 2560x1600')
    p.add_argument('--client', default=None,
                   help='游戏客户区尺寸，如 2558x1440。录整个桌面（画面里有任务栏/标题栏）时用')
    p.add_argument('--roi-offset', default=None,
                   help='客户区原点在视频里的位置，如 16,45；填 auto 则自动探测。'
                        '配合 --client 用（录桌面/窗口时）')
    p.add_argument('--bar-box', default=None,
                   help='直接指定费用条槽在画面里的像素框 x,y,w,h。画面被放大 / 裁剪过'
                        '（只截到右下角一块）时用：ROI 只按条算，暂停 / 倍速通道关闭')
    p.add_argument('--templates', default=DEFAULT_TEMPLATES,
                   help='倍速模板目录（默认 templates/）。文件名形如 2_bright.npy / 1_bright.npy，'
                        '开头的整数就是倍速值；匹配不上时沿用上次倍速并标记 low')
    p.add_argument('--ui-scaler', type=float, default=None,
                   help='游戏内「UI 缩放」0..1（默认从注册表读；读不到按 1.0）')
    p.add_argument('--cost-rate', default=None,
                   help='回费速率 = 一费几个逻辑帧。不填 = 30（普通关卡，1 秒 1 费）；'
                        # argparse 会对 help 文本做 % 格式化，裸的 % 必须写成 %%
                        '60 = 回费速度 -50%%；120 = -75%%。'
                        'auto = 可选：从费用条一个周期里出现过多少种宽度自动认'
                        '（认错档会让绝对帧号整段漂移，所以默认不用）')
    p.add_argument('--print-layout', action='store_true', help='只打印算出来的 ROI 就退出')
    p.add_argument('--quiet', action='store_true', help='不打印进度')
    return p


def load_templates(dirpath: str):
    """加载 templates/ 下所有 .npy，返回 [(倍速值, 文件名, 数组), ...]。

    文件名开头的整数就是该模板代表的倍速（2_bright.npy -> 2）。形状不一致的模板会被
    read_speed 跳过，所以在别的分辨率下采集的模板同时放着也不会互相干扰。
    """
    out = []
    if not dirpath or not os.path.isdir(dirpath):
        return out
    for fn in sorted(os.listdir(dirpath)):
        if not fn.endswith('.npy'):
            continue
        m = re.match(r'^(\d+)', fn)
        if not m:
            continue
        try:
            arr = np.load(os.path.join(dirpath, fn))
        except (OSError, ValueError):
            continue
        out.append((float(m.group(1)), fn, arr.astype(np.float32)))
    return out


def cache_suffix(bar_box, client_size, offset) -> str:
    """抽流缓存目录的后缀：把「决定 ROI 的那几项」编进目录名。

    为什么必须编进去：缓存里存的是三路 ROI 裁出来的灰度流，换一组 ROI 就是另一套
    数据。以前只区分了手工条框，于是「同一段录像先带 --roi-offset 跑一遍、再不带跑
    一遍」会命中前一次的缓存 —— 读的是上一次的 ROI，画面里明明没有费用条，程序却
    一本正经地报数（实测踩到过：两个 2558x1440 的录像换了 offset，读数全废）。
    """
    if bar_box:
        return '_barbox'
    if not client_size and not offset:
        return ''
    bits = []
    if client_size:
        bits.append('c%dx%d' % (client_size[0], client_size[1]))
    if offset:
        bits.append('o%d_%d' % (offset[0], offset[1]))
    return '_' + '_'.join(bits).replace('-', 'm')


def registry_ui_scaler() -> float:
    """从注册表读游戏内「UI 缩放」；非 Windows / 读不到都按 1.0。"""
    try:
        from .win32 import arknights_ui_scaler
        return arknights_ui_scaler()
    except Exception:
        return 1.0


def resolve_images(path: str):
    if os.path.isdir(path):
        files = []
        for ext in IMAGE_EXT:
            files += glob.glob(os.path.join(path, '*' + ext))
        return sorted(files)
    if any(ch in path for ch in '*?['):
        return sorted(glob.glob(path))
    if os.path.splitext(path)[1].lower() in IMAGE_EXT:
        return [path]
    return []


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    templates = load_templates(args.templates)

    ui_scaler = args.ui_scaler if args.ui_scaler is not None else registry_ui_scaler()
    images = resolve_images(args.input)
    if images:
        layout, fps, frames = open_images(images, fps=args.fps or 60.0, ui_scaler=ui_scaler)
        source_desc = '图片序列 %d 张' % len(images)
    else:
        size = None
        if args.size:
            w, h = args.size.lower().split('x')
            size = (int(w), int(h))
        client_size = None
        if args.client:
            w, h = args.client.lower().split('x')
            client_size = (int(w), int(h))
        roi_offset = None
        if args.roi_offset:
            spec = args.roi_offset.strip().lower()
            if spec == 'auto':
                # 自动对齐：见 align.py。录屏里客户区原点未知，靠人肉试很容易错一整段。
                from .align import detect_offset
                found = detect_offset(args.input, find_ffmpeg(args.ffmpeg),
                                      client_size=client_size, ui_scaler=ui_scaler,
                                      size=size)
                if found is None:
                    print('自动对齐  : 失败 —— 没在画面里找到费用条。'
                          '请用 --roi-offset X,Y 手动指定。')
                    return 2
                roi_offset = (found[0], found[1])
                print('自动对齐  : offset=%d,%d（频率阶跃 %.3f，填充出现频率 %.3f）'
                      % found)
            else:
                ox, oy = spec.split(',')
                roi_offset = (int(ox), int(oy))
        bar_box = None
        if args.bar_box:
            parts = [q for q in re.split(r'[,\s]+', args.bar_box.strip()) if q]
            if len(parts) != 4:
                print('--bar-box 要四个数：x,y,w,h（例 2133,1436,419,15）')
                return 2
            bar_box = tuple(int(q) for q in parts)
        cache = args.cache
        if cache is None:
            stem = os.path.splitext(os.path.basename(args.input))[0]
            # 决定 ROI 的参数（手工条框 / 客户区尺寸 / 原点偏移）都编进目录名，
            # 免得两套 ROI 互相覆盖缓存（见 cache_suffix）
            cache = os.path.join('out', 'cache',
                                 stem + cache_suffix(bar_box, client_size, roi_offset))
        layout, fps, frames = open_video(args.input, cache, ffmpeg=args.ffmpeg,
                                        force=args.force, size=size, ui_scaler=ui_scaler,
                                        client_size=client_size, offset=roi_offset,
                                        bar_box=bar_box)
        source_desc = '视频 %s' % os.path.basename(args.input)
        if bar_box:
            print('费用条框  : x=%d y=%d w=%d h=%d  <- 手工指定（暂停/倍速通道关闭）'
                  % bar_box)

    print('输入      : %s' % source_desc)
    print('帧率      : %.3f fps' % fps)
    print('ROI       : bar=%s pause=%s speed=%s (scale=%.4f)'
          % (layout.bar, layout.pause, layout.speed, layout.scale))
    print('条槽宽    : %d px（真实 %.2f px），每逻辑帧 %.3f px，UI 缩放 %.2f'
          % (layout.bar_w, layout.bar_width_f, layout.px_per_frame, ui_scaler))
    if not layout.buttons_visible:
        print('注意      : 画面里没有暂停键 / 倍速键（截屏或放大裁剪过），这两个通道关闭。')
        print('            计数只由费用条驱动；满费那几秒没有相位信息，按挂钟补，')
        print('            此时若按了暂停会多记（条正常推进时不受影响）。')
    print('倍速模板  : %s' % (', '.join('%s->%.0fX' % (n, v) for v, n, _ in templates)
                              if templates else '无（全部标记 low，沿用初始 2X）'))
    if args.print_layout:
        return 0

    # ---- 回费速率 ----
    # 「一费几个逻辑帧」默认 30（普通关卡，1 秒 1 费）；带了减回费速度 tag 的关卡用
    # --cost-rate 60 / 120 直接指定。--cost-rate auto 是可选功能：数「一个完整费用
    # 周期里条出现过多少种不同宽度」自己认（见 cost_rate.py）—— 认错档会让绝对帧号
    # 整段漂移，而打轴器要的正是绝对帧号，所以默认不用。
    rate_note = None
    frames_per_cost = DEFAULT_FRAMES_PER_COST
    spec = args.cost_rate
    if spec is None or str(spec).strip() == '':
        pass                     # 默认档，不用在输出里占一行
    elif str(spec).lower() == 'auto':
        # 认速率要先看完整段录像，所以这一趟把帧和条读进来（ROI 很小，几 MB 级；
        # 真正的大头是抽流缓存，那个在 out/cache 里，不进内存）
        bars = []
        prepared = []
        for fr in frames:
            bar = read_bar(fr.bar, layout)
            bars.append(bar)
            prepared.append((fr, bar))
        frames = prepared
        est = infer_rate(collect_cycles([b.fill for b in bars], [b.state for b in bars],
                                        layout.model_bar_w),
                         layout.model_bar_w, layout.bar_width_f, layout.bar.w)
        if est.frames_per_cost:
            frames_per_cost = est.frames_per_cost
        rate_note = est
    else:
        try:
            frames_per_cost = float(spec)
        except (TypeError, ValueError):
            print('--cost-rate 要填 auto 或一个正数（一费几个逻辑帧，例如 60）：%r'
                  % (spec,))
            return 2
        if not (frames_per_cost > 0):
            print('--cost-rate 要填正数（一费几个逻辑帧，例如 60）')
            return 2

    if rate_note is not None:
        if rate_note.frames_per_cost:
            print('回费速率  : %.0f 帧/费  <- 数出 %.0f 种条宽（%s）'
                  % (rate_note.frames_per_cost, rate_note.median_levels, rate_note.conf))
        elif rate_note.level_count:
            print('回费速率  : 按 30 帧/费算 —— 认不出来：数出 %.0f 种条宽，和已知档位'
                  '都对不上。要手工指定就加 --cost-rate N'
                  % rate_note.median_levels)
        else:
            print('回费速率  : 按 30 帧/费算 —— 这段录像里没有走完一个完整费用周期，'
                  '量不出来。要手工指定就加 --cost-rate N')
    elif abs(frames_per_cost - DEFAULT_FRAMES_PER_COST) > 1e-9:
        print('回费速率  : %.0f 帧/费  <- --cost-rate 手工指定' % frames_per_cost)

    counter = Counter(layout.model_bar_w, fps=fps, frames_per_cost=frames_per_cost,
                      bar_width_frac=layout.bar_width_f, visible_w=layout.bar.w)
    mode_count = collections.Counter()
    state_count = collections.Counter()
    warn_count = collections.Counter()
    runs = collections.Counter()
    n = 0
    prev_state, prev_run = None, None

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.output, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for item in frames:
            # 自动认速率时这一趟已经把条读过了（顺手存下来，省一次 median），
            # 手工指定时还是原始 Frame，现场读
            if isinstance(item, tuple):
                fr, bar = item
            else:
                fr, bar = item, read_bar(item.bar, layout)
            pause = read_pause(fr.pause, layout)
            speed = read_speed(fr.speed, layout, templates)
            res = counter.step(fr.index, fr.t, bar, pause, speed)
            writer.writerow(res.as_row())

            n += 1
            mode_count[res.mode] += 1
            state_count[res.bar_state] += 1
            if res.warn:
                warn_count[res.warn] += 1
            if res.bar_state != prev_state:
                if prev_state is not None:
                    runs['seg_' + prev_state] += 1
                prev_state = res.bar_state
            if res.running != prev_run:
                if prev_run is not None:
                    runs['seg_' + ('run' if prev_run else 'pause')] += 1
                prev_run = res.running
            if not args.quiet and n % 600 == 0:
                print('  ... 已处理 %d 帧，累计 %d 逻辑帧' % (n, res.elapsed))

    if prev_state is not None:
        runs['seg_' + prev_state] += 1
    if prev_run is not None:
        runs['seg_' + ('run' if prev_run else 'pause')] += 1

    print()
    print('输出      : %s' % os.path.abspath(args.output))
    print('处理帧数  : %d' % n)
    fps_meas, probe_frames, probe_mt = counter.probe()
    print('累计逻辑帧: %d  (约 %.2f 秒游戏内 1 倍速时间)' % (counter.elapsed, counter.elapsed / 30.0))
    # 「一费几帧」是唯一精确的口径：它只数「条从空走到下一次空」记了几帧，
    # 不除时间、不乘倍速，所以窗口头尾各差半帧这点量化误差根本进不来。
    # 「实测帧率」要除时间，天生带 ±1 帧/秒（1 秒窗口就是 ±3%），只在没走完一个
    # 完整周期、没别的口径可量时才拿出来兜底 —— 免得用户看到 29.5 以为在漏帧。
    n_cyc, cyc_med, cyc_min, cyc_max = counter.cycle_stats()
    if n_cyc:
        print('费用周期  : 一费 %d 帧（中位）  <- %d 个完整周期，范围 %d..%d 帧'
              % (cyc_med, n_cyc, cyc_min, cyc_max))
        if cyc_min != cyc_max:
            print('            逐周期(帧): ' + ' '.join(str(v) for v in counter.cycle_lengths))
        if cyc_min != cyc_max and (cyc_min < frames_per_cost or cyc_max > frames_per_cost):
            print('            提示：一费应为 %.0f 帧/费（本段判定值）。不稳定说明相位模型'
                  '还差一帧，或者这段里回费速率变过（局内减速 / 负费）。'
                  % frames_per_cost)
    else:
        print('费用周期  : 这段录像里没有走完一个完整周期，量不出「一费几帧」')
        if fps_meas == fps_meas:
            print('实测帧率  : %.2f fps（粗估，±1 帧/秒）<- 只统计条在动且未暂停的样本，'
                  '累计 %.2f 秒 / %d 帧' % (fps_meas, probe_mt, probe_frames))
        elif probe_mt > 0:
            print('实测帧率  : 样本不足（条在动且未暂停的时间只有 %.2f 秒，不到 %.1f 秒），'
                  '不报数' % (probe_mt, PROBE_MIN_SECONDS))
    print('计数通道  : ' + '  '.join('%s=%d' % kv for kv in sorted(mode_count.items())))
    print('条状态帧数: ' + '  '.join('%s=%d' % kv for kv in sorted(state_count.items())))
    print('分段数    : ' + '  '.join('%s=%d' % kv for kv in sorted(runs.items())))
    if warn_count:
        print('告警      : ' + '  '.join('%s=%d' % kv for kv in sorted(warn_count.items())))
    else:
        print('告警      : 无')
    return 0


if __name__ == '__main__':
    sys.exit(main())
