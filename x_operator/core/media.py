"""配图 / 视频附件：本地存文件，发送时再上传到 X。

为什么不直接存 X 的 media_id：X 的 media_id 只对上传它的那个账号有效，而且大约 24 小时就作废；
一条素材会被不同小号在不同时间反复使用，所以库里存的是本地文件（data/media/ 下的相对路径），
分发器真正发送前才用「这次发送的账号」把文件上传一遍，拿到当次有效的 media_id。

X 的附件规则（回复和主贴一样）：图片、GIF、视频合计最多 4 个，可以混搭（X 2022 年 10 月起支持）。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..db import database

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mov"}
ALL_EXT = IMAGE_EXT | GIF_EXT | VIDEO_EXT

MAX_ITEMS = 4
POOL_MAX_ITEMS = 30   # 定时发帖「附件素材池」最多放多少个文件（每次发帖只随机挑 1 个）
IMAGE_MAX_BYTES = 5 * 1024 * 1024
GIF_MAX_BYTES = 15 * 1024 * 1024
VIDEO_MAX_BYTES = 512 * 1024 * 1024
ACCEPT = ".jpg,.jpeg,.png,.webp,.gif,.mp4,.mov"

KIND_LABEL = {"image": "图片", "gif": "GIF", "video": "视频"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".mp4": "video/mp4", ".mov": "video/quicktime"}

RULE_TEXT = "图片（jpg/png/webp，单张 ≤5MB）、GIF（≤15MB）、视频（mp4/mov，≤512MB）合计最多 4 个，可以混搭。"
POOL_RULE_TEXT = f"图片（jpg/png/webp，单张 ≤5MB）、GIF（≤15MB）、视频（mp4/mov，≤512MB），素材池最多放 {POOL_MAX_ITEMS} 个；每次发帖从里面随机挑 1 个。"


def media_dir() -> Path:
    """data/media/（跟数据库同目录）。"""
    if database._DB_PATH is None:
        raise RuntimeError("数据库尚未初始化")
    d = database._DB_PATH.parent / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parse_files(raw) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in v if x] if isinstance(v, list) else []


def dump_files(files: list[str] | None) -> str:
    return json.dumps(list(files or []), ensure_ascii=False)


def media_kind(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in GIF_EXT:
        return "gif"
    if ext in VIDEO_EXT:
        return "video"
    return None


def mime_for(name: str) -> str:
    return MIME.get(Path(name).suffix.lower(), "application/octet-stream")


def abs_path(rel: str) -> Path:
    return media_dir() / rel


def url_for(rel: str) -> str:
    return "/media/" + rel.replace("\\", "/")


def check_one(name: str, size: int) -> str:
    """单个文件能不能收。返回错误原因，空串 = 可以。"""
    kind = media_kind(name)
    if kind is None:
        return f"不支持的文件类型：{Path(name).suffix or name}（只收 jpg/png/webp/gif/mp4/mov）"
    limit = {"image": IMAGE_MAX_BYTES, "gif": GIF_MAX_BYTES, "video": VIDEO_MAX_BYTES}[kind]
    if size > limit:
        return f"{KIND_LABEL[kind]}太大：{size / 1024 / 1024:.1f}MB，上限 {limit // 1024 // 1024}MB"
    return ""


def check_set(files: list[str], max_items: int = MAX_ITEMS) -> str:
    """一组附件合不合 X 的规则。返回错误原因，空串 = 可以。max_items：素材池模式可以放得比一条推文能带的多。"""
    kinds = [media_kind(f) for f in files]
    if any(k is None for k in kinds):
        return "附件里有不支持的文件类型"
    if len(files) > max_items:
        return f"附件合计最多 {max_items} 个"
    return ""


def can_add(files: list[str], name: str, max_items: int = MAX_ITEMS) -> str:
    """再加一个文件行不行（给上传框用）。"""
    return check_set(list(files) + [name], max_items)


def pick_from_pool(pool: list[str], recent_used: list[str]) -> list[str]:
    """从附件素材池里随机挑 1 个：优先挑最近没用过的；全用过一轮就避开最近一次用的那个再随机。
    recent_used 按「最近的在前」排。池子为空返回 []。"""
    pool = [f for f in (pool or []) if f]
    if not pool:
        return []
    if len(pool) == 1:
        return [pool[0]]
    used = [f for f in recent_used if f in pool]
    fresh = [f for f in pool if f not in used]
    if not fresh:
        fresh = [f for f in pool if f != used[0]] if used else list(pool)
    return [random.choice(fresh)]


def _ext_of(original_name: str) -> str:
    ext = Path(original_name).suffix.lower()
    return ".jpg" if ext == ".jpeg" else ext


def new_rel_path(original_name: str) -> str:
    """生成存放用的相对路径：YYYYMM/uuid.ext。原文件名只保留扩展名，避免奇怪字符。"""
    return f"{datetime.now(timezone.utc):%Y%m}/{uuid.uuid4().hex}{_ext_of(original_name)}"


def new_tmp_path(original_name: str) -> Path:
    """上传先落到 data/media/tmp/ 里（不在正式命名规则内，不会被当成附件），查重后再决定复用还是转正。"""
    d = media_dir() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex}{_ext_of(original_name)}"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_same_content(path: Path) -> str | None:
    """在 data/media/ 里找一个内容完全相同的已有附件（先比大小，大小一样再比 sha256）。返回相对路径，没有返回 None。
    不同场景（素材库 / 回复 / 定时发帖）共用同一个附件目录，同一张图重复上传时直接复用，不再多存一份。"""
    size = path.stat().st_size
    root = media_dir()
    target: str | None = None
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.resolve() == path.resolve():
            continue
        rel = p.relative_to(root).as_posix()
        if not is_safe_rel(rel):
            continue
        try:
            if p.stat().st_size != size:
                continue
        except OSError:
            continue
        if target is None:
            target = file_hash(path)
        if file_hash(p) == target:
            return rel
    return None


def commit_upload(tmp: Path, original_name: str) -> tuple[str, bool]:
    """把临时上传文件转正：内容和已有附件相同 → 删掉临时文件、返回已有的相对路径 (rel, True)；
    否则移到正式位置 YYYYMM/uuid.ext，返回 (rel, False)。"""
    existing = find_same_content(tmp)
    if existing:
        tmp.unlink(missing_ok=True)
        return existing, True
    rel = new_rel_path(original_name)
    dst = abs_path(rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(dst)
    return rel, False


def describe(files: list[str]) -> str:
    """给卡片上显示：「2 张图片」「1 个视频」「2 张图片 + 1 个视频」。"""
    files = list(files or [])
    if not files:
        return ""
    counts: dict[str, int] = {}
    for f in files:
        k = media_kind(f) or "image"
        counts[k] = counts.get(k, 0) + 1
    parts = []
    for k in ("image", "gif", "video"):
        if counts.get(k):
            parts.append(f"{counts[k]} 张图片" if k == "image" else f"{counts[k]} 个{KIND_LABEL[k]}")
    return " + ".join(parts)


def missing(files: list[str]) -> list[str]:
    return [f for f in files if not abs_path(f).is_file()]


def upload_all(client, files: list[str]) -> list[str]:
    """发送前把附件逐个上传到 X，返回 media_id 列表。任何一个失败都抛 MediaError（分发器据此标记失败）。"""
    from ..adapters.base import MediaError
    files = list(files or [])
    if not files:
        return []
    err = check_set(files)
    if err:
        raise MediaError("附件不合规则：" + err)
    lost = missing(files)
    if lost:
        raise MediaError("附件文件已不存在（可能被移动或删除）：" + "、".join(Path(x).name for x in lost)
                         + "。请到这条的「附件」里重新上传")
    ids: list[str] = []
    for rel in files:
        kind = media_kind(rel) or "image"
        ids.append(str(client.upload_media(str(abs_path(rel)), kind)))
    return ids


_SAFE_REL = re.compile(r"^[0-9]{6}/[0-9a-f]{32}\.[a-z0-9]{2,4}$")


def is_safe_rel(rel: str) -> bool:
    return bool(_SAFE_REL.match(rel or ""))


def delete_file(rel: str) -> None:
    """删掉本地文件（只删我们自己生成的路径，防止误删）。"""
    if not is_safe_rel(rel):
        return
    try:
        abs_path(rel).unlink(missing_ok=True)
    except OSError:
        pass


def sweep_orphans() -> int:
    """删除没有任何记录引用的附件文件。返回删除数。"""
    refs = referenced_files()
    n = 0
    root = media_dir()
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if rel not in refs and is_safe_rel(rel):
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
    # 上传中途断掉留下的临时文件（启动时跑，此刻没有正在进行的上传）
    for p in (root / "tmp").glob("*"):
        if p.is_file():
            try:
                p.unlink(); n += 1
            except OSError:
                pass
    return n


# ---------- 占用空间统计 / 让用户自己去目录里清理 ----------

def fmt_size(n: int) -> str:
    """字节数的易读写法：850KB / 12.3MB / 1.2GB。"""
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0" + unit, unit)
    return f"{n}B"


def referenced_files() -> set[str]:
    """库里（素材含回收站、审核队列、定时发帖）还在引用的附件相对路径。"""
    refs: set[str] = set()
    with database.get_conn() as conn:
        for table, col in (("materials", "media_files"), ("review_queue", "final_media_files"), ("scheduled_posts", "media_files")):
            for row in conn.execute(f"SELECT {col} AS f FROM {table} WHERE {col} != '[]'").fetchall():
                refs.update(f.replace("\\", "/") for f in parse_files(row["f"]))
    return refs


def storage_stats() -> dict:
    """data/media/ 的占用：文件数、总大小，以及其中已没有任何素材/条目引用的「孤儿」文件（可以放心删）。"""
    d = media_dir()
    refs = referenced_files()
    total = count = orphan_bytes = 0
    orphans: list[tuple[str, int]] = []
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        size = p.stat().st_size
        count += 1; total += size
        rel = p.relative_to(d).as_posix()
        if rel not in refs:
            orphans.append((rel, size)); orphan_bytes += size
    orphans.sort(key=lambda x: -x[1])
    return {"dir": d, "count": count, "bytes": total, "orphans": orphans, "orphan_bytes": orphan_bytes}


def open_dir() -> str:
    """在本机的文件管理器里打开 data/media/（程序跑在用户自己电脑上，所以能直接弹资源管理器）。返回错误原因，空串 = 已打开。"""
    import subprocess
    import sys
    d = media_dir()
    try:
        if sys.platform.startswith("win"):
            import os
            os.startfile(str(d))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ""
    except Exception as ex:  # noqa: BLE001
        return f"打不开文件管理器：{ex}。请手动打开这个目录：{d}"
