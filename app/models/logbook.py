"""
Manager Logbook — a running, timestamped shift journal, modeled after
Restaurant365's "Logbook & Chat". This is distinct from Checklists & SOPs
(which track completion of recurring, repeatable task lists): the logbook is
a free-form chronological feed managers scan at the start of a shift to see
what happened since they were last on — a maintenance issue, a guest
complaint, a staffing note — the digital replacement for a paper log book at
the host stand.
"""

from __future__ import annotations
from datetime import datetime, date, timezone
from sqlalchemy import String, ForeignKey, DateTime, Date, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

LOG_CATEGORIES = ["General", "Guest", "Maintenance", "Safety", "Staffing", "Cash/Finance", "Equipment"]


class LogEntry(Base):
    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    entry_date: Mapped[date] = mapped_column(Date, default=date.today)
    category: Mapped[str] = mapped_column(String(30), default="General")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    location = relationship("Location")
    author = relationship("User")
