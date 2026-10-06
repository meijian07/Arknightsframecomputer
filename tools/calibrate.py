#!/usr/bin/env python
"""换分辨率时的校准工具：量出费用条槽的真实几何，写进实测校准表。

为什么要它：geometry.layout_for() 的单位常数只在 2560x1600 上实测标定过，别的分辨率
是按 ui_scale() 的规则**推**出来的。推得对不对只能拿真实画面量一遍 —— 这个工具就是
拿一段录像（或一张截图）量条槽的宽度和位置，和推算值对比，并写进
src/akframe/resolutions_measured.json 让 layout_for() 优先用它。

用法：

    # 1) 录像就是客户区（OBS「窗口捕获」游戏窗口，或者只截游戏窗口）
    python tools/calibrate.py "录像.mp4"

    # 2) 录的是整个桌面：客户区尺寸和原点都要给（和 run_reader.py 同一套参数）
    python tools/calibrate.py "录像.mp4" --client 2558x1494 --roi-offset 2,45

    # 3) 原点不知道就让它自己解（这时**只有条宽可信**，锚点不会写进表）
    python tools/calibrate.py "录像.mp4" --client 2558x1494 --roi-offset auto

    # 4) 量完写进校准表，并立刻用新表跑一遍录像验收（费用周期应当正好 30 帧）
    python tools/calibrate.py "录像.mp4" --client 2558x1494 --roi-offset 2,45 --write

    # 5) 看看表里现在有什么
    python tools/calibrate.py --list

录像怎么录（每次 10~20 秒就够）：
    进关卡之后让费用条**正常回费** —— 每个费用周期填充白块都会从条槽左端铺到右端，
    工具就是把这些白块并起来量条槽的（最左列 = 左边缘，最右列 = 右边缘）。
    录的时候别切菜单、别叠直播弹幕 / 低三分之一字幕条之类的覆盖层：
    它们会盖住费用条，白块读不出来就量不了（工具会直接报「没量出来」，不会瞎猜）。

量出来的三个数怎么用：
    条槽宽度  -> scale（= 条槽宽 / 180），决定「一费 30 帧」里每帧几像素
    条左边缘  -> 连同上一条算出 right_margin_unit（条槽右边缘距客户区右边）
    条槽中心  -> bar_cy_unit（条槽中心距客户区底边）
    --write 之后这些都会进实测表，换尺寸时 layout_for() 优先用实测值。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from akframe.calib import (MAX_FRAMES, calibrate_video, format_report,  # noqa: E402
                          record_from)
from akframe.capture import find_ffmpeg                                        # noqa: E402
from akframe.geometry import (layout_for, measured_layouts, measured_path,     # noqa: E402
                              reload_measured, save_measured)
from akframe.win32 import arknights_ui_scaler                                  # noqa: E402


def parse_size(spec):
    if not spec:
        return None
    w, h = spec.strip().lower().split('x')
    return (int(w), int(h))


def parse_offset(spec):
    if not spec:
        return None
    spec = spec.strip().lower()
    if spec == 'auto':
        return 'auto'
    dx, dy = spec.split(',')
    return (int(dx), int(dy))


def build_parser():
    p = argparse.ArgumentParser(
        description='量一段录像里的费用条槽，校准新分辨率',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('input', nargs='?', help='录像文件 / 单张截图')
    p.add_argument('--client', default=None,
                   help='客户区尺寸 WxH（录整个桌面时必须给；和 run_reader.py 一致）')
    p.add_argument('--roi-offset', default=None,
                   help='客户区原点在画面里的位置 dx,dy；写 auto 让它自己解')
    p.add_argument('--size', default=None, help='画面尺寸 WxH（一般不用给，自动读）')
    p.add_argument('--ui-scaler', type=float, default=None,
                   help='游戏内「UI 缩放」(0..1)，不传就从注册表读')
    p.add_argument('--frames', type=int, default=MAX_FRAMES,
                   help='最多解多少帧（默认 %d；白块每周期都会铺满，不需要全解）'
                        % MAX_FRAMES)
    p.add_argument('--ffmpeg', default=None, help='ffmpeg 路径')
    p.add_argument('--write', action='store_true',
                   help='把结果写进 %s' % os.path.basename(measured_path()))
    p.add_argument('--no-verify', action='store_true',
                   help='--write 之后不要立刻用新表跑一遍录像验收')
    p.add_argument('--list', action='store_true', help='打印当前校准表就退出')
    return p


def list_table() -> int:
    table = measured_layouts()
    print('校准表：%s' % measured_path())
    if not table:
        print('  （空 —— 所有尺寸都还在按 ui_scale() 的规则推算）')
        return 0
    for key in sorted(table, key=lambda k: (len(k), k)):
        r = table[key]
        print('  %-12s scale=%-8s 条宽=%-7s %s'
              % (key, r.get('scale'), r.get('bar_w'), r.get('source') or ''))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        return list_table()
    if not args.input:
        build_parser().print_help()
        return 2
    if not os.path.isfile(args.input):
        print('找不到文件：%s' % args.input)
        return 2

    try:
        ffmpeg = find_ffmpeg(args.ffmpeg)
    except FileNotFoundError as e:
        print(e)
        return 2
    client = parse_size(args.client)
    offset = parse_offset(args.roi_offset)
    ui_scaler = args.ui_scaler
    if ui_scaler is None:
        try:
            ui_scaler = arknights_ui_scaler()
        except Exception:
            ui_scaler = 1.0

    print('输入      : %s' % os.path.basename(args.input))
    print('UI 缩放   : %.2f' % ui_scaler)
    rep = calibrate_video(args.input, ffmpeg, client_size=client, ui_scaler=ui_scaler,
                          size=parse_size(args.size), frames=args.frames,
                          offset=None if offset == 'auto' else offset)
    if rep is None:
        print()
        print('❌ 没量出来。常见原因（按可能性排序）：')
        print('   * 录的是整个桌面但没给 --client / --roi-offset，'
              'ROI 全落在桌面别的地方了 —— 先给对这两个参数；')
        print('   * 这段画面里压根没有费用条（不在关卡里，或者被菜单 / 覆盖层盖住了）；')
        print('   * 这段录像里费用条没动过（判据是「白块铺满过整格」）——'
              '重录一段正常回费的；')
        print('   * 画面被放大 / 裁剪过（只有右下角一块），几何推不出来 ——'
              '这种情况用 run_reader.py 的 --bar-box 手工给条框。')
        return 1

    print()
    print(format_report(rep))
    rec = record_from(rep, source='录像 %s' % os.path.basename(args.input))

    cw, ch = rep['client']
    key = '%dx%d' % (cw, ch)
    print()
    print('这个尺寸在表里的写法（key = %s）：' % key)
    import json
    print('  %s' % json.dumps(rec, ensure_ascii=False))
    if args.write:
        old = measured_layouts().get(key)
        save_measured(key, rec)
        print()
        print('已写入    : %s%s' % (measured_path(),
                                    ('（覆盖了旧的一条）' if old else '')))
        if not args.no_verify:
            print()
            print('用新表跑一遍这段录像验收（费用周期应当是 30 帧）……')
            print('-' * 60)
            from akframe.cli import main as reader_main
            argv2 = [args.input, '-o', os.path.join('out', 'calib_check.csv'),
                     '--quiet']
            if args.client:
                argv2 += ['--client', args.client]
            if args.roi_offset and args.roi_offset.strip().lower() != 'auto':
                argv2 += ['--roi-offset=%s' % args.roi_offset]
            reader_main(argv2)
            print('-' * 60)
            print('验收跑完。费用周期那行是 30 帧就说明这套几何是对的；'
                  '不是 30 就说明这段录像本身特殊（条没走完一格 / 回费速率被减过）。')
    else:
        print()
        print('加 --write 才会写进表。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
