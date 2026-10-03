#!/usr/bin/env python
"""轴文件自检：把 md 轴解析出来打印，或者模拟计数器走一遍看什么时候提词。

用法：
    python tools/check_timeline.py maps/example.md              # 只解析
    python tools/check_timeline.py maps/example.md --sim 1200   # 模拟 0..1200 帧
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.timeline import Teleprompter, parse_file   # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='check_timeline.py')
    ap.add_argument('md_path')
    ap.add_argument('--sim', type=int, default=0, help='模拟计数器从 0 走到这一帧')
    ap.add_argument('--step', type=int, default=1, help='模拟时的步长（默认 1）')
    args = ap.parse_args(argv)

    tl = parse_file(args.md_path)
    print('文件    : %s' % tl.source)
    print('轴条数  : %d' % len(tl))
    print()
    print('%6s %6s %8s  %s' % ('行号', '帧号', '时间', '说明'))
    for c in tl.cues:
        print('%6d %6d %7.2fs  %s' % (c.line, c.frame, c.frame / 30.0,
                                      c.text.replace('\n', ' / ')))
    if tl.warnings:
        print()
        print('提醒：')
        for w in tl.warnings:
            print('  - %s' % w)
    else:
        print()
        print('解析无异常。')

    if args.sim > 0:
        print()
        print('模拟计数器 0 -> %d 帧（步长 %d）：' % (args.sim, args.step))
        tp = Teleprompter(tl)
        fired = 0
        for frame in range(0, args.sim + 1, max(1, args.step)):
            p = tp.update(frame)
            for c in p.newly:
                fired += 1
                print('  [%5d 帧] 第 %d 条触发 -> %s' % (frame, fired,
                                                        c.text.replace('\n', ' / ')))
        p = tp.update(args.sim)
        print('  共触发 %d / %d 条；当前显示：%s' % (
            p.done, p.total, p.current.as_line() if p.current else '(还没到第一条)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())