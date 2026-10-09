"""帧来源：视频文件 / 图片序列。

视频走 ffmpeg：**一次调用同时抽出三路原始灰度流**，写进缓存目录，之后逐帧读缓存。
这样算法迭代时不用反复解码（本例 56 秒的视频解码一次约 40 秒，抽一次能重复用很久）。

缓存目录里有：
    bar.raw / pause.raw / speed.raw   逐帧灰度原始数据
    meta.json                          帧率、帧数、ROI 定义
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Iterator

import numpy as np

from .geometry import (Layout, Rect, bar_only_layout, layout_for,
                       reframe_layout)
from .paths import app_dir

ROI_NAMES = ('bar', 'pause', 'speed')
FFMPEG_LABELS = {'bar': 'b', 'pause': 'p', 'speed': 's'}


def roi_names(layout) -> tuple:
    """这套 layout 实际要抽哪几路 ROI。"""
    return ROI_NAMES

_FPS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*fps')
_SIZE_RE = re.compile(r'Video:.*?(\d{2,5})x(\d{2,5})')
_DUR_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')


@dataclass
class Frame:
    """一帧的几个 ROI 灰度图。"""
    index: int
    t: float
    bar: np.ndarray
    pause: np.ndarray
    speed: np.ndarray


@dataclass
class VideoInfo:
    fps: float | None
    size: tuple | None
    duration: float | None


def _ffmpeg_candidates():
    """ffmpeg 可能待的地方（按「越可能是本机装的那个」排序）。

    为什么要猜这几个目录：这台机器上唯一一份 ffmpeg 是**哔哩哔哩客户端自带的**
    （%APPDATA%\\bilibili\\ffmpeg\\ffmpeg.exe），既不在 PATH 里，用户也不知道它在那儿。
    只认 PATH 的话，「打开录像」会直接报「找不到 ffmpeg」—— 而这跟用户做的事毫无关系。
    """
    home = os.path.expanduser('~')
    appdata = os.environ.get('APPDATA') or os.path.join(home, 'AppData', 'Roaming')
    local = os.environ.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local')
    pf = os.environ.get('ProgramFiles') or r'C:\Program Files'
    rel = ('ffmpeg.exe', os.path.join('bin', 'ffmpeg.exe'),
           os.path.join('ffmpeg', 'ffmpeg.exe'),
           os.path.join('ffmpeg', 'bin', 'ffmpeg.exe'))
    roots = [
        app_dir(),                                  # 放在程序旁边（打包后跟着 exe）
        os.path.join(appdata, 'bilibili', 'ffmpeg'),
        os.path.join(local, 'Microsoft', 'WinGet', 'Links'),
        os.path.join(pf, 'ffmpeg'),
        os.path.join(home, 'scoop', 'shims'),
        os.path.join(local, 'Programs', 'ffmpeg'),
    ]
    for root in roots:
        for r in rel:
            yield os.path.join(root, r)


def find_ffmpeg(explicit: str | None = None) -> str:
    """找 ffmpeg：显式参数 > 环境变量 AKFRAME_FFMPEG > 常见安装位置 > PATH。"""
    if explicit:
        if not os.path.isfile(explicit):
            raise FileNotFoundError('指定的 ffmpeg 不存在：%s' % explicit)
        return explicit
    env = os.environ.get('AKFRAME_FFMPEG')
    if env and os.path.isfile(env):
        return env
    for cand in _ffmpeg_candidates():
        if os.path.isfile(cand):
            return cand
    found = shutil.which('ffmpeg')
    if found:
        return found
    raise FileNotFoundError(
        '找不到 ffmpeg（录像输入要用它把视频解码成三路 ROI 灰度流）。'
        '三个办法任选：把 ffmpeg.exe 放到程序旁边；设环境变量 AKFRAME_FFMPEG=完整路径；'
        '或把它加到 PATH。已经找过：程序目录、%APPDATA%\\bilibili\\ffmpeg、'
        'winget / scoop 的 shims、C:\\Program Files\\ffmpeg、PATH。')


def probe_video(ffmpeg: str, path: str) -> VideoInfo:
    """跑一次 ffmpeg -i 从 stderr 里抓帧率 / 分辨率 / 时长。"""
    proc = subprocess.run([ffmpeg, '-hide_banner', '-i', path],
                          capture_output=True)
    text = proc.stderr.decode('utf-8', 'replace')
    fps = None
    m = _FPS_RE.search(text)
    if m:
        fps = float(m.group(1))
    size = None
    m = _SIZE_RE.search(text)
    if m:
        size = (int(m.group(1)), int(m.group(2)))
    duration = None
    m = _DUR_RE.search(text)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return VideoInfo(fps=fps, size=size, duration=duration)


def extract(ffmpeg: str, video: str, layout: Layout, outdir: str, force: bool = False,
            progress=None, total_frames: int = None) -> dict:
    """抽出三路 ROI 原始灰度流，返回 meta。已抽过且 ROI 一致则直接复用缓存。

    progress     进度回调 progress(frac, done_bytes, total_bytes)。frac = 0..1，
                 总数不知道时给 -1。抽一段几十秒的录像要几十秒，界面必须能显示它在动。
    total_frames 预期帧数（界面拿 duration x fps 估的），只用来把进度换算成百分比。
    """
    os.makedirs(outdir, exist_ok=True)
    meta_path = os.path.join(outdir, 'meta.json')

    if not force and os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding='utf-8') as fh:
                old = json.load(fh)
            same_roi = old.get('layout') == layout.to_dict()
            have_all = all(os.path.isfile(os.path.join(outdir, n + '.raw'))
                           for n in roi_names(layout))
            if same_roi and have_all:
                return old
        except (OSError, ValueError):
            pass  # 缓存坏了就重抽

    # 先 format=gray 再 crop：见 Rect.crop_filter 的说明（奇数尺寸会被 yuv420p 取偶改掉）
    names = roi_names(layout)
    graph = ';'.join(
        '[0:v]format=gray,%s[%s]' % (getattr(layout, n).crop_filter(), FFMPEG_LABELS[n])
        for n in names)
    # -vsync 0（passthrough）绝不能省：原生 120 tbr 的 HEVC 录像（NVIDIA HDR 采集）
    # 在默认的 cfr 模式下会被 ffmpeg 按「输出帧率 = tbr」补帧，1197 帧的 19.95 秒
    # 录像被抽成 2393 帧 —— 帧数翻倍、时间轴减半，挂钟通道直接按 2 倍速走。
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', video,
           '-filter_complex', graph, '-vsync', '0']
    for n in names:
        cmd += ['-map', '[%s]' % FFMPEG_LABELS[n], '-f', 'rawvideo', '-pix_fmt', 'gray',
                '-y', os.path.join(outdir, n + '.raw')]
    if progress is None:
        subprocess.run(cmd, check=True)
    else:
        _run_with_progress(cmd, outdir, names, layout, total_frames, progress)

    frame_bytes = {n: getattr(layout, n).pixels for n in names}
    counts = {}
    for n in names:
        size = os.path.getsize(os.path.join(outdir, n + '.raw'))
        fsize = frame_bytes[n]
        if size % fsize != 0:
            raise RuntimeError('%s.raw 大小 %d 不是单帧字节 %d 的整数倍，抽流异常'
                               % (n, size, fsize))
        counts[n] = size // fsize
    n_frames = min(counts.values())
    if len(set(counts.values())) > 1:
        raise RuntimeError('各 ROI 帧数不一致：%s' % counts)

    meta = {
        'video': os.path.abspath(video),
        'fps': float(_probe_fps_or(ffmpeg, video)),
        'layout': layout.to_dict(),
        'frames': int(n_frames),
        'frame_bytes': frame_bytes,
    }
    with open(meta_path, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return meta


def _run_with_progress(cmd, outdir, names, layout, total_frames, progress) -> None:
    """跑 ffmpeg 并汇报进度。

    进度用**输出文件已经写出来的字节数**估：rawvideo 不压缩、ffmpeg 又是顺序按帧写，
    所以「已写字节 / 单帧字节 = 已抽帧数」，除以预期帧数就是百分比。比解析 ffmpeg 的
    -progress 输出简单，而且不用管三路输出（三个文件）之间的对齐。
    stderr 转向文件而不是管道：一是避免管道写满 64KB 把 ffmpeg 堵死（那就永远不结束了），
    二是失败时能把原因读出来，报给面板/控制台 —— 打包后没有控制台，不报出来就查不了。
    """
    err_path = os.path.join(outdir, 'ffmpeg.log')
    per_frame = sum(getattr(layout, n).pixels for n in names)
    total = float(per_frame) * float(total_frames) if total_frames else 0.0
    outs = [os.path.join(outdir, n + '.raw') for n in names]
    with open(err_path, 'wb') as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
        try:
            while True:
                # 文件还没建出来是正常的（ffmpeg 先解码、后写）：不算进度就行
                done = sum(os.path.getsize(p) for p in outs if os.path.isfile(p))
                progress(min(1.0, done / total) if total > 0 else -1.0, done, total)
                if proc.poll() is not None:
                    break
                time.sleep(0.15)
        except BaseException:                    # 界面取消 / 进程被杀：别留下孤儿 ffmpeg
            proc.kill()
            proc.wait()
            raise
    if proc.returncode != 0:
        raise RuntimeError('ffmpeg 抽流失败（exit %s）：%s'
                           % (proc.returncode, _tail(err_path)))


def _tail(path: str, lines: int = 4) -> str:
    """读日志尾巴（失败时给人看的那几行）。"""
    try:
        with open(path, 'rb') as fh:
            text = fh.read().decode('utf-8', 'replace')
    except OSError:
        return '(读不到日志)'
    text = text.strip()
    if not text:
        return '(ffmpeg 没有输出任何错误信息)'
    return ' | '.join(text.splitlines()[-lines:])


def _probe_fps_or(ffmpeg: str, video: str) -> float:
    info = probe_video(ffmpeg, video)
    if info.fps and info.fps > 0:
        return info.fps
    return 60.0


def load_meta(outdir: str) -> dict:
    with open(os.path.join(outdir, 'meta.json'), encoding='utf-8') as fh:
        return json.load(fh)


def iter_cached(outdir: str) -> Iterator[Frame]:
    """逐帧读缓存。帧数按 meta 里的 frames，时间戳按 CFR 假设算。"""
    meta = load_meta(outdir)
    layout = Layout.from_dict(meta['layout'])
    fps = float(meta['fps'])
    names = roi_names(layout)
    handles = {n: open(os.path.join(outdir, n + '.raw'), 'rb') for n in names}
    try:
        for i in range(int(meta['frames'])):
            imgs = {}
            truncated = False
            for n in names:
                rect = getattr(layout, n)
                buf = handles[n].read(rect.pixels)
                if len(buf) < rect.pixels:
                    truncated = True
                    break
                imgs[n] = np.frombuffer(buf, dtype=np.uint8).reshape(rect.h, rect.w)
            if truncated:
                break
            yield Frame(index=i, t=i / fps,
                        bar=imgs['bar'], pause=imgs['pause'], speed=imgs['speed'])
    finally:
        for h in handles.values():
            h.close()


def open_video(path: str, outdir: str, ffmpeg: str | None = None,
               force: bool = False, size: tuple | None = None,
               ui_scaler: float | None = None,
               client_size: tuple | None = None,
               offset: tuple | None = None,
               bar_box: tuple | None = None,
               layout: Layout | None = None) -> tuple:
    """准备视频输入，返回 (layout, fps, 帧迭代器)。

    ui_scaler   游戏内「UI 缩放」；None = 用默认档 1.0（不改变 ROI）。
    client_size 客户区尺寸。录制整个桌面（画面里含任务栏 / 窗口标题栏）时，ROI 要按
                客户区尺寸算，而不是按视频尺寸 —— 否则条的位置整个偏掉。
    offset      客户区原点在视频坐标里的位置 (x, y)，配合 client_size 用。
    bar_box     直接给出费用条槽在画面里的像素框 (x, y, w, h)。画面被放大 / 裁剪过、
                几何推不出来时用，见 geometry.bar_only_layout。
    layout      整套 ROI 直接给全（比 bar_box 更底层，主要给测试用）。
    """
    # 缓存命中时连 ffmpeg 都不需要（抽流是一次性成本，之后只读 .raw）
    # 缓存命中时连 ffmpeg 都不需要 —— 但只有「按视频尺寸算 ROI」这一路才敢直接复用
    # （给了 client_size / offset 说明画面里有别的东西，layout 得重新算）
    if (not force and size is None and client_size is None and offset is None
            and bar_box is None and layout is None):
        cached = _valid_cache(outdir, path)
        if cached is not None:
            return Layout.from_dict(cached['layout']), float(cached['fps']), iter_cached(outdir)

    exe = find_ffmpeg(ffmpeg)
    if size is None:
        info = probe_video(exe, path)
        if info.size is None:
            raise RuntimeError('无法从视频里读出分辨率，请用 --size WxH 手动指定')
        size = info.size
    scaler = ui_scaler if ui_scaler is not None else 1.0
    if bar_box is not None:
        layout = bar_only_layout(size[0], size[1], Rect(*[int(v) for v in bar_box]))
    elif layout is None:
        cw, ch = client_size if client_size else size
        layout = layout_for(cw, ch, scaler)
        if offset:
            layout = reframe_layout(layout, int(offset[0]), int(offset[1]), size[0], size[1])
    meta = extract(exe, path, layout, outdir, force=force)
    return layout, float(meta['fps']), iter_cached(outdir)


def _valid_cache(outdir: str, video: str) -> dict | None:
    """缓存可用（meta 存在、指向同一个视频、三路 raw 都在）则返回 meta，否则 None。"""
    meta_path = os.path.join(outdir, 'meta.json')
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, encoding='utf-8') as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    if meta.get('video') != os.path.abspath(video):
        return None
    cached_layout = Layout.from_dict(meta['layout'])
    if not all(os.path.isfile(os.path.join(outdir, n + '.raw'))
               for n in roi_names(cached_layout)):
        return None
    return meta


def open_images(paths, fps: float = 60.0, ui_scaler: float | None = None) -> tuple:
    """准备图片序列输入，返回 (layout, fps, 帧迭代器)。第一张图决定 ROI。"""
    if not paths:
        raise ValueError('图片列表为空')
    from PIL import Image
    first = np.asarray(Image.open(paths[0]).convert('L'))
    layout = layout_for(int(first.shape[1]), int(first.shape[0]),
                        ui_scaler if ui_scaler is not None else 1.0)
    return layout, float(fps), _iter_images(paths, layout, float(fps))


def _iter_images(paths, layout: Layout, fps: float) -> Iterator[Frame]:
    from PIL import Image
    for i, p in enumerate(paths):
        img = np.asarray(Image.open(p).convert('L'))
        if img.shape[1] != layout.width or img.shape[0] != layout.height:
            raise ValueError('第 %d 张图尺寸 %dx%d 与首张 %dx%d 不一致'
                             % (i, img.shape[1], img.shape[0], layout.width, layout.height))
        def cut(r):
            return img[r.y:r.y2 + 1, r.x:r.x2 + 1].copy()
        yield Frame(index=i, t=i / fps,
                    bar=cut(layout.bar), pause=cut(layout.pause), speed=cut(layout.speed))
