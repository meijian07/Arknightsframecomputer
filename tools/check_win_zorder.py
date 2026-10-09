#!/usr/bin/env python
"""置顶 Z 序自检：抬目标会不会把面板压下去、抬面板能不能再抬回来。

为什么单独一个检查：这条链路用到的全是 win32 的真实行为（SetWindowPos 的
HWND_TOPMOST / HWND_TOP），而「置顶是一个层，同一层里最后抬起来的在最上面」正是
2026-10-09 那个 bug 的全部内容 —— 面板每 0.7 秒被盖回去一次（见 docs 第 28 节）。

演法：开两个 2x2 像素的小窗口（肉眼基本看不见，但它们是**真的**窗口，真的在 Z 序里），
A 当「被读的目标窗口」、B 当「我们的面板」，两边都带 WS_EX_TOPMOST（和面板一样）：

    1. 钉 A 置顶 + 抬 A   -> A 在 B 上面（这就是「面板被盖回去」那一刻）
    2. 抬 B              -> B 在 A 上面（面板压到目标之上 = 用户要的效果）
    3. 再钉一次 A        -> A 又跑到 B 上面（**bug 复现**）
    4. 再抬一次 B        -> B 回到上面（_keep_pin 钉完目标紧接着做的事）

谁在谁上面不问坐标：从窗口管理器最顶上那个窗口开始，沿 GW_HWNDNEXT 往下数一遍，
序号小的在上面。

用法：python tools/check_win_zorder.py
"""

import ctypes
import os
import sys
import time
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe import win32 as W                                        # noqa: E402

FAIL = []


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


u = ctypes.windll.user32
u.GetTopWindow.restype = ctypes.c_void_p
u.GetTopWindow.argtypes = [ctypes.c_void_p]
u.GetWindow.restype = ctypes.c_void_p
u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
u.GetParent.restype = ctypes.c_void_p
u.GetParent.argtypes = [ctypes.c_void_p]
u.GetClassNameA.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]

GW_HWNDNEXT = 2


def order():
    """整个桌面从最上到最下的句柄表（顶层窗口 + 它们的子窗口，Z 序贯通的）。"""
    out = []
    h = u.GetTopWindow(None)
    while h:
        out.append(int(h))
        h = u.GetWindow(ctypes.c_void_p(int(h)), GW_HWNDNEXT)
        if len(out) > 20000:              # 兜个底，别在坏掉的环境里转圈
            break
    return out


def rank_map():
    return {w: i for i, w in enumerate(order())}


def class_name(h):
    buf = ctypes.create_string_buffer(256)
    u.GetClassNameA(ctypes.c_void_p(int(h)), buf, 256)
    return buf.value.decode('ascii', 'replace')


def above(a, b, rk):
    """a 在 b 上面吗（含「查不到」= 不算）。"""
    return a in rk and b in rk and rk[a] < rk[b]


def main() -> int:
    print('置顶 Z 序自检（两个 2x2 小窗口演目标窗口和面板）')
    a = tk.Tk()
    b = tk.Tk()
    tops = []
    for w in (a, b):
        w.overrideredirect(True)
        w.geometry('2x2+2+2')
        w.attributes('-topmost', True)     # 和面板一样：两边都是置顶窗口
        w.update()
    ha = W.top_level(a.winfo_id())
    hb = W.top_level(b.winfo_id())
    tops.append((a, b))
    try:
        # ---- 0. top_level：Tk 给的是里面的 TkChild，排 Z 序的是父窗口 TkTopLevel ----
        check('winfo_id() 是 TkChild、top_level() 给的是 TkTopLevel',
              class_name(a.winfo_id()) == 'TkChild'
              and class_name(ha) == 'TkTopLevel'
              and ha == int(u.GetParent(ctypes.c_void_p(a.winfo_id())) or 0),
              '%s(%s) -> %s(%s)' % (hex(a.winfo_id()), class_name(a.winfo_id()),
                                    hex(ha), class_name(ha)))

        # ---- 1. 钉 A + 抬 A：A 压在 B 上面（「面板被盖回去」那一刻）----
        W.set_topmost(ha, True)
        W.raise_window(ha)
        time.sleep(0.05)
        rk = rank_map()
        check('钉+抬目标窗口之后，目标在面板上面（这就是 bug 那一幕）',
              above(ha, hb, rk), 'A=%d B=%d' % (rk.get(ha, -1), rk.get(hb, -1)))

        # ---- 2. 抬 B：面板压到目标之上 ----
        W.raise_window(hb)
        time.sleep(0.05)
        rk = rank_map()
        check('再抬一次面板，面板就压到目标之上了',
              above(hb, ha, rk), 'A=%d B=%d' % (rk.get(ha, -1), rk.get(hb, -1)))

        # ---- 3. 再钉一次 A：又跑回上面（bug 复现）----
        W.set_topmost(ha, True)
        time.sleep(0.05)
        rk = rank_map()
        check('下一轮重新钉目标时，目标又跑到面板上面（所以必须再抬一次面板）',
              above(ha, hb, rk), 'A=%d B=%d' % (rk.get(ha, -1), rk.get(hb, -1)))

        # ---- 4. 再抬一次 B：回到我们要的状态 ----
        W.raise_window(hb)
        time.sleep(0.05)
        rk = rank_map()
        check('钉完紧接着抬面板（_keep_pin 的顺序）：面板稳住不掉下去',
              above(hb, ha, rk), 'A=%d B=%d' % (rk.get(ha, -1), rk.get(hb, -1)))

        # ---- 5. 抬子窗口没用：必须抬 top_level 那一个 ----
        W.raise_window(a.winfo_id())       # 故意抬 TkChild
        time.sleep(0.05)
        rk = rank_map()
        check('只抬 TkChild（不换成顶层句柄）抬不动 Z 序',
              above(hb, ha, rk), 'A=%d B=%d' % (rk.get(ha, -1), rk.get(hb, -1)))
    finally:
        for w in tops[0]:
            try:
                w.destroy()
            except Exception:              # noqa: BLE001
                pass
    print()
    if FAIL:
        print('%d 项没过：%s' % (len(FAIL), ' / '.join(FAIL)))
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
