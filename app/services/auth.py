"""Authentication, users, and editable role permissions."""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, List, Dict, Set
from sqlalchemy.orm import Session, joinedload

from app.models import (
    User, Role, RolePermission,
    DEFAULT_ROLE_PERMISSIONS, PERMISSIONS,
)


def seed_roles(db: Session) -> None:
    """Ensure standard roles exist and seed default permissions if empty."""
    defaults = [
        ("owner", "Owner", "Full access including user management"),
        ("manager", "Manager", "Operations, purchasing, counts, costs"),
        ("kitchen", "Kitchen", "Counts entry, waste, view inventory & recipes"),
        ("server", "Server", "Limited — waste logging and dashboard"),
    ]
    for code, name, desc in defaults:
        role = db.query(Role).filter(Role.code == code).first()
        if not role:
            role = Role(code=code, name=name, description=desc)
            db.add(role)
            db.flush()
        # Seed permission rows only if this role has none yet
        existing = db.query(RolePermission).filter(RolePermission.role_id == role.id).count()
        if existing == 0:
            for key in DEFAULT_ROLE_PERMISSIONS.get(code, []):
                db.add(RolePermission(role_id=role.id, permission_key=key, allowed=True))
    db.commit()


def get_role_permissions_map(db: Session) -> Dict[str, Set[str]]:
    """Return {role_code: set(permission_keys allowed)} from DB."""
    seed_roles(db)
    roles = db.query(Role).options(joinedload(Role.permission_rows)).all()
    result: Dict[str, Set[str]] = {}
    for role in roles:
        allowed = {r.permission_key for r in role.permission_rows if r.allowed}
        if not allowed and role.code in DEFAULT_ROLE_PERMISSIONS:
            allowed = set(DEFAULT_ROLE_PERMISSIONS[role.code])
        result[role.code] = allowed
    return result


def set_role_permission(db: Session, role_code: str, permission_key: str, allowed: bool) -> None:
    """Toggle a single permission for a role. Owners must keep users_admin."""
    if role_code == "owner" and permission_key == "users_admin" and not allowed:
        raise ValueError("Owner must retain users_admin permission.")
    if permission_key not in PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission_key}")

    role = db.query(Role).filter(Role.code == role_code).first()
    if not role:
        raise ValueError(f"Unknown role: {role_code}")

    row = (
        db.query(RolePermission)
        .filter(RolePermission.role_id == role.id, RolePermission.permission_key == permission_key)
        .first()
    )
    if row:
        row.allowed = allowed
    else:
        db.add(RolePermission(role_id=role.id, permission_key=permission_key, allowed=allowed))
    db.commit()


def reset_role_permissions(db: Session, role_code: str) -> None:
    """Restore a role to DEFAULT_ROLE_PERMISSIONS."""
    role = db.query(Role).filter(Role.code == role_code).first()
    if not role:
        raise ValueError(f"Unknown role: {role_code}")
    db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
    for key in DEFAULT_ROLE_PERMISSIONS.get(role_code, []):
        db.add(RolePermission(role_id=role.id, permission_key=key, allowed=True))
    db.commit()


def permissions_for_user(db: Session, user: User) -> Set[str]:
    if not user or not user.role:
        return set()
    m = get_role_permissions_map(db)
    return m.get(user.role.code, set())


def create_user(
    db: Session,
    name: str,
    role_code: str,
    pin: Optional[str] = None,
    email: Optional[str] = None,
) -> User:
    seed_roles(db)
    role = db.query(Role).filter(Role.code == role_code).first()
    if not role:
        raise ValueError(f"Unknown role: {role_code}")
    user = User(
        name=name.strip(),
        email=email.strip() if email else None,
        pin=pin.strip() if pin else None,
        role_id=role.id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_pin(db: Session, pin: str) -> Optional[User]:
    if not pin or not pin.strip():
        return None
    user = (
        db.query(User)
        .options(joinedload(User.role).joinedload(Role.permission_rows))
        .filter(User.pin == pin.strip(), User.is_active == True)
        .first()
    )
    if user:
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
    return user


def list_users(db: Session, active_only: bool = True) -> List[User]:
    q = db.query(User).options(joinedload(User.role)).order_by(User.name)
    if active_only:
        q = q.filter(User.is_active == True)
    return q.all()


def deactivate_user(db: Session, user_id: int) -> None:
    user = db.get(User, user_id)
    if user:
        user.is_active = False
        db.commit()


def user_can(user: Optional[User], permission: str, db: Optional[Session] = None) -> bool:
    if not user:
        return False
    if db is not None:
        return permission in permissions_for_user(db, user)
    return user.has_permission(permission)
