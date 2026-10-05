"""回费速率设置：一费需要多少个逻辑帧。

为什么不自动认、改成手填：游戏里 30 逻辑帧 = 1 费是常态，只有战前点了减回费速度的
tag 才会变成 60（-50%）或 120（-75%）。自动识别一旦认错档位，**绝对帧号会整段漂移**
（差一档就是几百帧），而打轴器要的正是绝对帧号 —— 所以默认给一个人手可控的档位，
自动识别只当可选功能保留（见 cost_rate.py）。

设置文件（默认 cost_rate.json）：

    {"frames_per_cost": 60.0}

文件不存在 / 读不动 / 值不是正数 -> 一律退回 30，不抛异常：这是给前端启动时读的，
宁可给一个能用的默认值，也不要因为一个坏文件就打不开界面。

档位取值就是「一费几个逻辑帧」，和倍速无关（倍速只改变每秒走多少逻辑帧，不改变
一费要攒多少），也和画面帧率（60 / 120）无关。
"""

from __future__ import annotations

import json
import os

from .state import LOGICAL_FPS

DEFAULT_FRAMES_PER_COST = LOGICAL_FPS      # 30：1 秒 1 费，普通关卡
MIN_FRAMES_PER_COST = 1.0
MAX_FRAMES_PER_COST = 300.0                # 已知最慢档是 120（4 秒 1 费），留一倍余量

# 前端下拉框里的档位：(一费几个逻辑帧, 说明)。39 / 45 / 90 是前人文档里出现过的
# 非标档（1.3 / 1.5 / 3 秒 1 费），列出来省得手输；档位表见 docs 第 15.1 节。
COST_RATE_PRESETS = (
    (30.0, '正常：1 秒 1 费'),
    (39.0, '1.3 秒 1 费'),
    (45.0, '1.5 秒 1 费'),
    (60.0, '回费 -50%：2 秒 1 费'),
    (90.0, '3 秒 1 费'),
    (120.0, '回费 -75%：4 秒 1 费'),
)


def clamp_frames_per_cost(value) -> float:
    """把任意输入夹到 [1, 300]；不是数 / NaN / 非正数 -> 默认 30。

    前端输入框里什么都可能被敲进来，统一走这里，别让一个手滑把状态机带崩。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_FRAMES_PER_COST
    if v != v or v <= 0:                   # NaN / 0 / 负数
        return DEFAULT_FRAMES_PER_COST
    return float(min(max(v, MIN_FRAMES_PER_COST), MAX_FRAMES_PER_COST))


def format_frames_per_cost(frames: float) -> str:
    """给界面 / 控制台用的一句话，例如 60 -> '60 帧/费（2.00 秒 1 费）'。"""
    f = clamp_frames_per_cost(frames)
    return '%.0f 帧/费（%.2f 秒 1 费）' % (f, f / LOGICAL_FPS)


def load_cost_rate(path: str) -> float:
    """读设置文件里的「一费几个逻辑帧」。文件不存在 / 损坏 -> 30。"""
    if not path or not os.path.isfile(path):
        return DEFAULT_FRAMES_PER_COST
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return clamp_frames_per_cost(data.get('frames_per_cost'))
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_FRAMES_PER_COST


def save_cost_rate(path: str, frames_per_cost: float) -> None:
    """写设置文件。写不动（目录不存在 / 只读 / 被占用）由调用方决定怎么提示。"""
    data = {'frames_per_cost': clamp_frames_per_cost(frames_per_cost)}
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
