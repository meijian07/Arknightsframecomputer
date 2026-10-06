# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller 打包配置（onedir）。

    .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean akframe.spec

产物：dist/ArknightsFrameCounter/ 整个目录（exe + _internal/）。
分发给别人的时候把整个目录压成 zip 就行。

为什么是 onedir 而不是 onefile：
    onefile 每次启动都要先把自己解压到临时目录（启动慢、容易被杀软拦、出问题没法查），
    onedir 启动快、目录里有什么一眼可见。这个程序是长时间挂着用的，启动快更重要。

为什么不需要把 ffmpeg 打进去：
    实时链路走的是 Windows 的 BitBlt 抓屏（src/akframe/win32.py），**不碰 ffmpeg**；
    只有离线读录像（run_reader.py）才需要它。所以打包出来的图形程序没有外部依赖，
    双击就能用。以后要加「读视频」功能时，让用户自己装 ffmpeg 或用 AKFRAME_FFMPEG
    环境变量指定路径即可（capture.find_ffmpeg 支持这两条路）。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, os.path.join(ROOT, 'src'))       # 让 collect_submodules 找得到 akframe

a = Analysis(
    [os.path.join(ROOT, 'run_monitor.py')],
    pathex=[os.path.join(ROOT, 'src')],
    binaries=[],
    # 两份数据文件要跟着 exe 一起发：
    #   templates/                  倍速模板（paths.templates_dir() 会先看 exe 旁边的同名目录）
    #   akframe/resolutions_measured.json  实测分辨率校准表（geometry.measured_path() 同样
    #                                     先看 exe 旁边，再回落到这里）
    # 漏掉校准表的后果实测过：exe 里读不到表，条槽 ROI 退回推算值（x2318 w240），
    # 比实测值右移 9px —— 计数器能用，但「条空到第一个像素」那段会数不准。
    datas=[(os.path.join(ROOT, 'templates'), 'templates'),
           (os.path.join(ROOT, 'src', 'akframe', 'resolutions_measured.json'), 'akframe')],
    # akframe.ui 是在 main() 里才 import 的，静态分析看不到，必须显式收集
    hiddenimports=collect_submodules('akframe'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'IPython', 'matplotlib', 'scipy', 'pandas',
              'PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
    noarchive=False,
    optimize=0,
)
# ---- 不要把 ucrtbase.dll 打进包里 -------------------------------------------
# 三条理由：
#   1) 它是 Windows 10 以后自带的系统 UCRT，本来就在 C:\Windows\System32 里，
#      程序会直接从系统目录加载这个 DLL，包里那份纯属重复；
#   2) 实测本机打包时，往 dist 里写这个文件的副本会被安全软件拦下来
#      （PermissionError: ...\_internal\ucrtbase.dll），而换个名字写就没事 ——
#      典型的「DLL 劫持防护」启发式误伤，重跑 / 清目录都不解决；
#   3) 顺带省 1.3 MB。
# 只筛这一个，别的系统 DLL 不动。
KEEP_SYS_DLL = ('ucrtbase.dll',)
a.binaries = [b for b in a.binaries
              if os.path.basename(b[0]).lower() not in KEEP_SYS_DLL]
print('collected binaries:', len(a.binaries))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ArknightsFrameCounter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 图形程序：不要那扇黑控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ArknightsFrameCounter',
)
