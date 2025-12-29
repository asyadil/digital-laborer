"""SQLAlchemy models for the referral automation system."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class."""


class TimestampMixin:
    """Mixin providing created/updated audit columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AccountType(enum.Enum):
    """Supported account platforms."""
    REDDIT = "reddit"
    YOUTUBE = "youtube"
    QUORA = "quora"


class AccountStatus(enum.Enum):
    """Account status enum."""
    active = "active"
    flagged = "flagged"
    banned = "banned"
    suspended = "suspended"


class PostStatus(enum.Enum):
    pending = "pending"
    posted = "posted"
    flagged = "flagged"
    removed = "removed"


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[AccountType] = mapped_column(Enum(AccountType), index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    password_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), nullable=False, index=True)
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    total_posts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_conversions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    health_score: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON)

    posts: Mapped[list[Post]] = relationship(
        "Post", back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    health_events: Mapped[list["AccountHealth"]] = relationship(
        "AccountHealth", back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("ix_accounts_platform_status", "platform", "status"),)


class Post(Base, TimestampMixin):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[Optional[str]] = mapped_column(String(512))
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[PostStatus] = mapped_column(Enum(PostStatus), default=PostStatus.pending, index=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality_score: Mapped[Optional[float]] = mapped_column(Float)
    human_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON)

    account: Mapped[Account] = relationship("Account", back_populates="posts")

    __table_args__ = (
        Index("ix_posts_platform_status", "platform", "status"),
        Index("ix_posts_account", "account_id", "posted_at"),
    )


class AccountHealth(Base):
    __tablename__ = "account_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    health_score: Mapped[float] = mapped_column(Float, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)

    account: Mapped["Account"] = relationship("Account", back_populates="health_events")

    __table_args__ = (Index("ix_account_health_account_time", "account_id", "timestamp"),)


class ReferralLink(Base, TimestampMixin):
    __tablename__ = "referral_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform_name: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    commission_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    earnings: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    __table_args__ = (Index("ix_referral_links_platform", "platform_name", "active"),)


class TelegramInteraction(Base, TimestampMixin):
    __tablename__ = "telegram_interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    context: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    response_value: Mapped[Optional[str]] = mapped_column(Text)
    timeout: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (Index("ix_telegram_interactions_action", "action_type", "requested_at"),)


class SystemMetric(Base, TimestampMixin):
    __tablename__ = "system_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    metric_type: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON)

    __table_args__ = (Index("ix_system_metrics_type_time", "metric_type", "timestamp"),)


class ErrorLog(Base, TimestampMixin):
    __tablename__ = "error_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    severity: Mapped[str] = mapped_column(String(50), nullable=False)
    component: Mapped[str] = mapped_column(String(100), nullable=False)
    error_type: Mapped[str] = mapped_column(String(200), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    stack_trace: Mapped[Optional[str]] = mapped_column(Text)
    context: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON)

    __table_args__ = (Index("ix_error_log_component", "component", "timestamp"),)


class SystemState(Base, TimestampMixin):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value_json: Mapped[Optional[Dict[str, Any]]] = mapped_column("value", JSON)

    __table_args__ = (Index("ix_system_state_key", "key"),)
