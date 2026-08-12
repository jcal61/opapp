"""Manager Logbook — write/read services for the shift communication feed."""

from __future__ import annotations
from datetime import date
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models import LogEntry


def create_entry(
    db: Session,
    location_id: int,
    author_id: Optional[int],
    message: str,
    category: str = "General",
    entry_date: Optional[date] = None,
    pinned: bool = False,
) -> LogEntry:
    if not message or not message.strip():
        raise ValueError("Log entry can't be empty.")
    entry = LogEntry(
        location_id=location_id,
        author_id=author_id,
        entry_date=entry_date or date.today(),
        category=(category or "General").strip() or "General",
        message=message.strip(),
        pinned=bool(pinned),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def list_entries(
    db: Session, location_id: int, start: date, end: date, category: Optional[str] = None
) -> List[LogEntry]:
    q = (
        db.query(LogEntry)
        .options(joinedload(LogEntry.author))
        .filter(LogEntry.location_id == location_id, LogEntry.entry_date >= start, LogEntry.entry_date <= end)
    )
    if category:
        q = q.filter(LogEntry.category == category)
    return q.order_by(LogEntry.pinned.desc(), LogEntry.entry_date.desc(), LogEntry.created_at.desc()).all()


def toggle_pin(db: Session, entry_id: int) -> LogEntry:
    entry = db.get(LogEntry, entry_id)
    if not entry:
        raise ValueError("Log entry not found")
    entry.pinned = not entry.pinned
    db.commit()
    db.refresh(entry)
    return entry


def delete_entry(db: Session, entry_id: int) -> None:
    entry = db.get(LogEntry, entry_id)
    if entry:
        db.delete(entry)
        db.commit()
