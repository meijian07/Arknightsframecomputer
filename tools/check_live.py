#!/usr/bin/env python
"""交叉验证实时链路：把实时 CSV 和离线 CSV 对到同一个视频时间轴上比较。

原理：tools/fake_screen.py 会把缓存里的真实 ROI 画面按时间重放到屏幕上，并把自己的
「墙钟时间 -> 视频帧号」写进日志；实时读取器读的正是这块屏幕。于是：

    实时第 i 行的 elapsed  - 实时第 0 行的 elapsed
        ≈ 离线在 [idx(第 i 行), idx(第 0 行)] 区间内的 elapsed 增量

两者一致（差几帧以内），说明「抓屏 + 特征 + 状态机」这条实时链路是对的。

用法：
    python tools/check_live.py --live out/live_test.csv --fake out/fake_log.txt \
                              --offline out/frames.csv
"""

import argparse
import csv
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_offline(path):
    idx, elapsed, state = [], [], []
    with open(path, encoding='utf-8') as fh:
        for row in csv.DictReader(fh):
            idx.append(int(row['idx']))
            elapsed.append(int(row['elapsed']))
            state.append(row['bar_state'])
    return np.array(idx), np.array(elapsed), state


def load_fake(path):
    wall, idx = [], []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2:
                wall.append(float(parts[0]))
                idx.append(float(parts[1]))
    return np.array(wall), np.array(idx)


def load_live(path):
    rows = []
    with open(path, encoding='utf-8') as fh:
        for row in csv.DictReader(fh):
            if not row.get('wall'):
                continue
            rows.append(row)
    return rows


def build_fixture_reference(cache, size):
    """算出「假窗口缩放之后」的参考时间轴。

    为什么要这个：tools/fake_screen.py 把 2560x1600 的 ROI 缩放后贴到小窗口里，
    这个缩放本身会改变几个像素的读数（实测 3401 帧里有 46 帧 fill 变化 >2px），
    于是实时读数和「原始分辨率的离线参考」本来就会有差异 —— 那是**测试夹具的伪影**，
    不是实时链路的误差。

    所以这里按 fake_screen.py 完全相同的方式重新渲染一遍（纯离线、不碰屏幕），
    把它当作这个夹具下的真值。实时链路和它比较，比较的才是「抓屏 + 实时采样」引入的误差。
    """
    sys.path.insert(0, os.path.join(ROOT, 'src'))
    from PIL import Image
    from akframe.cli import DEFAULT_TEMPLATES, load_templates
    from akframe.features import read_bar, read_pause, read_speed
    from akframe.geometry import Layout, layout_for
    from akframe.state import Counter

    with open(os.path.join(cache, 'meta.json'), encoding='utf-8') as fh:
        meta = json.load(fh)
    src = Layout.from_dict(meta['layout'])
    dst = layout_for(size[0], size[1])
    fps = float(meta['fps'])
    frames = int(meta['frames'])
    names = ('bar', 'pause', 'speed')
    handles = {n: open(os.path.join(cache, n + '.raw'), 'rb') for n in names}
    tpl = load_templates(DEFAULT_TEMPLATES)
    counter = Counter(dst.bar_w, fps=fps)
    idxs, elapsed, states, running = [], [], [], []
    try:
        for i in range(frames):
            canvas = np.zeros((dst.height, dst.width), np.uint8)
            for n in names:
                r0 = getattr(src, n)
                handles[n].seek(i * r0.pixels)
                a = np.frombuffer(handles[n].read(r0.pixels), np.uint8).reshape(r0.h, r0.w)
                r1 = getattr(dst, n)
                if a.shape != (r1.h, r1.w):
                    a = np.asarray(Image.fromarray(a).resize((r1.w, r1.h), Image.NEAREST))
                canvas[r1.y:r1.y + r1.h, r1.x:r1.x + r1.w] = a
            def cut(r):
                return canvas[r.y:r.y + r.h, r.x:r.x + r.w]
            res = counter.step(i, i / fps, read_bar(cut(dst.bar), dst),
                               read_pause(cut(dst.pause), dst),
                               read_speed(cut(dst.speed), dst, tpl))
            idxs.append(i)
            elapsed.append(res.elapsed)
            states.append(res.bar_state)
            running.append('1' if res.running else '0')
    finally:
        for h in handles.values():
            h.close()
    return np.array(idxs), np.array(elapsed), states, running


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', default='out/live_test.csv')
    ap.add_argument('--fake', default='out/fake_log.txt')
    ap.add_argument('--offline', default='out/frames.csv')
    ap.add_argument('--tol', type=int, default=4, help='允许的帧数误差')
    ap.add_argument('--fixture-size', default=None,
                    help='如果跑的是 tools/fake_screen.py（有缩放），填它的窗口尺寸，'
                         '比如 1280x800：参考值会按同样的缩放重新算一遍')
    ap.add_argument('--cache', default=None, help='抽流缓存目录（用 --fixture-size 时需要）')
    args = ap.parse_args(argv)

    if args.fixture_size:
        w, h = [int(v) for v in args.fixture_size.lower().split('x')]
        cache = args.cache
        if cache is None:
            base = os.path.join(ROOT, 'out', 'cache')
            subs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
            cache = os.path.join(base, sorted(subs)[0])
        print('用夹具参考值（按 %s 重算，缓存 %s）' % (args.fixture_size, os.path.basename(cache)))
        _, off_elapsed, off_state, off_run = build_fixture_reference(cache, (w, h))
    else:
        _, off_elapsed, off_state = load_offline(args.offline)
    wall, fidx = load_fake(args.fake)
    rows = load_live(args.live)
    if not rows:
        print('实时 CSV 没有数据')
        return 1
    if len(wall) < 2:
        print('fake 日志太短')
        return 1

    def idx_at(w):
        return float(np.interp(w, wall, fidx))

    first, last = rows[0], rows[-1]
    i1 = idx_at(float(first['wall']))
    i2 = idx_at(float(last['wall']))
    j1 = int(round(i1))
    j2 = int(round(i2))
    expect = int(off_elapsed[j2]) - int(off_elapsed[j1])
    got = int(last['elapsed']) - int(first['elapsed'])

    print('实时行数      : %d' % len(rows))
    print('覆盖视频帧号  : %d -> %d (%.2f -> %.2f 秒)' % (j1, j2, j1 / 60.01, j2 / 60.01))
    print('离线 elapsed  : %d -> %d，增量 %d' % (off_elapsed[j1], off_elapsed[j2], expect))
    print('实时 elapsed  : %s -> %s，增量 %d' % (first['elapsed'], last['elapsed'], got))
    print('差值          : %+d 帧（容差 ±%d）%s' % (got - expect, args.tol,
                                                 '✅ 通过' if abs(got - expect) <= args.tol else '❌ 不通过'))

    # 逐行把实时样本对到参考时间轴上（采样率不同，不能按行号硬对齐）
    midx = np.clip(np.round(np.interp([float(r['wall']) for r in rows], wall, fidx)).astype(int),
                   0, len(off_state) - 1)
    a = np.array([off_state[k] for k in midx])
    b = np.array([r['bar_state'] for r in rows])
    print()
    print('条状态分布（夹具参考 / 实时，按占比）:')
    for sname in ('TRACK', 'MAX', 'MAX_FADE', 'EMPTY'):
        print('  %-9s %5.1f%% / %5.1f%%' % (sname, float((a == sname).mean()) * 100,
                                            float((b == sname).mean()) * 100))
    if args.fixture_size:
        ref_run = np.array([off_run[k] for k in midx])
        live_run = np.array([r['running'] for r in rows])
        print('运行/暂停一致率: %.2f%%（参考里运行占 %.1f%%，实时 %.1f%%）' % (
            float((live_run == ref_run).mean()) * 100,
            float((ref_run == '1').mean()) * 100, float((live_run == '1').mean()) * 100))
    modes = {}
    for r in rows:
        modes[r['mode']] = modes.get(r['mode'], 0) + 1
    print('计数通道      : %s' % modes)
    warns = {}
    for r in rows:
        if r['warn']:
            warns[r['warn']] = warns.get(r['warn'], 0) + 1
    print('告警          : %s' % (warns or '无'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
