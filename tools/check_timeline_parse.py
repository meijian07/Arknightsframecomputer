#!/usr/bin/env python
"""轴解析回归：「数字-数字」开头的关卡名不能被读成帧号，原有写法一条都不能变。

起因：`# 1-7 速通轴` 会被解析成「第 1 帧：-7 速通轴」，开局多出一条提词，而且没有任何
告警（标题行 / 列表行只要匹配到帧号就无条件算轴）。主线关卡名全是这种格式。

只用到 timeline.py，不需要游戏窗口、numpy 或界面：

    python tools/check_timeline_parse.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from akframe.timeline import parse_file, parse_lines   # noqa: E402

FAIL = []


def check(name, ok, detail=''):
    print('%-4s %s%s' % ('ok' if ok else 'FAIL', name, ('  -> ' + detail) if detail else ''))
    if not ok:
        FAIL.append(name)


def cues(text):
    tl = parse_lines(text.splitlines())
    return [(c.frame, c.text) for c in tl.cues], tl.warnings


# ---- 1. 关卡名不再被当成帧号 ----
got, warns = cues('# 1-7 速通轴\n- 10 部署 桃金娘')
check('标题「# 1-7 速通轴」不产生轴', got == [(10, '部署 桃金娘')], repr(got))
check('标题里的关卡名不报告警', not warns, '；'.join(warns))

got, _ = cues('## 12-17 打法\n- 300 撤退 银灰')
check('两位数关卡名「12-17」不被拆成第 1 帧', got == [(300, '撤退 银灰')], repr(got))

got, warns = cues('12-17 打法\n300 撤退')
check('普通行「12-17 打法」不产生轴', got == [(300, '撤退')], repr(got))
check('普通行认不出帧号时照旧报告警（不静默吞掉）',
      any('第 1 行' in w for w in warns), '；'.join(warns))

got, _ = cues('- 4-4 注意落点\n- 120 部署 山')
check('列表行「- 4-4」不产生轴', got == [(120, '部署 山')], repr(got))

got, _ = cues('# 1－7 全角横线\n30 部署 桃金娘')
check('全角横线「1－7」同样不算帧号', got == [(30, '部署 桃金娘')], repr(got))

got, _ = cues('## 120\n1-7 记得先开技能')
check('说明块里的「1-7 …」仍然并进说明', got == [(120, '1-7 记得先开技能')], repr(got))

# ---- 2. 原有写法不受影响 ----
CASES = [
    ('120 部署银灰', (120, '部署银灰')),
    ('120帧 部署银灰', (120, '部署银灰')),
    ('120f 部署银灰', (120, '部署银灰')),
    ('120F 部署银灰', (120, '部署银灰')),
    ('350开银灰技能', (350, '开银灰技能')),          # 帧号和说明之间的空格可以省
    ('1:23 部署银灰', (2490, '部署银灰')),
    ('1:23.5 部署银灰', (2505, '部署银灰')),
    ('- **120** 部署银灰', (120, '部署银灰')),
    ('| 300 | 开技能 |', (300, '开技能')),
    ('## 0 帧', (0, '')),
    ('120-部署银灰', (120, '-部署银灰')),            # 只有「数字-数字」才排除
]
for line, want in CASES:
    got, _ = cues(line)
    check('原有写法「%s」' % line, got == [want], repr(got))

# ---- 3. 仓库自带的示例轴结果不变 ----
tl = parse_file(os.path.join(ROOT, 'maps', 'example.md'))
frames = [c.frame for c in tl.cues]
check('maps/example.md 帧号不变', frames == [0, 120, 350, 600, 900, 1950], repr(frames))
check('maps/example.md 没有解析告警', not tl.warnings, '；'.join(tl.warnings))

print()
if FAIL:
    print('%d 项没过：%s' % (len(FAIL), '；'.join(FAIL)))
    sys.exit(1)
print('全部通过。')
