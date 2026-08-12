"""
Staff scheduling — the Connecteam-style piece this app was missing: who's
supposed to work when, built as a weekly schedule with open/assigned shifts
that can be drafted and then published to the team.
"""

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Shift(Base):
    """A scheduled shift. May be unassigned (open shift to be claimed/filled)."""
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # null = open/unassigned shift
    role_code: Mapped[str | None] = mapped_column(String(40))  # position worked: manager, kitchen, server, bar...
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published | cancelled
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    location = relationship("Location")
    user = relationship("User")

    def scheduled_hours(self) -> float:
        seconds = (self.end_at - self.start_at).total_seconds()
        return max(0.0, seconds / 3600.0 - (self.break_minutes or 0) / 60.0)
