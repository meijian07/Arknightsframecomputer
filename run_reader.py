#!/usr/bin/env python
"""akframe 离线读取器入口。

用法：
    python run_reader.py <视频或图片目录> [-o out/frames.csv]

第一次用前请确认 ffmpeg 能被找到（见 README 的「命令行」那一节）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

from akframe.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
