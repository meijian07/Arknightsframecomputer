"""回费速率设置：一费需要多少个逻辑帧。

为什么不自动认、改成手填：游戏里 30 逻辑帧 = 1 费是常态，只有战前点了减回费速度的
tag 才会变成 60（-50%）或 120（-75%）。自动识别一旦认错档位，**绝对帧号会整段漂移**
（差一档就是几百帧），而打轴器要的正是绝对帧号 —— 所以默认给一个人手可控的档位，
自动识别只当可选功能保留（见 cost_rate.py）。

设置文件（默认 cost_rate.json），两个键共用一个文件：

    {"frames_per_cost": 60.0, "negative": "auto"}

frames_per_cost  = 战前减回费 tag：一费几个逻辑帧（见上）；
negative         = 局内负费那个手动档：auto / on / off（见下面 NEGATIVE_MODES）。

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

# ---- 局内负费（费用条上出现减号）的手动档 ----
# 负费期间每逻辑帧只攒一半，一费 30 -> 60 逻辑帧（见 state.speed_for_cost_state）。
#   auto  按画面判（费用数字左边那条纯白横杠，见 features.read_sign）—— 默认
#   on    强制按负费算（认不出减号时救计数用）
#   off   强制不按负费算（误判成负费时压制用）
# 为什么留手动档：那个扫描框是照抄前人的（geometry.sign_rect），本机还没有一段真实的
# 负费录像能对着钉它。面板上那行「减号 N/thr」就是用来对的（见 docs 第 31 节）。
NEGATIVE_MODES = ('auto', 'on', 'off')
NEGATIVE_LABELS = {
    'auto': '自动（看画面里有没有减号）',
    'on': '强制开：按负费算（一费 60 帧）',
    'off': '强制关：按正常回费算（一费 30 帧）',
}
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


def parse_negative(value) -> str:
    """把任意输入（'auto'/'on'/'off'，或 True/False/1/0）归一成三个档之一。

    认不出来一律按 'auto'（按画面判）——这是唯一「不会把计数带偏」的兜底：
    强开强关一旦选错，整段录像就是 2 倍偏差。
    """
    if value is True:
        return 'on'
    if value is False:
        return 'off'
    text = str(value or '').strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return 'on'
    if text in ('0', 'false', 'no', 'off'):
        return 'off'
    return 'auto'


def negative_override(mode):
    """手动档 -> Counter.set_negative_override 要的那个值：auto -> None。"""
    mode = parse_negative(mode)
    return None if mode == 'auto' else (mode == 'on')


def _read_settings(path: str) -> dict:
    """读整个设置文件；不存在 / 坏了 / 不是对象 -> 空 dict（不是异常）。"""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_cost_rate(path: str) -> float:
    """读设置文件里的「一费几个逻辑帧」。文件不存在 / 损坏 -> 30。"""
    return clamp_frames_per_cost(_read_settings(path).get('frames_per_cost'))


def save_cost_rate(path: str, frames_per_cost: float) -> None:
    """写设置文件。写不动（目录不存在 / 只读 / 被占用）由调用方决定怎么提示。

    **保留同文件里的另一个键**（negative）：两个设置共用一个 cost_rate.json，
    整份覆盖写会把对方那一档悄悄清掉。
    """
    data = _read_settings(path)
    data['frames_per_cost'] = clamp_frames_per_cost(frames_per_cost)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def load_negative_mode(path: str) -> str:
    """读负费手动档（auto / on / off）。文件不存在 / 损坏 -> auto。"""
    return parse_negative(_read_settings(path).get('negative'))


def save_negative_mode(path: str, mode: str) -> None:
    """写负费手动档，同样保留 frames_per_cost。"""
    data = _read_settings(path)
    data['negative'] = parse_negative(mode)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
