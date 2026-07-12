"""SQLAlchemy models for the idea-to-PR pipeline."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TrackedMessage(Base):
    __tablename__ = "tracked_messages"
    __table_args__ = (
        UniqueConstraint("slack_channel_id", "slack_message_ts", name="uq_channel_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slack_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_message_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="")
    author_slack_id: Mapped[str] = mapped_column(String(32), default="")
    reaction_count: Mapped[int] = mapped_column(Integer, default=0)
    triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    # feature_request | bug_report | not_actionable; set when threshold fires
    intent: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PositiveReply(Base):
    """A thread reply classified as supporting the parent tracked message."""

    __tablename__ = "positive_replies"
    __table_args__ = (
        UniqueConstraint("tracked_message_id", "slack_reply_ts", name="uq_message_reply"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracked_message_id: Mapped[int] = mapped_column(ForeignKey("tracked_messages.id"), nullable=False)
    replier_slack_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_reply_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracked_message_id: Mapped[int] = mapped_column(ForeignKey("tracked_messages.id"), nullable=False)
    slack_dm_channel_id: Mapped[str] = mapped_column(String(32), default="")  # the bot<->author DM channel
    status: Mapped[str] = mapped_column(String(16), default="gathering")  # gathering | ready | cancelled | issue_filed | done
    gathered_context: Mapped[str] = mapped_column(Text, default="")  # JSON blob
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkItem(Base):
    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    github_issue_number: Mapped[int] = mapped_column(Integer, nullable=True)
    plan_md_path: Mapped[str] = mapped_column(String(255), default="")
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | in_review | approved | merged | failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
