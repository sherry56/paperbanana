from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent / "user_data"
GENERATED_ROOT = ROOT / "generated"
EDITABLE_ROOT = ROOT / "editable"


def _safe_username(username: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", (username or "").strip())
    return (s[:80] or "anonymous")


def _user_dir(kind_root: Path, username: str) -> Path:
    d = kind_root / _safe_username(username)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(kind_root: Path, username: str) -> Path:
    return _user_dir(kind_root, username) / "manifest.json"


def _load_manifest(kind_root: Path, username: str) -> List[Dict[str, Any]]:
    p = _manifest_path(kind_root, username)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_manifest(kind_root: Path, username: str, entries: List[Dict[str, Any]]) -> None:
    p = _manifest_path(kind_root, username)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def save_generated_image(
    username: str,
    image_bytes: bytes,
    *,
    source: str,
    caption: str = "",
    ext: str = ".png",
    extra: Optional[Dict[str, Any]] = None,
) -> tuple[bool, str]:
    if not image_bytes or len(image_bytes) < 32:
        return False, "无效图像数据。"
    entry_id = uuid.uuid4().hex[:16]
    ext = ext if ext.startswith(".") else f".{ext}"
    file_name = f"{entry_id}{ext}"
    user_dir = _user_dir(GENERATED_ROOT, username)
    file_path = user_dir / file_name
    try:
        file_path.write_bytes(image_bytes)
    except OSError as e:
        return False, f"写入失败：{e}"
    entry = {
        "id": entry_id,
        "file": file_name,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source[:80],
        "caption": (caption or "")[:500],
        "extra": extra or {},
    }
    items = _load_manifest(GENERATED_ROOT, username)
    items.append(entry)
    _save_manifest(GENERATED_ROOT, username, items)
    return True, "已保存到“我的生成图片”。"


def save_editable_file(
    username: str,
    source_path: str,
    *,
    caption: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> tuple[bool, str]:
    src = Path(source_path)
    if not src.is_file():
        return False, "可编辑文件不存在。"
    entry_id = uuid.uuid4().hex[:16]
    suffix = src.suffix or ".drawio"
    dst_name = f"{entry_id}{suffix}"
    dst = _user_dir(EDITABLE_ROOT, username) / dst_name
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        return False, f"复制失败：{e}"
    entry = {
        "id": entry_id,
        "file": dst_name,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "caption": (caption or "")[:500],
        "extra": extra or {},
    }
    items = _load_manifest(EDITABLE_ROOT, username)
    items.append(entry)
    _save_manifest(EDITABLE_ROOT, username, items)
    return True, "已保存到“我的可编辑文件”。"


def list_generated(username: str) -> List[Dict[str, Any]]:
    items = _load_manifest(GENERATED_ROOT, username)
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items


def list_editable(username: str) -> List[Dict[str, Any]]:
    items = _load_manifest(EDITABLE_ROOT, username)
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items


def get_generated_path(username: str, entry_id: str) -> Optional[Path]:
    for e in _load_manifest(GENERATED_ROOT, username):
        if e.get("id") == entry_id:
            p = _user_dir(GENERATED_ROOT, username) / str(e.get("file", ""))
            if p.is_file():
                return p
    return None


def get_editable_path(username: str, entry_id: str) -> Optional[Path]:
    for e in _load_manifest(EDITABLE_ROOT, username):
        if e.get("id") == entry_id:
            p = _user_dir(EDITABLE_ROOT, username) / str(e.get("file", ""))
            if p.is_file():
                return p
    return None


def delete_generated(username: str, entry_id: str) -> tuple[bool, str]:
    items = _load_manifest(GENERATED_ROOT, username)
    kept = []
    rm = None
    for e in items:
        if e.get("id") == entry_id:
            rm = e
        else:
            kept.append(e)
    if not rm:
        return False, "条目不存在。"
    p = _user_dir(GENERATED_ROOT, username) / str(rm.get("file", ""))
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    _save_manifest(GENERATED_ROOT, username, kept)
    return True, "已删除。"


def delete_editable(username: str, entry_id: str) -> tuple[bool, str]:
    items = _load_manifest(EDITABLE_ROOT, username)
    kept = []
    rm = None
    for e in items:
        if e.get("id") == entry_id:
            rm = e
        else:
            kept.append(e)
    if not rm:
        return False, "条目不存在。"
    p = _user_dir(EDITABLE_ROOT, username) / str(rm.get("file", ""))
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    _save_manifest(EDITABLE_ROOT, username, kept)
    return True, "已删除。"


def purge_user_assets(username: str) -> tuple[bool, str]:
    """Delete all generated/editable data for a user."""
    uname = _safe_username(username)
    if not uname or uname == "anonymous":
        return False, "用户名无效。"

    removed = []
    for root in (GENERATED_ROOT, EDITABLE_ROOT):
        user_dir = root / uname
        if not user_dir.exists():
            continue
        try:
            shutil.rmtree(user_dir)
            removed.append(str(user_dir))
        except OSError as e:
            return False, f"删除用户素材失败：{e}"
    if removed:
        return True, "已清理用户素材与可编辑文件。"
    return True, "无用户素材可清理。"
