from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(128), nullable=False)
    pbkdf2_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=390000)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_edit_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gen_quota_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_gen_mode: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    allowed_tiers: Mapped[list["UserAllowedTier"]] = relationship(
        "UserAllowedTier", back_populates="user", cascade="all, delete-orphan"
    )
    approved_presets: Mapped[list["ApprovedPreset"]] = relationship(
        "ApprovedPreset", back_populates="user", cascade="all, delete-orphan"
    )


class UserAllowedTier(Base):
    __tablename__ = "user_allowed_tiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), ForeignKey("users.username"), nullable=False)
    tier_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    user: Mapped[User] = relationship("User", back_populates="allowed_tiers")


class ApprovedPreset(Base):
    __tablename__ = "approved_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), ForeignKey("users.username"), nullable=False)
    preset_id: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    user: Mapped[User] = relationship("User", back_populates="approved_presets")


class TierRequest(Base):
    __tablename__ = "tier_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="tier")
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    tier_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ts: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    target: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    order_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    unit_price_yuan: Mapped[float] = mapped_column(nullable=False, default=3.0)
    buy_times: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_amount_yuan: Mapped[float] = mapped_column(nullable=False, default=3.0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="created")
    alipay_trade_no: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_payload: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class UserMessage(Base):
    """
    Per-user message / payment suggestion log.
    Admin can review/delete; used to capture user payment method preference or notes.
    """

    __tablename__ = "user_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    order_no: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    pay_channel: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class UserMessageReply(Base):
    """
    Admin reply / feedback for a given UserMessage.
    Stored separately so we can keep appending/deleting messages safely.
    """

    __tablename__ = "user_message_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    admin_username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

