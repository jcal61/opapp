"""
Scheduling and time clock — shift planning plus actual punches, and the
labor-cost math that ties them together and to POS sales (labor cost %,
the sibling KPI to food cost %).
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session, joinedload

from app.models import Shift, TimeClockEntry, User, POSSale


# ---------- Shifts ----------

def create_shift(
    db: Session,
    location_id: int,
    start_at: datetime,
    end_at: datetime,
    user_id: Optional[int] = None,
    role_code: Optional[str] = None,
    break_minutes: int = 0,
    notes: Optional[str] = None,
    status: str = "draft",
) -> Shift:
    if end_at <= start_at:
        raise ValueError("Shift end time must be after start time.")
    shift = Shift(
        location_id=location_id,
        user_id=user_id,
        role_code=(role_code or "").strip() or None,
        start_at=start_at,
        end_at=end_at,
        break_minutes=break_minutes or 0,
        status=status,
        notes=(notes or "").strip() or None,
    )
    db.add(shift)
    db.commit()
    db.refresh(shift)
    return shift


def update_shift(
    db: Session,
    shift_id: int,
    user_id: Optional[int] = ...,
    role_code: Optional[str] = None,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    break_minutes: Optional[int] = None,
    notes: Optional[str] = None,
    status: Optional[str] = None,
) -> Shift:
    shift = db.get(Shift, shift_id)
    if not shift:
        raise ValueError("Shift not found")
    if user_id is not ...:
        shift.user_id = user_id
    if role_code is not None:
        shift.role_code = role_code.strip() or None
    if start_at is not None:
        shift.start_at = start_at
    if end_at is not None:
        shift.end_at = end_at
    if break_minutes is not None:
        shift.break_minutes = break_minutes
    if notes is not None:
        shift.notes = notes.strip() or None
    if status is not None:
        shift.status = status
    if shift.end_at <= shift.start_at:
        raise ValueError("Shift end time must be after start time.")
    db.commit()
    db.refresh(shift)
    return shift


def delete_shift(db: Session, shift_id: int) -> None:
    shift = db.get(Shift, shift_id)
    if shift:
        db.delete(shift)
        db.commit()


def publish_shifts(db: Session, shift_ids: List[int]) -> int:
    n = 0
    for sid in shift_ids:
        shift = db.get(Shift, sid)
        if shift and shift.status != "published":
            shift.status = "published"
            n += 1
    db.commit()
    return n


def list_shifts(
    db: Session,
    location_id: int,
    start: datetime,
    end: datetime,
    user_id: Optional[int] = None,
) -> List[Shift]:
    q = (
        db.query(Shift)
        .options(joinedload(Shift.user))
        .filter(Shift.location_id == location_id, Shift.start_at < end, Shift.end_at > start)
    )
    if user_id is not None:
        q = q.filter(Shift.user_id == user_id)
    return q.order_by(Shift.start_at).all()


# ---------- Time clock ----------

def get_open_entry(db: Session, user_id: int) -> Optional[TimeClockEntry]:
    return (
        db.query(TimeClockEntry)
        .filter(TimeClockEntry.user_id == user_id, TimeClockEntry.clock_out.is_(None))
        .first()
    )


def clock_in(
    db: Session,
    user_id: int,
    location_id: int,
    shift_id: Optional[int] = None,
) -> TimeClockEntry:
    if get_open_entry(db, user_id):
        raise ValueError("Already clocked in — clock out first.")
    user = db.get(User, user_id)
    if not user:
        raise ValueError("User not found")

    # If no shift given, try to auto-match a published shift happening now.
    if shift_id is None:
        now = datetime.now(timezone.utc)
        candidate = (
            db.query(Shift)
            .filter(
                Shift.location_id == location_id,
                Shift.user_id == user_id,
                Shift.status == "published",
                Shift.start_at <= now + timedelta(hours=1),
                Shift.end_at >= now - timedelta(hours=1),
            )
            .order_by(Shift.start_at)
            .first()
        )
        shift_id = candidate.id if candidate else None

    entry = TimeClockEntry(
        user_id=user_id,
        location_id=location_id,
        shift_id=shift_id,
        clock_in=datetime.now(timezone.utc),
        hourly_rate_snapshot=user.hourly_rate or 0.0,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def clock_out(db: Session, entry_id: int, break_minutes: Optional[int] = None) -> TimeClockEntry:
    entry = db.get(TimeClockEntry, entry_id)
    if not entry:
        raise ValueError("Time clock entry not found")
    if entry.clock_out is not None:
        raise ValueError("Already clocked out.")
    entry.clock_out = datetime.now(timezone.utc)
    if break_minutes is not None:
        entry.break_minutes = break_minutes
    db.commit()
    db.refresh(entry)
    return entry


def edit_time_entry(
    db: Session,
    entry_id: int,
    clock_in: Optional[datetime] = None,
    clock_out: Optional[datetime] = None,
    break_minutes: Optional[int] = None,
    notes: Optional[str] = None,
) -> TimeClockEntry:
    """Manager correction of a punch (forgot to clock out, typo'd time, etc.)."""
    entry = db.get(TimeClockEntry, entry_id)
    if not entry:
        raise ValueError("Time clock entry not found")
    if clock_in is not None:
        entry.clock_in = clock_in
    if clock_out is not None:
        entry.clock_out = clock_out
    if break_minutes is not None:
        entry.break_minutes = break_minutes
    if notes is not None:
        entry.notes = notes.strip() or None
    if entry.clock_out and entry.clock_out <= entry.clock_in:
        raise ValueError("Clock-out must be after clock-in.")
    db.commit()
    db.refresh(entry)
    return entry


def list_time_entries(
    db: Session,
    location_id: int,
    start: datetime,
    end: datetime,
    user_id: Optional[int] = None,
) -> List[TimeClockEntry]:
    q = (
        db.query(TimeClockEntry)
        .options(joinedload(TimeClockEntry.user))
        .filter(
            TimeClockEntry.location_id == location_id,
            TimeClockEntry.clock_in < end,
        )
        .filter((TimeClockEntry.clock_out >= start) | (TimeClockEntry.clock_out.is_(None)))
    )
    if user_id is not None:
        q = q.filter(TimeClockEntry.user_id == user_id)
    return q.order_by(TimeClockEntry.clock_in).all()


# ---------- Labor cost / scheduled vs actual ----------

def labor_cost_summary(db: Session, location_id: int, start: datetime, end: datetime) -> Dict[str, Any]:
    """Actual hours/cost from punches, grouped by user, for the given window."""
    entries = list_time_entries(db, location_id, start, end)
    now = datetime.now(timezone.utc)
    by_user: Dict[int, Dict[str, Any]] = {}
    total_hours = 0.0
    total_cost = 0.0
    for e in entries:
        hrs = e.hours(as_of=now)
        cost = hrs * (e.hourly_rate_snapshot or 0.0)
        total_hours += hrs
        total_cost += cost
        uid = e.user_id
        if uid not in by_user:
            by_user[uid] = {
                "user_id": uid,
                "name": e.user.name if e.user else "Unknown",
                "hours": 0.0,
                "cost": 0.0,
                "open_entry": False,
            }
        by_user[uid]["hours"] += hrs
        by_user[uid]["cost"] += cost
        if e.is_open():
            by_user[uid]["open_entry"] = True

    sales_total = (
        db.query(POSSale)
        .filter(POSSale.location_id == location_id, POSSale.sold_at >= start, POSSale.sold_at < end)
        .all()
    )
    total_sales = sum(s.total_amount or 0.0 for s in sales_total)
    labor_pct = (total_cost / total_sales * 100.0) if total_sales else None

    return {
        "total_hours": round(total_hours, 2),
        "total_cost": round(total_cost, 2),
        "total_sales": round(total_sales, 2),
        "labor_cost_percent": round(labor_pct, 1) if labor_pct is not None else None,
        "by_user": sorted(by_user.values(), key=lambda r: -r["cost"]),
    }


def scheduled_vs_actual(db: Session, location_id: int, start: datetime, end: datetime) -> Dict[str, Any]:
    """Compare planned labor (published shifts) to actual punches for the window."""
    shifts = list_shifts(db, location_id, start, end)
    scheduled_hours = sum(s.scheduled_hours() for s in shifts if s.status != "cancelled")
    rate_by_user = {u.id: (u.hourly_rate or 0.0) for u in db.query(User).all()}
    scheduled_cost = sum(
        s.scheduled_hours() * rate_by_user.get(s.user_id, 0.0)
        for s in shifts
        if s.status != "cancelled" and s.user_id
    )

    actual = labor_cost_summary(db, location_id, start, end)
    return {
        "scheduled_hours": round(scheduled_hours, 2),
        "scheduled_cost": round(scheduled_cost, 2),
        "actual_hours": actual["total_hours"],
        "actual_cost": actual["total_cost"],
        "variance_hours": round(actual["total_hours"] - scheduled_hours, 2),
        "variance_cost": round(actual["total_cost"] - scheduled_cost, 2),
    }


def week_bounds(anchor_date) -> tuple[datetime, datetime]:
    """Monday–Sunday window (as UTC datetimes) containing anchor_date."""
    monday = anchor_date - timedelta(days=anchor_date.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    return start, end
