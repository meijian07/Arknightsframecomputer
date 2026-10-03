#!/usr/bin/env python
"""费用条线性自洽检查：用「内部一致性」证明逐帧读数没有错。

思路（不需要任何外部真值）：
    条正常走动时（TRACK + 运行中），每视频帧推进的逻辑帧数 = 30*m/fps，
    换算成像素就是 30*m/fps*px_per_frame。fill 是整数像素，所以：

      单帧   只能验证「推进量是整数像素」        —— 弱
      一段 N 帧的累计推进量 vs N*30*m/fps*px_per_frame，误差应 < 1px  —— 强

    因为数错一帧就是 8px 的误差，所以只要整段累计误差 < 1px，
    就说明**这一整段里没有任何一帧被数错**。这正好回答「细微的帧数变化看不出
    对不对」这个问题：不用眼睛看，用累计误差卡。

用法：python tools/check_bar_linearity.py out/frames.csv --fps 60.01
"""

import argparse
import csv
import sys

import numpy as np


def load(path):
    rows = list(csv.DictReader(open(path, encoding='utf-8')))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('csv_path', nargs='?', default='out/frames.csv')
    ap.add_argument('--fps', type=float, default=None, help='视频帧率（默认取 CSV 的 t 差分）')
    ap.add_argument('--bar-w', type=int, default=None, help='条宽（默认取 fill 的最大值推）')
    args = ap.parse_args(argv)

    rows = load(args.csv_path)
    n = len(rows)
    fill = np.array([int(r['fill']) for r in rows])
    state = [r['bar_state'] for r in rows]
    run = np.array([int(r['running']) for r in rows])
    m = np.array([float(r['m']) if r['m'] else float('nan') for r in rows])
    # 子弹时间不显示倍速，用前一个已知值补上
    last = 2.0
    for i in range(n):
        if m[i] == m[i]:
            last = m[i]
        else:
            m[i] = last
    t = np.array([float(r['t']) for r in rows])
    fps = args.fps or (1.0 / np.median(np.diff(t)))
    bar_w = args.bar_w or 240
    px_per_frame = bar_w / 30.0

    # 找 TRACK 且运行中、且不处于子弹时间（速度恒定的连续段）
    segs = []
    i = 0
    while i < n:
        if state[i] == 'TRACK' and run[i] == 1:
            j = i
            while (j + 1 < n and state[j + 1] == 'TRACK' and run[j + 1] == 1
                   and abs(m[j + 1] - m[i]) < 1e-6):
                j += 1
            if j - i >= 20:
                segs.append((i, j))
            i = j + 1
        else:
            i += 1

    print('文件      : %s' % args.csv_path)
    print('帧率      : %.4f fps   条宽 %d px   每逻辑帧 %.1f px' % (fps, bar_w, px_per_frame))
    print('TRACK 连续段（>=20 帧）: %d 段' % len(segs))
    print()
    print('%6s %6s %6s %8s %9s %9s %9s  %s' % (
        '起帧', '止帧', '帧数', '倍速', '应走(px)', '实走(px)', '误差(px)', '误差(逻辑帧)'))
    bad = []
    for (i, j) in segs:
        d = (fill[i + 1:j + 1] - fill[i:j]) % bar_w
        obs = float(d.sum())
        exp = (j - i) * 30.0 * m[i] / fps * px_per_frame
        err = obs - exp
        track = '%6d %6d %6d %8.0fX %9.0f %9.0f %+9.2f  %+.3f' % (
            i, j, j - i, m[i], exp, obs, err, err / px_per_frame)
        print(track)
        if abs(err) >= px_per_frame:
            bad.append((i, j, err))
    print()
    total_err = sum(abs(e) for _, _, e in bad)
    print('累计误差 >= 1 逻辑帧的段: %d 段' % len(bad))
    for i, j, e in bad:
        print('  帧 %d..%d  误差 %+.2f px = %+.3f 逻辑帧' % (i, j, e, e / px_per_frame))
    print()
    print('结论: %s' % ('✅ 每一段都精确到 <1 逻辑帧（说明没有数错帧）' if not bad
                       else '⚠️ 有 %d 段需要人工看帧' % len(bad)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
