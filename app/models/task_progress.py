"""Employee progress notes; incoming MAX payloads are never stored here."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TaskProgress(Base):
    __tablename__ = "task_progress"
    __table_args__ = (
        CheckConstraint(
            "state IN ('in_progress', 'done', 'not_done', 'needs')", name="valid_state"
        ),
        CheckConstraint(
            "state NOT IN ('not_done', 'needs') OR (note IS NOT NULL AND length(trim(note)) > 0)",
            name="required_note",
        ),
        Index("ix_task_progress_request_created", "request_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("requests.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
