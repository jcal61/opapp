"""
Scheduling — shift planning, publishing, and the scheduled labor-cost math
(against pay rate and POS sales) that gives managers a labor cost % to sit
next to food cost %, without requiring an actual time clock.
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session, joinedload

from app.models import Shift, User, POSSale


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


# ---------- Scheduled labor cost ----------

def scheduled_labor_cost(db: Session, location_id: int, start: datetime, end: datetime) -> Dict[str, Any]:
    """Planned hours/cost from the schedule (not cancelled), grouped by
    employee, plus labor cost % against POS sales for the same window."""
    shifts = [s for s in list_shifts(db, location_id, start, end) if s.status != "cancelled"]
    rate_by_user = {u.id: (u.hourly_rate or 0.0) for u in db.query(User).all()}

    by_user: Dict[int, Dict[str, Any]] = {}
    total_hours = 0.0
    total_cost = 0.0
    for s in shifts:
        hrs = s.scheduled_hours()
        total_hours += hrs
        if not s.user_id:
            continue  # open/unfilled shift — hours don't have a rate to cost against
        rate = rate_by_user.get(s.user_id, 0.0)
        cost = hrs * rate
        total_cost += cost
        if s.user_id not in by_user:
            by_user[s.user_id] = {
                "user_id": s.user_id,
                "name": s.user.name if s.user else "Unknown",
                "hours": 0.0,
                "cost": 0.0,
            }
        by_user[s.user_id]["hours"] += hrs
        by_user[s.user_id]["cost"] += cost

    sales = (
        db.query(POSSale)
        .filter(POSSale.location_id == location_id, POSSale.sold_at >= start, POSSale.sold_at < end)
        .all()
    )
    total_sales = sum(sl.total_amount or 0.0 for sl in sales)
    labor_pct = (total_cost / total_sales * 100.0) if total_sales else None

    return {
        "total_hours": round(total_hours, 2),
        "total_cost": round(total_cost, 2),
        "total_sales": round(total_sales, 2),
        "labor_cost_percent": round(labor_pct, 1) if labor_pct is not None else None,
        "by_user": sorted(by_user.values(), key=lambda r: -r["cost"]),
    }


def week_bounds(anchor_date) -> tuple[datetime, datetime]:
    """Monday–Sunday window (as UTC datetimes) containing anchor_date."""
    monday = anchor_date - timedelta(days=anchor_date.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    return start, end
