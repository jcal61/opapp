"""Employee checklists and SOPs — Jolt-style accountability, temps, corrective actions."""

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Integer, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class SOP(Base):
    """Information Library-style SOP (Jolt Info Library)."""
    __tablename__ = "sops"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))  # opening, closing, safety, prep, FOH
    role_codes: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(20), default="1.0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class ChecklistTemplate(Base):
    """Jolt Lists-style template: opening, closing, food safety, walkthrough."""
    __tablename__ = "checklist_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    role_codes: Mapped[str | None] = mapped_column(String(120))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sop_id: Mapped[int | None] = mapped_column(ForeignKey("sops.id"))
    # Jolt-style schedule hints
    list_type: Mapped[str | None] = mapped_column(String(40), default="ops")
    # ops | food_safety | walkthrough | cleaning | opening | closing
    schedule_hint: Mapped[str | None] = mapped_column(String(80))  # e.g. daily_open, every_4h, eod

    tasks = relationship(
        "ChecklistTaskTemplate",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="ChecklistTaskTemplate.sort_order",
    )
    runs = relationship("ChecklistRun", back_populates="template")


class ChecklistTaskTemplate(Base):
    """Task with optional temp range, photo proof, and corrective action (Jolt pattern)."""
    __tablename__ = "checklist_task_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("checklist_templates.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(40), default="checkbox")
    # checkbox | photo | text | number | yes_no | score
    requires_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    # Temperature / numeric bounds (food safety)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)
    unit_label: Mapped[str | None] = mapped_column(String(20))  # °F, ppm, etc.
    # Just-in-time training / corrective action
    training_note: Mapped[str | None] = mapped_column(Text)  # short JIT guidance
    corrective_action: Mapped[str | None] = mapped_column(Text)  # shown when out of range

    template = relationship("ChecklistTemplate", back_populates="tasks")


class ChecklistRun(Base):
    """Live checklist instance with due time for overdue tracking."""
    __tablename__ = "checklist_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("checklist_templates.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(40), default="open")  # open | completed | cancelled | overdue
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    template = relationship("ChecklistTemplate", back_populates="runs")
    completions = relationship("ChecklistTaskCompletion", back_populates="run", cascade="all, delete-orphan")


class ChecklistTaskCompletion(Base):
    """Accountable completion: who, when, value, out-of-range, corrective action taken."""
    __tablename__ = "checklist_task_completions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("checklist_runs.id"), nullable=False)
    task_template_id: Mapped[int] = mapped_column(ForeignKey("checklist_task_templates.id"), nullable=False)
    completion_type: Mapped[str] = mapped_column(String(40), default="checkbox")
    photo_path: Mapped[str | None] = mapped_column(String(500))
    photo_url: Mapped[str | None] = mapped_column(String(500))
    text_value: Mapped[str | None] = mapped_column(Text)
    number_value: Mapped[float | None] = mapped_column(Float)
    score_value: Mapped[int | None] = mapped_column(Integer)  # 1–5 walkthrough score
    out_of_range: Mapped[bool] = mapped_column(Boolean, default=False)
    corrective_action_taken: Mapped[str | None] = mapped_column(Text)
    employee_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    notes: Mapped[str | None] = mapped_column(Text)

    run = relationship("ChecklistRun", back_populates="completions")
