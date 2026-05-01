from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.app_config import load_app_config
from app.models import AppSetting, ApprovedPreset, TierRequest, User, UserAllowedTier
from utils.usage_tiers import TIER_ORDER, TIERS, normalize_legacy_tier_mode, normalize_pipeline_config
from utils.user_assets import purge_user_assets
from utils.user_gallery import purge_user_gallery

APP_CONFIG = load_app_config()
ADMIN_USERNAME = APP_CONFIG.admin_username
ROLE_ADMIN = "admin"
ROLE_USER = "user"
DEFAULT_PBKDF2_ITERATIONS = 390_000
PBKDF2_DK_LEN_BYTES = 32

# 各用户最近一次生成耗时（秒），用于下次预计时间
_GEN_LAST_SEC_JSON_KEY = "gen_last_sec_by_user"
_METHOD_OPTIMIZE_PERM_KEY_PREFIX = "user_method_optimize_perm:"


def get_user_last_generation_seconds(db: Session, username: str) -> int | None:
    row = db.get(AppSetting, _GEN_LAST_SEC_JSON_KEY)
    if not row or not row.value:
        return None
    try:
        data = json.loads(row.value)
        v = data.get(username)
        if v is None:
            return None
        return max(1, int(float(v)))
    except Exception:
        return None


def set_user_last_generation_seconds(db: Session, username: str, seconds: int) -> None:
    row = db.get(AppSetting, _GEN_LAST_SEC_JSON_KEY)
    data: dict[str, Any] = {}
    if row and row.value:
        try:
            data = json.loads(row.value)
        except Exception:
            data = {}
    data[username] = max(1, int(seconds))
    if not row:
        db.add(AppSetting(key=_GEN_LAST_SEC_JSON_KEY, value=json.dumps(data, ensure_ascii=False)))
    else:
        row.value = json.dumps(data, ensure_ascii=False)
    db.commit()


def _normalize_username(name: str) -> str:
    # Make registration/login more robust against copy/paste whitespace issues.
    # Example: "alice\u00a0" (NBSP) or "alice  " (multiple spaces).
    s = (name or "").strip()
    if not s:
        return ""
    # Remove any inner whitespace characters to ensure idempotent username matching.
    s = re.sub(r"\s+", "", s)
    return s[:128]


def _pbkdf2_hash(password: str, salt_hex: str, iterations: int = DEFAULT_PBKDF2_ITERATIONS) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
        dklen=PBKDF2_DK_LEN_BYTES,
    )
    return dk.hex()


def _new_salt() -> str:
    return secrets.token_hex(16)


def _allowed_tiers_from_user(user: User) -> list[str]:
    if user.role == ROLE_ADMIN:
        return list(TIER_ORDER)
    if not user.authorized:
        return []
    tiers = sorted({r.tier_id for r in user.allowed_tiers if r.tier_id in TIER_ORDER}, key=TIER_ORDER.index)
    return tiers or ["budget"]


def _method_optimize_perm_key(username: str) -> str:
    return f"{_METHOD_OPTIMIZE_PERM_KEY_PREFIX}{_normalize_username(username)}"


def _can_optimize_method_for_user(db: Session, user: User) -> bool:
    if user.role == ROLE_ADMIN:
        return True
    row = db.get(AppSetting, _method_optimize_perm_key(user.username))
    if not row or not row.value:
        return False
    return str(row.value).strip().lower() in {"1", "true", "yes", "on"}


def _approved_presets_from_user(user: User) -> list[dict[str, Any]]:
    if user.role == ROLE_ADMIN:
        return []
    return [
        {"id": p.preset_id, "label": p.label, "config": p.config or {}}
        for p in user.approved_presets
        if p.preset_id and isinstance(p.config, dict)
    ]


def ensure_admin_user(db: Session) -> None:
    admin = db.get(User, ADMIN_USERNAME)
    admin_password = (APP_CONFIG.admin_password or "").strip()
    if not admin_password:
        return
    if admin:
        # Keep admin credential deterministic with code config for local deployment.
        try:
            expect = _pbkdf2_hash(
                admin_password,
                admin.salt,
                iterations=int(admin.pbkdf2_iterations or DEFAULT_PBKDF2_ITERATIONS),
            )
        except Exception:
            expect = ""
        if not expect or not secrets.compare_digest(expect, admin.password_hash or ""):
            salt = _new_salt()
            admin.salt = salt
            admin.pbkdf2_iterations = DEFAULT_PBKDF2_ITERATIONS
            admin.password_hash = _pbkdf2_hash(admin_password, salt)
            admin.role = ROLE_ADMIN
            admin.authorized = True
            admin.can_edit_image = True
            db.commit()
        return
    salt = _new_salt()
    admin = User(
        username=ADMIN_USERNAME,
        password_hash=_pbkdf2_hash(admin_password, salt),
        salt=salt,
        pbkdf2_iterations=DEFAULT_PBKDF2_ITERATIONS,
        role=ROLE_ADMIN,
        authorized=True,
        can_edit_image=True,
        gen_quota_remaining=None,
        default_gen_mode="",
    )
    db.add(admin)
    db.commit()


def verify_login(db: Session, username: str, password: str) -> dict[str, Any] | None:
    u = _normalize_username(username)
    rec = db.get(User, u)
    if not rec:
        return None
    try:
        h = _pbkdf2_hash(password, rec.salt, iterations=int(rec.pbkdf2_iterations))
    except Exception:
        return None
    if not secrets.compare_digest(h, rec.password_hash):
        return None
    return {
        "username": rec.username,
        "role": rec.role,
        "authorized": bool(rec.authorized),
        "allowed_tiers": _allowed_tiers_from_user(rec),
        "approved_presets": _approved_presets_from_user(rec),
        "can_edit_image": bool(rec.can_edit_image or rec.role == ROLE_ADMIN),
        "can_optimize_method": _can_optimize_method_for_user(db, rec),
        "gen_quota_remaining": rec.gen_quota_remaining,
        "default_gen_mode": normalize_legacy_tier_mode(rec.default_gen_mode or ""),
    }


def get_user_profile(db: Session, username: str) -> dict[str, Any] | None:
    rec = db.get(User, _normalize_username(username))
    if not rec:
        return None
    return {
        "username": rec.username,
        "role": rec.role,
        "authorized": bool(rec.authorized),
        "allowed_tiers": _allowed_tiers_from_user(rec),
        "approved_presets": _approved_presets_from_user(rec),
        "can_edit_image": bool(rec.can_edit_image or rec.role == ROLE_ADMIN),
        "can_optimize_method": _can_optimize_method_for_user(db, rec),
        "gen_quota_remaining": rec.gen_quota_remaining,
        "default_gen_mode": normalize_legacy_tier_mode(rec.default_gen_mode or ""),
    }


def register_user(db: Session, username: str, password: str) -> tuple[bool, str]:
    u = _normalize_username(username)
    if len(u) < 2:
        return False, "用户名至少 2 个字符。"
    if len(password) < 4:
        return False, "密码至少 4 个字符。"
    if u == ADMIN_USERNAME:
        return False, "该用户名不可用。"
    if db.get(User, u):
        return False, "用户名已存在。"
    salt = _new_salt()
    default_mode = get_default_new_user_mode(db)
    user = User(
        username=u,
        password_hash=_pbkdf2_hash(password, salt),
        salt=salt,
        pbkdf2_iterations=DEFAULT_PBKDF2_ITERATIONS,
        role=ROLE_USER,
        authorized=False,
        can_edit_image=False,
        gen_quota_remaining=0,
        default_gen_mode=default_mode,
    )
    db.add(user)
    if default_mode.startswith("tier:"):
        t = default_mode[5:]
        if t in TIER_ORDER:
            user.allowed_tiers.append(UserAllowedTier(username=u, tier_id=t))
    try:
        db.commit()
    except IntegrityError:
        # Race-condition / duplicate submission protection.
        db.rollback()
        return False, "用户名已存在。"
    return True, "注册成功，请等待管理员授权后再使用生成功能。"


def create_user_by_admin(
    db: Session,
    username: str,
    password: str,
    role: str = ROLE_USER,
    authorized: bool = False,
) -> tuple[bool, str]:
    u = _normalize_username(username)
    if len(u) < 2:
        return False, "用户名至少 2 个字符。"
    if len(password) < 4:
        return False, "密码至少 4 个字符。"
    if db.get(User, u):
        return False, "用户名已存在。"
    role_clean = ROLE_ADMIN if role == ROLE_ADMIN else ROLE_USER
    salt = _new_salt()
    rec = User(
        username=u,
        password_hash=_pbkdf2_hash(password, salt),
        salt=salt,
        pbkdf2_iterations=DEFAULT_PBKDF2_ITERATIONS,
        role=role_clean,
        authorized=True if role_clean == ROLE_ADMIN else bool(authorized),
        can_edit_image=True if role_clean == ROLE_ADMIN else False,
        gen_quota_remaining=None if role_clean == ROLE_ADMIN else 0,
        default_gen_mode="" if role_clean == ROLE_ADMIN else get_default_new_user_mode(db),
    )
    db.add(rec)
    if rec.role == ROLE_USER and rec.default_gen_mode.startswith("tier:"):
        t = rec.default_gen_mode[5:]
        if t in TIER_ORDER:
            rec.allowed_tiers.append(UserAllowedTier(username=rec.username, tier_id=t))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False, "用户名已存在。"
    return True, "用户创建成功。"


def admin_reset_user_password(db: Session, username: str, new_password: str) -> tuple[bool, str]:
    rec = db.get(User, _normalize_username(username))
    if not rec:
        return False, "用户不存在。"
    if len(new_password or "") < 4:
        return False, "新密码至少 4 个字符。"
    salt = _new_salt()
    rec.salt = salt
    rec.pbkdf2_iterations = DEFAULT_PBKDF2_ITERATIONS
    rec.password_hash = _pbkdf2_hash(new_password, salt)
    db.commit()
    return True, "密码重置成功。"


def change_user_password(
    db: Session, username: str, current_password: str, new_password: str
) -> tuple[bool, str]:
    rec = db.get(User, _normalize_username(username))
    if not rec:
        return False, "用户不存在。"
    if len(new_password or "") < 4:
        return False, "新密码至少 4 个字符。"
    try:
        current_hash = _pbkdf2_hash(
            current_password or "",
            rec.salt,
            iterations=int(rec.pbkdf2_iterations or DEFAULT_PBKDF2_ITERATIONS),
        )
    except Exception:
        return False, "当前密码校验失败。"
    if not secrets.compare_digest(current_hash, rec.password_hash or ""):
        return False, "当前密码错误。"
    salt = _new_salt()
    rec.salt = salt
    rec.pbkdf2_iterations = DEFAULT_PBKDF2_ITERATIONS
    rec.password_hash = _pbkdf2_hash(new_password, salt)
    db.commit()
    return True, "密码更新成功。"


def list_managed_users(db: Session) -> dict[str, dict[str, Any]]:
    rows = db.execute(select(User)).scalars().all()
    out: dict[str, dict[str, Any]] = {}
    for u in rows:
        out[u.username] = {
            "role": u.role,
            "authorized": bool(u.authorized),
            "allowed_tiers": _allowed_tiers_from_user(u),
            "approved_presets": _approved_presets_from_user(u),
            "can_edit_image": bool(u.can_edit_image or u.role == ROLE_ADMIN),
            "can_optimize_method": _can_optimize_method_for_user(db, u),
            "gen_quota_remaining": u.gen_quota_remaining,
            "default_gen_mode": normalize_legacy_tier_mode(u.default_gen_mode or ""),
        }
    return out


def set_user_authorized(db: Session, username: str, authorized: bool) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if u.role == ROLE_ADMIN:
        return False, "不能修改管理员授权状态。"
    u.authorized = bool(authorized)
    if u.authorized and not u.allowed_tiers:
        db.add(UserAllowedTier(username=u.username, tier_id="budget"))
    db.commit()
    return True, "已更新登录许可。"


def set_user_edit_permission(db: Session, username: str, can_edit_image: bool) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if u.role == ROLE_ADMIN:
        return False, "管理员默认拥有该权限。"
    u.can_edit_image = bool(can_edit_image)
    db.commit()
    return True, "编辑权限已保存。"


def set_user_method_optimize_permission(db: Session, username: str, enabled: bool) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if u.role == ROLE_ADMIN:
        return False, "管理员默认拥有该权限。"
    key = _method_optimize_perm_key(u.username)
    row = db.get(AppSetting, key)
    val = "1" if bool(enabled) else "0"
    if row is None:
        db.add(AppSetting(key=key, value=val))
    else:
        row.value = val
    db.commit()
    return True, "方法优化权限已保存。"


def set_user_generation_quota(db: Session, username: str, quota_remaining: int | None) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if quota_remaining is not None and int(quota_remaining) < 0:
        return False, "额度必须为非负整数或空。"
    u.gen_quota_remaining = None if quota_remaining is None else int(quota_remaining)
    db.commit()
    return True, "生成额度已保存。"


def consume_user_generation_quota(db: Session, username: str, amount: int) -> tuple[bool, str]:
    need = int(amount)
    if need <= 0:
        return True, "本次无需消耗额度。"
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if u.gen_quota_remaining is None:
        return True, "无限制额度，不消耗。"
    if u.gen_quota_remaining < need:
        return False, f"您的生成额度不足（剩余 {u.gen_quota_remaining}，本次需要 {need}）。"
    u.gen_quota_remaining -= need
    db.commit()
    return True, f"已扣减额度 {need}。剩余 {u.gen_quota_remaining}。"


def refund_user_generation_quota(db: Session, username: str, amount: int) -> None:
    """生成失败时退回已扣额度（管理员无限制额度时不修改）。"""
    need = int(amount)
    if need <= 0:
        return
    u = db.get(User, _normalize_username(username))
    if not u or u.gen_quota_remaining is None:
        return
    u.gen_quota_remaining = int(u.gen_quota_remaining) + need
    db.commit()


def set_user_allowed_tiers(db: Session, username: str, tiers: list[str]) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if u.role == ROLE_ADMIN:
        return False, "管理员拥有全部档位，无需配置。"
    clean = sorted({t for t in tiers if t in TIER_ORDER}, key=TIER_ORDER.index)
    if u.authorized and not clean:
        return False, "已授权用户至少保留一个档位（例如最低成本）。"
    u.allowed_tiers.clear()
    for t in clean:
        u.allowed_tiers.append(UserAllowedTier(username=u.username, tier_id=t))
    db.commit()
    return True, "档位权限已保存。"


def remove_user_preset(db: Session, username: str, preset_id: str) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    hit = None
    for p in u.approved_presets:
        if p.preset_id == preset_id:
            hit = p
            break
    if not hit:
        return False, "未找到该组合。"
    db.delete(hit)
    db.commit()
    return True, "已移除该获批组合。"


def set_user_default_gen_mode(db: Session, username: str, mode: str) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if not mode:
        u.default_gen_mode = ""
        db.commit()
        return True, "已清除默认组合。"
    if mode.startswith("tier:"):
        tid = mode[5:]
        if tid not in _allowed_tiers_from_user(u):
            return False, "该用户未开通此档位。"
    elif mode.startswith("preset:"):
        pid = mode[7:]
        if not any(p.preset_id == pid for p in u.approved_presets):
            return False, "该用户未获批此组合。"
    else:
        return False, "默认组合格式无效。"
    u.default_gen_mode = mode
    db.commit()
    return True, "默认组合已保存。"


def add_user_preset_by_admin(
    db: Session, username: str, label: str, cfg: dict[str, Any]
) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if u.role == ROLE_ADMIN:
        return False, "管理员无需添加该组合。"
    norm, err = normalize_pipeline_config(cfg)
    if err:
        return False, err
    pid = secrets.token_hex(6)
    u.approved_presets.append(
        ApprovedPreset(
            username=u.username,
            preset_id=pid,
            label=(label or "管理员下发组合")[:100],
            config=norm,
        )
    )
    db.commit()
    return True, f"已添加组合：{pid}"


def update_user_preset_by_admin(
    db: Session, username: str, preset_id: str, label: str, cfg: dict[str, Any]
) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    norm, err = normalize_pipeline_config(cfg)
    if err:
        return False, err
    target = None
    for p in u.approved_presets:
        if p.preset_id == preset_id:
            target = p
            break
    if not target:
        return False, "未找到该组合。"
    target.label = (label or target.label or "管理员下发组合")[:100]
    target.config = norm
    db.commit()
    return True, "组合已更新。"


def list_tier_requests(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(select(TierRequest).order_by(TierRequest.id.asc())).scalars().all()
    return [
        {
            "kind": r.kind,
            "username": r.username,
            "tier_id": r.tier_id,
            "note": r.note,
            "label": r.label,
            "config": r.config,
            "ts": r.ts,
        }
        for r in rows
    ]


def submit_tier_request(db: Session, username: str, tier_id: str, note: str) -> tuple[bool, str]:
    if tier_id not in TIER_ORDER:
        return False, "无效档位。"
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if tier_id in _allowed_tiers_from_user(u):
        return False, "您已有该档位权限。"
    dup = db.execute(
        select(TierRequest).where(
            TierRequest.username == u.username,
            TierRequest.kind == "tier",
            TierRequest.tier_id == tier_id,
        )
    ).scalar_one_or_none()
    if dup:
        return False, "该档位申请已在审核中。"
    row = TierRequest(
        kind="tier",
        username=u.username,
        tier_id=tier_id,
        note=(note or "")[:500],
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    db.add(row)
    db.commit()
    return True, "已提交申请，请等待管理员审核。"


def submit_edit_permission_request(db: Session, username: str, note: str) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    if u.role == ROLE_ADMIN:
        return False, "管理员默认拥有该权限。"
    dup = db.execute(
        select(TierRequest).where(TierRequest.username == u.username, TierRequest.kind == "edit")
    ).scalar_one_or_none()
    if dup:
        return False, "申请已在审核中。"
    db.add(
        TierRequest(
            kind="edit",
            username=u.username,
            note=(note or "")[:500],
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    db.commit()
    return True, "已提交申请，请等待管理员审核。"


def submit_custom_preset_request(
    db: Session, username: str, label: str, cfg: dict[str, Any], note: str
) -> tuple[bool, str]:
    u = db.get(User, _normalize_username(username))
    if not u:
        return False, "用户不存在。"
    norm, err = normalize_pipeline_config(cfg)
    if err:
        return False, err
    db.add(
        TierRequest(
            kind="custom",
            username=u.username,
            label=(label or "")[:100],
            config=norm,
            note=(note or "")[:500],
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    db.commit()
    return True, "已提交申请，请等待管理员审核。"


def approve_access_request(db: Session, req_index: int) -> tuple[bool, str]:
    rows = db.execute(select(TierRequest).order_by(TierRequest.id.asc())).scalars().all()
    if req_index < 0 or req_index >= len(rows):
        return False, "申请索引无效。"
    req = rows[req_index]
    u = db.get(User, req.username)
    if not u:
        db.delete(req)
        db.commit()
        return False, "用户不存在，申请已移除。"
    if req.kind == "tier":
        u.authorized = True
        if req.tier_id and req.tier_id in TIER_ORDER:
            exists = any(t.tier_id == req.tier_id for t in u.allowed_tiers)
            if not exists:
                u.allowed_tiers.append(UserAllowedTier(username=u.username, tier_id=req.tier_id))
    elif req.kind == "edit":
        u.authorized = True
        u.can_edit_image = True
        if not u.allowed_tiers:
            u.allowed_tiers.append(UserAllowedTier(username=u.username, tier_id="budget"))
    else:
        u.authorized = True
        p_cfg = req.config if isinstance(req.config, dict) else {}
        u.approved_presets.append(
            ApprovedPreset(
                username=u.username,
                preset_id=secrets.token_hex(6),
                label=req.label or "自定义组合",
                config=p_cfg,
            )
        )
    db.delete(req)
    db.commit()
    return True, "已通过该申请。"


def reject_access_request(db: Session, req_index: int) -> tuple[bool, str]:
    rows = db.execute(select(TierRequest).order_by(TierRequest.id.asc())).scalars().all()
    if req_index < 0 or req_index >= len(rows):
        return False, "申请索引无效。"
    db.delete(rows[req_index])
    db.commit()
    return True, "已拒绝该申请。"


def delete_user_and_all_data(db: Session, username: str) -> tuple[bool, str]:
    u = _normalize_username(username)
    if not u:
        return False, "用户名无效。"
    if u == ADMIN_USERNAME:
        return False, "不能删除管理员账号。"
    rec = db.get(User, u)
    if not rec:
        return False, "用户不存在。"
    if rec.role == ROLE_ADMIN:
        return False, "不能删除管理员账号。"

    ok_assets, msg_assets = purge_user_assets(u)
    if not ok_assets:
        return False, msg_assets
    ok_gallery, msg_gallery = purge_user_gallery(u)
    if not ok_gallery:
        return False, msg_gallery

    db.query(TierRequest).filter(TierRequest.username == u).delete(synchronize_session=False)
    db.delete(rec)
    db.commit()
    return True, f"已删除用户 {u}，并清理全部相关数据（素材/图库/申请记录）。"


def get_default_new_user_mode(db: Session) -> str:
    row = db.get(AppSetting, "default_new_user_mode")
    raw = row.value if row and row.value else "tier:budget"
    return normalize_legacy_tier_mode(raw)


def get_generation_unit_price_yuan(db: Session) -> float:
    row = db.get(AppSetting, "generation_unit_price_yuan")
    if not row or not row.value:
        return 3.0
    try:
        val = float(row.value)
        if val <= 0:
            return 3.0
        return round(val, 2)
    except Exception:
        return 3.0


def set_generation_unit_price_yuan(db: Session, price_yuan: float) -> tuple[bool, str]:
    try:
        val = round(float(price_yuan), 2)
    except Exception:
        return False, "价格格式无效。"
    if val <= 0:
        return False, "价格必须大于 0。"
    row = db.get(AppSetting, "generation_unit_price_yuan")
    if not row:
        row = AppSetting(key="generation_unit_price_yuan", value=f"{val:.2f}")
        db.add(row)
    else:
        row.value = f"{val:.2f}"
    db.commit()
    return True, "单次价格已更新。"


_PRICE_PACKAGES_JSON_KEY = "generation_price_packages_json"


def get_price_packages(db: Session) -> list[dict[str, Any]]:
    """
    Price packages shown on UI (admin config -> users display).

    Stored in AppSetting as JSON list:
      [
        {"id": "p1", "label": "入门", "times": 1},
        ...
      ]
    Note: per-package total price can be stored in `price_yuan` (optional).
    If `price_yuan` is missing, UI/backend may derive it from `generation_unit_price_yuan * times`.
    """
    row = db.get(AppSetting, _PRICE_PACKAGES_JSON_KEY)
    raw = row.value if row and row.value else ""
    try:
        data = json.loads(raw) if raw else []
    except Exception:
        data = []

    if not isinstance(data, list):
        data = []

    out: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "").strip()
        label = str(it.get("label") or "").strip()
        times = it.get("times")
        try:
            times_i = int(times)
        except Exception:
            continue
        if not pid:
            pid = f"p{len(out)+1}"
        if not label:
            label = f"套餐 {len(out)+1}"
        if times_i < 1:
            continue
        price_yuan: float | None = None
        raw_price = it.get("price_yuan")
        if raw_price is not None and raw_price != "":
            try:
                v = float(raw_price)
                if v > 0:
                    price_yuan = round(v, 2)
            except Exception:
                price_yuan = None

        out.append({"id": pid, "label": label[:60], "times": times_i, "price_yuan": price_yuan})

    if not out:
        out = [
            {"id": "p1", "label": "入门 · 1次", "times": 1, "price_yuan": None},
            {"id": "p2", "label": "进阶 · 3次", "times": 3, "price_yuan": None},
            {"id": "p3", "label": "畅享 · 5次", "times": 5, "price_yuan": None},
        ]

    out = out[:6]
    while len(out) < 6:
        idx = len(out) + 1
        out.append({"id": f"p{idx}", "label": "", "times": 1, "price_yuan": None})
    return out


def set_price_packages(db: Session, packages: list[dict[str, Any]]) -> tuple[bool, str]:
    clean: list[dict[str, Any]] = []
    if not isinstance(packages, list):
        return False, "套餐配置格式无效。"

    for it in packages:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or "").strip()
        pid = str(it.get("id") or "").strip()
        times = it.get("times")
        price_yuan: float | None = None
        raw_price = it.get("price_yuan")
        if raw_price is not None and raw_price != "":
            try:
                v = float(raw_price)
                if v > 0:
                    price_yuan = round(v, 2)
            except Exception:
                price_yuan = None
        try:
            times_i = int(times)
        except Exception:
            continue
        if times_i < 1:
            continue
        if not label:
            continue
        if not pid:
            pid = f"p{len(clean)+1}"
        clean.append({"id": pid, "label": label[:60], "times": times_i, "price_yuan": price_yuan})

    if not clean:
        return False, "至少配置一个有效套餐（label/times）。"

    row = db.get(AppSetting, _PRICE_PACKAGES_JSON_KEY)
    data_str = json.dumps(clean[:6], ensure_ascii=False, indent=2)
    if not row:
        db.add(AppSetting(key=_PRICE_PACKAGES_JSON_KEY, value=data_str))
    else:
        row.value = data_str
    db.commit()
    return True, "套餐配置已更新。"


def get_tier_templates(db: Session) -> dict[str, dict[str, Any]]:
    base: dict[str, dict[str, Any]] = {}
    for tid in TIER_ORDER:
        cfg = TIERS.get(tid, {})
        base[tid] = {
            "label": str(cfg.get("label", tid)),
            "exp_mode": str(cfg.get("exp_mode", "demo_planner_critic")),
            "retrieval_setting": str(cfg.get("retrieval_setting", "auto")),
            "num_candidates": int(cfg.get("num_candidates", 1)),
            "aspect_ratio": str(cfg.get("aspect_ratio", "16:9")),
            "max_critic_rounds": int(cfg.get("max_critic_rounds", 2)),
            "main_model_name": str(cfg.get("main_model_name", "")),
            "image_gen_model_name": str(cfg.get("image_gen_model_name", "")),
            "max_refine_resolution": str(cfg.get("max_refine_resolution", "2K")),
        }
    row = db.get(AppSetting, "tier_templates_json")
    if not row or not row.value:
        return base
    import json

    try:
        raw = json.loads(row.value)
    except Exception:
        return base
    if not isinstance(raw, dict):
        return base
    for tid in TIER_ORDER:
        override = raw.get(tid)
        if not isinstance(override, dict):
            continue
        merged = dict(base[tid])
        for k in (
            "label",
            "exp_mode",
            "retrieval_setting",
            "num_candidates",
            "aspect_ratio",
            "max_critic_rounds",
            "main_model_name",
            "image_gen_model_name",
            "max_refine_resolution",
        ):
            if k in override:
                merged[k] = override[k]
        base[tid] = merged
    return base


def upsert_tier_template_by_admin(
    db: Session,
    tier_id: str,
    label: str,
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    tid = (tier_id or "").strip()
    if tid not in TIER_ORDER:
        return False, "模板档位无效。"
    norm, err = normalize_pipeline_config(cfg)
    if err:
        return False, err
    import json

    row = db.get(AppSetting, "tier_templates_json")
    raw: dict[str, Any] = {}
    if row and row.value:
        try:
            obj = json.loads(row.value)
            if isinstance(obj, dict):
                raw = obj
        except Exception:
            raw = {}
    raw[tid] = {
        "label": (label or f"{tid} 模板")[:100],
        **norm,
    }
    if not row:
        row = AppSetting(key="tier_templates_json", value=json.dumps(raw, ensure_ascii=False))
        db.add(row)
    else:
        row.value = json.dumps(raw, ensure_ascii=False)
    db.commit()
    return True, "模板已保存。"


def reset_tier_template_by_admin(db: Session, tier_id: str) -> tuple[bool, str]:
    tid = (tier_id or "").strip()
    if tid not in TIER_ORDER:
        return False, "模板档位无效。"
    import json

    row = db.get(AppSetting, "tier_templates_json")
    if not row or not row.value:
        return True, "该模板已是系统默认。"
    try:
        raw = json.loads(row.value)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if tid in raw:
        raw.pop(tid, None)
        row.value = json.dumps(raw, ensure_ascii=False)
        db.commit()
    return True, "已重置为系统默认模板。"


def get_tier_template_override_tier_ids(db: Session) -> set[str]:
    """数据库中已保存自定义覆盖的档位 ID（用于管理页是否显示「删除」）。"""
    row = db.get(AppSetting, "tier_templates_json")
    if not row or not row.value:
        return set()
    try:
        raw = json.loads(row.value)
    except Exception:
        return set()
    if not isinstance(raw, dict):
        return set()
    return {k for k in raw.keys() if k in TIER_ORDER}


def delete_tier_template_by_admin(db: Session, tier_id: str) -> tuple[bool, str]:
    """删除该档位的自定义模板（从数据库移除覆盖项），生成逻辑回退为内置 TIERS 默认。"""
    return reset_tier_template_by_admin(db, tier_id)


def set_default_new_user_mode(db: Session, mode: str) -> tuple[bool, str]:
    m = (mode or "").strip()
    if m and not (m.startswith("tier:") or m.startswith("preset:")):
        return False, "默认模式格式无效。"
    if m.startswith("tier:"):
        m = normalize_legacy_tier_mode(m)
    row = db.get(AppSetting, "default_new_user_mode")
    if not row:
        row = AppSetting(key="default_new_user_mode", value=m or "tier:budget")
        db.add(row)
    else:
        row.value = m or "tier:budget"
    db.commit()
    return True, "新用户默认模式已保存。"


def apply_default_new_user_mode_to_all_users(db: Session) -> tuple[bool, str]:
    mode = get_default_new_user_mode(db)
    rows = db.execute(select(User)).scalars().all()
    touched = 0
    for u in rows:
        if u.role == ROLE_ADMIN:
            continue
        u.default_gen_mode = mode
        touched += 1
        if mode.startswith("tier:"):
            tid = mode[5:]
            if tid in TIER_ORDER:
                exists = any(t.tier_id == tid for t in u.allowed_tiers)
                if not exists:
                    u.allowed_tiers.append(UserAllowedTier(username=u.username, tier_id=tid))
    db.commit()
    return True, f"已应用到 {touched} 个普通用户。"

