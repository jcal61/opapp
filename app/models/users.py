"""User, Role, and permission models with editable role overrides."""

from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, Float, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


PERMISSIONS = {
    "dashboard": "View dashboard",
    "inventory_view": "View inventory",
    "inventory_edit": "Edit inventory / adjust stock",
    "recipes_view": "View recipes",
    "recipes_edit": "Edit recipes",
    "purchasing": "Access purchasing area",
    "counts": "Access physical counts",
    "variance": "Run variance reports",
    "waste": "Log waste",
    "pos_import": "Import / simulate POS sales",
    "invoices": "Capture invoices",
    "users_admin": "Manage users and roles",
    "view_costs": "See dollar costs, food cost %, variance $",
    "counts_enter": "Enter quantities on a count",
    "counts_close": "Close a physical count",
    "counts_align": "Align theoretical stock when closing a count",
    "purchasing_create": "Create purchase orders",
    "purchasing_submit": "Submit purchase orders",
    "purchasing_receive": "Receive goods against POs",
    "purchasing_cancel": "Cancel purchase orders",
    "checklists": "Run employee checklists",
    "checklists_admin": "Manage checklist templates and SOPs",
    "scheduling_view": "View the staff schedule",
    "scheduling_edit": "Build and publish the staff schedule, view scheduled labor cost",
    "training_view": "Take training courses and quizzes",
    "training_admin": "Create and manage training courses, lessons, and quizzes",
    "financials_view": "View the operating P&L, prime cost, and budget vs. actual reports",
    "financials_admin": "Manage budgets and operating expenses",
    "cash_management": "Record and view cash drawer counts",
    "logbook": "Post and read manager logbook entries",
}

# Defaults used when seeding / resetting a role
DEFAULT_ROLE_PERMISSIONS = {
    "owner": list(PERMISSIONS.keys()),
    "manager": [
        "dashboard",
        "inventory_view", "inventory_edit",
        "recipes_view", "recipes_edit",
        "purchasing", "purchasing_create", "purchasing_submit", "purchasing_receive", "purchasing_cancel",
        "counts", "counts_enter", "counts_close", "counts_align",
        "variance", "waste", "pos_import", "invoices",
        "view_costs", "checklists", "checklists_admin",
        "scheduling_view", "scheduling_edit",
        "training_view", "training_admin",
        "financials_view", "financials_admin", "cash_management", "logbook",
    ],
    "kitchen": [
        "dashboard",
        "inventory_view",
        "recipes_view",
        "counts", "counts_enter",
        "waste", "checklists",
        "scheduling_view",
        "training_view",
        "logbook",
    ],
    "server": [
        "dashboard",
        "waste",
        "checklists",
        "scheduling_view",
        "training_view",
        "cash_management", "logbook",
    ],
}

# Backwards-compatible alias
ROLE_PERMISSIONS = DEFAULT_ROLE_PERMISSIONS


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    users = relationship("User", back_populates="role")
    permission_rows = relationship(
        "RolePermission",
        back_populates="role",
        cascade="all, delete-orphan",
    )


class RolePermission(Base):
    """Editable per-role permission flags (overrides defaults when seeded)."""
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_key", name="uq_role_perm"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    permission_key: Mapped[str] = mapped_column(String(60), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)

    role = relationship("Role", back_populates="permission_rows")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), unique=True)
    pin: Mapped[str | None] = mapped_column(String(20))
    password_hash: Mapped[str | None] = mapped_column(String(200))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    hourly_rate: Mapped[float | None] = mapped_column(Float, default=0.0)  # $/hr, used for labor cost
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    role = relationship("Role", back_populates="users")

    def has_permission(self, key: str, db_allowed: set[str] | None = None) -> bool:
        """
        Prefer live DB permission set when provided; otherwise fall back to
        role.permission_rows or DEFAULT_ROLE_PERMISSIONS.
        """
        if not self.is_active or not self.role:
            return False
        if db_allowed is not None:
            return key in db_allowed
        # Prefer rows attached to role if loaded
        rows = getattr(self.role, "permission_rows", None) or []
        if rows:
            return any(r.permission_key == key and r.allowed for r in rows)
        return key in DEFAULT_ROLE_PERMISSIONS.get(self.role.code, [])

    def permission_list(self) -> list[str]:
        if not self.role:
            return []
        rows = getattr(self.role, "permission_rows", None) or []
        if rows:
            return [r.permission_key for r in rows if r.allowed]
        return list(DEFAULT_ROLE_PERMISSIONS.get(self.role.code, []))
