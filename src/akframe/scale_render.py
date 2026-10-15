"""把一张刻度表的 Layout 画成位图（PIL）。界面和离屏预览**共用这一份**。

为什么要有这一层：界面原来用 tkinter 的 Canvas 一个图元一个图元地画（刻度线、数字、
轴标签、高亮条……约 95 个），每帧还要 delete('all') 重建一遍。改成「PIL 画成一张图
+ 作为**一个** canvas item 贴上去」之后，实测 `_draw_scale` 整体 **2.68 ms**
（PIL 画 1.19 + 转 PhotoImage 0.24 + 贴图 0.00）。Canvas 坐标和物理像素是 1:1
（实测 reqwidth 420 -> winfo_width 424，差的 4 是 2px 边框），贴上去逐像素清晰，不会糊。

**别把这一条的收益想大了。** 早先这里写过「95 个 item 要 10.9 ms，把面板从 124 Hz 压到
53 Hz」—— 那两个数都是 update_idletasks 口径，只算了 Python 记账，没算 Tk 真正描画。
后来在真实 mainloop 里量：面板速率的上限是「整窗口重绘约 14.7 ms」，跟刻度表贴不贴图
关系不大（贴图只省下 2~3 ms）。当时真正的瓶颈是 `poll_ms=16` 踩到了 Windows 15.6 ms
的定时器粒度、被抬成 31.2 ms —— 那一条的实测表在 docs 14.2。

顺带修掉一个隐患：tools/preview_scale.py 以前自己抄了一份画法，和界面各画各的，
改一边另一边不动 —— 「预览图」于是可能不是面板上真正画出来的东西。现在两边都调这里。

这一层只依赖 PIL 和 akframe.scale（纯数学），**不碰 tkinter**：所以离屏出图和自动
检查都能跑（tools/preview_scale.py 就是拿它出图的）。
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .scale import Theme, clip, one_line

# 数字用 Bahnschrift（Windows 自带的窄体，和游戏 HUD 的观感接近），中文用微软雅黑。
FONT_NUM = r'C:\Windows\Fonts\bahnschrift.ttf'
FONT_CJK = r'C:\Windows\Fonts\msyh.ttc'
FONT_CJK_BD = r'C:\Windows\Fonts\msyhbd.ttc'

_fonts = {}


def _font(path: str, size: int):
    """按 (文件, 字号) 缓存字体对象。找不到就用 PIL 的默认位图字体，不抛异常。"""
    key = (path, size)
    if key not in _fonts:
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            f = ImageFont.load_default()
        _fonts[key] = f
    return _fonts[key]


def font_sizes(scale: float = 1.0) -> tuple:
    """三档字号（数字 / 轴动作 / 轴说明），随放大倍数走。

    2026-10-11 按用户要求整体放大一档（9/11/12 -> 11/13/13）：面板上刻度表的字
    原来是照着「尽量缩小」定的，缩到 9px 之后帧号已经要凑近才看得清。刻度条宽
    (ruler_w=50) 不变，把刻度线往里收 2px、给「120」这种三位数腾出位置就够了。
    """
    return (max(8, int(round(11 * scale))),
            max(9, int(round(13 * scale))),
            max(9, int(round(13 * scale))))


def render(lay, theme: Theme = None, scale: float = 1, flash: bool = False) -> Image.Image:
    """把一个 Layout 画成 PIL 图像。

    scale 是放大倍数，只给离屏出图用（界面恒为 1）。
    flash = True 时把「下一条」那一行整行反色（打轴器到帧瞬间的反馈）。
    """
    g = lay.geom
    th = theme or Theme()
    W, H = int(round(g.width * scale)), int(round(g.height * scale))
    img = Image.new('RGB', (W, H), th.bg)
    dr = ImageDraw.Draw(img)
    n_num, n_act, n_nm = font_sizes(scale)
    f_num = _font(FONT_NUM, n_num)
    f_act = _font(FONT_CJK_BD, n_act)
    f_nm = _font(FONT_CJK, n_nm)
    f_small = _font(FONT_CJK_BD, n_num)

    def S(v):
        return v * scale

    # ---- 刻度条：只铺底色，**不画右边缘那条竖线** ----
    # 2026-10-11 按用户要求去掉：那条竖线从头贯到尾，比刻度本身还抢眼，
    # 而它并不承载任何信息（刻度线本身就标出了刻度条在哪）。
    dr.rectangle([0, 0, max(0, S(g.ruler_w) - 1), H - 1], fill=th.ruler_bg)

    # ---- 小/大刻度线与帧号 ----
    for f, y, major in lay.ticks:
        dr.line([S(26 if major else 32), S(y), S(g.ruler_w), S(y)],
                fill=th.tick_major if major else th.tick)
        if major:
            dr.text((S(3), S(y)), str(f), font=f_num, fill=th.num, anchor='lm')

    # （这里原来画「青柱」：指针往下到下一条那根 2px 的白柱。2026-10-11 用户要求
    #   「去掉」—— 截图里看着像一条多余的竖线，删掉。）

    # ---- 轴点 ----
    x_act = g.ruler_w + 10
    for m in lay.marks:
        col = {'past': th.mark_past, 'next': th.mark_next}.get(m.state, th.mark_future)
        dr.line([0, S(m.tick_y), S(g.ruler_w), S(m.tick_y)], fill=col)
        if m.spread:      # 标签被推开了，用细线连回真实刻度
            dr.line([S(g.ruler_w) - 2, S(min(m.tick_y, m.label_y)),
                     S(g.ruler_w) - 2, S(max(m.tick_y, m.label_y))], fill=th.connector)
        hit = flash and m.state == 'next'
        top = S(m.label_y - g.label_h / 2.0)
        if m.state == 'next':
            dr.rectangle([S(g.ruler_w) + 1, top, W - 1, top + S(g.label_h) - 1],
                         fill=th.accent if hit else th.next_fill)
            if not hit:
                dr.rectangle([S(g.ruler_w) + 1, top, S(g.ruler_w) + 2,
                              top + S(g.label_h) - 1], fill=th.accent)
        # 说明按第一个空格切成「动作 + 内容」两段，动作上色，扫起来更快；
        # 没空格的（比如「开银灰技能」）就整条当动作，也不出错。
        head, _, rest = one_line(m.cue.text).partition(' ')
        head = clip(head, 6)
        txt = th.bg if hit else col
        dr.text((S(x_act), S(m.label_y)), head, font=f_act, fill=txt, anchor='lm')
        if rest:
            dr.text((S(x_act) + dr.textlength(head, font=f_act) + S(6), S(m.label_y)),
                    clip(rest, 26), font=f_nm, anchor='lm',
                    fill=txt if hit else (th.faint if m.state == 'past'
                                          else (th.text if m.state == 'next' else th.dim)))
        # 右边印**绝对帧号**（2026-10-11 照用户看板批注：不再是「+40」这种带符号的
        # 帧差）。还差多少帧看指针线旁边那个「现在」，以及刻度表下面那句
        # 「下一条 … 还差 N 帧」——两个都还在，信息没丢。
        dr.text((S(g.width - g.pad_right), S(m.label_y)), '%d' % round(m.cue.frame),
                font=f_act, fill=txt, anchor='rm')

    # ---- 「现在」线：钉在正中不动。刻度整条往上滚，就是轴点在穿过这条线 ----
    # （「回看」时那条额外的灰线 2026-10-13 跟着翻条那两颗键一起删了：指针不再离位，
    #   刻度摆的那一帧就是「现在」。）
    py = S(lay.pointer_y)
    dr.line([0, py, W, py], fill=th.accent)
    dr.polygon([(S(g.ruler_w) - 7, py - S(4)), (S(g.ruler_w) - 7, py + S(4)),
                (S(g.ruler_w), py)], fill=th.accent)
    # 帧号读数压在指针线上，底下垫一块底色把线断开，读数才看得清。
    # 「现在」两个字必须走中文字体：Bahnschrift 里没有汉字，PIL 也不会像 Tk 那样
    # 自动回退，用错字体就会画成两个空豆腐块。数字仍旧用 Bahnschrift，和刻度上的
    # 帧号保持同一种观感。
    lbl, val = '现在', '%.1f' % lay.frame
    w_lbl = dr.textlength(lbl, font=f_small)
    w_val = dr.textlength(val, font=f_num)
    # 读数右边缘 = W - now_pad_right（2026-10-13 从 pad_right 换过来）：原来读数贴最右边，
    # 正好压在轴帧号那一列上（截图里「570」被读数盖掉一半）。垫底那块底色**只盖读数自己
    # 这一段**，不再一路铺到右边 —— 否则指针线在读数右边整段被擦掉，那条线看着像断了。
    x1 = W - S(g.now_pad_right)
    nx = x1 - w_lbl - S(4) - w_val
    dr.rectangle([nx - S(3), py - S(7), x1 + S(3), py + S(7)], fill=th.bg)
    dr.text((nx, py), lbl, font=f_small, fill=th.accent, anchor='lm')
    dr.text((nx + w_lbl + S(4), py), val, font=f_num, fill=th.accent, anchor='lm')

    # ---- 刻度区四边 1px 描边（2026-10-13）----
    # 为什么要有：刻度区底 (#0b0b0e) 对面板底 (#151517) 只差 1.06:1，画布边界看不出来，
    # 刻度表像浮在面板上（用户看板：「刻度区读不出『一块凹槽』」）。描边最后画，压在
    # 底色和「下一条」那条高亮行上面 —— 那条行本来一直铺到 W-1。
    dr.rectangle([0, 0, W - 1, H - 1], outline=th.stroke,
                 width=max(1, int(round(scale))))
    return img
