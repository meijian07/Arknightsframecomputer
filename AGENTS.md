# AFC 项目日志与开发约定

> 项目：AFC（Arknights Frame Counter，明日方舟全局逻辑帧计数器）
> 仓库：https://github.com/meijian07/Arknightsframecomputer
> 当前版本：**v1.1.2**（2026-10-27）
> 主分支：`master`
> 平台：Windows 10 / 11，Python 3.9+

这份文件既是项目日志，也是给后续开发者 / AI Agent 的工作说明。改代码前先读
「计数架构」「关键不变量」「已知限制」三节，能避免把已经踩过的坑再踩一遍。

---

## 0. 工作方式

- 用户说「提交 / push / 发布」之前，**不要**自行 `git commit`、`git push` 或创建 Release。
- 用户说「回退」时，先确认回退到哪个提交；不要用 `git reset --hard` 一把清掉工作区。
- 改识别 / 计数逻辑时，先跑对应回归，再实机或录像验证；不要只凭预览图判断。
- 保持改动最小、聚焦当前问题。不要顺手重构无关模块，也不要删除用户没要求删的功能。
- 根目录的 `tmp_*.py`、`out/`、个人校准 JSON、截图、录像、论文文件夹都是本地文件，
  不属于发布内容，提交前用 `git status` 逐个确认。
- 代码注释用中文，解释「为什么」而不是复述「做了什么」。项目里已有的长注释是有原因的，
  多数记录了实测录像、错误现象和失败方案，删改前先读懂。

---

## 1. 项目目标

AFC 是一个**不读内存、不改游戏文件**的画面识别计数器。它截取游戏窗口里的费用条和
右上角 HUD，实时推算明日方舟的逻辑帧号，并把轴文件里的操作提示滚动显示出来。

三个核心需求：

1. **全局帧数计数**：费用条动的时候看费用条；费用条满的时候看挂钟。
2. **打轴器**：导入 Markdown 轴文件，到对应帧显示操作，刻度表随帧号滚动。
3. **多输入源**：支持 PC 端游戏窗口、播放器 / 模拟器窗口；离线录像分析保留在
   `run_reader.py` 和 `video.py` 中，但主 GUI 不再提供「打开录像文件」入口。

非目标：

- 不读取游戏内存、不注入进程、不修改游戏资源。
- 不承诺在所有模拟器 / 所有 UI 缩放下免校准。
- 不把「主菜单 / 仓库 / 结算」等非关卡界面当成关卡计数。

---

## 2. 当前发布状态

| 版本 | 状态 | 说明 |
| --- | --- | --- |
| `v1.1.0` | 已发布 | 计数稳定性修复与 Release 自动发布流程 |
| `v1.11` | 已发布 | 改名 AFC、标志、2X 计数修复 |
| `v1.1.2` | 本版 | 费用条遮罩 / 暗态适配、打轴器字号与横向缩放修复 |

发布资产命名：`AFC-v<版本>-win64.zip`，由 GitHub Actions 在 tag 推送后自动构建并上传。

---

## 3. 技术栈与运行

- Python 3.9+（开发机为 3.13）
- `numpy`、`pillow`（`requirements.txt`）
- Tkinter（Python 自带）
- PyInstaller（仅打包用）
- ffmpeg（仅离线录像链路需要；实时 PC 端用 BitBlt 抓屏，不需要 ffmpeg）

开发运行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run_monitor.py
```

打包：

```powershell
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean akframe.spec
```

产物：`dist\AFC\AFC.exe`。

---

## 4. 目录结构

```text
run_monitor.py              GUI 入口
run_reader.py               离线录像 CLI
build_app.ps1               本地打包脚本
akframe.spec                PyInstaller 配置（onedir）
AGENTS.md                   本文件：项目日志 / 开发约定
README.md                   用户文档

src/akframe/
  ui.py                     主面板、打轴器、二级窗口、手动校准
  live.py                   PC 端实时采样线程
  window.py                 任意窗口输入
  capture.py                输入源抽象 / 抓屏
  win32.py                  BitBlt、置顶、Win32 API
  geometry.py               ROI、分辨率表、布局
  features.py               费用条 / 暂停键 / 倍速键 / 减号特征
  state.py                  逻辑帧状态机、锚点与通道仲裁
  fp24.py                   fp24 定点数与费用相位模型
  synthesis.py              相位 -> 可见宽度曲线
  cost_rate.py              回费效率识别 / 设置
  timeline.py               轴文件解析与提词器
  scale.py                  刻度表布局数学
  scale_render.py           刻度表 PIL 绘制（界面和预览共用）
  boxpick.py                手动框选 / 放大镜
  calib.py / align.py       校准与条槽对齐
  video.py                  录像抽帧 / 缓存
  demo.py                   演示数据
  settings.py               回费效率等设置

templates/                  1X / 2X / 子弹时间模板
maps/                       示例轴
tools/                      自检、校准、预览、诊断
docs/                       实测记录与开发文档
.github/workflows/release.yml  tag -> 打包 -> Release
```

---

## 5. 计数架构

### 5.1 每拍采样

```text
抓屏 / 录像帧
  -> read_bar()      费用条读数（fill / state / grey146 / grey146_core）
  -> read_pause()    暂停键（运行中 / 已暂停 / 未画出）
  -> read_speed()    倍速键（数字 / 三个三角 / 空白）
  -> read_sign()     费用数字左侧减号（局内负费）
  -> Counter.step()  状态机 -> 本拍新增逻辑帧
  -> UI / CSV / 打轴器
```

### 5.2 四种速率

游戏内只有四种会改变**游戏进行速度**的状态：

1. 1X：30 逻辑帧/秒
2. 2X：60 逻辑帧/秒
3. 子弹时间（`>>>`）：固定 6 逻辑帧/秒
4. 拖拽干员的子弹时间：同样固定 6 逻辑帧/秒

关键结论：**这些速率不影响费用条像素通道**。费用条走多少像素就代表多少逻辑帧；
只有「真满费、条被钉住」时才需要用速率当挂钟。不要在正常像素通道里乘倍速。

### 5.3 两条计数通道

**像素通道**（正常推进）

- 用 `fp24` 累加器 + `synthesis` 的相位-宽度曲线，把实测条宽反推成前进了几帧。
- 与 1X / 2X / 子弹时间无关。
- 跨过回费边界时记 `wrap`，并在周期边界做锚点校正。
- 局内负费时每逻辑帧只攒一半（一费 30 -> 60；25% 档 120 -> 240）。

**挂钟通道**（只给真满费）

- 满费时条被游戏钉住，像素里没有位置信息。
- 速率来源：子弹时间固定 6；否则 30 × 倍速数字；都读不到时按「本局读到过数字
  之后的空白 = 拖拽」兜底 6。
- 不足 1 帧的余量留在 `_clock_frac`，跨暂停、跨通道继续攒。

**冻结**

- 不在关卡、手动暂停、条读不到、相位模型不可信时冻结。
- 宁可少记，不凭空补。

### 5.4 费用条状态

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `TRACK` | 正常可见填充，左端白条 + 右侧暗轨道 | 像素通道 |
| `MAX` | 真满费：整条 146 灰 + 中间白色 MAX 字 | 挂钟通道 |
| `MAX_FADE` | 满费退出的 88 灰过渡 | 挂钟 / 冻结 |
| `MAX_COVER` | 条槽右半段被部署网格压成灰，左端白条还在 | 不挂钟；按遮罩处理 |
| `EMPTY` | 条没画出来 | 只有刚回费那一拍记 1，其余冻结 |

**判定满费 / 遮罩时必须用 `grey146_core`（146±3），不要用 `grey146`（146±10）。**
原因是 HUD 被压暗后的正常白条实测是 154，154 落在 146±10 里；用宽容差会把
「暗态正常条」误判成满费，表现就是「部署时数字停住 / 还在挂钟」。

### 5.5 遮罩 / 暗态处理（v1.1.2 重点）

- 部署 / 拖拽时，部署网格会把费用条右半段压成 146 灰，左端白条仍然可见。
- 旧逻辑把它当 `MAX`，冻结或走挂钟；遮罩消失后条从 146px 跳到 176px，又被判成
  `pixel-jump`，整段帧数丢失。
- 现在：
  - `features._left_white_run()` 从条槽左端数白条，允许 2px 起始暗抗锯齿；
  - `grey146_core >= 5%` 且左端有白条 -> `MAX_COVER`；
  - 如果只是整条压暗白 154 / 暗轨道，仍然是 `TRACK`，正常走像素通道，数字连续上涨；
  - `MAX_COVER` 期间按拖拽固定 6 帧/秒推进相位模型，面板实时 +1，遮罩结束只允许
    0~1 帧修正，不再一次性跳 10~20 帧；
  - 暂停键明确读到「已暂停」（`drawn=True`）时仍按暂停冻结，避免暂停菜单的
    HUD 淡入位移被补进来。

### 5.6 暂停键

- 读取形状带（双竖条 = 运行中，单三角 = 已暂停）。
- 相对阈值用 **99 分位**（`rel_threshold(roi, robust=True)`），不要用 `max`：
  部署遮罩下双竖条本体只有 ~80，而 ROI 里一个反光点可能 190+；用 max 会把阈值
  抬到 86，把「运行中」误判成「没画出来」。
- 倍速键不要用 robust 阈值：实测会让「有数字」多认 165 帧，压掉子弹时间三角。

### 5.7 锚点

- **满费锚点**：MAX 上升沿对齐到「上次满费 + n 个完整周期」，只在偏差很小时拨。
- **周期锚点**：每次条满 -> 条空回卷，把累计帧钉回 `上次边界 + frames_per_cost`，
  防止几十秒后跨周期漂移。
- **首费起锚**：30 帧档容差 ±4，60 帧 ±7，120 帧 ±15；25% 从本费第 11 帧启动时
  补平到 120 帧。
- 正负费混合周期不做「硬掰回整周期」。

---

## 6. 打轴器

- 轴文件是 Markdown：帧号（或 `1:23` 时间）+ 文本，解析在 `timeline.py`。
- `Teleprompter` 根据当前帧给出 `next / past / active`；刻度表由 `scale.py` 布局、
  `scale_render.py` 用 PIL 画成一张图，再贴到 Canvas。
- 刻度跨度固定 150 帧（5 费），指针居中，整条刻度向上滚。
- 字号（2026-10-27）：数字 / 操作 / 说明统一 **22px**（从 26 调小一级）。
- 无空格操作（`部署银灰`、`开银灰技能`）必须能用满整行；旧代码强制 42% 宽度，
  必然截成 `部署…`。有空格时才按动作 / 内容分栏。
- 横向缩放：Canvas 宽度立刻变，但 PIL 图是上一拍画的。宽度变化时按 8px 节流
  重画，松手再强制重画一次，避免右侧露出画布底色（用户说的「黑块」）。
- 界面不注册键盘快捷键，避免和游戏内按键冲突。

---

## 7. 输入源与校准

- **PC 端**：按客户区尺寸 + UI 缩放推算 ROI；可直接读取 `Arknights.exe`。
- **窗口**：读取任意可见窗口（播放器 / 模拟器）；窗口缩放、黑边、播放器拉伸都可能
  需要手动校准。
- 手动校准：条槽左右边界必须贴住白条起点 / 条槽右端；暂停键 / 倍速键按虚影对齐。
- 费用条对准后，负号扫描框按条槽位置自动推算，不需要单独框。
- 校准结果按「进程名 + 客户区尺寸 / 画面尺寸」归档；个人校准 JSON 不提交。

---

## 8. 开发与自检

### 8.1 常用检查

```powershell
$env:PYTHONIOENCODING='utf-8'

.venv\Scripts\python.exe tools\check_state_synth.py
.venv\Scripts\python.exe tools\check_start_anchor.py
.venv\Scripts\python.exe tools\check_cycle_anchor.py
.venv\Scripts\python.exe tools\check_full_anchor.py
.venv\Scripts\python.exe tools\check_cost_rate.py
.venv\Scripts\python.exe tools\check_sign_box.py
.venv\Scripts\python.exe tools\check_features_scale.py
.venv\Scripts\python.exe tools\check_bullet_glyph.py
.venv\Scripts\python.exe tools\check_speed_mix.py
.venv\Scripts\python.exe tools\check_video.py
.venv\Scripts\python.exe tools\check_scale.py
.venv\Scripts\python.exe tools\check_ui_keys.py
.venv\Scripts\python.exe tools\check_ui_window.py
```

### 8.2 回归关注点

- 费用条一费 30 帧（100%）、40 帧（75%）、60 帧（50%）、120 帧（25%）。
- 局内负费一费翻倍（30 -> 60，120 -> 240）。
- 2X / 子弹时间 / 拖拽子弹时间不改变「一费多少逻辑帧」。
- 暂停期间不凭空涨帧；满费帧操的不足 1 帧余量要保留。
- 部署 / 拖拽遮罩期间不挂钟、不丢帧、数字连续上涨。
- 打轴器字号、无空格操作完整显示、横向缩放不露黑块。

### 8.3 环境注意

- 控制台是 GBK 时，带 emoji 的自检脚本会 `UnicodeEncodeError`；先设
  `$env:PYTHONIOENCODING='utf-8'`。
- `check_live.py` 需要 `out/fake_log.txt`，没有就跳过，不是代码失败。
- 打包后 Windows 可能缓存旧任务栏图标；必要时重启资源管理器或重新登录。

---

## 9. 发布流程

1. 更新 `src/akframe/__init__.py` 的 `__version__`。
2. 更新 `AGENTS.md` / `README.md` 的版本与变更说明。
3. 跑第 8 节的相关回归。
4. 本地打包并冒烟启动：

```powershell
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean akframe.spec
```

5. 只提交本次相关文件（源码、测试、文档、`assets/logo/afc/`），不要提交 `tmp_*.py`、
   `out/`、个人校准 JSON、旧 logo 备选目录。
6. 推送 master，再推 tag：

```powershell
git push origin master
git tag -a v1.1.2 -m "AFC v1.1.2"
git push origin v1.1.2
```

7. `.github/workflows/release.yml` 会在 tag 推送后构建 Windows 包并创建 Release。
   资产名：`AFC-v1.1.2-win64.zip`。
8. 检查 Release 页面资产是否上传成功；如果 Actions 失败，再手动本地打包并上传。

---

## 10. 版本日志

### v1.1.2（2026-10-27）

- 修复部署 / 拖拽遮罩被误判成满费：新增 `MAX_COVER`，用 `grey146_core`（146±3）
  和左端白条区分真满费 / 遮罩 / 暗态正常条。
- 修复「数字会停但实际记到了」：遮罩期间按 6 帧/秒实时推进，遮罩结束不再跳帧。
- 修复暗态暂停键读不到：暂停键相对阈值改用 99 分位，避免单个反光点抬阈值。
- 修复暗态白条被 `146±10` 误判成满费：引入核心灰判定；154 的压暗白条走 `TRACK`。
- 修复 `_left_white_run` 不认条槽左端 1px 暗抗锯齿，导致整段暗态白条读成 0。
- 打轴器字号 26 -> 22；无空格操作使用整行宽度，`部署银灰` 不再截成 `部署…`。
- 打轴器横向缩放：Canvas / PhotoImage / ScaleGeom 同步，拖动按 8px 节流重画，
  松手强制对齐，修复右侧黑块。
- 任务栏小尺寸图标改为像素级简化标（16–48px），64+ 保留完整标志。

### v1.11（2026-10-25）

- 程序更名为 AFC，新增 AFC 标志与 Windows 图标。
- 修复 2X 下减号框压到数字横腰导致误判负费、整段多算一个周期。
- 修复 2X 下大数字刷新晚约 2 帧。
- 修复视频补帧虚影被算进费用条填充。
- 任务栏图标小尺寸重绘。

### v1.1.0（2026-10-11 前后）

- 计数稳定性修复、启动起锚 / 周期 / 满费锚点、布局边界回归。
- 移除仓库内 v1.0 压缩包，改由 GitHub Releases 分发。
- 加入 Release 自动构建流程。

### 更早

- 从 ArknightsCostBarRuler 的校准 / 相位重同步思路起步。
- 逐步形成：像素通道 + 满费挂钟、暂停 / 倍速 / HUD 关卡判定、负费减半、
  回费效率档位、窗口输入、手动校准、打轴器。

---

## 11. 关键不变量（改代码前务必确认）

1. **费用条未满时，绝对不要按倍速乘帧。** 像素通道天然免疫倍速；乘了就会出
   「一费 62 帧 / 212 帧变 372 帧」这类系统性错误。
2. **不要用 `grey146`（±10）判满费。** 用 `grey146_core`（±3）；154 是暗态白条。
3. **不要把 `MAX_COVER` 当 `MAX`。** 遮罩期间条还在走，挂钟会凭空涨帧。
4. **不要在暂停期间保留追补窗口**，除非暂停键明确读不出来且条确实在遮罩里走。
5. **像素通道的余量不要随便清零。** 每次换通道丢一次，几十次就会少几帧。
6. **周期性锚点只修小偏差。** 中途被机制停条 / 切场景的周期不硬掰。
7. **界面不绑键盘快捷键。** 游戏内有快捷键，会冲突。
8. **个人校准 / 录像 / 截图 / tmp 脚本不提交。**

---

## 12. 已知限制 / TODO

- 模拟器帧率不稳、补帧、压缩糊边时仍可能漏采 / 误读。
- 第 15 章等特殊机制会让费用条停止；目前只保证不凭空补帧，没有单独识别该机制。
- 萨卡兹术士结晶锁定费用条尚未单独处理。
- 鼠标划过费用条仍可能影响读数；操作时尽量避开。
- 回费效率需要手动选择；自动识别是可选功能，默认不用。
- 满费挂钟仍有采样量化误差；长期漂移靠满费 / 周期锚点拉回。
- 打轴器目前是固定 150 帧跨度；如需更多跨度，先确认交互设计。

---

## 13. 发布前检查表

- [ ] `__version__` 与 tag 一致。
- [ ] `git status` 里没有 tmp / out / 个人校准 / 旧 logo 目录。
- [ ] `check_state_synth.py`、`check_features_scale.py`、`check_scale.py`、
      `check_ui_keys.py`、`check_speed_mix.py` 通过。
- [ ] 本地 exe 能启动，面板 / 打轴器 / 手动校准入口正常。
- [ ] 部署遮罩段数字连续上涨，遮罩结束不跳。
- [ ] 打轴器横向拖动不露黑块，`部署银灰` 完整显示。
- [ ] 推送 master 和 tag 后，GitHub Actions 成功，Release 资产存在。

