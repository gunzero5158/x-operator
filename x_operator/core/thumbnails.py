"""视频预览帧：按文件版本缓存，独立于上传和发送使用的原始附件。"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from threading import BoundedSemaphore

import imageio_ffmpeg

from . import media

log = logging.getLogger(__name__)
_workers = BoundedSemaphore(2)
_failed: dict[str, float] = {}


def thumbnail_url(rel: str) -> str:
    """版本参数让替换过的本地视频不会继续显示浏览器中的旧预览。"""
    try:
        stat = media.abs_path(rel).stat()
    except OSError:
        return f"/media-thumbnail/{rel}"
    return f"/media-thumbnail/{rel}?v={stat.st_mtime_ns}-{stat.st_size}"


def video_thumbnail(rel: str) -> Path | None:
    """由请求线程调用；最多两个转码进程，失败短暂记忆，避免坏文件反复占用 CPU。"""
    if not media.is_safe_rel(rel) or media.media_kind(rel) != "video":
        return None
    root = media.media_dir().resolve()
    source = media.abs_path(rel).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return None
    stat = source.stat()
    key = hashlib.sha256(f"v1:{rel}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    cache = root.parent / "thumbnails"
    target = cache / f"{key}.jpg"
    if target.is_file():
        return target
    with _workers:
        if target.is_file():
            return target
        if time.monotonic() - _failed.get(key, float('-inf')) < 300:
            return None
        cache.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            with tempfile.NamedTemporaryFile(dir=cache, suffix=".jpg", delete=False) as file:
                temporary = Path(file.name)
            # 通常跳过开头的黑场；不足一秒的短视频退回首帧。
            for seek in (1, 0):
                result = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-protocol_whitelist", "file,pipe", "-ss", str(seek), "-threads", "1",
                     "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
                     "-vf", "thumbnail=12,scale=480:320:force_original_aspect_ratio=decrease",
                     "-frames:v", "1", "-threads", "1", "-q:v", "3", str(temporary)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0 and temporary.stat().st_size:
                    temporary.replace(target)
                    _failed.pop(key, None)
                    return target
            log.warning("视频缩略图生成失败：%s", rel)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            log.warning("视频缩略图生成失败：%s", rel, exc_info=True)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if len(_failed) >= 256:
            _failed.pop(next(iter(_failed)))
        _failed[key] = time.monotonic()
    return None
