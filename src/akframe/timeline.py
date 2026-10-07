"""打轴器：读一个 Markdown 轴文件，运行时按帧号提词。

轴文件格式（故意写得很宽松，几种常见写法都认）
------------------------------------------------
1) 一行一条，帧号在最前面：
       120 部署银灰
       120帧 部署银灰
       120f 部署银灰            （帧 / f / F 后缀可以省）
2) 小标题 + 下面几行说明（说明可以写多行）：
       ## 120帧
       部署银灰在 A2
       先下奶再下输出
       （帧号后面没写东西时，说明取它下面接着的行；空行不打断，
         遇到下一条轴或下一个 # 标题为止）
3) 用时间代替帧号（按 1 倍速算，30 帧 = 1 秒）：
       1:23 部署银灰            -> 2490 帧
       1:23.5 部署银灰          （小数也认）
4) Markdown 装饰会自动剥掉，所以列表符号、粗体、表格都能写：
       - **120** 部署银灰
       | 300 | 开技能 |
5) `<!-- ... -->`、`//` 开头的行当注释，直接跳过。

帧号的含义：**1 倍速下的逻辑帧**，和计数器显示的数字、和攻略里说的「xx 帧」是同一个东西。
游戏里 2 倍速 / 子弹时间只改变它多快被走到，不改变帧号的数值。

认不出帧号的行会被忽略并记一条 warning —— 宁可报出来也不瞎猜。
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field

LOGICAL_FPS = 30.0     # 轴里的帧号 = 1 倍速逻辑帧（和 state.LOGICAL_FPS 同一个约定）
UPCOMING_MAX = 6       # 提词器默认往后显示几条
RECENT_MAX = 3         # 刻度表在「现在」线下面回看几条

_LIST_RE = re.compile(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)')
_HEAD_RE = re.compile(r'^\s*#{1,6}\s*')
_COMMENT_RE = re.compile(r'^\s*(?:<!--|-->|//)')
_TIME_RE = re.compile(r'^(\d{1,3}):(\d{1,2}(?:\.\d+)?)\s*(.*)$')
_FRAME_RE = re.compile(r'^(\d+)\s*(帧|[fF])?\s*(.*)$')


@dataclass
class Cue:
    """一条轴。"""
    frame: int
    text: str
    line: int          # 在文件里的行号（1 基，报错用）
    raw: str           # 原始行（调试用）

    def as_line(self) -> str:
        return '%6d 帧  %s' % (self.frame, self.text.replace('\n', ' / '))


@dataclass
class Timeline:
    cues: list = field(default_factory=list)
    source: str = ''
    warnings: list = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cues)

    @property
    def name(self) -> str:
        return os.path.basename(self.source) if self.source else '(未命名)'


# ---------------- 解析 ----------------

def strip_markup(line: str) -> str:
    """剥掉 markdown 装饰，只留内容。"""
    s = line.strip()
    s = re.sub(r'^>+\s*', '', s)          # 引用
    s = _LIST_RE.sub('', s)               # 列表符号
    s = _HEAD_RE.sub('', s)               # 标题井号
    s = s.replace('|', ' ')               # 表格竖线当分隔符
    s = s.replace('**', '').replace('__', '').replace('`', '')
    return s.strip()


def match_frame(text: str):
    """把一行开头解析成帧号，返回 (帧号, 剩余文字, 写法明确吗) 或 None。

    第三个返回值是「这行的帧号写法是否明确」：带 帧/f 后缀、或写成 m:ss 才算明确。
    只有明确的写法才允许在**说明块中间**开启新轴 —— 否则说明里随手写的
    「2 秒后开技能」会被误判成第 2 帧的一条轴。
    """
    m = _TIME_RE.match(text)
    if m:
        seconds = int(m.group(1)) * 60 + float(m.group(2))
        return int(round(seconds * LOGICAL_FPS)), m.group(3).strip(), True
    m = _FRAME_RE.match(text)
    if m:
        return int(m.group(1)), m.group(3).strip(), bool(m.group(2))
    return None


def parse_lines(lines, source: str = '') -> Timeline:
    tl = Timeline(source=source)
    pending = None            # 正在收集说明的那条轴（帧号后面没写文字时）
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip('\r\n')
        if _COMMENT_RE.match(line):
            continue
        body = strip_markup(line)
        if not body:
            continue          # 空行不打断说明块（多段说明很常见）
        heading = line.strip().startswith('#')
        hit = match_frame(body)
        is_cue = False
        if hit is not None:
            # 说明块外面：裸数字也认；说明块里面：要求写法明确（防止说明文字被误读）
            if pending is None or hit[2] or heading or _LIST_RE.match(line):
                is_cue = True
        if is_cue:
            frame, rest, _ = hit
            cue = Cue(frame=frame, text=rest, line=lineno, raw=line)
            tl.cues.append(cue)
            pending = cue if not rest else None
        elif heading:
            pending = None                       # 标题结束上一段说明
        elif pending is not None:
            pending.text = (pending.text + '\n' + body) if pending.text else body
        else:
            tl.warnings.append('第 %d 行认不出帧号，已忽略：%s' % (lineno, line.strip()))

    frames = [c.frame for c in tl.cues]
    if frames != sorted(frames):
        tl.warnings.append('帧号不是递增的，已按帧号重新排序（提词必须按顺序）')
    tl.cues.sort(key=lambda c: c.frame)

    empty = [c for c in tl.cues if not c.text.strip()]
    if empty:
        tl.warnings.append('有 %d 条轴没写说明：第 %s 行'
                           % (len(empty), ', '.join(str(c.line) for c in empty[:6])))
    dup = [f for f, n in Counter(frames).items() if n > 1]
    if dup:
        tl.warnings.append('这些帧号有 %d 条轴（会同时显示）：%s'
                           % (len(dup), ', '.join(str(f) for f in sorted(dup)[:6])))
    return tl


def parse_file(path: str) -> Timeline:
    """读一个 md 轴文件。用 utf-8-sig 顺带吃掉 BOM（记事本存的文件常带）。"""
    with open(path, encoding='utf-8-sig') as fh:
        return parse_lines(fh.readlines(), source=os.path.abspath(path))


# ---------------- 运行时提词 ----------------

@dataclass
class Prompt:
    """一次更新的结果，交给界面渲染。"""
    current: Cue | None       # 最近一条已经到帧的轴
    newly: list               # 这一拍刚到的轴（可能不止一条，帧号跳了）
    upcoming: list            # [(Cue, 还差几帧), ...]
    next_cue: Cue | None
    next_in: int | None       # 距离下一条还有几帧
    done: int                 # 已经触发了几条
    total: int
    # 刚过去的那几条（不含 current 本身），帧号升序。
    # 刻度表要在「现在」线下面画它们，所以光有 current 不够。
    recent: list = field(default_factory=list)


class Teleprompter:
    """把「当前帧号」翻译成「现在该看哪条轴」。

    只在帧号前进时推进；帧号倒退（清零、重锚）时自动回到起点。
    一拍的帧号可能一次跳好几帧（2 倍速、子弹时间、界面刷新间隔），所以按区间触发：
    (上一帧, 当前帧] 里所有没触发过的轴全部触发，界面用 newly 决定要不要高亮提醒。
    """

    def __init__(self, timeline: Timeline = None, upcoming: int = UPCOMING_MAX):
        self.timeline = timeline
        self.upcoming = int(upcoming)
        self.done = 0
        self._last_frame = None

    def set_timeline(self, timeline: Timeline) -> None:
        self.timeline = timeline
        self.reset()

    def reset(self) -> None:
        self.done = 0
        self._last_frame = None

    def update(self, frame: int) -> Prompt:
        tl = self.timeline
        if tl is None or not tl.cues:
            return Prompt(None, [], [], None, None, 0, 0)
        if self._last_frame is not None and frame < self._last_frame:
            self.reset()                     # 计数器清零了，轴跟着回到起点
        self._last_frame = frame
        newly = []
        while self.done < len(tl.cues) and tl.cues[self.done].frame <= frame:
            newly.append(tl.cues[self.done])
            self.done += 1
        current = tl.cues[self.done - 1] if self.done else None
        upcoming = [(c, c.frame - frame) for c in tl.cues[self.done:self.done + self.upcoming]]
        nxt = tl.cues[self.done] if self.done < len(tl.cues) else None
        lo = max(0, self.done - 1 - RECENT_MAX)
        recent = tl.cues[lo:self.done - 1]
        return Prompt(current=current, newly=newly, upcoming=upcoming, next_cue=nxt,
                      next_in=(nxt.frame - frame) if nxt is not None else None,
                      done=self.done, total=len(tl.cues), recent=recent)
