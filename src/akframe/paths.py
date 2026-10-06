"""程序自己的文件放哪儿 —— 开发时是仓库根目录，打包成 exe 后是 exe 所在目录。

为什么需要单独一个模块：打包成 exe 之后 __file__ 指向 PyInstaller 的解包目录
（每次启动都可能换一个临时目录），靠 __file__ 往上数几层那套推断全废。
而 templates/（倍速模板）、cost_rate.json（回费速率）、layout_calib.json（ROI 校准）
这些都是**程序自己的**文件：开发时它们躺在仓库根目录，打包后应该待在 exe 旁边，
用户才能看见、才能改。
"""

from __future__ import annotations

import os
import sys


def frozen() -> bool:
    """是不是在 PyInstaller 打出来的 exe 里跑。"""
    return bool(getattr(sys, 'frozen', False))


def app_dir() -> str:
    """程序自己的目录：打包后 = exe 所在目录；开发时 = 仓库根目录。"""
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def templates_dir() -> str:
    """倍速模板目录。

    打包后先找 exe 旁边的 templates/（用户可以直接加自己的模板），
    再找 PyInstaller 解包目录里的那份（随 exe 一起打进去的默认模板）。
    """
    cands = [os.path.join(app_dir(), 'templates')]
    if frozen():
        base = getattr(sys, '_MEIPASS', None)
        if base:
            cands.append(os.path.join(base, 'templates'))
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


def data_path(name: str) -> str:
    """用户 / 程序自己的数据文件（cost_rate.json、layout_calib.json、导出的 csv）。"""
    return os.path.join(app_dir(), name)
