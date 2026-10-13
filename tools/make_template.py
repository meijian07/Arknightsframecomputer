"""从抽好的缓存生成倍速数字模板。

模板文件名约定 <倍速>_<渲染>.npy，例如：
    1_bright.npy   1 倍速、不透明渲染
    2_bright.npy   2 倍速、不透明渲染
    2_dim.npy      2 倍速、半透明渲染
读取器会把 templates/ 下的所有 .npy 都当候选模板，取相关系数最高的那个，
所以补素材只要生成新文件即可，不用改代码。

模板尺寸与分辨率绑定，换分辨率必须重新生成。

用法：
    python tools/make_template.py <缓存目录> --label 2_bright --times 17,18,19,20
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from akframe import capture  # noqa: E402
from akframe.geometry import Layout, SPEED_DIGIT_BAND, SPEED_DIGIT_COLS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('cache', help='抽流缓存目录（含 meta.json / speed.raw）')
    ap.add_argument('--label', required=True, help='模板名，如 2_bright / 1_bright')
    ap.add_argument('-o', '--output', default=None, help='输出路径，默认 templates/<label>.npy')
    ap.add_argument('--times', default='17,17.5,18,18.5,19,19.5,20',
                    help='用于平均的时刻（秒），逗号分隔。这些帧必须是亮态且显示目标倍速')
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = args.output or os.path.join(root, 'templates', args.label + '.npy')

    meta = capture.load_meta(args.cache)
    layout = Layout.from_dict(meta['layout'])
    fps = float(meta['fps'])
    r0, r1 = layout.speed.band_rows(*SPEED_DIGIT_BAND)
    c0, c1 = layout.speed.cols_range(*SPEED_DIGIT_COLS)
    want = sorted(int(round(float(s) * fps)) for s in args.times.split(','))

    acc = None
    used = 0
    for fr in capture.iter_cached(args.cache):
        if fr.index not in want:
            continue
        roi = fr.speed
        band = roi[r0:r1 + 1, c0:c1 + 1]
        med = float(np.median(roi))
        span = max(1.0, float(roi.max()) - med)
        norm = np.clip((band.astype(np.float32) - med) / span, 0.0, 1.0)
        acc = norm if acc is None else acc + norm
        used += 1
        if used == len(want):
            break

    if acc is None or used == 0:
        print('没找到指定帧，检查 --times', file=sys.stderr)
        return 1
    tmpl = (acc / used).astype(np.float32)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    np.save(out_path, tmpl)
    print('模板 %s（%s）：%d 帧平均，形状 %s，亮度均值 %.3f'
          % (out_path, args.label, used, tmpl.shape, float(tmpl.mean())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
