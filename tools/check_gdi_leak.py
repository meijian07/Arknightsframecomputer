# -*- coding: utf-8 -*-
"""抓屏器的 GDI / 内存释放自检。

为什么单独一个：ScreenGrabber 里是 CreateCompatibleDC / CreateDIBSection，
这两样**不归 Python 管**，只把引用置成 None 不会还给系统。实测一个 468x1214
的抓屏器值 3 个 GDI 句柄 + 约 2 MB；抓屏连续失败时每拍换一个，几十秒就能把
GDI 句柄吃光，表现出来就是游戏和面板一起卡。

这里不碰游戏窗口，只抓桌面左上角 64x64，验证四件事：
    1. 不 close 直接丢引用 -> __del__ 兜底释放，GDI 回到基线；
    2. with ScreenGrabber() -> 退出时释放；
    3. close() 调两次不炸、不重复释放；
    4. LiveMonitor.set_layout / stop 会把旧抓屏器关掉（不是只置 None）。
"""

import ctypes
import ctypes.wintypes as wt
import gc
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.geometry import layout_for                    # noqa: E402
from akframe.live import LiveMonitor                       # noqa: E402
from akframe.win32 import ScreenGrabber, enable_dpi_awareness  # noqa: E402

FAILED = []


def check(name, cond, detail=''):
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s   %s' % (name, detail))
        FAILED.append(name)


class PMC(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_psapi = ctypes.WinDLL('psapi', use_last_error=True)
_user32 = ctypes.WinDLL('user32', use_last_error=True)
_k32.GetCurrentProcess.restype = wt.HANDLE
_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
_psapi.GetProcessMemoryInfo.restype = wt.BOOL
_user32.GetGuiResources.argtypes = [wt.HANDLE, wt.DWORD]
_user32.GetGuiResources.restype = wt.DWORD


def gdi_count():
    return int(_user32.GetGuiResources(_k32.GetCurrentProcess(), 0))


def working_set():
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    if not _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return 0
    return int(pmc.WorkingSetSize)


def grab_once():
    g = ScreenGrabber()
    g.grab_bgra(0, 0, 64, 64)
    return g


def main():
    print('抓屏器 GDI / 内存释放自检')
    enable_dpi_awareness()
    gc.collect()
    base_gdi = gdi_count()
    base_ws = working_set()

    # 1) 最关键的回归：不 close，直接丢引用（旧代码就是这样漏的）
    for _ in range(8):
        grab_once()
    gc.collect()
    check('丢引用后 __del__ 把 GDI 还回去',
          gdi_count() == base_gdi, 'GDI %d -> %d' % (base_gdi, gdi_count()))
    check('丢引用后工作集不持续上涨',
          working_set() - base_ws < 12 * 1024 * 1024,
          'WS %.1f -> %.1f MB' % (base_ws / 2 ** 20, working_set() / 2 ** 20))

    # 2) with 语句
    for _ in range(8):
        with ScreenGrabber() as g:
            g.grab_bgra(0, 0, 64, 64)
    gc.collect()
    check('with ScreenGrabber() 退出即释放',
          gdi_count() == base_gdi, 'GDI %d -> %d' % (base_gdi, gdi_count()))

    # 3) close 幂等
    g = grab_once()
    g.close()
    g.close()
    del g
    gc.collect()
    check('close() 调两次不重复释放',
          gdi_count() == base_gdi, 'GDI %d -> %d' % (base_gdi, gdi_count()))

    # 4) LiveMonitor 的两条换抓屏器路径
    lay = layout_for(1280, 720)
    mon = LiveMonitor(lay)
    try:
        mon._grabber = grab_once()
        mon.set_layout(lay)
        check('set_layout 关掉旧抓屏器（不是只置 None）',
              gdi_count() == base_gdi, 'GDI %d -> %d' % (base_gdi, gdi_count()))
        mon._grabber = grab_once()
        mon.stop()
        check('stop() 关掉抓屏器（停计数时把 2 MB 还回去）',
              gdi_count() == base_gdi, 'GDI %d -> %d' % (base_gdi, gdi_count()))
    finally:
        mon._close_grabber()

    if FAILED:
        print('\n失败 %d 项：%s' % (len(FAILED), '、'.join(FAILED)))
        return 1
    print('\n全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
