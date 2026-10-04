"""Windows 平台底层能力：DPI 感知 / 找窗口 / 抓屏 / 点击穿透。

只依赖 ctypes 和 numpy，不需要额外装库。

抓屏走 GDI BitBlt（从屏幕 DC 取像素）。注意两点：
1. 每次 BitBlt 有约 3ms 的固定开销（要等 DWM 合成回读），所以不要一个 ROI 抓一次，
   而是**一次抓「三个 ROI 的外接矩形」**，再在内存里切三块。
2. 只能抓到「屏幕上真实显示的内容」，被别的窗口盖住 / 最小化时读不到，这点和视频输入
   不同，实时链路必须自己发现并停下来（见 live.py）。

坐标系约定：全部是**物理像素**，需要先调 enable_dpi_awareness()。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from dataclasses import dataclass

import numpy as np

_user32 = ctypes.WinDLL('user32', use_last_error=True)
_gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080


def enable_dpi_awareness() -> str:
    """让进程按物理像素工作。必须在建立任何窗口之前调用。

    不调用的话，本机 150% 缩放下 GetSystemMetrics 会返回 1707x1067（被系统缩放后的
    逻辑尺寸），抓屏坐标随之整体偏小 1/3，ROI 全错。
    """
    if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
        return 'per-monitor-v2'
    if _user32.SetProcessDPIAware():                                 # 老接口兜底
        return 'system'
    return 'none'


@dataclass
class WindowInfo:
    """一个可见的顶层窗口。client 是客户区（不含标题栏/边框）在屏幕上的位置与大小。"""
    hwnd: int
    pid: int
    exe: str
    title: str
    client: tuple          # (x, y, w, h)
    minimized: bool

    @property
    def client_size(self) -> tuple:
        return (self.client[2], self.client[3])

    def __str__(self) -> str:
        return '%s [pid=%d] %dx%d @(%d,%d) %s%s' % (
            self.exe, self.pid, self.client[2], self.client[3], self.client[0],
            self.client[1], self.title, ' (最小化)' if self.minimized else '')


def client_rect(hwnd: int) -> tuple:
    """客户区在屏幕上的 (x, y, w, h)。窗口移动/缩放后要重新取。"""
    r = wt.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(r)):
        return (0, 0, 0, 0)
    pt = wt.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, r.right - r.left, r.bottom - r.top)


def is_minimized(hwnd: int) -> bool:
    return bool(_user32.IsIconic(hwnd))


def _exe_name(pid: int) -> str:
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit('\\', 1)[-1]
        return ''
    finally:
        _kernel32.CloseHandle(h)


_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def list_windows() -> list:
    """列出所有可见、有标题的顶层窗口（按面积从大到小）。"""
    out = []

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        x, y, w, h = client_rect(hwnd)
        if w <= 0 or h <= 0:
            return True
        out.append(WindowInfo(int(hwnd), int(pid.value), _exe_name(pid.value),
                              buf.value, (x, y, w, h), is_minimized(int(hwnd))))
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    out.sort(key=lambda w: w.client[2] * w.client[3], reverse=True)
    return out


def find_window(exe: str = None, title: str = None, pid: int = None,
                min_size: tuple = (320, 240)) -> WindowInfo | None:
    """按 进程名 / 标题子串 / pid 找窗口，返回面积最大的那个。

    exe 和 title 都是「包含」匹配，大小写不敏感；exe 的 `.exe` 后缀可写可不写
    （进程名是 `Arknights`，写 `Arknights.exe` 也要能找到）。找不到返回 None。
    """
    best = None
    for w in list_windows():
        if w.minimized:
            continue
        if pid is not None:
            # 明确给了 pid 就只认 pid：这时 exe/title 不该再把窗口过滤掉
            # （比如用 tools/fake_screen.py 造的假窗口是 python.exe）
            if w.pid != int(pid):
                continue
        else:
            if exe:
                # 进程名不带 .exe（任务管理器里是 Arknights），所以两边都剥掉后缀再比
                want = os.path.splitext(exe.lower())[0]
                have = os.path.splitext(w.exe.lower())[0]
                if want and want not in have:
                    continue
            if title and title.lower() not in w.title.lower():
                continue
        if w.client[2] < min_size[0] or w.client[3] < min_size[1]:
            continue
        if best is None or w.client[2] * w.client[3] > best.client[2] * best.client[3]:
            best = w
    return best


def virtual_screen_bounds() -> tuple:
    """多显示器下的虚拟桌面范围 (x, y, w, h)。"""
    gsm = _user32.GetSystemMetrics
    x = gsm(76)   # SM_XVIRTUALSCREEN
    y = gsm(77)
    w = gsm(78)
    h = gsm(79)
    if w <= 0 or h <= 0:
        w, h = gsm(0), gsm(1)
        x = y = 0
    return (x, y, w, h)


def set_click_through(hwnd: int) -> None:
    """给窗口加上「鼠标穿透 + 分层」样式，让它只显示不挡鼠标。"""
    get = _user32.GetWindowLongPtrW
    get.restype = ctypes.c_ssize_t
    get.argtypes = [wt.HWND, ctypes.c_int]
    setl = _user32.SetWindowLongPtrW
    setl.restype = ctypes.c_ssize_t
    setl.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    ex = get(hwnd, GWL_EXSTYLE)
    setl(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wt.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
        ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
        ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', ctypes.c_long),
        ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wt.DWORD),
        ('biClrImportant', wt.DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [('rgbBlue', ctypes.c_ubyte), ('rgbGreen', ctypes.c_ubyte),
                ('rgbRed', ctypes.c_ubyte), ('rgbReserved', ctypes.c_ubyte)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', _RGBQUAD * 1)]


class _Surface:
    """一块 32bpp、自上而下的 DIB（内存位图），绑定到一块内存 DC。"""

    def __init__(self, screen_dc, w: int, h: int):
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 负号 = 自上而下，行序和 numpy 一致
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0      # BI_RGB
        bits = ctypes.c_void_p()
        self.dc = _gdi32.CreateCompatibleDC(screen_dc)
        self.bmp = _gdi32.CreateDIBSection(self.dc, ctypes.byref(bi), DIB_RGB_COLORS,
                                           ctypes.byref(bits), None, 0)
        if not self.bmp:
            raise OSError('CreateDIBSection 失败')
        self._old = _gdi32.SelectObject(self.dc, self.bmp)
        self._buf = (ctypes.c_ubyte * (w * h * 4)).from_address(bits.value)
        self.arr = np.frombuffer(self._buf, dtype=np.uint8).reshape(h, w, 4)  # BGRA
        self.w, self.h = w, h

    def free(self) -> None:
        _gdi32.SelectObject(self.dc, self._old)
        _gdi32.DeleteObject(self.bmp)
        _gdi32.DeleteDC(self.dc)


class ScreenGrabber:
    """按矩形抓屏。相同尺寸复用同一块 DIB，避免每帧分配内存。

    典型开销（本机 2560x1600 @150% 缩放，实测）：
        240x17    3.3ms      420x1212   5.5ms
    可以看出开销几乎与面积无关、主要是每次调用的固定成本 —— 所以要**合并成一次抓取**。
    """

    def __init__(self):
        self._screen = _user32.GetDC(None)
        if not self._screen:
            raise OSError('GetDC(NULL) 失败')
        self._surfaces = {}

    def _surface(self, w: int, h: int) -> _Surface:
        s = self._surfaces.get((w, h))
        if s is None:
            s = _Surface(self._screen, w, h)
            self._surfaces[(w, h)] = s
        return s

    def grab_bgra(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        """抓屏幕矩形，返回 (h, w, 4) 的 BGRA 数组（拷贝，调用方可以留着）。"""
        s = self._surface(w, h)
        if not _gdi32.BitBlt(s.dc, 0, 0, w, h, self._screen, x, y, SRCCOPY):
            raise OSError('BitBlt 失败：%dx%d @(%d,%d)' % (w, h, x, y))
        return s.arr.copy()

    def close(self) -> None:
        for s in self._surfaces.values():
            s.free()
        self._surfaces.clear()
        if self._screen:
            _user32.ReleaseDC(None, self._screen)
            self._screen = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def to_gray(bgra: np.ndarray) -> np.ndarray:
    """BGRA -> 灰度，权重和 ffmpeg 的 format=gray（BT.601）对齐。

    离线链路（ffmpeg）和实时链路（抓屏）必须用同一套灰度定义，否则
    特征模块里的灰度阈值（填充 255 / 满费 146 / 过渡 88）就对不上。
    """
    b = bgra[:, :, 0].astype(np.uint16)
    g = bgra[:, :, 1].astype(np.uint16)
    r = bgra[:, :, 2].astype(np.uint16)
    return ((r * 77 + g * 150 + b * 29) >> 8).astype(np.uint8)


# ---- 给 ctypes 补上 64 位下容易出错的函数签名 ----
_user32.GetDC.restype = wt.HDC
_user32.GetDC.argtypes = [wt.HWND]
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.IsIconic.argtypes = [wt.HWND]
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wt.LPARAM]
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateDIBSection.restype = wt.HANDLE
_gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(_BITMAPINFO), wt.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
_gdi32.SelectObject.restype = wt.HANDLE
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
_gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
_gdi32.DeleteObject.argtypes = [wt.HANDLE]
_gdi32.DeleteDC.argtypes = [wt.HDC]
_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                 ctypes.POINTER(wt.DWORD)]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]


# ---- 游戏设置（注册表）----
#
# 明日方舟 PC 端把画质设置存成注册表里的一个 UTF-8 JSON 字节数组，键名固定前缀是
# common_setting，后面跟一个随安装变化的哈希 —— 所以只能按前缀找，不能写死键名。
# 里面有两个我们关心的字段：
#     resolutionSetting.{width,height,isFullScreen,isBorderless}   窗口/分辨率
#     uiScaler                                                     游戏内「UI 缩放」0..1

ARK_REG_PATH = r'Software\Hypergryph\Arknights'
ARK_SETTING_PREFIX = 'common_setting'


def arknights_settings() -> dict:
    """读游戏设置 JSON。读不到（没装、换机器、无权限）返回空 dict，调用方必须容错。"""
    import json
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ARK_REG_PATH) as key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    return {}
                index += 1
                if not name.startswith(ARK_SETTING_PREFIX):
                    continue
                if isinstance(value, bytes):
                    text = value.decode('utf-8', 'ignore').rstrip('\x00')
                elif isinstance(value, str):
                    text = value
                else:
                    continue
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if isinstance(data, dict) and 'resolutionSetting' in data:
                    return data
    except OSError:
        return {}
    return {}


def arknights_ui_scaler() -> float:
    """游戏内「UI 缩放」（0..1）。读不到就按 1.0（= 不影响 ROI 距离）。

    为什么需要它：UI 缩放只改变 HUD 元素到右边缘/下边缘的距离，元素本身大小不变，
    所以它只该乘在「距右/距底」上（见 geometry.ui_edge_scale）。
    """
    value = arknights_settings().get('uiScaler')
    try:
        scaler = float(value)
    except (TypeError, ValueError):
        return 1.0
    if scaler != scaler:      # nan
        return 1.0
    return min(1.0, max(0.0, scaler))
