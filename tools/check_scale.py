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
    # 晚的帧在屏幕**下方**：刻度整条往上滚，未来的轴从下面进来（2026-10-11 照看板改）
    d = a[1210].tick_y - a[1198].tick_y
    check('1210 与 1198 相隔 12 帧 -> 18.0px', abs(d - 12 * g.px_per_frame) < 1e-6,
          '实际 %.3f' % d)
    # 远距离那一对（1310）在 1.5px/帧 下已经出画面了（底部可见范围只有 83 帧），
    # 所以换 1px/帧 量它 —— 验的是「等比例」，跟缩放档无关。
    a1 = by_frame(lay(1186.2, g=ScaleGeom(px_per_frame=1.0)))
    d2 = a1[1310].tick_y - a1[1168].tick_y
    check('1310 与 1168 相隔 142 帧 -> 142.0px', abs(d2 - 142 * 1.0) < 1e-6,
          '实际 %.3f' % d2)
    check('晚的帧一定在下方（y 更大）', a[1210].tick_y > a[1198].tick_y > a[1168].tick_y)
    check('高亮的那条（下一条）在指针**下面**，紧挨着线',
          g.pointer_y < a[1198].tick_y < g.pointer_y + 30.0,
          'pointer=%.1f next=%.1f' % (g.pointer_y, a[1198].tick_y))

    print('2) 状态划分：之前是 past，第 done 条是 next，之后是 future')
    past = [m.cue.text for m in lay(1186.2).marks if m.state == 'past']
    check('1186.2 时只有 1168 是 past', past == ['部署 银灰'], str(past))
    check('1186.2 时 1198 是 next', a[1198].state == 'next')
    check('1210 / 1252 是 future',
          a[1210].state == 'future' and a[1252].state == 'future')

    print('3) 到帧那一刻状态迁移（1197.5 -> 1198.0）')
    na = [m.cue.text for m in lay(1197.5).marks if m.state == 'next']
    nb = [m.cue.text for m in lay(1198.0).marks if m.state == 'next']
    check('到帧前 next = 部署 山', na == ['部署 山'], str(na))
    check('到帧后 next = 技能 史尔特尔', nb == ['技能 史尔特尔'], str(nb))

    def state_of(frame, text):
        return [m.state for m in lay(frame).marks if m.cue.text == text][0]

    def mark_of(frame, text):
        return [m for m in lay(frame).marks if m.cue.text == text][0]

    check('执行前 3 帧：部署 山 保持 next + active（白色预告）',
          all(mark_of(f, '部署 山').state == 'next' and mark_of(f, '部署 山').active
              for f in (1195.0, 1196.0, 1197.0)))
    check('离执行还有 3.1 帧：还是普通灰色 next',
          mark_of(1194.9, '部署 山').state == 'next'
          and not mark_of(1194.9, '部署 山').active)
    check('到帧执行后：部署 山 变回普通 past',
          state_of(1198.0, '部署 山') == 'past')

    print('4) 向上滚：frame 变大，同一个轴点往上走')
    def y_at(frame, text):
        return [m for m in lay(frame).marks if m.cue.text == text][0].tick_y
    y1, y2 = y_at(1186.0, '撤离 桃金娘'), y_at(1190.0, '撤离 桃金娘')
    check('1190 时比 1186 时高 6.0px', abs((y1 - y2) - 6.0) < 1e-6, '实际 %.3f' % (y1 - y2))
    check('上移量 = 帧号差 x 缩放', abs((y1 - y2) - (1190 - 1186) * g.px_per_frame) < 1e-6)

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

    print('6) 青柱：2026-10-11 按用户要求**删掉了**')
    l6 = lay(1186.2)
    far = lay(1000.0)
    check('Layout 上已经没有 mercury 字段',
          not hasattr(l6, 'mercury') and not hasattr(far, 'mercury'))
    # 像素级钉一遍：青柱原来画在刻度条右边缘那两列（ruler_w-3 .. ruler_w-2），
    # 从指针往下扫，白色一个都不该有。指针那一条横线和它左端那个三角箭头也压在
    # 这两列上（三角是 py-4 .. py+4），所以从 pointer_y + 6 往下扫。
    from akframe import scale_render
    im6 = scale_render.render(l6)
    # 判据得是「**竖着**的连续白」：轴点自己的刻度线是白的（mark_next 就是 #ffffff），
    # 那是 1px 高的横线，扫列时会撞上它。青柱是一条从指针连到下一条的**实心柱**，
    # 所以看每一列里最长的连续白段有多长。
    worst = 0
    for x in range(g.ruler_w - 3, g.ruler_w - 1):
        run = 0
        for y in range(int(g.pointer_y) + 6, im6.height):
            run = run + 1 if im6.getpixel((x, y)) == (255, 255, 255) else 0
            worst = max(worst, run)
    check('刻度条右边缘从指针往下没有白色竖柱（只剩 1px 的横刻度线）',
          worst <= 2, '最长的一段连续白 = %dpx' % worst)

    print('7) 边界：没有轴 / 一条都没到 / 全走完')
    check('没有轴不崩', build(100.0, [], 0, g).marks == [])
    check('表外一条都没到时也不崩（表是空的、指针还在）',
          far.marks == [] and far.pointer_y > 0)
    end = lay(1320.0)
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
          abs((lw[1210].tick_y - lw[1198].tick_y) - 36.0) < 1e-6,
          '%.3f' % (lw[1210].tick_y - lw[1198].tick_y))
    check('pointer_ratio 生效', abs(ScaleGeom(pointer_ratio=0.5).pointer_y - 125.0) < 1e-9)
    check('默认指针就在正中（2026-10-11 照看板改的）',
          abs(ScaleGeom().pointer_y - ScaleGeom().height / 2.0) < 1e-9)
    check('指针对称：上下一半一半',
          abs(ScaleGeom().frames_up() - ScaleGeom().frames_down()) < 1e-9)
    check('看到的过去帧数 = 125 / 1.5',
          abs(ScaleGeom().frames_up() - 83.333) < 0.01, '%.3f' % ScaleGeom().frames_up())

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
    l_active = lay(1197.0)
    img_active = scale_render.render(l_active)
    th = scale_render.Theme()
    cur = [m for m in l_active.marks if m.cue.frame == 1198][0]
    l_next = lay(1190.0)
    nxt = [m for m in l_next.marks if m.cue.frame == 1198][0]
    sample_x = int(l_active.geom.ruler_w + 5)

    def theme_rgb(color):
        return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))

    check('操作执行前 3 帧：整行白色高亮',
          img_active.getpixel((sample_x, int(cur.label_y))) == theme_rgb(th.accent),
          str(img_active.getpixel((sample_x, int(cur.label_y)))))
    check('下一条：整行灰色高亮（不是白色）',
          scale_render.render(l_next).getpixel((sample_x, int(nxt.label_y))) ==
          theme_rgb(th.next_fill),
          str(scale_render.render(l_next).getpixel((sample_x, int(nxt.label_y)))))

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
        scale_render.render(l11)
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
    nums = sorted({t for f, t in drawn if t.isdigit()})
    check('轴点右边印的是**绝对帧号**（不再有 +40 这种带符号帧差）',
          '1198' in nums and not any(t.startswith('+') for f, t in drawn),
          '画到的数字：%r' % nums[:10])
    print('12) 刻度表：四边 1px 描边 / 「现在」读数给帧号那列让位（2026-10-13）')
    rgb = lambda hx: tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))   # noqa: E731
    th2 = scale_render.Theme()
    img2 = scale_render.render(l11)
    check('四边有 1px 描边（刻度区看着像一块凹槽，不再漂在面板上）',
          img2.getpixel((0, 0)) == rgb(th2.stroke)
          and img2.getpixel((img2.width - 1, img2.height - 1)) == rgb(th2.stroke),
          '%s vs %s' % (img2.getpixel((0, 0)), rgb(th2.stroke)))
    # 从右往左走指针线（白）：第一个非白像素就是「现在」读数垫底那块
    # 底色，它右边剩下多少像素就是留给轴帧号那列的。最多四位数（≈ 32px，
    # 右对齐在 W-pad_right），所以至少要留 36px；原来读数贴着 W-6，这个数是 9。
    # （最右一列 W-1 是描边，所以从 W-2 开始往左走）
    py = int(round(l11.geom.pointer_y))
    acc = rgb(th2.accent)
    x = img2.width - 2
    check('读数右边那条指针线照旧画到最右边（没被底色一并擦掉）',
          img2.getpixel((x, py)) == acc)
    while x > 0 and img2.getpixel((x, py)) == acc:
        x -= 1
    gap = img2.width - 2 - x
    check('「现在」读数往左让位：右边给帧号那列留出 >= 36px',
          gap >= 36, gap)
    print()

    if FAILED:
        print('失败 %d 项：%s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
