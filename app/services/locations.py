"""Location setup and selection helpers."""

from __future__ import annotations
from typing import Optional, List
from sqlalchemy.orm import Session

from app.models import Location


def list_locations(db: Session, active_only: bool = False) -> List[Location]:
    q = db.query(Location).order_by(Location.name)
    if active_only:
        q = q.filter(Location.is_active == True)
    return q.all()


def get_location(db: Session, location_id: int) -> Optional[Location]:
    return db.get(Location, location_id)


def create_location(
    db: Session,
    name: str,
    code: str,
    *,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    postal_code: str | None = None,
    timezone: str = "America/Chicago",
    closeout_hour: int = 4,
    phone: str | None = None,
    notes: str | None = None,
    is_active: bool = True,
    parent_id: int | None = None,
) -> Location:
    code = code.strip().upper()
    if db.query(Location).filter(Location.code == code).first():
        raise ValueError(f"Location code '{code}' already exists.")
    loc = Location(
        name=name.strip(),
        code=code,
        address=address or None,
        city=city or None,
        state=state or None,
        postal_code=postal_code or None,
        timezone=timezone,
        closeout_hour=closeout_hour,
        phone=phone or None,
        notes=notes or None,
        is_active=is_active,
        parent_id=parent_id,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def update_location(db: Session, location_id: int, **fields) -> Location:
    loc = db.get(Location, location_id)
    if not loc:
        raise ValueError("Location not found")
    if "code" in fields and fields["code"] is not None:
        new_code = fields["code"].strip().upper()
        clash = db.query(Location).filter(Location.code == new_code, Location.id != location_id).first()
        if clash:
            raise ValueError(f"Location code '{new_code}' already exists.")
        fields["code"] = new_code
    if "name" in fields and fields["name"] is not None:
        fields["name"] = fields["name"].strip()
    for k, v in fields.items():
        if hasattr(loc, k):
            setattr(loc, k, v)
    db.commit()
    db.refresh(loc)
    return loc


def set_location_active(db: Session, location_id: int, active: bool) -> Location:
    return update_location(db, location_id, is_active=active)
