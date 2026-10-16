#!/usr/bin/env python
"""清掉 out/cache（抽流缓存）：这是项目里唯一会长到几个 GB 的东西。

为什么要有这个脚本：out/cache 里存的是「把一段录像解码成三路 ROI 灰度流」的中间产物
（bar.raw / pause.raw / speed.raw / meta.json）。它是**纯缓存** —— 原始录像还在盘上就
随时能重算，所以随时可以删，删了不丢任何东西。

代价只有时间：删掉之后，下一次读同一段录像（或者跑 check_window / check_bullet_glyph
这类要真录像的自检）会重新抽一次流，几分钟起步。

实测的量级（本机 2026-10-16）：跑完一轮 19 个自检，out/ 从 0.3GB 长到 1.0GB；
历史上攒过 4.9GB。要备份 / 要发 Release / 觉得磁盘吵了，先跑一下这个。

用法：
    python tools/clean_cache.py            # 先看看会删掉什么（不真删）
    python tools/clean_cache.py --yes      # 真删
"""

import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只扫这两个子目录：out/ 下面还有设计图 / 出图产物 / 参考 CSV，那些是证据，不是缓存。
CACHE_DIRS = ('out/cache',)


def walk_size(path: str) -> tuple:
    """返回 (总字节数, 文件数)。"""
    total = 0
    count = 0
    for base, _sub, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
            count += 1
    return total, count


def main() -> int:
    ap = argparse.ArgumentParser(prog='clean_cache.py', description='清掉 out/cache（可再生的抽流缓存）')
    ap.add_argument('--yes', action='store_true', help='真的删（不加这个只是看看）')
    args = ap.parse_args()

    root_real = os.path.realpath(ROOT)
    targets = []
    for rel in CACHE_DIRS:
        p = os.path.join(ROOT, *rel.split('/'))
        if not os.path.isdir(p):
            continue
        # 每个目标先 resolve 一遍，确认落在仓库里 —— 这是递归删，路径必须先核实
        rp = os.path.realpath(p)
        if not rp.startswith(root_real + os.sep) or rp == root_real:
            print('跳过（不在仓库里）：%s' % rp)
            continue
        targets.append(p)

    if not targets:
        print('没有缓存目录，已经干净了')
        return 0

    total = 0
    for p in targets:
        n, cnt = walk_size(p)
        total += n
        print('%-24s %8.1f MB / %d 个文件' % (os.path.relpath(p, ROOT), n / 1048576.0, cnt))

    if not args.yes:
        print('\n合计 %.1f MB。确认要删就加 --yes（删了只是下次读录像慢一点，不会丢数据）'
              % (total / 1048576.0))
        return 0

    for p in targets:
        shutil.rmtree(p, ignore_errors=False)
        print('已删 %s' % os.path.relpath(p, ROOT))
    print('释放 %.1f MB' % (total / 1048576.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
