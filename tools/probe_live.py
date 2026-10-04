# -*- coding: utf-8 -*-
"""实时 ROI 自检：抓几帧真实游戏窗口，把三个 ROI 的原始读数和像素值全打出来。

用来回答「实时链路到底读到了什么」—— 离线视频读得准、实时却读不准时，先跑这个。

用法：
    python tools/probe_live.py                  # 抓 8 帧，报告写 out/probe_live.txt
    python tools/probe_live.py --tag base       # 换文件名，方便「关卡内 / 关卡外」各抓一次
    python tools/probe_live.py --frames 30      # 多抓几帧
    python tools/probe_live.py --wait 60        # 等游戏窗口出现，最多等 60 秒
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.cli import DEFAULT_TEMPLATES, load_templates          # noqa: E402
from akframe.features import read_bar, read_pause, read_speed       # noqa: E402
from akframe.geometry import apply_deltas, layout_for, load_calibration  # noqa: E402
from akframe.state import Counter                                   # noqa: E402
from akframe.win32 import (ScreenGrabber, arknights_settings, arknights_ui_scaler,  # noqa: E402
                           client_rect, enable_dpi_awareness, find_window, is_minimized,
                           to_gray)


def build_layout(cw, ch, calib):
    deltas, scale = load_calibration(calib)
    base = layout_for(cw, ch, arknights_ui_scaler())
    return (apply_deltas(base, deltas, scale) if deltas else base), base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', default='Arknights.exe')
    ap.add_argument('--calib', default=os.path.join(ROOT, 'layout_calib.json'))
    ap.add_argument('--frames', type=int, default=8)
    ap.add_argument('--wait', type=float, default=0.0, help='等窗口出现，最多等这么多秒')
    ap.add_argument('--tag', default='live')
    args = ap.parse_args()

    enable_dpi_awareness()
    lines = []

    def say(s=''):
        print(s)
        lines.append(s)

    deadline = time.time() + args.wait
    win = None
    while True:
        win = find_window(exe=args.exe)
        if win is not None or time.time() >= deadline:
            break
        time.sleep(1.0)
    if win is None:
        say('!! 找不到 %s 窗口。先把游戏开起来（最好进关卡，让费用条出现在画面上）。' % args.exe)
        return 1

    x, y, cw, ch = client_rect(win.hwnd)
    say('窗口      : %s / %s / pid=%d' % (win.exe, win.title, win.pid))
    say('客户区    : x=%d y=%d w=%d h=%d  最小化=%s' % (x, y, cw, ch, is_minimized(win.hwnd)))
    say('注册表    : uiScaler=%s  resolutionSetting=%s'
        % (arknights_ui_scaler(), arknights_settings().get('resolutionSetting')))

    lay, base = build_layout(cw, ch, args.calib)
    say('布局      : scale=%.4f 条槽整数宽=%d 真实条宽=%.3f 可见宽=%d'
        % (lay.scale, lay.bar_w, lay.bar_width_f, lay.bar.w))
    say('ROI       : bar=%s  pause=%s  speed=%s' % (lay.bar, lay.pause, lay.speed))
    say('ROI(未校准): bar=%s  pause=%s  speed=%s' % (base.bar, base.pause, base.speed))

    grab = ScreenGrabber()
    tpl = load_templates(DEFAULT_TEMPLATES)
    ctr = Counter(lay.model_bar_w, fps=60.0, bar_width_frac=lay.bar_width_f,
                  visible_w=lay.bar.w)

    say()
    say('--- 抓 %d 帧 ---' % args.frames)
    bars, samples = [], []
    t0 = time.perf_counter()
    for i in range(args.frames):
        full = grab.grab_bgra(x, y, cw, ch)

        bar_img = to_gray(full[lay.bar.y:lay.bar.y + lay.bar.h, lay.bar.x:lay.bar.x + lay.bar.w])
        b = read_bar(bar_img, lay)
        p = read_pause(to_gray(full[lay.pause.y:lay.pause.y + lay.pause.h,
                                    lay.pause.x:lay.pause.x + lay.pause.w]), lay)
        s = read_speed(to_gray(full[lay.speed.y:lay.speed.y + lay.speed.h,
                                     lay.speed.x:lay.speed.x + lay.speed.w]), lay, tpl)
        r = ctr.step(i, i / 60.0, b, p, s)
        bars.append(bar_img); samples.append((b, p, s, r))
        say('#%2d 条=%-9s fill=%3d grey146=%3d grey88=%3d | 运行=%d 比=%.3f | 数字=%d corr=%s '
            'm=%.1f 子弹=%d conf=%s | 通道=%-10s +%d | 告警=%s'
            % (i, b.state, b.fill, b.grey146, b.grey88, p.running, p.ratio, s.has_digit,
               ('%.3f' % s.corr_2x) if s.corr_2x == s.corr_2x else '  nan', s.m,
               s.bullet, s.conf, r.mode, r.delta, r.warn or '-'))
        time.sleep(1.0 / 60.0)
    dt = time.perf_counter() - t0
    say('抓完 %d 帧用时 %.2fs（%.1f Hz）' % (args.frames, dt, args.frames / dt))

    # ---- 条 ROI 的原始像素 ----
    mid = np.median(np.stack(bars), axis=0).astype(np.uint8)
    say()
    say('--- 条 ROI 原始像素（%d 帧中位数，形状 %s）---' % (args.frames, mid.shape))
    say('整块: min=%d max=%d mean=%.1f | >250=%d 151..165=%d 136..156=%d'
        % (mid.min(), mid.max(), mid.mean(), int((mid > 250).sum()),
           int(((mid >= 151) & (mid <= 165)).sum()), int(((mid >= 136) & (mid <= 156)).sum())))
    for i in range(mid.shape[0]):
        say('  行%2d 从右往左前 40: %s' % (i, list(mid[i][::-1][:40])))
    say('  列中位数(row): %s' % list(mid.astype(np.int16).mean(axis=0).round().astype(int)[::-1][:60]))

    # ---- 偏移搜索：条可能不在算出来的位置上 ----
    say()
    say('--- 条 ROI 平移搜索（看 fill 是否变成 1..239 且状态 TRACK）---')
    best = None
    for dy in (-16, -12, -8, -4, 0, 4, 8, 12, 16):
        row_txt = []
        for dx in (-8, -4, 0, 4, 8):
            yy, xx = lay.bar.y + dy, lay.bar.x + dx
            if yy < 0 or xx < 0 or yy + lay.bar.h > ch or xx + lay.bar.w > cw:
                row_txt.append('  --  ')
                continue
            img = to_gray(full[yy:yy + lay.bar.h, xx:xx + lay.bar.w])
            rb = read_bar(img, lay)
            row_txt.append('%s%3d' % (rb.state[0], rb.fill))
            score = 0 if rb.state == 'TRACK' else 1
            if rb.state == 'TRACK' and 1 <= rb.fill <= lay.bar.w - 1:
                score = -1
            if best is None or score < best[0]:
                best = (score, dx, dy, rb.state, rb.fill)
        say('  dy=%+3d: %s' % (dy, '  '.join(row_txt)))
    say('  表头: S=TRACK M=MAX F=MAX_FADE E=EMPTY，后面是 fill')
    if best:
        say('  最佳: dx=%+d dy=%+d -> %s fill=%d' % (best[1], best[2], best[3], best[4]))

    # ---- 存图 ----
    from PIL import Image
    outdir = os.path.join(ROOT, 'out')
    os.makedirs(outdir, exist_ok=True)
    Image.fromarray(mid).resize((mid.shape[1] * 3, mid.shape[0] * 3), Image.NEAREST).save(
        os.path.join(outdir, 'probe_%s_bar.png' % args.tag))
    Image.fromarray(bars[0]).resize((bars[0].shape[1] * 3, bars[0].shape[0] * 3),
                                    Image.NEAREST).save(
        os.path.join(outdir, 'probe_%s_bar_0.png' % args.tag))
    Image.fromarray(to_gray(full)).resize((cw // 3, ch // 3)).save(
        os.path.join(outdir, 'probe_%s_win.png' % args.tag))
    say()
    say('图: out/probe_%s_bar.png  out/probe_%s_win.png' % (args.tag, args.tag))

    txt = os.path.join(outdir, 'probe_%s.txt' % args.tag)
    with open(txt, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')
    print()
    print('报告已写到 %s' % txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
