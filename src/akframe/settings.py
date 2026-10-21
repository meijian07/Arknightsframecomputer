"""回费速率设置：一费需要多少个逻辑帧。

为什么不自动认、改成手填：游戏里 30 逻辑帧 = 1 费是常态，只有战前点了减回费速度的
tag 才会变成 60（-50%）或 120（-75%）。自动识别一旦认错档位，**绝对帧号会整段漂移**
（差一档就是几百帧），而打轴器要的正是绝对帧号 —— 所以默认给一个人手可控的档位，
自动识别只当可选功能保留（见 cost_rate.py）。

设置文件（默认 cost_rate.json），两个键共用一个文件：

    {"frames_per_cost": 60.0}

frames_per_cost  = 战前减回费 tag：一费几个逻辑帧（见上）；
# （2026-10-11 起只有 frames_per_cost 一个键：局内负费改成一直按画面判，不再可配）

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

# ---- 局内负费（费用条上出现减号）----
# 负费期间每逻辑帧只攒一半，一费 30 -> 60 逻辑帧（见 state.speed_for_cost_state）。
# 2026-10-11 按用户要求：界面上的 auto / on / off 下拉框删了，负费**一直开着**
# —— 就是 'auto'：按画面判（费用数字左边那条纯白横条，见 features.read_sign）。
# 仅保留命令行 --negative（run_monitor）作为真出问题时的逃生通道：扫描框是照抄前人的
# （geometry.sign_rect），本机还没有一段真实的负费录像能对着钉它（见 docs 第 31 节）。

MIN_FRAMES_PER_COST = 1.0
MAX_FRAMES_PER_COST = 300.0                # 已知最慢档是 120（4 秒 1 费），留一倍余量

# 前端下拉框里的档位：(一费几个逻辑帧, 说明)。39 / 45 / 90 是前人文档里出现过的
# 非标档（1.3 / 1.5 / 3 秒 1 费），列出来省得手输；档位表见 docs 第 15.1 节。
#
# 说明文字必须**短**：它和帧数拼成下拉框条目的文案，而这个文案同时也印在收起态面板
# 那个按钮上（Tk 的 OptionMenu 按钮宽度跟着显示文本走）—— 文案长一格，整个面板就宽
# 一格。档位统一显示成「回费效率百分比」：30 帧/费 = 100%，60 帧/费 = 50%，
# 120 帧/费 = 25%。内部仍存 frames_per_cost，存档格式不变。
# 实测长文案会把收起态面板顶宽 —— 档位菜单
# 还没显式给 width 的时候，收起态面板干脆被顶到 578（帧号三位数那一下）。
# 「几秒 1 费」可以从帧数推出来（帧数 / 30 = 秒），切完档提示行里也会原样印一遍
# （见 format_frames_per_cost），所以不放进档位名里。
COST_RATE_PRESETS = (
    (30.0, '100% 正常回费'),
    (40.0, '75% 一级减费'),
    (60.0, '50% 二级减费'),
    (120.0, '25% 三级减费'),
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

    这个文件里现在只有 frames_per_cost 一个键（负费已改成一直按画面判）。
    """
    data = _read_settings(path)
    data['frames_per_cost'] = clamp_frames_per_cost(frames_per_cost)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
