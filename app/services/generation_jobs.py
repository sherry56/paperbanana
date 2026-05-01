"""
后台生成任务：避免单次 HTTP 请求长时间阻塞（代理/浏览器超时、连接被重置）。

单进程内存存储；多 worker 部署需改为 Redis 等共享存储。
"""

from __future__ import annotations

import threading
import time
import uuid
import io
import json
import os
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.services.generation_service import (
    DEFAULT_MAX_CONCURRENT,
    result_image_to_png_bytes,
    run_parallel_candidates_sync,
)
from app.services.user_service import refund_user_generation_quota, set_user_last_generation_seconds
from PIL import Image

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}

_MAX_JOB_AGE_SEC = 7200

# Global concurrency guards to keep server stable when >5 users generate simultaneously.
# - MAX_ACTIVE_JOBS: how many generation workers may actively run at the same time.
# - PB_GLOBAL_MAX_CONCURRENT_CANDIDATES: approximate cap for "candidate tasks" across all active jobs.
_MAX_ACTIVE_JOBS = int(os.getenv("PB_MAX_ACTIVE_JOBS", "10"))
_GLOBAL_MAX_CONCURRENT_CANDIDATES = int(os.getenv("PB_GLOBAL_MAX_CONCURRENT_CANDIDATES", "40"))
_job_semaphore = threading.Semaphore(_MAX_ACTIVE_JOBS)
_active_job_count = 0
_active_job_lock = threading.Lock()

# Keep generated candidates under project-root user_data for consistent ops/cleanup.
_RESULT_ROOT = Path(__file__).resolve().parents[2] / "user_data" / "results"
_JOBS_MANIFEST_NAME = "jobs_manifest.json"


def _safe_username(username: str) -> str:
    # Keep consistent with utils/user_gallery.py
    import re

    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", (username or "").strip())
    return (s[:80] or "anonymous")


def _job_dir(username: str, job_id: str) -> Path:
    d = _RESULT_ROOT / _safe_username(username) / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_job_candidate_png_path(username: str, job_id: str, candidate_id: int) -> Path:
    return _job_dir(username, job_id) / f"candidate_{int(candidate_id)}.png"


def _jobs_manifest_path(username: str) -> Path:
    d = _RESULT_ROOT / _safe_username(username)
    d.mkdir(parents=True, exist_ok=True)
    return d / _JOBS_MANIFEST_NAME


def _load_jobs_manifest(username: str) -> dict[str, Any]:
    p = _jobs_manifest_path(username)
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_jobs_manifest(username: str, data: dict[str, Any]) -> None:
    p = _jobs_manifest_path(username)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _persist_job_state(
    username: str,
    job_id: str,
    *,
    status: str,
    exp_mode: str | None = None,
    elapsed_sec: int | None = None,
    candidate_count: int | None = None,
    error: str | None = None,
) -> None:
    # Only persist terminal states (done/error) for resumption.
    if status not in {"done", "error"}:
        return
    with _lock:
        manifest = _load_jobs_manifest(username)
        manifest[job_id] = {
            "status": status,
            "exp_mode": exp_mode,
            "elapsed_sec": elapsed_sec,
            "candidate_count": candidate_count,
            "error": error,
            "updated_at": time.time(),
        }
        _save_jobs_manifest(username, manifest)


def get_job_disk_status(username: str, job_id: str) -> str | None:
    with _lock:
        manifest = _load_jobs_manifest(username)
        row = manifest.get(job_id)
        if not isinstance(row, dict):
            return None
        return str(row.get("status") or "")


def get_job_disk_poll_json(username: str, job_id: str) -> dict[str, Any] | None:
    with _lock:
        manifest = _load_jobs_manifest(username)
        row = manifest.get(job_id)
        if not isinstance(row, dict):
            return None
        status = str(row.get("status") or "")
        out: dict[str, Any] = {"status": status}
        if row.get("elapsed_sec") is not None:
            out["elapsed_sec"] = int(row.get("elapsed_sec") or 0)
        if row.get("candidate_count") is not None:
            count = max(0, int(row.get("candidate_count") or 0))
            out["candidate_count"] = count
            out["candidate_ids"] = list(range(count))
        if status == "error":
            out["error"] = str(row.get("error") or "unknown error")
        return out


def get_latest_done_job_id_from_disk(username: str) -> str | None:
    with _lock:
        manifest = _load_jobs_manifest(username)
        best_jid = None
        best_ts = -1.0
        for jid, row in manifest.items():
            if not isinstance(row, dict):
                continue
            if row.get("status") != "done":
                continue
            ts = float(row.get("updated_at") or 0.0)
            if ts > best_ts:
                best_ts = ts
                best_jid = jid
        return best_jid


def take_job_from_disk_for_session(
    username: str, job_id: str
) -> tuple[str | None, list[dict[str, Any]] | None, str | None, int | None]:
    """
    For resumption after disconnect/reload.
    If disk has done data: return (None, rows, exp_mode, elapsed_sec) and remove the disk entry.
    If disk has error: return (error, None, None, None) and remove the disk entry.
    Otherwise: return (err, None, None, None).
    """
    with _lock:
        manifest = _load_jobs_manifest(username)
        row = manifest.get(job_id)
        if not isinstance(row, dict):
            return ("任务不存在或已过期。", None, None, None)
        status = str(row.get("status") or "")
        if status == "done":
            cc = row.get("candidate_count")
            try:
                candidate_count = int(cc)
            except Exception:
                candidate_count = 0
            exp_mode = str(row.get("exp_mode") or "demo_planner_critic")
            elapsed_sec = row.get("elapsed_sec")
            try:
                elapsed_i = int(elapsed_sec)
            except Exception:
                elapsed_i = 0
            manifest.pop(job_id, None)
            _save_jobs_manifest(username, manifest)
            rows = [{"candidate_id": i} for i in range(max(0, candidate_count))]
            return (None, rows, exp_mode, elapsed_i)
        if status == "error":
            err_msg = str(row.get("error") or "生成失败")
            manifest.pop(job_id, None)
            _save_jobs_manifest(username, manifest)
            return (err_msg, None, None, None)
        return ("任务尚未完成。", None, None, None)


def _blank_png_bytes() -> bytes:
    img = Image.new("RGBA", (16, 16), (255, 255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _cleanup_stale() -> None:
    now = time.time()
    with _lock:
        to_del: list[str] = []
        for jid, j in list(_jobs.items()):
            started = float(j.get("started_at", now))
            if now - started > _MAX_JOB_AGE_SEC:
                to_del.append(jid)
        for jid in to_del:
            _jobs.pop(jid, None)


def spawn_generation_job(
    username: str,
    *,
    data_list: list[dict[str, Any]],
    exp_mode: str,
    retrieval_setting: str,
    main_model_name: str,
    image_gen_model_name: str,
    quota_need: int,
) -> str:
    _cleanup_stale()
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "username": username,
            "status": "running",
            "started_at": time.time(),
            "quota_need": int(quota_need),
        }

    def _worker() -> None:
        global _active_job_count
        acquired = False
        counted = False
        try:
            _job_semaphore.acquire()
            acquired = True
            with _active_job_lock:
                _active_job_count += 1
                active_jobs = _active_job_count
                counted = True

            # Dynamically scale per-job candidate concurrency so total system concurrency
            # doesn't blow up when many users generate at once.
            max_concurrent = max(1, _GLOBAL_MAX_CONCURRENT_CANDIDATES // max(1, active_jobs))
            max_concurrent = min(int(max_concurrent), int(DEFAULT_MAX_CONCURRENT))

            t0 = time.perf_counter()
            results = run_parallel_candidates_sync(
                data_list=data_list,
                exp_mode=exp_mode,
                retrieval_setting=retrieval_setting,
                main_model_name=main_model_name,
                image_gen_model_name=image_gen_model_name,
                max_concurrent=max_concurrent,
            )
            elapsed_sec = max(1, int(time.perf_counter() - t0))
            db = SessionLocal()
            try:
                set_user_last_generation_seconds(db, username, elapsed_sec)
            finally:
                db.close()
            job_dir = _job_dir(username, job_id)
            rows: list[dict[str, Any]] = []
            for i, r in enumerate(results):
                # Convert candidate result to PNG immediately, so later UI/save/download never relies on base64/raw blobs.
                png = result_image_to_png_bytes(r, exp_mode)
                if not png:
                    raise RuntimeError(f"candidate image empty: candidate_{i}")
                out_path = job_dir / f"candidate_{i}.png"
                try:
                    out_path.write_bytes(png)
                except OSError as e:
                    raise RuntimeError(f"写入候选文件失败: {out_path} ({e})") from e
                rows.append({"candidate_id": i})
            with _lock:
                j = _jobs.get(job_id)
                if j is None:
                    return
                j.update(
                    {
                        "status": "done",
                        "completed_at": time.time(),
                        "rows": rows,
                        "exp_mode": exp_mode,
                        "elapsed_sec": elapsed_sec,
                    }
                )
            _persist_job_state(
                username,
                job_id,
                status="done",
                exp_mode=exp_mode,
                elapsed_sec=elapsed_sec,
                candidate_count=len(rows),
                error=None,
            )
        except Exception as e:
            err_msg = str(e) or "生成失败"
            db = SessionLocal()
            try:
                refund_user_generation_quota(db, username, int(quota_need))
            finally:
                db.close()
            with _lock:
                j = _jobs.get(job_id)
                if j is None:
                    return
                j.update(
                    {
                        "status": "error",
                        "completed_at": time.time(),
                        "error": err_msg,
                    }
                )
            _persist_job_state(
                username,
                job_id,
                status="error",
                exp_mode=exp_mode,
                elapsed_sec=None,
                candidate_count=0,
                error=err_msg,
            )
        finally:
            if counted:
                with _active_job_lock:
                    _active_job_count = max(0, _active_job_count - 1)
            if acquired:
                try:
                    _job_semaphore.release()
                except Exception:
                    pass

    thread = threading.Thread(target=_worker, name=f"gen-{job_id[:8]}", daemon=True)
    thread.start()
    return job_id


def job_is_owned(job_id: str, username: str) -> bool:
    with _lock:
        j = _jobs.get(job_id)
        return bool(j and j.get("username") == username)


def peek_job_status(job_id: str, username: str) -> str | None:
    """返回 running | done | error；任务不存在或用户不匹配时返回 None。"""
    with _lock:
        j = _jobs.get(job_id)
        if not j or j.get("username") != username:
            return None
        return str(j.get("status", ""))


def get_job_poll_json(job_id: str, username: str) -> dict[str, Any] | None:
    """供前端轮询；不含原始结果大对象。"""
    with _lock:
        j = _jobs.get(job_id)
        if not j or j.get("username") != username:
            return None
        status = str(j.get("status", ""))
        out: dict[str, Any] = {"status": status}
        if status == "running":
            out["elapsed_sec"] = max(0, int(time.time() - float(j.get("started_at", time.time()))))
        elif status == "done":
            out["elapsed_sec"] = int(j.get("elapsed_sec") or 0)
            rows = j.get("rows") or []
            if isinstance(rows, list):
                ids: list[int] = []
                for idx, row in enumerate(rows):
                    if isinstance(row, dict):
                        try:
                            ids.append(int(row.get("candidate_id", idx)))
                        except Exception:
                            ids.append(idx)
                    else:
                        ids.append(idx)
                out["candidate_count"] = len(ids)
                out["candidate_ids"] = ids
        elif status == "error":
            out["error"] = str(j.get("error", "未知错误"))
        return out


def take_error_job(job_id: str, username: str) -> str | None:
    """若任务为 error，返回错误说明并移除任务；否则返回 None。"""
    with _lock:
        j = _jobs.get(job_id)
        if not j or j.get("username") != username:
            return None
        if j.get("status") != "error":
            return None
        msg = str(j.get("error", "生成失败"))
        _jobs.pop(job_id, None)
        return msg


def pop_job_for_session(job_id: str, username: str) -> tuple[str | None, list[dict[str, Any]] | None, str | None, int | None]:
    """
    成功时返回 (None, rows, exp_mode, elapsed_sec) 并移除任务；
    失败时返回 (error_msg, None, None, None)。
    """
    with _lock:
        j = _jobs.get(job_id)
        if not j:
            return ("任务不存在或已过期。", None, None, None)
        if j.get("username") != username:
            return ("无权访问该任务。", None, None, None)
        if j.get("status") != "done":
            return ("任务尚未完成。", None, None, None)
        rows = j.get("rows")
        exp_mode = j.get("exp_mode") or "demo_planner_critic"
        elapsed_sec = int(j.get("elapsed_sec") or 0)
        _jobs.pop(job_id, None)
        if not isinstance(rows, list):
            return ("结果数据无效。", None, None, None)
        return (None, rows, str(exp_mode), elapsed_sec)
