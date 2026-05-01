from __future__ import annotations

import json
import os
from pathlib import Path

from app.db import Base, SessionLocal, engine
from app.models import ApprovedPreset, TierRequest, User, UserAllowedTier


def migrate(json_path: Path) -> None:
    if not json_path.exists():
        raise FileNotFoundError(f"未找到文件：{json_path}")
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    users = raw.get("users", {}) if isinstance(raw, dict) else {}
    reqs = raw.get("tier_requests", []) if isinstance(raw, dict) else []

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        for uname, rec in users.items():
            if not isinstance(rec, dict):
                continue
            user = db.get(User, uname)
            if not user:
                user = User(username=uname)
                db.add(user)
            user.password_hash = str(rec.get("password_hash", ""))
            user.salt = str(rec.get("salt", ""))
            user.pbkdf2_iterations = int(rec.get("pbkdf2_iterations", 390000))
            user.role = str(rec.get("role", "user"))
            user.authorized = bool(rec.get("authorized", False))
            user.can_edit_image = bool(rec.get("can_edit_image", user.role == "admin"))
            q = rec.get("gen_quota_remaining")
            user.gen_quota_remaining = None if q is None else int(q)
            user.default_gen_mode = str(rec.get("default_gen_mode", ""))
            user.allowed_tiers.clear()
            for t in rec.get("allowed_tiers", []) or []:
                user.allowed_tiers.append(UserAllowedTier(username=uname, tier_id=str(t)))
            user.approved_presets.clear()
            for p in rec.get("approved_presets", []) or []:
                if not isinstance(p, dict) or not p.get("id"):
                    continue
                user.approved_presets.append(
                    ApprovedPreset(
                        username=uname,
                        preset_id=str(p.get("id")),
                        label=str(p.get("label", "")),
                        config=p.get("config") if isinstance(p.get("config"), dict) else {},
                    )
                )

        db.query(TierRequest).delete(synchronize_session=False)
        for r in reqs:
            if not isinstance(r, dict):
                continue
            db.add(
                TierRequest(
                    kind=str(r.get("kind") or ("tier" if r.get("tier_id") else "custom")),
                    username=str(r.get("username", "")),
                    tier_id=(None if r.get("tier_id") is None else str(r.get("tier_id"))),
                    note=str(r.get("note", "")),
                    label=str(r.get("label", "")),
                    config=r.get("config") if isinstance(r.get("config"), dict) else None,
                    ts=str(r.get("ts", "")),
                )
            )
        db.commit()


if __name__ == "__main__":
    src = os.getenv("USER_AUTH_STORE_PATH", str(Path(__file__).resolve().parents[1] / "user_auth_store.json"))
    migrate(Path(src))
    print("迁移完成。")

