#!/usr/bin/env python
"""费用条「像素行程 vs 记了多少帧」的自洽检查（不需要知道倍速）。

思路（最关键的一点：**不需要知道倍速**）：

    费用条每往前走 8px（= 一个逻辑帧的行程）就是 1 个逻辑帧，
    这个事实和 1X / 2X / 0.2X / 暂停 都无关。

    所以对每一段「条画着填充」的连续帧：

        真值下界 = 这一段里 fill 的正增量之和 / 每逻辑帧像素
        上界     = 下界 + 每绕一圈补 3 帧（一费开头 1~2 帧条还没画出来，像素里看不见）

    然后拿状态机实际记的帧数去卡这个区间：

        记多了 -> 说明有「不看条、凭空补帧」的地方（倍速读错就会这样，一费 30 帧变 62 帧）
        记少了 -> 说明有该记没记的地方

    这个检查是**绝对**的：它不信任倍速、不信任帧率、不信任挂钟，只信费用条自己的像素。

用法：
    python tools/check_bar_linearity.py out/frames.csv --bar-w 240
"""

import argparse
import csv
import sys


def load(path):
    with open(path, encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


def runs(rows, state='TRACK', min_len=20):
    out = []
    for i, r in enumerate(rows):
        if r['bar_state'] != state:
            continue
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [(a, b) for a, b in out if b - a + 1 >= min_len]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('csv')
    ap.add_argument('--bar-w', type=float, default=240.0,
                    help='条槽宽（像素）。只有绕圈判定和每帧像素用到，误差影响很小')
    ap.add_argument('--min-len', type=int, default=20, help='只检查长度 >= 这么多的连续段')
    args = ap.parse_args()

    rows = load(args.csv)
    ppf = 1.03892 * args.bar_w / 30.0
    segs = runs(rows, 'TRACK', args.min_len)
    print('文件      : %s' % args.csv)
    print('条槽宽    : %.1f px   每逻辑帧 %.3f px' % (args.bar_w, ppf))
    print('TRACK 连续段（>=%d 帧）: %d 段' % (args.min_len, len(segs)))
    print()
    print('    起帧     止帧     帧数     正行程   绕圈      记为     应记(下界~上界)   判定')

    bad = 0
    for a, b in segs:
        px = 0.0
        wraps = 0
        for i in range(a + 1, b + 1):
            d = int(rows[i]['fill']) - int(rows[i - 1]['fill'])
            if d > 0:
                px += d
            elif d < -args.bar_w / 2:
                wraps += 1
        got = sum(int(rows[i]['delta']) for i in range(a, b + 1))
        lo = px / ppf
        hi = lo + 3.0 * wraps + 3.0
        ok = (lo - 1.0) <= got <= hi
        if not ok:
            bad += 1
        print('  %6d  %6d  %6d  %9.0f  %5d  %7d   %7.1f ~ %-7.1f  %s'
              % (a, b, b - a + 1, px, wraps, got, lo, hi, 'OK' if ok else '❌ 记多了' if got > hi else '❌ 记少了'))
    print()
    if bad:
        print('结论：❌ %d 段和费用条的像素行程对不上' % bad)
    else:
        print('结论：✅ 每一段记的帧数都落在费用条像素行程给出的区间里')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
