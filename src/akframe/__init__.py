"""akframe —— 明日方舟全局逻辑帧计数器。

模块划分：
    geometry  按窗口尺寸推算三个 UI 的 ROI
    capture   帧来源（视频 / 图片序列）
    features  逐帧特征提取（费用条 / 暂停键 / 倍速键）
    state     状态机：像素通道 + 时钟通道 -> 全局逻辑帧
    cli       命令行入口

设计说明见 docs/费用条读取逻辑.md。
"""

__version__ = '0.1.0'
