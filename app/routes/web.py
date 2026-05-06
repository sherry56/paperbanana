from __future__ import annotations

from typing import Any

import json
import random
import secrets
import io
import time
import zipfile
import csv
import os
import re
from pathlib import Path
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app_config import load_app_config
from app.db import get_db
from app.models import AppSetting, PaymentOrder, User, UserMessage, UserMessageReply
from app.services.alipay_service import alipay_config_ready
from app.services.generation_jobs import (
    get_job_poll_json,
    get_job_candidate_png_path,
    get_job_disk_poll_json,
    get_latest_done_job_id_from_disk,
    get_job_disk_status,
    job_is_owned,
    peek_job_status,
    pop_job_for_session,
    spawn_generation_job,
    take_error_job,
    take_job_from_disk_for_session,
)
from app.services.generation_service import (
    create_sample_inputs,
    result_image_to_png_bytes,
)
from app.services.user_service import (
    ROLE_ADMIN,
    ROLE_USER,
    admin_reset_user_password,
    add_user_preset_by_admin,
    apply_default_new_user_mode_to_all_users,
    approve_access_request,
    change_user_password,
    consume_user_generation_quota,
    create_user_by_admin,
    delete_user_and_all_data,
    get_price_packages,
    get_default_new_user_mode,
    get_generation_unit_price_yuan,
    set_price_packages,
    get_tier_template_override_tier_ids,
    get_tier_templates,
    get_user_last_generation_seconds,
    get_user_profile,
    list_managed_users,
    list_tier_requests,
    register_user,
    reject_access_request,
    remove_user_preset,
    set_default_new_user_mode,
    set_generation_unit_price_yuan,
    set_user_allowed_tiers,
    set_user_authorized,
    set_user_default_gen_mode,
    set_user_edit_permission,
    set_user_method_optimize_permission,
    set_user_generation_quota,
    upsert_tier_template_by_admin,
    delete_tier_template_by_admin,
    reset_tier_template_by_admin,
    verify_login,
    update_user_preset_by_admin,
)
from utils import user_gallery
from utils.usage_tiers import TIER_ORDER, flatten_tier_for_demo, normalize_legacy_tier_mode, tier_label
from utils.generation_utils import GPT_DIRECT_MODE_ERROR, is_gpt_model_name

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
APP_CONFIG = load_app_config()
_ADMIN_USERNAME = APP_CONFIG.admin_username
_PAYMENT_QR_DIR = Path(__file__).resolve().parents[1] / "static" / "payment_qr"
_PAYMENT_QR_KEY_BY_CHANNEL = {
    "alipay": "payment_qr_alipay_url",
    "wechat": "payment_qr_wechat_url",
}

# UI: customer service contact (replace payment QR).
_CUSTOMER_SERVICE_QR_KEY = "customer_service_wechat_qr_image_url"

DEFAULT_EXAMPLE_METHOD = """## Methodology: The PaperVizAgent Framework

In this section, we present the architecture of PaperVizAgent, a reference-driven agentic framework for automated academic illustration. As illustrated in Figure \\ref{fig:methodology_diagram}, PaperVizAgent orchestrates a collaborative team of five specialized agents—Retriever, Planner, Stylist, Visualizer, and Critic—to transform raw scientific content into publication-quality diagrams and plots.

### Retriever Agent

Given the source context $S$ and the communicative intent $C$, the Retriever Agent identifies the most relevant examples from a fixed reference set to guide downstream generation.

### Planner Agent

The Planner Agent translates source context and retrieved references into a comprehensive textual plan for the target illustration.

### Stylist Agent

The Stylist Agent refines the plan according to academic aesthetics, including color palette, layout, typography, and visual consistency.

### Visualizer and Critic Loop

The Visualizer generates candidate images from the refined plan, while the Critic checks factual alignment and visual quality, then proposes improved prompts. This loop iterates for multiple rounds to obtain publication-quality figures.
"""

DEFAULT_EXAMPLE_CAPTION = (
    "Figure 1: Overview of our PaperVizAgent framework. Given the source context and communicative intent, "
    "we first retrieve relevant reference examples and synthesize a stylistically optimized description. "
    "Then an iterative Visualizer-Critic loop performs multi-round refinement to produce the final academic figure."
)

_SUB2API_SSO_ENABLED = os.getenv("PB_SUB2API_SSO_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_SUB2API_BASE_URL = (os.getenv("PB_SUB2API_BASE_URL", "http://sub2api:8080") or "").rstrip("/")
_SUB2API_TIMEOUT_SECONDS = float(os.getenv("PB_SUB2API_TIMEOUT_SECONDS", "6"))
_SUB2API_SSO_ONLY = os.getenv("PB_SUB2API_SSO_ONLY", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_HIDE_ACCOUNT_FEATURES = os.getenv("PB_HIDE_ACCOUNT_FEATURES", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_SUB2API_SERVICE_TOKEN = os.getenv("PB_SUB2API_SERVICE_TOKEN", "").strip()
_REQUIRE_SUB2API_SERVICE_TOKEN = os.getenv("PB_REQUIRE_SUB2API_SERVICE_TOKEN", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _require_sub2api_service(request: Request) -> JSONResponse | None:
    if not _SUB2API_SERVICE_TOKEN:
        if _REQUIRE_SUB2API_SERVICE_TOKEN:
            return JSONResponse({"ok": False, "error": "service token is not configured"}, status_code=503)
        return None
    token = (request.headers.get("x-sub2api-service-token") or "").strip()
    if not secrets.compare_digest(token, _SUB2API_SERVICE_TOKEN):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return None


def _safe_sso_username(user_id: Any, email: str, username_hint: str) -> str:
    if user_id not in (None, ""):
        base = f"s2a_{str(user_id).strip()}"
    elif email:
        local = email.split("@", 1)[0]
        base = f"s2a_{local}"
    elif username_hint:
        base = f"s2a_{username_hint}"
    else:
        return ""
    base = re.sub(r"[^0-9A-Za-z_]+", "_", base).strip("_")
    if not base:
        return ""
    if base == _ADMIN_USERNAME:
        base = f"s2a_{base}"
    return base[:120]


def _extract_sub2api_user(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("user"), dict):
        data = data["user"]
    if not isinstance(data, dict):
        return None
    return {
        "id": data.get("id") or data.get("user_id"),
        "email": str(data.get("email") or "").strip(),
        "username": str(data.get("username") or "").strip(),
        "role": str(data.get("role") or ROLE_USER).strip().lower(),
    }


def _fetch_sub2api_user_by_token(token: str) -> dict[str, Any] | None:
    raw = (token or "").strip()
    if not raw or not _SUB2API_BASE_URL:
        return None

    if _SUB2API_BASE_URL.endswith("/api/v1"):
        urls = [f"{_SUB2API_BASE_URL}/auth/me"]
    else:
        urls = [f"{_SUB2API_BASE_URL}/api/v1/auth/me", f"{_SUB2API_BASE_URL}/auth/me"]

    headers = {"Authorization": f"Bearer {raw}"}
    with httpx.Client(timeout=_SUB2API_TIMEOUT_SECONDS, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = client.get(url, headers=headers)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if int(payload.get("code", 0)) != 0:
                continue
            u = _extract_sub2api_user(payload)
            if u:
                return u
    return None


def _upsert_sso_local_user(db: Session, ext_user: dict[str, Any]) -> str | None:
    username = _safe_sso_username(
        ext_user.get("id"),
        str(ext_user.get("email") or ""),
        str(ext_user.get("username") or ""),
    )
    if not username:
        return None

    role = ROLE_ADMIN if ext_user.get("role") == ROLE_ADMIN else ROLE_USER
    rec = db.get(User, username)
    if rec is None:
        ok, _msg = create_user_by_admin(
            db,
            username=username,
            password=secrets.token_urlsafe(24),
            role=role,
            authorized=True,
        )
        if not ok:
            return None
        rec = db.get(User, username)
        if rec is None:
            return None

    # Keep local user in sync with upstream identity; do not require extra registration/login here.
    rec.role = role
    rec.authorized = True
    rec.gen_quota_remaining = None
    if role == ROLE_ADMIN:
        rec.can_edit_image = True
        rec.default_gen_mode = ""
    db.commit()
    return rec.username


def _try_sub2api_sso_login(request: Request, db: Session, token: str | None) -> bool:
    if not _SUB2API_SSO_ENABLED:
        return False
    raw = (token or "").strip()
    if not raw:
        return False
    ext_user = _fetch_sub2api_user_by_token(raw)
    if not ext_user:
        return False
    username = _upsert_sso_local_user(db, ext_user)
    if not username:
        return False
    prof = get_user_profile(db, username)
    if not prof:
        return False
    request.session["user"] = prof
    return True


def _render_template(request: Request, name: str, context: dict[str, Any]):
    ctx = {"request": request}
    ctx.update(context)
    ctx.setdefault("show_local_login", not _SUB2API_SSO_ONLY)
    ctx.setdefault("hide_account_features", _HIDE_ACCOUNT_FEATURES)
    # Compatible with both old and new Starlette TemplateResponse signatures.
    try:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context=ctx,
        )
    except TypeError:
        return templates.TemplateResponse(name, ctx)


def _current_user(request: Request) -> dict[str, Any] | None:
    u = request.session.get("user")
    return u if isinstance(u, dict) else None


def _disabled_account_redirect(request: Request, target: str = "/") -> RedirectResponse:
    request.session["flash_error"] = "当前已关闭 PaperBanana 本地账户功能，请在 Sub2API 内直接使用科研绘图。"
    return RedirectResponse(target, status_code=303)


def _require_login(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _require_admin(request: Request):
    u = _current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    if u.get("role") != ROLE_ADMIN:
        return RedirectResponse("/", status_code=303)
    return None


def _safe_int(raw: str, default: int) -> int:
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _pick_user_mode(user: dict[str, Any]) -> str:
    default_mode = normalize_legacy_tier_mode((user.get("default_gen_mode") or "").strip())
    if default_mode:
        return default_mode
    allowed_tiers = user.get("allowed_tiers") or []
    if allowed_tiers:
        return f"tier:{allowed_tiers[0]}"
    presets = user.get("approved_presets") or []
    if presets and presets[0].get("id"):
        return f"preset:{presets[0]['id']}"
    return "tier:budget"


def _tier_label_from_templates(tier_templates: dict[str, dict[str, Any]], tid: str) -> str:
    row = tier_templates.get(tid) or {}
    return str(row.get("label") or tier_label(tid))


def _build_mode_options(user: dict[str, Any], tier_templates: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for t in user.get("allowed_tiers") or []:
        rows.append({"id": f"tier:{t}", "label": f"档位 · {_tier_label_from_templates(tier_templates, t)}"})
    for p in user.get("approved_presets") or []:
        pid = p.get("id")
        if pid:
            rows.append({"id": f"preset:{pid}", "label": f"组合 · {p.get('label', pid)}"})
    if user.get("role") == ROLE_ADMIN:
        for t in TIER_ORDER:
            mode = f"tier:{t}"
            if not any(r["id"] == mode for r in rows):
                rows.append({"id": mode, "label": f"档位 · {_tier_label_from_templates(tier_templates, t)}"})
    return rows


def _mode_to_config(user: dict[str, Any], mode: str, tier_templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    mode = normalize_legacy_tier_mode((mode or "").strip())
    if mode.startswith("tier:"):
        tid = mode[5:]
        if tid in tier_templates:
            row = tier_templates.get(tid) or {}
            return {
                "exp_mode": str(row.get("exp_mode", "demo_planner_critic")),
                "retrieval_setting": str(row.get("retrieval_setting", "auto")),
                "num_candidates": int(row.get("num_candidates", 1)),
                "aspect_ratio": str(row.get("aspect_ratio", "16:9")),
                "max_critic_rounds": int(row.get("max_critic_rounds", 2)),
                "main_model_name": str(row.get("main_model_name", "")),
                "image_gen_model_name": str(row.get("image_gen_model_name", "")),
            }
    if mode.startswith("preset:"):
        pid = mode[7:]
        for p in user.get("approved_presets") or []:
            if p.get("id") == pid and isinstance(p.get("config"), dict):
                cfg = dict(p["config"])
                cfg["num_candidates"] = int(cfg.get("num_candidates", 1))
                cfg["max_critic_rounds"] = int(cfg.get("max_critic_rounds", 2))
                return cfg
    return flatten_tier_for_demo("budget")


def _fmt_duration_cn(sec: int | None) -> str:
    if sec is None:
        return ""
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    if m == 0:
        return f"{s} 秒"
    return f"{m} 分 {s} 秒"


def _estimated_generation_seconds(db: Session, user: dict[str, Any]) -> int:
    last_gen_sec = get_user_last_generation_seconds(db, user["username"])
    if last_gen_sec is not None:
        return last_gen_sec
    return random.randint(180, 240)


def _order_status_label(status: str) -> str:
    m = {
        "created": "已申请",
        "paid": "已授权",
        "closed": "已驳回",
    }
    return m.get((status or "").strip(), status or "-")


def _parse_order_meta(payload: str) -> dict[str, str]:
    if not payload:
        return {}
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items() if v is not None}
    except Exception:
        pass
    return {}


def _get_payment_qr_urls(db: Session) -> tuple[str, str]:
    a = db.get(AppSetting, _PAYMENT_QR_KEY_BY_CHANNEL["alipay"])
    w = db.get(AppSetting, _PAYMENT_QR_KEY_BY_CHANNEL["wechat"])
    alipay_url = (a.value if a and a.value else APP_CONFIG.alipay_qr_image_url).strip()
    wechat_url = (w.value if w and w.value else APP_CONFIG.wechat_qr_image_url).strip()
    return alipay_url, wechat_url


def _get_customer_service_qr_url(db: Session) -> str:
    row = db.get(AppSetting, _CUSTOMER_SERVICE_QR_KEY)
    return (row.value if row and row.value else "").strip()


def _refresh_session_user(request: Request, db: Session, user: dict[str, Any]) -> dict[str, Any]:
    prof = get_user_profile(db, user["username"])
    if not prof:
        return user
    for k in ("_pay_url", "_recent_orders"):
        if k in user:
            prof[k] = user[k]
    request.session["user"] = prof
    return prof


def _index_context(
    user: dict[str, Any],
    tier_templates: dict[str, dict[str, Any]],
    *,
    error: str = "",
    success: str = "",
    results: list[dict[str, Any]] | None = None,
    pwd_msg: str = "",
    db: Session | None = None,
    pending_job_finish_url: str | None = None,
):
    # Keep first paint responsive: render a moderate window and paginate client-side.
    gallery_items = user_gallery.list_user_gallery(user["username"])[:24]
    mode_options = _build_mode_options(user, tier_templates)
    selected_mode = _pick_user_mode(user)
    examples = {
        "PaperVizAgent Framework": DEFAULT_EXAMPLE_METHOD,
    }
    captions = {
        "PaperVizAgent Framework": DEFAULT_EXAMPLE_CAPTION,
    }
    # Always use project built-in references for consistent UX across all users.
    showcase_cards = [
        {
            "title": "方法概览 / 系统框架",
            "desc": "项目内置参考图（architecture）。",
            "image_url": "/assets/scenario_architecture1.png",
        },
        {
            "title": "对比与消融",
            "desc": "项目内置参考图（ablation）。",
            "image_url": "/assets/scenario_ablation.svg",
        },
        {
            "title": "数据来源与论文页",
            "desc": "项目内置参考图（dataset/paper page）。",
            "image_url": "/assets/dataset-on-hf-xl.png",
        },
    ]
    result_reference_images = [c.get("image_url", "") for c in showcase_cards if c.get("image_url")]
    recent_orders = []
    try:
        # Newest orders first; keep page lightweight.
        recent_orders = (
            user.get("_recent_orders")
            or []
        )
    except Exception:
        recent_orders = []
    last_gen_sec: int | None = None
    if db and user and user.get("username"):
        last_gen_sec = get_user_last_generation_seconds(db, user["username"])
    estimated_sec = _estimated_generation_seconds(db, user) if db else random.randint(180, 240)
    customer_service_qr_url = ""
    alipay_qr_url = APP_CONFIG.alipay_qr_image_url
    wechat_qr_url = APP_CONFIG.wechat_qr_image_url
    price_packages: list[dict[str, Any]] = []
    if db:
        customer_service_qr_url = _get_customer_service_qr_url(db)
        alipay_qr_url, wechat_qr_url = _get_payment_qr_urls(db)
        price_packages = get_price_packages(db)
    return {
        "user": user,
        "tier_order": TIER_ORDER,
        "tier_label": lambda tid: _tier_label_from_templates(tier_templates, tid),
        "gallery_items": gallery_items,
        "error": error,
        "success": success,
        "results": results or [],
        "generation_unit_price": 3.0,
        "showcase_cards": showcase_cards,
        "result_reference_images": result_reference_images,
        "example_method_options": list(examples.keys()),
        "example_caption_options": list(captions.keys()),
        "example_methods_json": json.dumps(examples, ensure_ascii=False),
        "example_captions_json": json.dumps(captions, ensure_ascii=False),
        "available_modes": mode_options,
        "selected_mode": selected_mode,
        "pay_url": user.get("_pay_url", ""),
        "recent_orders": recent_orders,
        "alipay_ready": alipay_config_ready(),
        "alipay_qr_image_url": alipay_qr_url,
        "wechat_qr_image_url": wechat_qr_url,
        "customer_service_wechat_qr_image_url": customer_service_qr_url,
        "price_packages": price_packages,
        "pwd_msg": pwd_msg,
        "gen_last_actual_seconds": last_gen_sec,
        "gen_last_actual_label": _fmt_duration_cn(last_gen_sec) if last_gen_sec is not None else "",
        "gen_estimated_seconds": estimated_sec,
        "gen_estimated_label": _fmt_duration_cn(estimated_sec),
        "pending_job_finish_url": pending_job_finish_url or "",
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    token: str = Query("", description="Sub2API access token for SSO"),
    access_token: str = Query("", description="Alias of token"),
    db: Session = Depends(get_db),
):
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    sso_token = (token or access_token or "").strip()
    if sso_token and _try_sub2api_sso_login(request, db, sso_token):
        return RedirectResponse("/", status_code=303)
    if _SUB2API_SSO_ONLY:
        return _render_template(
            request,
            "login.html",
            {"error": "请从 Sub2API 侧边栏打开科研绘图，系统会自动单点登录。"},
        )
    return _render_template(request, "login.html", {"error": ""})


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return RedirectResponse("/login", status_code=303)


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if _SUB2API_SSO_ONLY:
        return _render_template(
            request,
            "login.html",
            {"error": "已关闭本地账号登录，请从 Sub2API 侧边栏进入。"},
        )
    prof = verify_login(db, username=username, password=password)
    if not prof:
        return _render_template(request, "login.html", {"error": "用户名或密码错误。"})
    request.session["user"] = prof
    return RedirectResponse("/", status_code=303)


@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    return RedirectResponse("/login", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def index_page(request: Request, db: Session = Depends(get_db)):
    tier_templates = get_tier_templates(db)
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    user = _refresh_session_user(request, db, user)
    price = get_generation_unit_price_yuan(db)
    orders = (
        db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.username == user["username"])
            .order_by(PaymentOrder.created_at.desc())
            .limit(5)
        )
        .scalars()
        .all()
    )
    recent_rows = []
    user_message_rows: list[dict[str, Any]] = []
    order_nos = [o.order_no for o in orders if o and o.order_no]
    order_status_label_map: dict[str, str] = {
        str(o.order_no): _order_status_label(o.status) for o in orders if o and o.order_no
    }

    if order_nos:
        msgs = db.execute(select(UserMessage).where(UserMessage.order_no.in_(order_nos))).scalars().all()
        msg_ids = [m.id for m in msgs if m is not None and getattr(m, "id", None)]

        reply_map: dict[int, UserMessageReply] = {}
        if msg_ids:
            replies = (
                db.execute(select(UserMessageReply).where(UserMessageReply.message_id.in_(msg_ids)))
                .scalars()
                .all()
            )
            reply_map = {r.message_id: r for r in replies if r is not None}

        for m in msgs:
            if not m:
                continue
            r = reply_map.get(m.id)
            user_message_rows.append(
                {
                    "id": m.id,
                    "order_no": m.order_no,
                    "order_status_label": order_status_label_map.get(str(m.order_no), ""),
                    "pay_channel": m.pay_channel,
                    "content": m.content,
                    "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "admin_reply": r.content if r else "",
                    "admin_reply_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r else "",
                }
            )

    for o in orders:
        meta = _parse_order_meta(o.notify_payload or "")
        recent_rows.append(
            {
                "order_no": o.order_no,
                "status": o.status,
                "status_label": _order_status_label(o.status),
                "buy_times": o.buy_times,
                "pay_channel": meta.get("pay_channel", ""),
                "payer_note": meta.get("payer_note", ""),
                "total_amount_yuan": f"{o.total_amount_yuan:.2f}",
                "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    user["_recent_orders"] = recent_rows
    pending_finish = ""
    pid = request.session.get("pending_generation_job_id")
    if pid:
        st = peek_job_status(pid, user["username"])
        if st == "done":
            pending_finish = f"/generate/finish/{pid}"
        elif st == "error":
            err_msg = take_error_job(pid, user["username"])
            request.session.pop("pending_generation_job_id", None)
            if err_msg:
                request.session["flash_error"] = f"上次生成失败：{err_msg}"
        elif st is None:
            # In-memory job list might be gone (server restart); fall back to disk.
            st_disk = get_job_disk_status(user["username"], pid)
            if st_disk == "done":
                pending_finish = f"/generate/finish/{pid}"
            else:
                request.session.pop("pending_generation_job_id", None)
    flash_ok = request.session.pop("flash_success", None)
    flash_err = request.session.pop("flash_error", None)
    if not pending_finish:
        # No session hint; try latest completed job from disk for this user.
        latest_jid = get_latest_done_job_id_from_disk(user["username"])
        if latest_jid:
            pending_finish = f"/generate/finish/{latest_jid}"
    session_results = request.session.get("last_results") or []
    if not isinstance(session_results, list):
        session_results = []
    ctx = _index_context(
        user,
        tier_templates,
        db=db,
        success=str(flash_ok or ""),
        error=str(flash_err or ""),
        pending_job_finish_url=pending_finish or None,
        results=session_results if session_results else None,
    )
    ctx["generation_unit_price"] = price
    ctx["user_message_rows"] = user_message_rows
    return _render_template(request, "index.html", ctx)


@router.post("/generate", response_class=HTMLResponse)
def generate_submit(
    request: Request,
    method_content: str = Form(...),
    caption: str = Form(""),
    optimize_method_content: str = Form("0"),
    generation_mode: str = Form(""),
    exp_mode: str = Form("demo_planner_critic"),
    retrieval_setting: str = Form("auto"),
    num_candidates: int = Form(1),
    aspect_ratio: str = Form("16:9"),
    max_critic_rounds: int = Form(3),
    main_model_name: str = Form(""),
    image_gen_model_name: str = Form(""),
    db: Session = Depends(get_db),
):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    user = _refresh_session_user(request, db, user)
    tier_templates = get_tier_templates(db)

    # Permission gate:
    # Non-admin users must be explicitly authorized before they can generate.
    # (Quota is still consumed/checked separately in consume_user_generation_quota.)
    if user.get("role") != ROLE_ADMIN and not user.get("authorized"):
        msg = "您尚未获得生成权限，请先在“账号与申请”提交申请并等待管理员审核。"
        ctx = _index_context(user, tier_templates, error=msg, db=db)
        ctx["generation_unit_price"] = get_generation_unit_price_yuan(db)
        return _render_template(request, "index.html", ctx)

    want_opt = str(optimize_method_content).strip() in {"1", "true", "yes", "on"}
    allow_opt = bool(user.get("role") == ROLE_ADMIN or user.get("can_optimize_method"))
    if want_opt and not allow_opt:
        msg = "当前账号未开通“方法内容优化”权限，请联系管理员配置。"
        ctx = _index_context(user, tier_templates, error=msg, db=db)
        ctx["generation_unit_price"] = get_generation_unit_price_yuan(db)
        return _render_template(request, "index.html", ctx)

    # Idempotency / de-dup:
    # If the client retries POST /generate while a job is already running/done/error,
    # don't spawn another job (this avoids duplicate token consumption).
    pending_id = request.session.get("pending_generation_job_id")
    if pending_id:
        st = peek_job_status(pending_id, user["username"]) or get_job_disk_status(user["username"], pending_id)
        if st == "running":
            return RedirectResponse(f"/generate/wait/{pending_id}", status_code=303)
        if st in {"done", "error"}:
            return RedirectResponse(f"/generate/finish/{pending_id}", status_code=303)
        # If pending_id has no corresponding job, clear it so user can submit again.
        if st is None or st == "":
            request.session.pop("pending_generation_job_id", None)

    use_custom_combo = user.get("role") == ROLE_ADMIN and generation_mode == "custom"
    if use_custom_combo:
        # Admin explicit custom combination: use form values directly.
        exp_mode = exp_mode
        retrieval_setting = retrieval_setting
        num_candidates = int(num_candidates)
        aspect_ratio = aspect_ratio
        max_critic_rounds = int(max_critic_rounds)
        main_model_name = (main_model_name or "").strip()
        image_gen_model_name = (image_gen_model_name or "").strip()
    else:
        if user.get("role") == ROLE_ADMIN:
            mode_to_use = generation_mode or _pick_user_mode(user)
        else:
            mode_to_use = _pick_user_mode(user)
        cfg = _mode_to_config(user, mode_to_use, tier_templates)
        exp_mode = cfg.get("exp_mode", exp_mode)
        retrieval_setting = cfg.get("retrieval_setting", retrieval_setting)
        num_candidates = int(cfg.get("num_candidates", num_candidates))
        aspect_ratio = cfg.get("aspect_ratio", aspect_ratio)
        max_critic_rounds = int(cfg.get("max_critic_rounds", max_critic_rounds))
        main_model_name = cfg.get("main_model_name", main_model_name)
        image_gen_model_name = cfg.get("image_gen_model_name", image_gen_model_name)
    main_model_name = (main_model_name or "").strip()
    image_gen_model_name = (image_gen_model_name or "").strip()
    if is_gpt_model_name(main_model_name) or is_gpt_model_name(image_gen_model_name):
        ctx = _index_context(user, tier_templates, error=GPT_DIRECT_MODE_ERROR, db=db)
        ctx["generation_unit_price"] = get_generation_unit_price_yuan(db)
        return _render_template(request, "index.html", ctx)
    need = max(1, int(num_candidates)) * (1 + max(1, int(max_critic_rounds)))
    ok_quota, msg_quota = consume_user_generation_quota(db, user["username"], need)
    if not ok_quota:
        ctx = _index_context(user, tier_templates, error=msg_quota, db=db)
        ctx["generation_unit_price"] = get_generation_unit_price_yuan(db)
        return _render_template(request, "index.html", ctx)

    data_list = create_sample_inputs(
        method_content=method_content,
        caption=caption,
        aspect_ratio=aspect_ratio,
        num_copies=max(1, min(20, int(num_candidates))),
        max_critic_rounds=max(1, min(5, int(max_critic_rounds))),
    )
    job_id = spawn_generation_job(
        user["username"],
        data_list=data_list,
        exp_mode=exp_mode,
        retrieval_setting=retrieval_setting,
        main_model_name=main_model_name,
        image_gen_model_name=image_gen_model_name,
        quota_need=need,
    )
    request.session["pending_generation_job_id"] = job_id
    return RedirectResponse(f"/generate/wait/{job_id}", status_code=303)


@router.post("/api/sub2api/generate")
async def sub2api_generate(request: Request):
    guard = _require_sub2api_service(request)
    if guard:
        return guard
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)

    username = str(payload.get("username") or payload.get("user_id") or "sub2api_user").strip()
    username = _safe_sso_username(payload.get("user_id"), str(payload.get("email") or ""), username) or "sub2api_user"
    method_content = str(payload.get("method_content") or "").strip()
    if not method_content:
        return JSONResponse({"ok": False, "error": "method_content is required"}, status_code=400)

    caption = str(payload.get("caption") or "")
    exp_mode = str(payload.get("exp_mode") or "demo_planner_critic").strip() or "demo_planner_critic"
    if exp_mode not in {"demo_planner_critic", "demo_full"}:
        exp_mode = "demo_planner_critic"
    retrieval_setting = str(payload.get("retrieval_setting") or "auto").strip() or "auto"
    if retrieval_setting not in {"auto", "manual", "random", "none"}:
        retrieval_setting = "auto"
    aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip() or "16:9"
    if aspect_ratio not in {"16:9", "21:9", "3:2"}:
        aspect_ratio = "16:9"
    try:
        num_candidates = max(1, min(20, int(payload.get("num_candidates") or 1)))
    except Exception:
        num_candidates = 1
    try:
        max_critic_rounds = max(1, min(5, int(payload.get("max_critic_rounds") or 3)))
    except Exception:
        max_critic_rounds = 3
    main_model_name = str(payload.get("main_model_name") or "").strip()
    image_gen_model_name = str(payload.get("image_gen_model_name") or "").strip()
    if is_gpt_model_name(main_model_name) or is_gpt_model_name(image_gen_model_name):
        return JSONResponse({"ok": False, "error": GPT_DIRECT_MODE_ERROR}, status_code=400)
    quota_need = max(1, int(num_candidates)) * (1 + max(1, int(max_critic_rounds)))

    data_list = create_sample_inputs(
        method_content=method_content,
        caption=caption,
        aspect_ratio=aspect_ratio,
        num_copies=num_candidates,
        max_critic_rounds=max_critic_rounds,
    )
    job_id = spawn_generation_job(
        username,
        data_list=data_list,
        exp_mode=exp_mode,
        retrieval_setting=retrieval_setting,
        main_model_name=main_model_name,
        image_gen_model_name=image_gen_model_name,
        quota_need=quota_need,
    )
    return JSONResponse(
        {
            "ok": True,
            "job_id": job_id,
            "username": username,
            "quota_need": quota_need,
            "candidate_count": num_candidates,
            "status_url": f"/api/sub2api/job/{job_id}?username={username}",
        }
    )


@router.get("/api/sub2api/job/{job_id}")
def sub2api_job_status(request: Request, job_id: str, username: str = Query(...)):
    guard = _require_sub2api_service(request)
    if guard:
        return guard
    username = str(username or "").strip()
    if not username:
        return JSONResponse({"ok": False, "error": "username is required"}, status_code=400)
    j = get_job_poll_json(job_id, username)
    if not j:
        disk_j = get_job_disk_poll_json(username, job_id)
        if disk_j:
            j = disk_j
        else:
            return JSONResponse({"ok": False, "status": "unknown", "error": "job not found"}, status_code=404)
    if str(j.get("status") or "") == "done":
        candidate_ids = j.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            candidate_count = max(0, int(j.get("candidate_count") or 0))
            candidate_ids = list(range(candidate_count))
        j["images"] = [
            {
                "candidate_id": int(candidate_id),
                "url": f"/api/sub2api/job/{job_id}/image/{int(candidate_id)}?username={username}",
            }
            for candidate_id in candidate_ids
        ]
    out = {"ok": True, "job_id": job_id, "username": username}
    out.update(j)
    return JSONResponse(out)


@router.get("/api/sub2api/job/{job_id}/image/{candidate_id}")
def sub2api_job_image(request: Request, job_id: str, candidate_id: int, username: str = Query(...)):
    guard = _require_sub2api_service(request)
    if guard:
        return guard
    username = str(username or "").strip()
    if not username:
        return JSONResponse({"ok": False, "error": "username is required"}, status_code=400)
    p = get_job_candidate_png_path(username, job_id, candidate_id)
    if not p.is_file():
        return JSONResponse({"ok": False, "error": "image not found"}, status_code=404)
    return FileResponse(path=str(p), media_type="image/png")


@router.get("/generate/wait/{job_id}", response_class=HTMLResponse)
def generate_wait_page(request: Request, job_id: str, db: Session = Depends(get_db)):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    user = _refresh_session_user(request, db, user)
    if not job_is_owned(job_id, user["username"]):
        return RedirectResponse("/", status_code=303)
    return _render_template(
        request,
        "generate_wait.html",
        {
            "user": user,
            "title": "生成中",
            "job_id": job_id,
            "gen_estimated_seconds": _estimated_generation_seconds(db, user),
        },
    )


@router.get("/generate/job/{job_id}")
def generate_job_status(request: Request, job_id: str):
    user = _current_user(request)
    if not user:
        return JSONResponse({"status": "unknown", "error": "未登录"}, status_code=401)
    j = get_job_poll_json(job_id, user["username"])
    if not j:
        return JSONResponse({"status": "unknown", "error": "任务不存在"}, status_code=404)
    return JSONResponse(j)


@router.get("/generate/finish/{job_id}")
def generate_finish(request: Request, job_id: str):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    err, rows, exp_mode, elapsed_sec = pop_job_for_session(job_id, user["username"])
    if err:
        # In-memory job might already be gone (server restart / session lost). Try disk manifest.
        disk_err, disk_rows, disk_exp_mode, disk_elapsed_sec = take_job_from_disk_for_session(
            user["username"], job_id
        )
        if disk_err is None and disk_rows is not None and disk_exp_mode is not None and disk_elapsed_sec is not None:
            rows = disk_rows
            exp_mode = disk_exp_mode
            elapsed_sec = disk_elapsed_sec
        else:
            request.session["flash_error"] = disk_err or err
            return RedirectResponse("/", status_code=303)
    if rows is None or exp_mode is None or elapsed_sec is None:
        request.session["flash_error"] = "无法读取生成结果。"
        return RedirectResponse("/", status_code=303)
    request.session["last_results"] = rows
    request.session["last_generation_job_id"] = job_id
    request.session["last_exp_mode"] = exp_mode
    request.session.pop("pending_generation_job_id", None)
    request.session["flash_success"] = (
        f"已完成生成，共 {len(rows)} 个候选。本次实际生成耗时约 {_fmt_duration_cn(elapsed_sec)}。"
    )
    return RedirectResponse("/", status_code=303)


@router.get("/results/image/{candidate_id}")
def results_image(candidate_id: int, request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    rows = request.session.get("last_results") or []
    if not isinstance(rows, list):
        rows = []
    exp_mode = request.session.get("last_exp_mode") or "demo_planner_critic"
    job_id = request.session.get("last_generation_job_id") or ""
    if job_id and user:
        p = get_job_candidate_png_path(user["username"], job_id, candidate_id)
        if p.is_file():
            return FileResponse(path=str(p), media_type="image/png")

    # Backward compatibility: older sessions may still keep raw base64 in session.
    if 0 <= candidate_id < len(rows):
        raw = rows[candidate_id].get("raw", {})
        png = result_image_to_png_bytes(raw, exp_mode)
        if png:
            return Response(content=png, media_type="image/png")
    return RedirectResponse("/", status_code=303)


@router.get("/results/download/{candidate_id}")
def results_download(candidate_id: int, request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    rows = request.session.get("last_results") or []
    if not isinstance(rows, list):
        rows = []
    exp_mode = request.session.get("last_exp_mode") or "demo_planner_critic"
    job_id = request.session.get("last_generation_job_id") or ""
    if job_id and user:
        p = get_job_candidate_png_path(user["username"], job_id, candidate_id)
        if p.is_file():
            return FileResponse(
                path=str(p),
                media_type="image/png",
                filename=f"candidate_{candidate_id}.png",
            )

    # Backward compatibility: older sessions may still keep raw base64 in session.
    if 0 <= candidate_id < len(rows):
        raw = rows[candidate_id].get("raw", {})
        png = result_image_to_png_bytes(raw, exp_mode)
        if png:
            return Response(
                content=png,
                media_type="image/png",
                headers={
                    "Content-Disposition": f'attachment; filename=\"candidate_{candidate_id}.png\"'
                },
            )
    return RedirectResponse("/", status_code=303)


@router.post("/results/discard/{candidate_id}")
def results_discard(candidate_id: int, request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    rows = request.session.get("last_results") or []
    if not isinstance(rows, list):
        rows = []
    if not rows:
        return RedirectResponse("/?tab=panel-generate", status_code=303)

    job_id = request.session.get("last_generation_job_id") or ""
    next_rows: list[dict[str, Any]] = []
    # candidate_id 可能不连续：通过显式 candidate_id 匹配，而不是依赖 list index。
    for idx, r in enumerate(rows):
        cid = r.get("candidate_id", idx)
        try:
            cid_i = int(cid)
        except Exception:
            cid_i = idx
        if cid_i == int(candidate_id):
            continue
        next_rows.append(r)

    request.session["last_results"] = next_rows

    # 删除临时结果文件（用于释放磁盘 + 避免后续下载拿到旧文件）
    if job_id and user:
        p = get_job_candidate_png_path(user["username"], job_id, int(candidate_id))
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass

    return RedirectResponse("/?tab=panel-results", status_code=303)


@router.post("/account/change-password")
def account_change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    if _HIDE_ACCOUNT_FEATURES:
        return _disabled_account_redirect(request)
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    tier_templates = get_tier_templates(db)
    if new_password != new_password_confirm:
        ctx = _index_context(user, tier_templates, pwd_msg="两次新密码不一致。", db=db)
        ctx["generation_unit_price"] = get_generation_unit_price_yuan(db)
        return _render_template(request, "index.html", ctx)
    ok, msg = change_user_password(db, user["username"], current_password, new_password)
    user = _refresh_session_user(request, db, user)
    ctx = _index_context(user, tier_templates, pwd_msg=msg if ok else msg, db=db)
    ctx["generation_unit_price"] = get_generation_unit_price_yuan(db)
    return _render_template(request, "index.html", ctx)


@router.post("/billing/create-order")
def billing_create_order(
    request: Request,
    buy_times: int = Form(1),
    pay_channel: str = Form("alipay"),
    price_package_id: str = Form(""),
    user_message: str = Form(""),
    payer_note: str = Form(""),
    db: Session = Depends(get_db),
):
    if _HIDE_ACCOUNT_FEATURES:
        return _disabled_account_redirect(request, "/?tab=panel-generate")
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    if user.get("role") == ROLE_ADMIN:
        return RedirectResponse("/", status_code=303)
    now_ts = time.time()
    last_ts = float(request.session.get("last_apply_submit_ts") or 0.0)
    if now_ts - last_ts < 8:
        request.session["flash_error"] = "请勿重复提交申请，8 秒后再试。"
        return RedirectResponse("/?tab=panel-account", status_code=303)
    channel = (pay_channel or "").strip().lower()
    if channel not in {"alipay", "wechat", "customer_service_wechat"}:
        channel = "alipay"

    times = max(1, int(buy_times))
    pkg_id = (price_package_id or "").strip()
    pkg_total_yuan: float | None = None
    if pkg_id:
        for pkg in get_price_packages(db):
            if str(pkg.get("id", "")).strip() == pkg_id:
                try:
                    times = max(1, int(pkg.get("times", times)))
                except Exception:
                    pass
                try:
                    raw_total = pkg.get("price_yuan")
                    pkg_total_yuan = float(raw_total) if raw_total is not None else None
                except Exception:
                    pkg_total_yuan = None
                break

    note = (payer_note or "").strip()[:200]
    user_suggestion = (user_message or "").strip()

    unit_price = get_generation_unit_price_yuan(db)
    total = round(float(pkg_total_yuan) if pkg_total_yuan is not None else unit_price * times, 2)
    order_no = f"RQ{datetime.now().strftime('%Y%m%d%H%M%S')}{secrets.randbelow(10**6):06d}"
    order = PaymentOrder(
        order_no=order_no,
        username=user["username"],
        unit_price_yuan=unit_price,
        buy_times=times,
        total_amount_yuan=total,
        status="created",
        notify_payload=json.dumps(
            {"pay_channel": channel, "payer_note": note, "price_package_id": pkg_id},
            ensure_ascii=False,
        ),
    )
    db.add(order)
    db.commit()

    if user_suggestion:
        try:
            db.add(
                UserMessage(
                    username=user["username"],
                    order_no=order_no,
                    pay_channel=channel,
                    content=user_suggestion[:2000],
                )
            )
            db.commit()
        except Exception:
            db.rollback()

    request.session["last_apply_submit_ts"] = now_ts
    request.session["flash_success"] = "申请已提交，预计 3-6 分钟内完成授权，请耐心等待。"
    return RedirectResponse("/?tab=panel-account", status_code=303)


@router.post("/gallery/save")
def save_gallery_entry(request: Request, candidate_id: int = Form(...)):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    rows = request.session.get("last_results") or []
    job_id = request.session.get("last_generation_job_id") or ""
    exp_mode = request.session.get("last_exp_mode") or "demo_planner_critic"
    if not isinstance(rows, list) or candidate_id < 0 or candidate_id >= len(rows):
        return RedirectResponse("/", status_code=303)
    png: bytes | None = None
    if job_id:
        p = get_job_candidate_png_path(user["username"], job_id, candidate_id)
        if p.is_file():
            try:
                png = p.read_bytes()
            except OSError:
                png = None

    # Backward compatibility: older sessions may still keep raw base64 in session.
    if not png:
        raw = rows[candidate_id].get("raw", {})
        png = result_image_to_png_bytes(raw, exp_mode)
    if png:
        user_gallery.save_png_to_gallery(
            user["username"],
            png,
            source="fastapi-demo",
            caption=f"candidate-{candidate_id}",
        )
    return RedirectResponse("/", status_code=303)


@router.post("/gallery/save-all")
def save_all_gallery_entries(request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    rows = request.session.get("last_results") or []
    job_id = request.session.get("last_generation_job_id") or ""
    exp_mode = request.session.get("last_exp_mode") or "demo_planner_critic"
    if not isinstance(rows, list) or not rows:
        return RedirectResponse("/", status_code=303)
    for i, row in enumerate(rows):
        cand_id = row.get("candidate_id", i)
        png: bytes | None = None
        if job_id:
            p = get_job_candidate_png_path(user["username"], job_id, int(cand_id))
            if p.is_file():
                try:
                    png = p.read_bytes()
                except OSError:
                    png = None
        if not png:
            raw = row.get("raw", {})
            png = result_image_to_png_bytes(raw, exp_mode)
        if png:
            user_gallery.save_png_to_gallery(
                user["username"],
                png,
                source="fastapi-demo",
                caption=f"candidate-{cand_id}",
            )
    return RedirectResponse("/?tab=panel-results", status_code=303)


@router.get("/results/download-zip")
def download_results_zip(request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    rows = request.session.get("last_results") or []
    job_id = request.session.get("last_generation_job_id") or ""
    exp_mode = request.session.get("last_exp_mode") or "demo_planner_critic"
    if not isinstance(rows, list) or not rows:
        return RedirectResponse("/", status_code=303)
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, row in enumerate(rows):
            cand_id = row.get("candidate_id", i)
            if job_id:
                p = get_job_candidate_png_path(_current_user(request)["username"], job_id, int(cand_id))
                if p.is_file():
                    zf.write(str(p), arcname=f"candidate_{cand_id}.png")
                    continue
            # Backward compatibility
            raw = row.get("raw", {})
            png = result_image_to_png_bytes(raw, exp_mode)
            if png:
                zf.writestr(f"candidate_{cand_id}.png", png)
    mem.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=mem.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="results_{ts}.zip"'},
    )


@router.get("/gallery/image/{entry_id}")
def gallery_image(entry_id: str, request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    p = user_gallery.get_gallery_image_path(user["username"], entry_id)
    if not p:
        return RedirectResponse("/", status_code=303)
    return FileResponse(path=str(p), media_type="image/png")


@router.get("/gallery/download/{entry_id}")
def gallery_download(entry_id: str, request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    p = user_gallery.get_gallery_image_path(user["username"], entry_id)
    if not p:
        return RedirectResponse("/", status_code=303)
    return FileResponse(path=str(p), media_type="image/png", filename=f"{entry_id}.png")


@router.post("/gallery/delete/{entry_id}")
def gallery_delete(entry_id: str, request: Request):
    guard = _require_login(request)
    if guard:
        return guard
    user = _current_user(request)
    user_gallery.delete_gallery_entry(user["username"], entry_id)
    return RedirectResponse("/?tab=panel-library", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, db: Session = Depends(get_db)):
    if _HIDE_ACCOUNT_FEATURES:
        return _disabled_account_redirect(request)
    guard = _require_admin(request)
    if guard:
        return guard
    users = list_managed_users(db)
    tier_templates = get_tier_templates(db)
    view = (request.query_params.get("view", "overview") or "overview").strip()
    if view not in {"overview", "users", "templates", "orders"}:
        view = "overview"
    reqs = list_tier_requests(db)
    q = request.query_params.get("q", "").strip()
    role_filter = request.query_params.get("role_filter", "").strip()
    auth_filter = request.query_params.get("auth_filter", "").strip()
    order_user = request.query_params.get("order_user", "").strip()
    order_status = request.query_params.get("order_status", "").strip()
    order_page = max(1, _safe_int(request.query_params.get("order_page", "1"), 1))
    order_page_size = max(10, min(100, _safe_int(request.query_params.get("order_page_size", "20"), 20)))
    page_size = max(10, min(50, _safe_int(request.query_params.get("page_size", "20"), 20)))
    page = max(1, _safe_int(request.query_params.get("page", "1"), 1))
    filtered = {}
    for uname, info in users.items():
        if q and q.lower() not in uname.lower():
            continue
        if role_filter and info.get("role") != role_filter:
            continue
        if auth_filter == "authorized" and not info.get("authorized"):
            continue
        if auth_filter == "unauthorized" and info.get("authorized"):
            continue
        filtered[uname] = info
    items = sorted(filtered.items(), key=lambda x: x[0].lower())
    total_filtered = len(items)
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    end = start + page_size
    users_page = dict(items[start:end])
    detail_user = (request.query_params.get("detail_user", "") or "").strip()
    detail_info = filtered.get(detail_user) if detail_user else None
    if detail_user and detail_info is None:
        detail_user = ""
    oq = select(PaymentOrder).order_by(PaymentOrder.created_at.desc())
    if order_user:
        oq = oq.where(PaymentOrder.username == order_user)
    if order_status:
        oq = oq.where(PaymentOrder.status == order_status)
    all_order_rows = db.execute(oq).scalars().all()
    order_total = len(all_order_rows)
    order_total_pages = max(1, (order_total + order_page_size - 1) // order_page_size)
    order_page = min(order_page, order_total_pages)
    ostart = (order_page - 1) * order_page_size
    oend = ostart + order_page_size
    order_rows = all_order_rows[ostart:oend]
    admin_orders = []
    for o in order_rows:
        meta = _parse_order_meta(o.notify_payload or "")
        admin_orders.append(
            {
                "order_no": o.order_no,
                "username": o.username,
                "status": o.status,
                "status_label": _order_status_label(o.status),
                "buy_times": o.buy_times,
                "unit_price_yuan": f"{o.unit_price_yuan:.2f}",
                "total_amount_yuan": f"{o.total_amount_yuan:.2f}",
                "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "paid_at": o.paid_at.strftime("%Y-%m-%d %H:%M:%S") if o.paid_at else "",
                "pay_channel": meta.get("pay_channel", ""),
                "payer_note": meta.get("payer_note", ""),
                "price_package_id": meta.get("price_package_id", ""),
                "notify_payload": o.notify_payload or "",
            }
        )

    page_order_nos = [o.order_no for o in order_rows]
    message_rows: list[dict[str, Any]] = []
    if page_order_nos:
        msgs = db.execute(select(UserMessage).where(UserMessage.order_no.in_(page_order_nos))).scalars().all()
        msg_ids = [m.id for m in msgs]
        reply_map: dict[int, UserMessageReply] = {}
        if msg_ids:
            replies = (
                db.execute(select(UserMessageReply).where(UserMessageReply.message_id.in_(msg_ids)))
                .scalars()
                .all()
            )
            reply_map = {r.message_id: r for r in replies if r is not None}
        for m in msgs:
            r = reply_map.get(m.id)
            message_rows.append(
                {
                    "id": m.id,
                    "username": m.username,
                    "order_no": m.order_no,
                    "pay_channel": m.pay_channel,
                    "content": m.content,
                    "admin_reply": r.content if r else "",
                    "admin_reply_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r else "",
                    "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    by_channel: dict[str, int] = {}
    for m in message_rows:
        k = str(m.get("pay_channel") or "unknown")
        by_channel[k] = by_channel.get(k, 0) + 1
    message_stats = {"total_messages": len(message_rows), "by_channel": by_channel}
    all_user_values = list(users.values())
    tier_override_ids = get_tier_template_override_tier_ids(db)
    customer_service_qr_image_url = _get_customer_service_qr_url(db)
    alipay_qr_image_url, wechat_qr_image_url = _get_payment_qr_urls(db)
    price_packages = get_price_packages(db)
    price_pkg_map: dict[str, dict[str, Any]] = {str(p.get("id", "")): p for p in (price_packages or [])}
    for ao in admin_orders:
        pkg_id = str(ao.get("price_package_id") or "").strip()
        if not pkg_id or pkg_id == "custom":
            ao["combo_label"] = f"自定义（{ao.get('buy_times') or 1}次）"
            continue
        pkg = price_pkg_map.get(pkg_id) if pkg_id else None
        if pkg and pkg.get("label"):
            ao["combo_label"] = f"{pkg.get('label')}（{ao.get('buy_times') or 1}次）"
        else:
            ao["combo_label"] = pkg_id
    overview_stats = {
        "total_users": len(users),
        "admins": sum(1 for u in all_user_values if u.get("role") == ROLE_ADMIN),
        "normal_users": sum(1 for u in all_user_values if u.get("role") != ROLE_ADMIN),
        "authorized_users": sum(1 for u in all_user_values if u.get("authorized")),
        "unauthorized_users": sum(1 for u in all_user_values if not u.get("authorized")),
        "total_orders": order_total,
        "paid_orders": sum(1 for o in all_order_rows if o.status == "paid"),
        "created_orders": sum(1 for o in all_order_rows if o.status == "created"),
    }
    return _render_template(
        request,
        "admin_users_accounts.html",
        {
            "user": _current_user(request),
            "users": users_page,
            "detail_user": detail_user,
            "detail_info": detail_info,
            "reqs": reqs,
            "tier_order": TIER_ORDER,
            "tier_label": lambda tid: _tier_label_from_templates(tier_templates, tid),
            "tier_templates": tier_templates,
            "default_new_user_mode": get_default_new_user_mode(db),
            "generation_unit_price": get_generation_unit_price_yuan(db),
            "q": q,
            "role_filter": role_filter,
            "auth_filter": auth_filter,
            "page_size": page_size,
            "page": page,
            "total_pages": total_pages,
            "total_filtered": total_filtered,
            "view": view,
            "user_crud_msg": request.query_params.get("msg", ""),
            "order_user": order_user,
            "order_status": order_status,
            "order_page": order_page,
            "order_page_size": order_page_size,
            "order_total": order_total,
            "order_total_pages": order_total_pages,
            "admin_orders": admin_orders,
            "message_rows": message_rows,
            "message_stats": message_stats,
            "overview_stats": overview_stats,
            "tier_override_ids": tier_override_ids,
            "customer_service_wechat_qr_image_url": customer_service_qr_image_url,
            "alipay_qr_image_url": alipay_qr_image_url,
            "wechat_qr_image_url": wechat_qr_image_url,
            "price_packages": price_packages,
            "error": "",
            "success": "",
        },
    )


@router.post("/admin/templates/{tier_id}/upsert")
def admin_upsert_tier_template(
    tier_id: str,
    request: Request,
    template_label: str = Form(""),
    exp_mode: str = Form("demo_planner_critic"),
    retrieval_setting: str = Form("auto"),
    num_candidates: int = Form(1),
    aspect_ratio: str = Form("16:9"),
    max_critic_rounds: int = Form(2),
    main_model_name: str = Form(""),
    image_gen_model_name: str = Form(""),
    max_refine_resolution: str = Form("2K"),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard
    cfg = {
        "exp_mode": exp_mode,
        "retrieval_setting": retrieval_setting,
        "num_candidates": num_candidates,
        "aspect_ratio": aspect_ratio,
        "max_critic_rounds": max_critic_rounds,
        "main_model_name": main_model_name.strip(),
        "image_gen_model_name": image_gen_model_name.strip(),
        "max_refine_resolution": max_refine_resolution,
    }
    ok, _ = upsert_tier_template_by_admin(db, tier_id=tier_id, label=template_label, cfg=cfg)
    return RedirectResponse(f"/admin/users?view=templates&msg={'template_saved' if ok else 'template_save_failed'}", status_code=303)


@router.post("/admin/templates/{tier_id}/delete")
def admin_delete_tier_template(tier_id: str, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    ok, _ = delete_tier_template_by_admin(db, tier_id=tier_id)
    return RedirectResponse(
        f"/admin/users?view=templates&msg={'template_deleted' if ok else 'template_delete_failed'}",
        status_code=303,
    )


@router.post("/admin/templates/{tier_id}/reset")
def admin_reset_tier_template(tier_id: str, request: Request, db: Session = Depends(get_db)):
    """兼容旧链接；与 delete 相同。"""
    guard = _require_admin(request)
    if guard:
        return guard
    ok, _ = reset_tier_template_by_admin(db, tier_id=tier_id)
    return RedirectResponse(f"/admin/users?view=templates&msg={'template_deleted' if ok else 'template_delete_failed'}", status_code=303)


@router.get("/admin/orders/export-csv")
def admin_export_orders_csv(
    request: Request,
    order_user: str = Query("", description="username filter"),
    order_status: str = Query("", description="status filter"),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard
    oq = select(PaymentOrder).order_by(PaymentOrder.created_at.desc())
    if order_user.strip():
        oq = oq.where(PaymentOrder.username == order_user.strip())
    if order_status.strip():
        oq = oq.where(PaymentOrder.status == order_status.strip())
    rows = db.execute(oq).scalars().all()
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(
        [
            "request_no",
            "username",
            "status",
            "apply_times",
            "unit_price_yuan",
            "total_amount_yuan",
            "pay_channel",
            "payer_note",
            "created_at",
            "reviewed_at",
        ]
    )
    for o in rows:
        meta = _parse_order_meta(o.notify_payload or "")
        writer.writerow(
            [
                o.order_no,
                o.username,
                _order_status_label(o.status),
                o.buy_times,
                f"{o.unit_price_yuan:.2f}",
                f"{o.total_amount_yuan:.2f}",
                meta.get("pay_channel", ""),
                meta.get("payer_note", ""),
                o.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                o.paid_at.strftime("%Y-%m-%d %H:%M:%S") if o.paid_at else "",
            ]
        )
    content = sio.getvalue().encode("utf-8-sig")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="orders_{ts}.csv"'},
    )


@router.post("/admin/requests/{idx}/approve")
def admin_approve_request(idx: int, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    approve_access_request(db, idx)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/requests/{idx}/reject")
def admin_reject_request(idx: int, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    reject_access_request(db, idx)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/authorized")
def admin_set_authorized(username: str, request: Request, authorized: int = Form(...), db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    set_user_authorized(db, username, bool(int(authorized)))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/tiers")
async def admin_set_tiers(username: str, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    form = await request.form()
    tiers = form.getlist("tiers")
    set_user_allowed_tiers(db, username, list(tiers))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/edit-perm")
def admin_toggle_edit(username: str, request: Request, can_edit_image: int = Form(...), db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    set_user_edit_permission(db, username, bool(int(can_edit_image)))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/method-opt-perm")
def admin_toggle_method_opt(
    username: str,
    request: Request,
    can_optimize_method: int = Form(...),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard
    set_user_method_optimize_permission(db, username, bool(int(can_optimize_method)))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/quota")
def admin_set_quota(username: str, request: Request, quota_raw: str = Form(""), db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    quota = None if not quota_raw.strip() else max(0, int(quota_raw))
    set_user_generation_quota(db, username, quota)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/remove-preset")
def admin_remove_preset(username: str, request: Request, preset_id: str = Form(...), db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    remove_user_preset(db, username, preset_id)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/default-mode")
def admin_set_user_default_mode(
    username: str, request: Request, mode: str = Form(""), db: Session = Depends(get_db)
):
    guard = _require_admin(request)
    if guard:
        return guard
    set_user_default_gen_mode(db, username, mode.strip())
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/add-preset")
def admin_add_preset_to_user(
    username: str,
    request: Request,
    preset_label: str = Form(""),
    exp_mode: str = Form("demo_planner_critic"),
    retrieval_setting: str = Form("auto"),
    num_candidates: int = Form(1),
    aspect_ratio: str = Form("16:9"),
    max_critic_rounds: int = Form(2),
    main_model_name: str = Form(""),
    image_gen_model_name: str = Form(""),
    max_refine_resolution: str = Form("2K"),
    set_as_default: int = Form(0),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard
    cfg = {
        "exp_mode": exp_mode,
        "retrieval_setting": retrieval_setting,
        "num_candidates": num_candidates,
        "aspect_ratio": aspect_ratio,
        "max_critic_rounds": max_critic_rounds,
        "main_model_name": main_model_name.strip(),
        "image_gen_model_name": image_gen_model_name.strip(),
        "max_refine_resolution": max_refine_resolution,
    }
    ok, msg = add_user_preset_by_admin(db, username, preset_label, cfg)
    if ok and set_as_default:
        users = list_managed_users(db)
        info = users.get(username, {})
        presets = info.get("approved_presets") or []
        if presets:
            set_user_default_gen_mode(db, username, f"preset:{presets[-1]['id']}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{username}/update-preset")
def admin_update_preset_of_user(
    username: str,
    request: Request,
    preset_id: str = Form(...),
    preset_label: str = Form(""),
    exp_mode: str = Form("demo_planner_critic"),
    retrieval_setting: str = Form("auto"),
    num_candidates: int = Form(1),
    aspect_ratio: str = Form("16:9"),
    max_critic_rounds: int = Form(2),
    main_model_name: str = Form(""),
    image_gen_model_name: str = Form(""),
    max_refine_resolution: str = Form("2K"),
    set_as_default: int = Form(0),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard
    cfg = {
        "exp_mode": exp_mode,
        "retrieval_setting": retrieval_setting,
        "num_candidates": num_candidates,
        "aspect_ratio": aspect_ratio,
        "max_critic_rounds": max_critic_rounds,
        "main_model_name": main_model_name.strip(),
        "image_gen_model_name": image_gen_model_name.strip(),
        "max_refine_resolution": max_refine_resolution,
    }
    ok, _ = update_user_preset_by_admin(db, username, preset_id, preset_label, cfg)
    if ok and set_as_default:
        set_user_default_gen_mode(db, username, f"preset:{preset_id}")
    return RedirectResponse("/admin/users?msg=preset_updated", status_code=303)


@router.post("/admin/settings/default-new-user-mode")
def admin_set_global_default_mode(
    request: Request, mode: str = Form(""), db: Session = Depends(get_db)
):
    guard = _require_admin(request)
    if guard:
        return guard
    set_default_new_user_mode(db, mode.strip())
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/settings/generation-unit-price")
def admin_set_generation_unit_price(
    request: Request,
    unit_price_yuan: str = Form("3.0"),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard
    try:
        price = float(unit_price_yuan)
    except Exception:
        price = -1
    set_generation_unit_price_yuan(db, price)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/settings/default-new-user-mode/apply-existing")
def admin_apply_global_default_mode_to_existing_users(request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    apply_default_new_user_mode_to_all_users(db)
    return RedirectResponse("/admin/users?msg=default_applied", status_code=303)


@router.post("/admin/settings/payment-qr/{channel}")
def admin_upload_payment_qr(
    channel: str,
    request: Request,
    qr_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    手动上传收款码图片（仅展示给用户扫码支付，不做自动回调校验）。
    """
    guard = _require_admin(request)
    if guard:
        return guard
    ch = (channel or "").strip().lower()
    if ch not in _PAYMENT_QR_KEY_BY_CHANNEL:
        return RedirectResponse("/admin/users?view=orders&msg=qr_update_failed", status_code=303)

    raw = qr_file.file.read()
    if not raw or len(raw) < 128:
        return RedirectResponse("/admin/users?view=orders&msg=qr_update_failed", status_code=303)
    if len(raw) > 5 * 1024 * 1024:
        return RedirectResponse("/admin/users?view=orders&msg=qr_update_failed", status_code=303)

    ext = ".png"
    name = (qr_file.filename or "").lower()
    if name.endswith(".jpg") or name.endswith(".jpeg"):
        ext = ".jpg"
    elif name.endswith(".webp"):
        ext = ".webp"

    _PAYMENT_QR_DIR.mkdir(parents=True, exist_ok=True)
    for old in _PAYMENT_QR_DIR.glob(f"{ch}.*"):
        try:
            old.unlink()
        except OSError:
            pass

    out_name = f"{ch}{ext}"
    out_path = _PAYMENT_QR_DIR / out_name
    try:
        out_path.write_bytes(raw)
    except OSError:
        return RedirectResponse("/admin/users?view=orders&msg=qr_update_failed", status_code=303)

    # Append version to avoid browser cache.
    url = f"/static/payment_qr/{out_name}?v={int(time.time())}"
    key = _PAYMENT_QR_KEY_BY_CHANNEL[ch]
    row = db.get(AppSetting, key)
    if not row:
        db.add(AppSetting(key=key, value=url))
    else:
        row.value = url
    db.commit()
    return RedirectResponse("/admin/users?view=orders&msg=qr_updated", status_code=303)


@router.post("/admin/settings/customer-service-wechat-qr")
def admin_upload_customer_service_wechat_qr(
    request: Request,
    qr_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    UI: replace payment QR with customer service contact QR.
    Users only see this QR as a contact/payment instruction.
    """
    guard = _require_admin(request)
    if guard:
        return guard

    raw = qr_file.file.read()
    if not raw or len(raw) < 128:
        return RedirectResponse("/admin/users?view=orders&msg=qr_update_failed", status_code=303)
    if len(raw) > 5 * 1024 * 1024:
        return RedirectResponse("/admin/users?view=orders&msg=qr_update_failed", status_code=303)

    ext = ".png"
    name = (qr_file.filename or "").lower()
    if name.endswith(".jpg") or name.endswith(".jpeg"):
        ext = ".jpg"
    elif name.endswith(".webp"):
        ext = ".webp"

    _PAYMENT_QR_DIR.mkdir(parents=True, exist_ok=True)
    for old in _PAYMENT_QR_DIR.glob("customer_service_wechat_qr.*"):
        try:
            old.unlink()
        except OSError:
            pass

    out_name = f"customer_service_wechat_qr{ext}"
    out_path = _PAYMENT_QR_DIR / out_name
    try:
        out_path.write_bytes(raw)
    except OSError:
        return RedirectResponse("/admin/users?view=orders&msg=qr_update_failed", status_code=303)

    url = f"/static/payment_qr/{out_name}?v={int(time.time())}"
    row = db.get(AppSetting, _CUSTOMER_SERVICE_QR_KEY)
    if not row:
        db.add(AppSetting(key=_CUSTOMER_SERVICE_QR_KEY, value=url))
    else:
        row.value = url
    db.commit()
    return RedirectResponse("/admin/users?view=orders&msg=qr_updated", status_code=303)


@router.post("/admin/settings/price-packages")
def admin_set_price_packages(
    request: Request,
    pkg1_label: str = Form(""),
    pkg1_times: int = Form(1),
    pkg1_price_yuan: str = Form(""),
    pkg2_label: str = Form(""),
    pkg2_times: int = Form(3),
    pkg2_price_yuan: str = Form(""),
    pkg3_label: str = Form(""),
    pkg3_times: int = Form(5),
    pkg3_price_yuan: str = Form(""),
    pkg4_label: str = Form(""),
    pkg4_times: int = Form(1),
    pkg4_price_yuan: str = Form(""),
    pkg5_label: str = Form(""),
    pkg5_times: int = Form(1),
    pkg5_price_yuan: str = Form(""),
    pkg6_label: str = Form(""),
    pkg6_times: int = Form(1),
    pkg6_price_yuan: str = Form(""),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard

    packages = []
    for pid, label, times, price_yuan in (
        ("p1", pkg1_label, pkg1_times, pkg1_price_yuan),
        ("p2", pkg2_label, pkg2_times, pkg2_price_yuan),
        ("p3", pkg3_label, pkg3_times, pkg3_price_yuan),
        ("p4", pkg4_label, pkg4_times, pkg4_price_yuan),
        ("p5", pkg5_label, pkg5_times, pkg5_price_yuan),
        ("p6", pkg6_label, pkg6_times, pkg6_price_yuan),
    ):
        label_s = (label or "").strip()
        if not label_s:
            continue
        try:
            times_i = int(times)
        except Exception:
            continue
        if times_i < 1:
            continue
        packages.append(
            {"id": pid, "label": label_s, "times": times_i, "price_yuan": (price_yuan or "").strip()}
        )

    ok, msg = set_price_packages(db, packages)
    if ok:
        return RedirectResponse("/admin/users?view=orders&msg=packages_updated", status_code=303)
    return RedirectResponse("/admin/users?view=orders&msg=packages_update_failed", status_code=303)


@router.post("/admin/users/create")
def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    authorized: int = Form(0),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard
    ok, _ = create_user_by_admin(db, username, password, role=role, authorized=bool(int(authorized)))
    return RedirectResponse(f"/admin/users?msg={'created' if ok else 'create_failed'}", status_code=303)


@router.post("/admin/users/{username}/password")
def admin_update_user_password(
    username: str, request: Request, new_password: str = Form(...), db: Session = Depends(get_db)
):
    guard = _require_admin(request)
    if guard:
        return guard
    ok, _ = admin_reset_user_password(db, username, new_password)
    return RedirectResponse(f"/admin/users?msg={'pwd_updated' if ok else 'pwd_update_failed'}", status_code=303)


@router.post("/admin/orders/{order_no}/mark-paid")
def admin_mark_order_paid(order_no: str, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    order = db.get(PaymentOrder, order_no)
    if not order:
        return RedirectResponse("/admin/users?view=orders&msg=order_not_found", status_code=303)
    # Only allow admin to process an order once (created -> paid).
    if order.status != "created":
        return RedirectResponse("/admin/users?view=orders&msg=order_already_processed", status_code=303)

    order.status = "paid"
    order.paid_at = datetime.utcnow()
    from app.models import User

    u = db.get(User, order.username)
    if u is not None and u.role != ROLE_ADMIN and u.gen_quota_remaining is not None:
        cur = int(u.gen_quota_remaining or 0)
        u.gen_quota_remaining = cur + int(order.buy_times or 0)
    db.commit()
    return RedirectResponse("/admin/users?view=orders&msg=order_paid", status_code=303)


@router.post("/admin/orders/{order_no}/close")
def admin_close_order(order_no: str, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    order = db.get(PaymentOrder, order_no)
    if not order:
        return RedirectResponse("/admin/users?view=orders&msg=order_not_found", status_code=303)
    # Only allow admin to process an order once (created -> closed).
    if order.status != "created":
        return RedirectResponse("/admin/users?view=orders&msg=order_already_processed", status_code=303)

    order.status = "closed"
    db.commit()
    return RedirectResponse("/admin/users?view=orders&msg=order_closed", status_code=303)


@router.post("/admin/orders/{order_no}/delete")
def admin_delete_order(order_no: str, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    order = db.get(PaymentOrder, order_no)
    if not order:
        return RedirectResponse("/admin/users?view=orders&msg=order_not_found", status_code=303)

    # Only allow delete after the order is processed (paid/closed).
    if order.status == "created":
        return RedirectResponse("/admin/users?view=orders&msg=order_cannot_delete_unprocessed", status_code=303)

    # Roll back quota if already authorized (paid).
    if order.status == "paid":
        from app.models import User

        u = db.get(User, order.username)
        if u is not None and u.role != ROLE_ADMIN and u.gen_quota_remaining is not None:
            cur = int(u.gen_quota_remaining or 0)
            u.gen_quota_remaining = max(0, cur - int(order.buy_times or 0))
        db.commit()

    try:
        db.delete(order)
        db.commit()
    except Exception:
        db.rollback()

    return RedirectResponse("/admin/users?view=orders&msg=order_deleted", status_code=303)


@router.post("/admin/messages/{msg_id}/delete")
def admin_delete_message(msg_id: int, request: Request, db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    msg = db.get(UserMessage, msg_id)
    if msg:
        try:
            # Delete reply first to avoid orphaned reply rows.
            reply = db.execute(select(UserMessageReply).where(UserMessageReply.message_id == msg_id)).scalars().first()
            if reply:
                db.delete(reply)
            db.delete(msg)
            db.commit()
        except Exception:
            db.rollback()
    return RedirectResponse("/admin/users?view=orders", status_code=303)


@router.post("/admin/messages/{msg_id}/reply")
def admin_reply_message(
    msg_id: int,
    request: Request,
    admin_reply: str = Form(""),
    db: Session = Depends(get_db),
):
    guard = _require_admin(request)
    if guard:
        return guard

    msg = db.get(UserMessage, msg_id)
    if not msg:
        return RedirectResponse("/admin/users?view=orders&msg=message_not_found", status_code=303)

    reply_s = (admin_reply or "").strip()
    if not reply_s:
        return RedirectResponse("/admin/users?view=orders&msg=message_reply_empty", status_code=303)

    actor = _current_user(request)
    admin_username = actor.get("username", "") if actor else ""

    try:
        # Upsert by deleting existing row(s) then inserting a new one.
        existing = db.execute(select(UserMessageReply).where(UserMessageReply.message_id == msg_id)).scalars().all()
        for r in existing:
            if r is not None:
                db.delete(r)
        db.add(
            UserMessageReply(
                message_id=msg_id,
                admin_username=admin_username,
                content=reply_s[:2000],
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse("/admin/users?view=orders&msg=message_reply_failed", status_code=303)

    return RedirectResponse("/admin/users?view=orders&msg=message_replied", status_code=303)


@router.post("/admin/users/{username}/delete")
def admin_delete_user(username: str, request: Request, confirm_username: str = Form(""), confirm_irreversible: int = Form(0), db: Session = Depends(get_db)):
    guard = _require_admin(request)
    if guard:
        return guard
    if confirm_irreversible and confirm_username == username:
        delete_user_and_all_data(db, username)
    return RedirectResponse("/admin/users", status_code=303)
