#!/usr/bin/env python
"""刻度表布局自检：不启动界面，直接把摆放规则验一遍。

用法：
    python tools/check_scale.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.scale import ScaleGeom, build            # noqa: E402
from akframe.timeline import Cue, Teleprompter        # noqa: E402

FAILED = []


def check(name, cond, detail=''):
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s   %s' % (name, detail))
        FAILED.append(name)


def mk_cues(pairs):
    return [Cue(frame=f, text=t, line=i + 1, raw='%d %s' % (f, t))
            for i, (f, t) in enumerate(pairs)]


CUES = mk_cues([(1168, '部署 银灰'), (1198, '部署 山'), (1204, '技能 史尔特尔'),
                (1210, '撤离 桃金娘'), (1252, '部署 银灰'), (1310, '技能 史尔特尔')])


def lay(frame, cues=None, g=None):
    """按 Teleprompter 的真实规则算出 done，再摆表 —— 不自己另写一套。"""
    cues = CUES if cues is None else cues
    tp = Teleprompter()
    from akframe.timeline import Timeline
    tp.set_timeline(Timeline(cues=list(cues), source='(test)'))
    p = tp.update(frame)
    return build(frame, cues, p.done, g or ScaleGeom())


def by_frame(layout):
    """按帧号取 —— 不能按文字取：两条轴的说明完全可能一模一样。"""
    return {m.cue.frame: m for m in layout.marks}


def main():
    g = ScaleGeom()

    print('1) 等比例：两个轴点的间距 = 帧差 x px_per_frame')
    a = by_frame(lay(1186.2))
    # 未来的帧在屏幕上方，y 更小 —— 所以「早的减晚的」才是正数
    d = a[1198].tick_y - a[1210].tick_y
    check('1210 与 1198 相隔 12 帧 -> 18.0px', abs(d - 12 * g.px_per_frame) < 1e-6,
          '实际 %.3f' % d)
    d2 = a[1168].tick_y - a[1310].tick_y
    check('1310 与 1168 相隔 142 帧 -> 213.0px', abs(d2 - 142 * g.px_per_frame) < 1e-6,
          '实际 %.3f' % d2)
    check('晚的帧一定在上方（y 更小）', a[1210].tick_y < a[1198].tick_y < a[1168].tick_y)

    print('2) 状态划分：done 之前是 past，第 done 条是 next，之后是 future')
    past = [m.cue.text for m in lay(1186.2).marks if m.state == 'past']
    check('1186.2 时只有 1168 是 past', past == ['部署 银灰'], str(past))
    check('1198 是 next', a[1198].state == 'next')
    check('1210 / 1252 是 future',
          a[1210].state == 'future' and a[1252].state == 'future')

    print('3) 到帧那一刻状态迁移（1197.5 -> 1198.0）')
    na = [m.cue.text for m in lay(1197.5).marks if m.state == 'next']
    nb = [m.cue.text for m in lay(1198.0).marks if m.state == 'next']
    check('到帧前 next = 部署 山', na == ['部署 山'], str(na))
    check('到帧后 next = 技能 史尔特尔', nb == ['技能 史尔特尔'], str(nb))
    check('到帧后 部署 山 变 past',
          any(m.cue.text == '部署 山' and m.state == 'past' for m in lay(1198.0).marks))

    print('4) 向下滚：frame 变大，同一个轴点往下走')
    def y_at(frame, text):
        return [m for m in lay(frame).marks if m.cue.text == text][0].tick_y
    y1, y2 = y_at(1186.0, '撤离 桃金娘'), y_at(1190.0, '撤离 桃金娘')
    check('1190 时比 1186 时低 6.0px', abs((y2 - y1) - 6.0) < 1e-6, '实际 %.3f' % (y2 - y1))
    check('下移量 = 帧号差 x 缩放', abs((y2 - y1) - (1190 - 1186) * g.px_per_frame) < 1e-6)

    print('5) 标签防重叠：相邻标签至少隔 label_gap')
    tight = mk_cues([(1000, 'a'), (1002, 'b'), (1004, 'c'), (1006, 'd')])
    l5 = lay(1000.0, tight)
    ys = sorted(m.label_y for m in l5.marks)
    gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
    check('4 条挤在 8 帧内，标签仍然分得开',
          all(x >= g.label_gap - 1e-6 for x in gaps), str(gaps))
    ticks = [m.tick_y for m in sorted(l5.marks, key=lambda m: m.cue.frame)]
    check('真实刻度没被改（tick_y 仍是等比例的）',
          abs(abs(ticks[1] - ticks[0]) - 2 * g.px_per_frame) < 1e-6,
          str(ticks))
    check('被推开的标签会被标出来（画的时候要连细线）',
          any(m.spread for m in l5.marks))

    print('5b) 「现在」那一行是保留带：标签不压到帧号上')
    for name, l in (('挤在一起的', l5), ('1198.5 时', lay(1198.5)), ('到帧那一刻', lay(1198.0))):
        worst = min(abs(m.label_y - g.pointer_y) for m in l.marks)
        check('%s 时最近的一个标签离指针 %.1fpx（>= %.0f）'
              % (name, worst, g.label_h / 2.0),
              worst >= g.label_h / 2.0 - 1e-6)

    print('6) 青柱：从「现在」连到下一条')
    l6 = lay(1186.2)
    check('1186.2 时青柱存在', l6.mercury is not None)
    check('青柱底 = pointer_y', abs(l6.mercury[1] - g.pointer_y) < 1e-9)
    check('青柱顶 = 下一条的真实刻度', abs(l6.mercury[0] - by_frame(l6)[1198].tick_y) < 1e-6)
    far = lay(1000.0)
    check('下一条还在屏幕外时，青柱顶夹在 0（不画到画布外面）',
          far.mercury[0] == 0.0, str(far.mercury))

    print('7) 边界：没有轴 / 一条都没到 / 全走完')
    check('没有轴不崩', build(100.0, [], 0, g).marks == [])
    check('一条都没到时不崩', far.mercury is not None)
    end = lay(1320.0)
    check('全走完时没有青柱', end.mercury is None)
    check('全走完时剩下的全是 past（%d 条）' % len(end.marks),
          len(end.marks) > 0 and all(m.state == 'past' for m in end.marks),
          str([m.cue.text for m in end.marks]))

    print('8) 可见范围：屏幕外的轴点不画')
    l8 = lay(1160.0)
    check('1160 帧时 1310 那条还没进表',
          all(m.cue.frame != 1310 for m in l8.marks), str([m.cue.frame for m in l8.marks]))
    check('但 1252 那条在', any(m.cue.frame == 1252 for m in l8.marks))
    check('轴点都在刻度区附近（留了 label_h 的余量）',
          all(-g.label_h * 2 <= m.tick_y <= g.height + g.label_h * 2 for m in l8.marks))

    print('9) 缩放和指针高度都是参数')
    lw = by_frame(lay(1186.2, g=ScaleGeom(px_per_frame=3.0)))
    check('3.0 px/帧 时间距翻倍',
          abs((lw[1198].tick_y - lw[1210].tick_y) - 36.0) < 1e-6,
          '%.3f' % (lw[1198].tick_y - lw[1210].tick_y))
    check('pointer_ratio 生效', abs(ScaleGeom(pointer_ratio=0.5).pointer_y - 125.0) < 1e-9)
    check('看得到的未来帧数随之变化',
          abs(ScaleGeom().frames_up() - 116.667) < 0.01, '%.3f' % ScaleGeom().frames_up())

    print('10) 不碰输入（build 不改 cues）')
    before = [(c.frame, c.text) for c in CUES]
    build(1186.2, CUES, 2, g)
    check('cues 原样', before == [(c.frame, c.text) for c in CUES])

    print('11) 画图那一层（akframe.scale_render）')
    from PIL import ImageDraw
    from akframe import scale_render

    l11 = lay(1186.2)
    img = scale_render.render(l11)
    check('出图尺寸 = 面板尺寸',
          img.size == (int(l11.geom.width), int(l11.geom.height)),
          '%s vs %s' % (img.size, (l11.geom.width, l11.geom.height)))
    check('放大倍数只把图放大，不改布局',
          scale_render.render(l11, scale=2).size ==
          (int(l11.geom.width) * 2, int(l11.geom.height) * 2),
          '%s' % (scale_render.render(l11, scale=2).size,))
    bg = scale_render.Theme().bg
    check('确实画了东西（不全是底色）',
          any(px != bg for px in img.getdata()))
    check('flash 会改画面（到帧瞬间反色）',
          list(img.getdata()) != list(scale_render.render(l11, flash=True).getdata()))

    # 把 render 里每一次 text() 都记下来：PIL 不像 Tk 会自动换字体，汉字喂给
    # 只含拉丁字母的 Bahnschrift 会画成空豆腐块（界面上「现在」就中过这一枪）。
    drawn = []
    orig_text = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **kw):
        drawn.append((os.path.basename(str(getattr(kw.get('font'), 'path', ''))),
                      str(text)))
        return orig_text(self, xy, text, *a, **kw)

    ImageDraw.ImageDraw.text = spy
    try:
        scale_render.render(l11, flash=True)
    finally:
        ImageDraw.ImageDraw.text = orig_text

    late = [t for f, t in drawn if f.lower().startswith('bahnschrift')]
    check('刻度数字走的是 Bahnschrift',
          any(t.isdigit() for t in late), '画到的：%r' % late[:6])
    bad = [t for t in late if not t.isascii()]
    check('汉字没喂给 Bahnschrift（喂了就是豆腐块）', not bad, '喂了 %r' % bad[:3])
    cjk = [t for f, t in drawn if not f.lower().startswith('bahnschrift')]
    check('汉字走的是中文字体',
          any(not t.isascii() for t in cjk), '画到的：%r' % cjk[:6])
    check('「现在」那一行在', any(t.startswith('现在') for f, t in drawn))
    print()
    if FAILED:
        print('失败 %d 项：%s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
