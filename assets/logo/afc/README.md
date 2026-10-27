# AFC — Arknights Frame Counter

## 主标：Frame A

`AFC` 的第一个字母 **A** 同时是画面里的“当前帧”：

- 四角取景框：画面识别与校准
- 橙色播放头：当前帧指针
- 底部 30 格刻度：明日方舟的 30 逻辑帧 / 秒
- 深炭黑底 + 纯白 + Arknights 费用条橙：与计数器面板、游戏 HUD 保持一致

主标优先使用 **A + 取景框 + 播放头**，在小尺寸（16–32px）下仍能读出字母 A；
30 格刻度在大尺寸和字标里承担“计数器”的语义。

## 文件

| 文件 | 用途 |
| --- | --- |
| `afc-icon.svg` | 主图标，深色圆角方形底 |
| `afc-icon-mono-white.svg` | 透明底白色单色版，用于深色背景 |
| `afc-icon-mono-black.svg` | 透明底黑色单色版，用于浅色背景 |
| `afc.ico` | Windows 多尺寸图标（16–48 简化标 + 64 / 128 / 256 完整标） |
| `make_ico.py` | 重新生成 `afc.ico`（小尺寸简化标 + 大尺寸完整标） |
| `afc-lockup-horizontal.svg` | 横向组合标（图标 + AFC + 英文全称） |
| `afc-lockup-vertical.svg` | 纵向组合标 |
| `afc-wordmark.svg` | 纯字标 + 30 帧刻度 |
| `afc-concept-b-dial.svg` | 备选 B：30 帧表盘 |
| `afc-concept-c-timecode.svg` | 备选 C：Timecode 横幅 |
| `preview/` | 1024 / 1920 宽的 PNG 预览与尺寸测试 |

## 色彩

| 名称 | 色值 | 用途 |
| --- | --- | --- |
| AFC Black | `#151517` | 面板 / 图标底色 |
| AFC Ink | `#0B0C0F` | 图标渐变暗端 |
| AFC White | `#F7F8FA` | 字母、取景框、主文字 |
| Playhead Orange | `#FF9F1C` | 播放头、当前刻度 |
| Muted Gray | `#8B919B` | 次要文字、普通刻度 |
| Border | `#2B2F36` | 圆角方形描边、分隔线 |

## 使用

- **exe / 任务栏 / 头像**：`afc.ico` 或 `afc-icon.svg`
- **README / 网站页头**：`afc-lockup-horizontal.svg`
- **社交媒体头像 / 竖版海报**：`afc-lockup-vertical.svg`
- **只需要文字**：`afc-wordmark.svg`
- **浅色背景**：`afc-icon-mono-black.svg`
- **深色背景**：`afc-icon.svg` 或 `afc-icon-mono-white.svg`

## 最小尺寸

- 图标：数字场景不小于 **16px**，推荐 24px 以上
- 横向组合标：不小于 **240px** 宽
- 纵向组合标：不小于 **160px** 宽

## 小尺寸图标

任务栏在 150% DPI 下取 24/32px。完整标志（取景框 + 30 刻度 + 播放头）
直接缩到这么小会糊成一团，所以 `afc.ico` 里：

- **16–48px**：像素级简化标，只保留深色圆角方块 + 粗 A + 橙色播放头
- **64–256px**：完整标志（四角取景框 + 30 刻度）
- ICO 包含 16 / 20 / 24 / 28 / 32 / 36 / 40 / 48 / 64 / 128 / 256 十一档，
  避免 Windows 从邻近尺寸拉伸

改完标志后运行：

```bash
python assets/logo/afc/make_ico.py
```

## 留白

组合标四周至少留出 **图标高度的 1/2** 作为空白；
图标本体内部不要再叠加文字、描边或阴影。

## 不要

- 不要拉伸、压扁或旋转标志
- 不要改变播放头的橙色
- 不要在图标上叠加额外文字
- 不要把 30 格刻度单独拆出来当装饰
- 不要在低对比度背景上使用白色单色版

## 字体

组合标里的 `AFC` 和英文全称使用 Bahnschrift / Arial Narrow / Arial 回退链。
如果要交给印刷或第三方使用，建议在 Illustrator / Inkscape 里把文字转曲后再导出。

## 预览

- `preview/afc-icon-1024.png`
- `preview/afc-icon-small-sizes.png`
- `preview/afc-icon-size-test.png`
- `preview/afc-lockup-horizontal-dark.png`
- `preview/afc-lockup-vertical-dark.png`
- `preview/afc-wordmark-dark.png`
- `preview/afc-concepts-contact.png`
