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
from dataclasses import dataclass
from typing import Iterator

import numpy as np

from .geometry import Layout, layout_for

ROI_NAMES = ('bar', 'pause', 'speed')
FFMPEG_LABELS = {'bar': 'b', 'pause': 'p', 'speed': 's'}

_FPS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*fps')
_SIZE_RE = re.compile(r'Video:.*?(\d{2,5})x(\d{2,5})')
_DUR_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')


@dataclass
class Frame:
    """一帧的三个 ROI 灰度图。"""
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


def find_ffmpeg(explicit: str | None = None) -> str:
    """找 ffmpeg：显式参数 > 环境变量 AKFRAME_FFMPEG > PATH。"""
    if explicit:
        if not os.path.isfile(explicit):
            raise FileNotFoundError('指定的 ffmpeg 不存在：%s' % explicit)
        return explicit
    env = os.environ.get('AKFRAME_FFMPEG')
    if env and os.path.isfile(env):
        return env
    found = shutil.which('ffmpeg')
    if found:
        return found
    raise FileNotFoundError(
        '找不到 ffmpeg。请把它加到 PATH，或设环境变量 AKFRAME_FFMPEG，'
        '或用 --ffmpeg 指定完整路径。')


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


def extract(ffmpeg: str, video: str, layout: Layout, outdir: str, force: bool = False) -> dict:
    """抽出三路 ROI 原始灰度流，返回 meta。已抽过且 ROI 一致则直接复用缓存。"""
    os.makedirs(outdir, exist_ok=True)
    meta_path = os.path.join(outdir, 'meta.json')

    if not force and os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding='utf-8') as fh:
                old = json.load(fh)
            same_roi = old.get('layout') == layout.to_dict()
            have_all = all(os.path.isfile(os.path.join(outdir, n + '.raw')) for n in ROI_NAMES)
            if same_roi and have_all:
                return old
        except (OSError, ValueError):
            pass  # 缓存坏了就重抽

    # 先 format=gray 再 crop：见 Rect.crop_filter 的说明（奇数尺寸会被 yuv420p 取偶改掉）
    graph = ';'.join(
        '[0:v]format=gray,%s[%s]' % (getattr(layout, n).crop_filter(), FFMPEG_LABELS[n])
        for n in ROI_NAMES)
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', video,
           '-filter_complex', graph]
    for n in ROI_NAMES:
        cmd += ['-map', '[%s]' % FFMPEG_LABELS[n], '-f', 'rawvideo', '-pix_fmt', 'gray',
                '-y', os.path.join(outdir, n + '.raw')]
    subprocess.run(cmd, check=True)

    frame_bytes = {n: getattr(layout, n).pixels for n in ROI_NAMES}
    counts = {}
    for n in ROI_NAMES:
        size = os.path.getsize(os.path.join(outdir, n + '.raw'))
        fsize = frame_bytes[n]
        if size % fsize != 0:
            raise RuntimeError('%s.raw 大小 %d 不是单帧字节 %d 的整数倍，抽流异常'
                               % (n, size, fsize))
        counts[n] = size // fsize
    n_frames = min(counts.values())
    if len(set(counts.values())) > 1:
        raise RuntimeError('三路 ROI 帧数不一致：%s' % counts)

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
    handles = {n: open(os.path.join(outdir, n + '.raw'), 'rb') for n in ROI_NAMES}
    try:
        for i in range(int(meta['frames'])):
            imgs = {}
            truncated = False
            for n in ROI_NAMES:
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
               ui_scaler: float | None = None) -> tuple:
    """准备视频输入，返回 (layout, fps, 帧迭代器)。

    ui_scaler 是游戏内「UI 缩放」；None = 用默认档 1.0（不改变 ROI）。
    """
    # 缓存命中时连 ffmpeg 都不需要（抽流是一次性成本，之后只读 .raw）
    if not force and size is None:
        cached = _valid_cache(outdir, path)
        if cached is not None:
            return Layout.from_dict(cached['layout']), float(cached['fps']), iter_cached(outdir)

    exe = find_ffmpeg(ffmpeg)
    if size is None:
        info = probe_video(exe, path)
        if info.size is None:
            raise RuntimeError('无法从视频里读出分辨率，请用 --size WxH 手动指定')
        size = info.size
    layout = layout_for(size[0], size[1], ui_scaler if ui_scaler is not None else 1.0)
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
    if not all(os.path.isfile(os.path.join(outdir, n + '.raw')) for n in ROI_NAMES):
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
