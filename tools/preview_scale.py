#!/usr/bin/env python
"""刻度表离屏预览：不开游戏、不开界面，直接把刻度表画成 PNG。

界面是最难自动验证的一层，所以把「布局数学」（akframe.scale）和「画」拆开：
布局有 check_scale.py 兜着，画这边用这个脚本出图，肉眼扫一眼就知道对不对。

画法本身在 akframe.scale_render —— **和界面共用同一份**。所以这张图就是面板上
真正会画出来的东西，不会出现「预览一个样、面板另一个样」。

用法：
    python tools/preview_scale.py                     # 默认三个时刻，输出 out/scale_preview.png
    python tools/preview_scale.py --demo              # 用内置的密集轴
    python tools/preview_scale.py --frames 1186,1198,1212
    python tools/preview_scale.py --px-per-frame 3 --pointer 0.5
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from PIL import Image, ImageDraw, ImageFont                    # noqa: E402

from akframe.scale import ScaleGeom, Theme, build              # noqa: E402
from akframe.scale_render import FONT_CJK, render              # noqa: E402
from akframe.timeline import Cue, Timeline, Teleprompter, parse_file   # noqa: E402

# --demo 用的密集轴：故意让几条挤在十几帧里，看刻度表怎么处理
DEMO_CUES = [(1168, '部署 银灰'), (1198, '部署 山'), (1204, '技能 史尔特尔'),
             (1210, '撤离 桃金娘'), (1252, '部署 银灰'), (1310, '技能 史尔特尔'),
             (1400, '部署 能天使')]


def caption_font(size):
    """面板外面那行说明文字才用得到，和刻度表本身无关。"""
    try:
        return ImageFont.truetype(FONT_CJK, size)
    except OSError:
        return ImageFont.load_default()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='preview_scale.py')
    ap.add_argument('--md', default=os.path.join(ROOT, 'maps', 'example.md'),
                    help='用哪个轴文件（默认 maps/example.md）')
    ap.add_argument('--demo', action='store_true',
                    help='用内置的密集轴（看几条轴挤在一起时的效果），不读文件')
    ap.add_argument('--frames', default=None,
                    help='要预览的几个「当前帧」，逗号分隔')
    ap.add_argument('--px-per-frame', type=float, default=1.5)
    ap.add_argument('--pointer', type=float, default=0.50)
    ap.add_argument('--scale', type=int, default=2, help='出图放大倍数')
    ap.add_argument('--out', default=os.path.join(ROOT, 'out', 'scale_preview.png'))
    args = ap.parse_args(argv)

    if args.demo:
        cues = [Cue(frame=f, text=t, line=i + 1, raw='')
                for i, (f, t) in enumerate(DEMO_CUES)]
    else:
        cues = parse_file(args.md).cues
    if not cues:
        print('这个轴文件里没有轴：%s' % args.md)
        return 1
    g = ScaleGeom(px_per_frame=args.px_per_frame, pointer_ratio=args.pointer)
    th = Theme()
    spec = args.frames or ('1186.2,1198.5,1212.0' if args.demo else '120,300,620')
    frames = [float(x) for x in spec.split(',') if x.strip()]

    tp = Teleprompter(Timeline(cues=list(cues), source=args.md))
    panels = []
    for fr in frames:
        p = tp.update(fr)
        lay = build(fr, cues, p.done, g)
        panels.append((fr, p, lay))

    z = args.scale
    panel_w = int(round(g.width * z))
    panel_h = int(round(g.height * z))
    pad, cap = 10, 22
    pw, ph = panel_w + pad * 2, panel_h + pad * 2 + cap
    sheet = Image.new('RGB', (pw * len(panels), ph), (5, 8, 11))
    dr = ImageDraw.Draw(sheet)
    f_cap = caption_font(12)
    for i, (fr, p, lay) in enumerate(panels):
        img = render(lay, th, z)
        ox = i * pw + pad
        sheet.paste(img, (ox, pad))
        nxt = '下一条 %s %+.1f 帧' % (p.next_cue.text, p.next_in) if p.next_cue \
            else '轴已走完'
        dr.text((ox, pad + panel_h + 4),
                '帧 %.1f ｜ %s' % (fr, nxt), font=f_cap, fill=(125, 147, 166))
        dr.rectangle([ox - 1, pad - 1, ox + panel_w, pad + panel_h],
                     outline=(31, 43, 54))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    sheet.save(args.out)
    print('已输出 %s  (%dx%d)' % (args.out, sheet.width, sheet.height))
    return 0


if __name__ == '__main__':
    sys.exit(main())
