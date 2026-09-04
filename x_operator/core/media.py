"""配图 / 视频附件：本地存文件，发送时再上传到 X。

为什么不直接存 X 的 media_id：X 的 media_id 只对上传它的那个账号有效，而且大约 24 小时就作废；
一条素材会被不同小号在不同时间反复使用，所以库里存的是本地文件（data/media/ 下的相对路径），
分发器真正发送前才用「这次发送的账号」把文件上传一遍，拿到当次有效的 media_id。

X 的附件规则（回复和主贴一样）：最多 4 张图；或 1 个 GIF；或 1 个视频；不能混搭。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..db import database

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mov"}
ALL_EXT = IMAGE_EXT | GIF_EXT | VIDEO_EXT

MAX_IMAGES = 4
IMAGE_MAX_BYTES = 5 * 1024 * 1024
GIF_MAX_BYTES = 15 * 1024 * 1024
VIDEO_MAX_BYTES = 512 * 1024 * 1024
ACCEPT = ".jpg,.jpeg,.png,.webp,.gif,.mp4,.mov"

KIND_LABEL = {"image": "图片", "gif": "GIF", "video": "视频"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".mp4": "video/mp4", ".mov": "video/quicktime"}

RULE_TEXT = "最多 4 张图片（jpg/png/webp，单张 ≤5MB）；或 1 个 GIF（≤15MB）；或 1 个视频（mp4/mov，≤512MB）。图片、GIF、视频不能混搭。"


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


def check_set(files: list[str]) -> str:
    """一组附件合不合 X 的规则。返回错误原因，空串 = 可以。"""
    kinds = [media_kind(f) for f in files]
    if any(k is None for k in kinds):
        return "附件里有不支持的文件类型"
    if not files:
        return ""
    if len(set(kinds)) > 1:
        return "图片、GIF、视频不能混在一条里"
    if kinds[0] == "image" and len(files) > MAX_IMAGES:
        return f"图片最多 {MAX_IMAGES} 张"
    if kinds[0] in ("gif", "video") and len(files) > 1:
        return f"{KIND_LABEL[kinds[0]]}一条只能带 1 个"
    return ""


def can_add(files: list[str], name: str) -> str:
    """再加一个文件行不行（给上传框用）。"""
    return check_set(list(files) + [name])


def new_rel_path(original_name: str) -> str:
    """生成存放用的相对路径：YYYYMM/uuid.ext。原文件名只保留扩展名，避免奇怪字符。"""
    ext = Path(original_name).suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    return f"{datetime.now(timezone.utc):%Y%m}/{uuid.uuid4().hex}{ext}"


def describe(files: list[str]) -> str:
    """给卡片上显示：「2 张图片」「1 个视频」。"""
    files = list(files or [])
    if not files:
        return ""
    kind = media_kind(files[0]) or "image"
    if kind == "image":
        return f"{len(files)} 张图片"
    return f"1 个{KIND_LABEL[kind]}"


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


def referenced_files() -> set[str]:
    """库里所有还被引用的附件，用来清理孤儿文件。"""
    refs: set[str] = set()
    with database.get_conn() as conn:
        for tbl, col in (("materials", "media_files"), ("review_queue", "final_media_files"), ("scheduled_posts", "media_files")):
            for r in conn.execute(f"SELECT {col} AS v FROM {tbl}").fetchall():
                refs.update(parse_files(r["v"]))
    return refs


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
    return n
