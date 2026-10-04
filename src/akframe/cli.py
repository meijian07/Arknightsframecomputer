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

from .capture import find_ffmpeg, open_images, open_video, probe_video
from .features import read_bar, read_pause, read_speed
from .geometry import layout_for
from .state import CSV_HEADER, Counter

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
    p.add_argument('--templates', default=DEFAULT_TEMPLATES,
                   help='倍速模板目录（默认 templates/）。文件名形如 2_bright.npy / 1_bright.npy，'
                        '开头的整数就是倍速值；匹配不上时沿用上次倍速并标记 low')
    p.add_argument('--ui-scaler', type=float, default=None,
                   help='游戏内「UI 缩放」0..1（默认从注册表读；读不到按 1.0）')
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
        cache = args.cache
        if cache is None:
            stem = os.path.splitext(os.path.basename(args.input))[0]
            cache = os.path.join('out', 'cache', stem)
        layout, fps, frames = open_video(args.input, cache, ffmpeg=args.ffmpeg,
                                        force=args.force, size=size, ui_scaler=ui_scaler)
        source_desc = '视频 %s' % os.path.basename(args.input)

    print('输入      : %s' % source_desc)
    print('帧率      : %.3f fps' % fps)
    print('ROI       : bar=%s pause=%s speed=%s (scale=%.4f)'
          % (layout.bar, layout.pause, layout.speed, layout.scale))
    print('条槽宽    : %d px（真实 %.2f px），每逻辑帧 %.3f px，UI 缩放 %.2f'
          % (layout.bar_w, layout.bar_width_f, layout.px_per_frame, ui_scaler))
    print('倍速模板  : %s' % (', '.join('%s->%.0fX' % (n, v) for v, n, _ in templates)
                              if templates else '无（全部标记 low，沿用初始 2X）'))
    if args.print_layout:
        return 0

    counter = Counter(layout.model_bar_w, fps=fps,
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
        for fr in frames:
            bar = read_bar(fr.bar, layout)
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
    if probe_mt > 0:
        print('实测帧率  : %.3f fps  <- 像素通道 %d 帧 / 时间轴 %.2f 秒（1 倍速等效）'
              % (fps_meas, probe_frames, probe_mt))
        print('            = %.4f 秒/费用周期（30 帧为一个周期）' % (30.0 / fps_meas))
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
