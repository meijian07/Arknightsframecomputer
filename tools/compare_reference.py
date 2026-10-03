"""把读取器的输出和「手工分析版」参考 CSV 做逐帧交叉验证。

参考表 docs/v2_frames.csv 是用另一套阈值（绝对像素数）独立算出来的，
两份在 fill / 条状态 / 运行暂停上必须逐帧一致 —— 不一致就是有 bug 或有漏判。

用法：
    python tools/compare_reference.py docs/v2_frames.csv out/frames.csv
"""

from __future__ import annotations

import argparse
import csv
import sys


def load(path):
    with open(path, encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('reference')
    ap.add_argument('reader')
    args = ap.parse_args()

    a = load(args.reference)
    b = load(args.reader)
    n = min(len(a), len(b))
    print('参考 %d 行，读取器 %d 行，比较前 %d 行' % (len(a), len(b), n))

    diff_fill = []
    diff_state = []
    diff_run = []
    for i in range(n):
        ra, rb = a[i], b[i]
        if int(ra['fill_px']) != int(rb['fill']):
            diff_fill.append((i, ra['fill_px'], rb['fill']))
        if ra['bar_state'] != rb['bar_state']:
            diff_state.append((i, ra['bar_state'], rb['bar_state']))
        run_ref = int(int(ra['pause_feat']) > 300)
        if run_ref != int(rb['running']):
            diff_run.append((i, run_ref, int(rb['running']), ra['pause_feat'], rb['pause_ratio']))

    print('fill     不一致: %d 帧' % len(diff_fill))
    for row in diff_fill[:10]:
        print('   帧 %d: 参考 %s vs 读取器 %s' % row)
    print('条状态   不一致: %d 帧' % len(diff_state))
    for row in diff_state[:10]:
        print('   帧 %d: 参考 %s vs 读取器 %s' % row)
    print('运行暂停 不一致: %d 帧' % len(diff_run))
    for row in diff_run[:10]:
        print('   帧 %d: 参考 %s vs 读取器 %s (feat=%s ratio=%s)' % row)

    ok = not (diff_fill or diff_state or diff_run)
    print('结论      : %s' % ('完全一致' if ok else '存在差异，需要排查'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
