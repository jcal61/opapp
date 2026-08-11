"""
Staff scheduling and time clock — the Connecteam-style piece this app was
missing: who's supposed to work when (Shift) vs. who actually worked when
(TimeClockEntry), so labor cost can sit next to food cost as a matched pair
of prime-cost KPIs.
"""

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, Boolean, DateTime, ForeignKey, Text
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
    time_entries = relationship("TimeClockEntry", back_populates="shift")

    def scheduled_hours(self) -> float:
        seconds = (self.end_at - self.start_at).total_seconds()
        return max(0.0, seconds / 3600.0 - (self.break_minutes or 0) / 60.0)


class TimeClockEntry(Base):
    """An actual clock-in/clock-out punch, optionally tied to a scheduled Shift."""
    __tablename__ = "time_clock_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"))
    clock_in: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    clock_out: Mapped[datetime | None] = mapped_column(DateTime)
    break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    hourly_rate_snapshot: Mapped[float] = mapped_column(Float, default=0.0)  # rate at time of punch
    notes: Mapped[str | None] = mapped_column(Text)

    user = relationship("User")
    location = relationship("Location")
    shift = relationship("Shift", back_populates="time_entries")

    def is_open(self) -> bool:
        return self.clock_out is None

    def hours(self, as_of: datetime | None = None) -> float:
        end = self.clock_out or as_of or datetime.now(timezone.utc)
        start = self.clock_in
        # SQLite may hand back naive datetimes; keep both sides consistent.
        if end.tzinfo is not None and start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None and start.tzinfo is not None:
            end = end.replace(tzinfo=timezone.utc)
        seconds = (end - start).total_seconds()
        return max(0.0, seconds / 3600.0 - (self.break_minutes or 0) / 60.0)

    def cost(self, as_of: datetime | None = None) -> float:
        return self.hours(as_of) * (self.hourly_rate_snapshot or 0.0)
