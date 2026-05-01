# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Per-user saved figure library (PNG) with JSON manifest."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_GALLERY_ROOT = Path(__file__).resolve().parent.parent / "user_data" / "gallery"


def _safe_username(username: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", (username or "").strip())
    return (s[:80] or "anonymous")


def _user_dir(username: str) -> Path:
    d = _GALLERY_ROOT / _safe_username(username)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(username: str) -> Path:
    return _user_dir(username) / "manifest.json"


def _load_manifest(username: str) -> List[Dict[str, Any]]:
    p = _manifest_path(username)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_manifest(username: str, entries: List[Dict[str, Any]]) -> None:
    p = _manifest_path(username)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def list_user_gallery(username: str) -> List[Dict[str, Any]]:
    """Newest first."""
    items = _load_manifest(username)
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items


def save_png_to_gallery(
    username: str,
    png_bytes: bytes,
    *,
    source: str,
    caption: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> tuple[bool, str]:
    if not png_bytes or len(png_bytes) < 32:
        return False, "无效图像数据。"
    uname = _safe_username(username)
    if not uname or uname == "anonymous":
        return False, "请先登录。"
    entry_id = uuid.uuid4().hex[:16]
    fname = f"{entry_id}.png"
    user_dir = _user_dir(username)
    fpath = user_dir / fname
    try:
        fpath.write_bytes(png_bytes)
    except OSError as e:
        return False, f"写入失败：{e}"
    entry = {
        "id": entry_id,
        "file": fname,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source[:80],
        "caption": (caption or "")[:500],
        "extra": extra or {},
    }
    entries = _load_manifest(username)
    entries.append(entry)
    _save_manifest(username, entries)
    return True, "已保存至「我的图库」。"


def get_gallery_image_path(username: str, entry_id: str) -> Optional[Path]:
    for e in _load_manifest(username):
        if e.get("id") == entry_id:
            p = _user_dir(username) / e.get("file", "")
            if p.is_file():
                return p
    return None


def delete_gallery_entry(username: str, entry_id: str) -> tuple[bool, str]:
    entries = _load_manifest(username)
    new_list = []
    removed = None
    for e in entries:
        if e.get("id") == entry_id:
            removed = e
            continue
        new_list.append(e)
    if not removed:
        return False, "条目不存在。"
    p = _user_dir(username) / removed.get("file", "")
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    _save_manifest(username, new_list)
    return True, "已删除。"


def purge_user_gallery(username: str) -> tuple[bool, str]:
    """Delete all gallery files/manifest for a user."""
    uname = _safe_username(username)
    if not uname or uname == "anonymous":
        return False, "用户名无效。"
    user_dir = _GALLERY_ROOT / uname
    if not user_dir.exists():
        return True, "无用户图库可清理。"
    try:
        shutil.rmtree(user_dir)
    except OSError as e:
        return False, f"删除用户图库失败：{e}"
    return True, "已清理用户图库。"
