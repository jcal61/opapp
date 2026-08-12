"""Checklist & SOP logic — Jolt-style accountability, temp ranges, corrective actions."""

from __future__ import annotations
import os
import shutil
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session, joinedload

from app.models.checklists import (
    SOP, ChecklistTemplate, ChecklistTaskTemplate,
    ChecklistRun, ChecklistTaskCompletion,
)
from app.models import User

PHOTO_ROOT = Path(os.environ.get("CRAFTABLE_PHOTO_ROOT", "/tmp/craftable_checklist_photos"))


def ensure_photo_root() -> Path:
    PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
    return PHOTO_ROOT


def save_task_photo(run_id: int, task_id: int, source_path: str) -> tuple[str, str]:
    root = ensure_photo_root()
    dest_dir = root / f"checklists/{run_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(source_path).suffix or ".jpg"
    dest = dest_dir / f"task_{task_id}_{uuid.uuid4().hex[:8]}{ext}"
    shutil.copy2(source_path, dest)
    rel = str(dest.relative_to(root))
    return rel, str(dest)


# ---------- SOPs ----------

def create_sop(
    db: Session,
    title: str,
    body: str | None = None,
    category: str | None = None,
    role_codes: str | None = None,
    location_id: int | None = None,
) -> SOP:
    sop = SOP(
        title=title.strip(),
        body=body,
        category=category,
        role_codes=role_codes,
        location_id=location_id,
    )
    db.add(sop)
    db.commit()
    db.refresh(sop)
    return sop


def list_sops(db: Session, active_only: bool = True, location_id: int | None = None) -> List[SOP]:
    q = db.query(SOP).order_by(SOP.category, SOP.title)
    if active_only:
        q = q.filter(SOP.is_active == True)
    if location_id is not None:
        q = q.filter((SOP.location_id == location_id) | (SOP.location_id.is_(None)))
    return q.all()


def update_sop(
    db: Session,
    sop_id: int,
    title: str | None = None,
    body: str | None = None,
    category: str | None = None,
    role_codes: str | None = None,
    version: str | None = None,
    location_id: int | None = ...,
) -> SOP:
    sop = db.get(SOP, sop_id)
    if not sop:
        raise ValueError("SOP not found")
    if title is not None:
        if not title.strip():
            raise ValueError("Title can't be empty.")
        sop.title = title.strip()
    if body is not None:
        sop.body = body
    if category is not None:
        sop.category = category
    if role_codes is not None:
        sop.role_codes = role_codes
    if version is not None:
        sop.version = version
    if location_id is not ...:
        sop.location_id = location_id
    db.commit()
    db.refresh(sop)
    return sop


def set_sop_active(db: Session, sop_id: int, active: bool) -> SOP:
    sop = db.get(SOP, sop_id)
    if not sop:
        raise ValueError("SOP not found")
    sop.is_active = active
    db.commit()
    db.refresh(sop)
    return sop


def delete_sop(db: Session, sop_id: int) -> None:
    """Hard-delete only if no checklist template still points to it as its
    reference SOP — otherwise deactivate it instead of breaking that link."""
    sop = db.get(SOP, sop_id)
    if not sop:
        return
    linked = db.query(ChecklistTemplate).filter(ChecklistTemplate.sop_id == sop_id).count()
    if linked:
        raise ValueError(
            f"'{sop.title}' is linked to {linked} checklist template(s) — unlink it or use Deactivate instead of Delete."
        )
    db.delete(sop)
    db.commit()


# ---------- Templates ----------

def create_template(
    db: Session,
    name: str,
    description: str | None = None,
    role_codes: str | None = None,
    location_id: int | None = None,
    sop_id: int | None = None,
    list_type: str = "ops",
    schedule_hint: str | None = None,
) -> ChecklistTemplate:
    t = ChecklistTemplate(
        name=name.strip(),
        description=description,
        role_codes=role_codes,
        location_id=location_id,
        sop_id=sop_id,
        list_type=list_type,
        schedule_hint=schedule_hint,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def add_task_to_template(
    db: Session,
    template_id: int,
    title: str,
    task_type: str = "checkbox",
    instructions: str | None = None,
    requires_photo: bool = False,
    sort_order: int | None = None,
    is_required: bool = True,
    min_value: float | None = None,
    max_value: float | None = None,
    unit_label: str | None = None,
    training_note: str | None = None,
    corrective_action: str | None = None,
) -> ChecklistTaskTemplate:
    if task_type == "photo":
        requires_photo = True
    if sort_order is None:
        # Default to the end of the list rather than 0, so newly added
        # tasks don't all pile up at the top needing to be reordered.
        current_max = (
            db.query(ChecklistTaskTemplate)
            .filter(ChecklistTaskTemplate.template_id == template_id)
            .count()
        )
        sort_order = current_max
    task = ChecklistTaskTemplate(
        template_id=template_id,
        title=title.strip(),
        task_type=task_type,
        instructions=instructions,
        requires_photo=requires_photo,
        sort_order=sort_order,
        is_required=is_required,
        min_value=min_value,
        max_value=max_value,
        unit_label=unit_label,
        training_note=training_note,
        corrective_action=corrective_action,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def update_task(
    db: Session,
    task_id: int,
    title: str | None = None,
    instructions: str | None = None,
    task_type: str | None = None,
    requires_photo: bool | None = None,
    is_required: bool | None = None,
    min_value: float | None = ...,
    max_value: float | None = ...,
    unit_label: str | None = None,
    training_note: str | None = None,
    corrective_action: str | None = None,
) -> ChecklistTaskTemplate:
    task = db.get(ChecklistTaskTemplate, task_id)
    if not task:
        raise ValueError("Task not found")
    if title is not None:
        if not title.strip():
            raise ValueError("Task title can't be empty.")
        task.title = title.strip()
    if instructions is not None:
        task.instructions = instructions
    if task_type is not None:
        task.task_type = task_type
        if task_type == "photo":
            task.requires_photo = True
    if requires_photo is not None:
        task.requires_photo = requires_photo
    if is_required is not None:
        task.is_required = is_required
    if min_value is not ...:
        task.min_value = min_value
    if max_value is not ...:
        task.max_value = max_value
    if unit_label is not None:
        task.unit_label = unit_label
    if training_note is not None:
        task.training_note = training_note
    if corrective_action is not None:
        task.corrective_action = corrective_action
    db.commit()
    db.refresh(task)
    return task


def set_task_active(db: Session, task_id: int, active: bool) -> ChecklistTaskTemplate:
    task = db.get(ChecklistTaskTemplate, task_id)
    if not task:
        raise ValueError("Task not found")
    task.is_active = active
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, task_id: int) -> None:
    """Hard-delete only if the task has never been completed on any run —
    completions reference the task by id, so deleting one with history would
    silently orphan those accountability records. Use set_task_active(False)
    instead when there's history to preserve."""
    task = db.get(ChecklistTaskTemplate, task_id)
    if not task:
        return
    has_history = (
        db.query(ChecklistTaskCompletion)
        .filter(ChecklistTaskCompletion.task_template_id == task_id)
        .first()
        is not None
    )
    if has_history:
        raise ValueError(
            f"'{task.title}' has completion history and can't be deleted — use Deactivate instead to keep the record intact."
        )
    db.delete(task)
    db.commit()


def move_task(db: Session, task_id: int, direction: str) -> None:
    """Swap sort_order with the adjacent task in the same template — the
    simplest reliable way to reorder a list in a plain Streamlit UI (no
    drag-and-drop widget needed)."""
    task = db.get(ChecklistTaskTemplate, task_id)
    if not task:
        raise ValueError("Task not found")
    siblings = (
        db.query(ChecklistTaskTemplate)
        .filter(ChecklistTaskTemplate.template_id == task.template_id)
        .order_by(ChecklistTaskTemplate.sort_order, ChecklistTaskTemplate.id)
        .all()
    )
    idx = next((i for i, t in enumerate(siblings) if t.id == task_id), None)
    if idx is None:
        return
    if direction == "up" and idx > 0:
        other = siblings[idx - 1]
    elif direction == "down" and idx < len(siblings) - 1:
        other = siblings[idx + 1]
    else:
        return  # already at the top/bottom — nothing to do
    task.sort_order, other.sort_order = other.sort_order, task.sort_order
    # Sort orders can collide (e.g. everything defaulted to 0 historically) —
    # if the swap didn't actually change relative order, fall back to a full
    # renumber so the button always visibly does something.
    if task.sort_order == other.sort_order:
        for i, t in enumerate(siblings):
            t.sort_order = i
        idx2 = idx - 1 if direction == "up" else idx + 1
        siblings[idx].sort_order, siblings[idx2].sort_order = siblings[idx2].sort_order, siblings[idx].sort_order
    db.commit()


def reorder_tasks(db: Session, template_id: int, ordered_task_ids: List[int]) -> None:
    """Bulk alternative to move_task — set sort_order from an explicit
    ordered list of task ids (all must belong to the given template)."""
    tasks = {
        t.id: t
        for t in db.query(ChecklistTaskTemplate).filter(ChecklistTaskTemplate.template_id == template_id).all()
    }
    if set(ordered_task_ids) != set(tasks.keys()):
        raise ValueError("Task list doesn't match this template's current tasks.")
    for i, tid in enumerate(ordered_task_ids):
        tasks[tid].sort_order = i
    db.commit()


def list_templates(db: Session, active_only: bool = True, location_id: int | None = None) -> List[ChecklistTemplate]:
    q = (
        db.query(ChecklistTemplate)
        .options(joinedload(ChecklistTemplate.tasks))
        .order_by(ChecklistTemplate.name)
    )
    if active_only:
        q = q.filter(ChecklistTemplate.is_active == True)
    if location_id is not None:
        q = q.filter(
            (ChecklistTemplate.location_id == location_id) | (ChecklistTemplate.location_id.is_(None))
        )
    return q.all()


def update_template(
    db: Session,
    template_id: int,
    name: str | None = None,
    description: str | None = None,
    role_codes: str | None = None,
    list_type: str | None = None,
    schedule_hint: str | None = None,
    sop_id: int | None = ...,
) -> ChecklistTemplate:
    t = db.get(ChecklistTemplate, template_id)
    if not t:
        raise ValueError("Template not found")
    if name is not None:
        if not name.strip():
            raise ValueError("Template name can't be empty.")
        t.name = name.strip()
    if description is not None:
        t.description = description
    if role_codes is not None:
        t.role_codes = role_codes
    if list_type is not None:
        t.list_type = list_type
    if schedule_hint is not None:
        t.schedule_hint = schedule_hint
    if sop_id is not ...:
        t.sop_id = sop_id
    db.commit()
    db.refresh(t)
    return t


def set_template_active(db: Session, template_id: int, active: bool) -> ChecklistTemplate:
    t = db.get(ChecklistTemplate, template_id)
    if not t:
        raise ValueError("Template not found")
    t.is_active = active
    db.commit()
    db.refresh(t)
    return t


def delete_template(db: Session, template_id: int) -> None:
    """Hard-delete (cascading to its tasks) only if the template has never
    been run — any ChecklistRun is a real accountability record that must
    stay intact, so a template with run history can only be deactivated."""
    t = db.get(ChecklistTemplate, template_id)
    if not t:
        return
    run_count = db.query(ChecklistRun).filter(ChecklistRun.template_id == template_id).count()
    if run_count:
        raise ValueError(
            f"'{t.name}' has {run_count} checklist run(s) on record and can't be deleted — use Deactivate instead."
        )
    db.delete(t)  # cascades to its tasks (none of which have completions, since there are no runs)
    db.commit()


# ---------- Runs & completions ----------

def start_checklist_run(
    db: Session,
    template_id: int,
    location_id: int,
    user_id: int | None = None,
    notes: str | None = None,
    due_minutes: int | None = 120,
) -> ChecklistRun:
    due_at = None
    if due_minutes:
        due_at = datetime.now(timezone.utc) + timedelta(minutes=due_minutes)
    run = ChecklistRun(
        template_id=template_id,
        location_id=location_id,
        assigned_user_id=user_id,
        started_by_user_id=user_id,
        status="open",
        notes=notes,
        due_at=due_at,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _is_out_of_range(task: ChecklistTaskTemplate, number_value: float | None) -> bool:
    if number_value is None:
        return False
    if task.min_value is not None and number_value < task.min_value:
        return True
    if task.max_value is not None and number_value > task.max_value:
        return True
    return False


def complete_task(
    db: Session,
    run_id: int,
    task_template_id: int,
    employee_user_id: int | None,
    *,
    completion_type: str = "checkbox",
    photo_source_path: str | None = None,
    text_value: str | None = None,
    number_value: float | None = None,
    score_value: int | None = None,
    notes: str | None = None,
    corrective_action_taken: str | None = None,
) -> ChecklistTaskCompletion:
    run = db.get(ChecklistRun, run_id)
    if not run or run.status not in ("open", "overdue"):
        raise ValueError("Checklist run is not open")

    task = db.get(ChecklistTaskTemplate, task_template_id)
    if not task or task.template_id != run.template_id:
        raise ValueError("Task does not belong to this checklist")

    photo_path = photo_url = None
    if completion_type == "photo" or task.requires_photo or task.task_type == "photo":
        if not photo_source_path:
            raise ValueError("Photo evidence required for this task (live capture / upload)")
        photo_path, abs_path = save_task_photo(run_id, task_template_id, photo_source_path)
        photo_url = f"/media/{photo_path}" if not photo_path.startswith("/") else abs_path
        completion_type = "photo"

    out_of_range = False
    if task.task_type == "number" or number_value is not None:
        out_of_range = _is_out_of_range(task, number_value)
        if out_of_range and not corrective_action_taken and task.corrective_action:
            # Soft require: still allow save but flag; UI should prompt
            pass

    existing = (
        db.query(ChecklistTaskCompletion)
        .filter(
            ChecklistTaskCompletion.run_id == run_id,
            ChecklistTaskCompletion.task_template_id == task_template_id,
        )
        .first()
    )
    if existing:
        existing.completion_type = completion_type
        existing.photo_path = photo_path or existing.photo_path
        existing.photo_url = photo_url or existing.photo_url
        existing.text_value = text_value
        existing.number_value = number_value
        existing.score_value = score_value
        existing.out_of_range = out_of_range
        existing.corrective_action_taken = corrective_action_taken
        existing.employee_user_id = employee_user_id
        existing.completed_at = datetime.now(timezone.utc)
        existing.notes = notes
        db.commit()
        db.refresh(existing)
        return existing

    comp = ChecklistTaskCompletion(
        run_id=run_id,
        task_template_id=task_template_id,
        completion_type=completion_type,
        photo_path=photo_path,
        photo_url=photo_url,
        text_value=text_value,
        number_value=number_value,
        score_value=score_value,
        out_of_range=out_of_range,
        corrective_action_taken=corrective_action_taken,
        employee_user_id=employee_user_id,
        notes=notes,
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)
    return comp


def finish_run(db: Session, run_id: int) -> ChecklistRun:
    run = (
        db.query(ChecklistRun)
        .options(
            joinedload(ChecklistRun.template).joinedload(ChecklistTemplate.tasks),
            joinedload(ChecklistRun.completions),
        )
        .filter(ChecklistRun.id == run_id)
        .first()
    )
    if not run:
        raise ValueError("Run not found")
    required_ids = {t.id for t in run.template.tasks if t.is_required}
    done_ids = {c.task_template_id for c in run.completions}
    missing = required_ids - done_ids
    if missing:
        raise ValueError(f"Required tasks incomplete: {len(missing)} remaining")
    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run


def mark_overdue_runs(db: Session, location_id: int | None = None) -> int:
    now = datetime.now(timezone.utc)
    q = db.query(ChecklistRun).filter(
        ChecklistRun.status == "open",
        ChecklistRun.due_at.isnot(None),
        ChecklistRun.due_at < now,
    )
    if location_id is not None:
        q = q.filter(ChecklistRun.location_id == location_id)
    count = 0
    for run in q.all():
        run.status = "overdue"
        count += 1
    if count:
        db.commit()
    return count


def list_open_runs(db: Session, location_id: int | None = None) -> List[ChecklistRun]:
    mark_overdue_runs(db, location_id)
    q = (
        db.query(ChecklistRun)
        .options(joinedload(ChecklistRun.template), joinedload(ChecklistRun.completions))
        .filter(ChecklistRun.status.in_(["open", "overdue"]))
        .order_by(ChecklistRun.started_at.desc())
    )
    if location_id is not None:
        q = q.filter(ChecklistRun.location_id == location_id)
    return q.all()


def get_run_progress(db: Session, run_id: int) -> Dict[str, Any]:
    run = (
        db.query(ChecklistRun)
        .options(
            joinedload(ChecklistRun.template).joinedload(ChecklistTemplate.tasks),
            joinedload(ChecklistRun.completions),
        )
        .filter(ChecklistRun.id == run_id)
        .first()
    )
    if not run:
        return {}
    all_tasks = sorted(run.template.tasks, key=lambda t: t.sort_order)
    done = {c.task_template_id: c for c in run.completions}
    if run.status in ("open", "overdue"):
        # A still-in-progress run only shows currently-active tasks — but
        # never hides one that's already been completed, in case it was
        # deactivated mid-run.
        tasks = [t for t in all_tasks if t.is_active or t.id in done]
    else:
        # Completed/cancelled runs always show the full historical task
        # list, including any since-deactivated tasks.
        tasks = all_tasks
    users = {u.id: u for u in db.query(User).all()}
    task_rows = []
    for t in tasks:
        c = done.get(t.id)
        emp_name = None
        if c and c.employee_user_id:
            u = users.get(c.employee_user_id)
            emp_name = u.name if u else f"#{c.employee_user_id}"
        task_rows.append({
            "task_id": t.id,
            "title": t.title,
            "type": t.task_type,
            "requires_photo": t.requires_photo,
            "required": t.is_required,
            "instructions": t.instructions,
            "training_note": t.training_note,
            "corrective_action": t.corrective_action,
            "min_value": t.min_value,
            "max_value": t.max_value,
            "unit_label": t.unit_label,
            "done": t.id in done,
            "completion": c,
            "employee_name": emp_name,
        })
    pct = (len(done) / max(len(tasks), 1)) * 100
    return {
        "run_id": run.id,
        "name": run.template.name,
        "list_type": run.template.list_type,
        "status": run.status,
        "total": len(tasks),
        "completed": len(done),
        "pct": round(pct, 1),
        "due_at": run.due_at,
        "started_at": run.started_at,
        "tasks": task_rows,
    }


def location_checklist_report(db: Session, location_id: int, limit: int = 20) -> Dict[str, Any]:
    """Jolt-style at-a-glance: completion rates, overdue, out-of-range temps."""
    mark_overdue_runs(db, location_id)
    open_runs = list_open_runs(db, location_id)
    recent = (
        db.query(ChecklistRun)
        .options(joinedload(ChecklistRun.template), joinedload(ChecklistRun.completions))
        .filter(ChecklistRun.location_id == location_id)
        .order_by(ChecklistRun.started_at.desc())
        .limit(limit)
        .all()
    )
    completed = [r for r in recent if r.status == "completed"]
    overdue = [r for r in open_runs if r.status == "overdue"]
    exceptions = (
        db.query(ChecklistTaskCompletion)
        .join(ChecklistRun)
        .filter(
            ChecklistRun.location_id == location_id,
            ChecklistTaskCompletion.out_of_range == True,
        )
        .order_by(ChecklistTaskCompletion.completed_at.desc())
        .limit(15)
        .all()
    )
    return {
        "open_count": len([r for r in open_runs if r.status == "open"]),
        "overdue_count": len(overdue),
        "completed_recent": len(completed),
        "avg_completion_pct": (
            round(
                sum(
                    (len(r.completions) / max(len(r.template.tasks), 1) * 100)
                    for r in completed
                    if r.template
                )
                / max(len(completed), 1),
                1,
            )
            if completed
            else 0
        ),
        "open_runs": open_runs,
        "overdue_runs": overdue,
        "temp_exceptions": exceptions,
    }


def seed_demo_checklists(db: Session, location_id: int) -> None:
    """Jolt-style demo: opening, closing, food safety temps, manager walkthrough."""
    if db.query(ChecklistTemplate).count() > 0:
        return

    sop = create_sop(
        db,
        title="Opening Line Checklist SOP",
        category="opening",
        role_codes="kitchen,manager",
        location_id=location_id,
        body=(
            "## Opening standards\n"
            "1. Sanitize all stations before product leaves the walk-in.\n"
            "2. Verify cooler temps — photo of thermometer required.\n"
            "3. Stock mise en place per prep list.\n"
            "4. Confirm fire suppression pin and exit paths are clear."
        ),
    )

    # Opening
    tmpl = create_template(
        db,
        name="Opening Checklist",
        description="Daily open — kitchen & floor (Jolt Lists style)",
        role_codes="kitchen,manager",
        location_id=location_id,
        sop_id=sop.id,
        list_type="opening",
        schedule_hint="daily_open",
    )
    add_task_to_template(db, tmpl.id, "Unlock and disarm alarm", "checkbox", sort_order=1)
    add_task_to_template(
        db, tmpl.id, "Walk-in cooler temperature", "number",
        instructions="Enter °F from calibrated thermometer",
        sort_order=2, min_value=33, max_value=41, unit_label="°F",
        training_note="Safe cold holding is 41°F or below.",
        corrective_action="Move product to working cooler, call manager, log incident.",
    )
    add_task_to_template(
        db, tmpl.id, "Photo: cooler thermometer reading", "photo",
        instructions="Live capture of the thermometer — no gallery on mobile",
        requires_photo=True, sort_order=3,
        training_note="Photo proof prevents pencil-whipping.",
    )
    add_task_to_template(db, tmpl.id, "Stock ice bins", "checkbox", sort_order=4)
    add_task_to_template(
        db, tmpl.id, "Sanitizer bucket concentration", "number",
        instructions="Test strip reading in ppm",
        sort_order=5, min_value=50, max_value=200, unit_label="ppm",
        corrective_action="Remake sanitizer solution and retest.",
    )
    add_task_to_template(db, tmpl.id, "All stations wiped and ready", "checkbox", sort_order=6)

    # Closing
    close = create_template(
        db,
        name="Closing Checklist",
        description="End of night shutdown",
        role_codes="kitchen,server,manager",
        location_id=location_id,
        list_type="closing",
        schedule_hint="eod",
    )
    add_task_to_template(db, close.id, "Break down line and cover food", "checkbox", sort_order=1)
    add_task_to_template(
        db, close.id, "Photo: clean line after breakdown", "photo",
        requires_photo=True, sort_order=2,
    )
    add_task_to_template(db, close.id, "Trash taken out / back door locked", "checkbox", sort_order=3)
    add_task_to_template(db, close.id, "Safe drop completed", "checkbox", sort_order=4)

    # Food safety line check (Jolt temp log style)
    fs = create_template(
        db,
        name="Food Safety Line Check",
        description="Hot/cold holding temps — HACCP-style log",
        role_codes="kitchen,manager",
        location_id=location_id,
        list_type="food_safety",
        schedule_hint="every_4h",
    )
    add_task_to_template(
        db, fs.id, "Hot holding — soup well", "number",
        sort_order=1, min_value=135, max_value=200, unit_label="°F",
        training_note="Hot holding must stay at or above 135°F.",
        corrective_action="Reheat to 165°F then return to well, or discard if time exceeded.",
    )
    add_task_to_template(
        db, fs.id, "Hot holding — protein", "number",
        sort_order=2, min_value=135, max_value=200, unit_label="°F",
        corrective_action="Reheat to 165°F or discard per policy.",
    )
    add_task_to_template(
        db, fs.id, "Cold holding — prep rail", "number",
        sort_order=3, min_value=33, max_value=41, unit_label="°F",
        corrective_action="Move product to walk-in; service line unit if above 41°F.",
    )
    add_task_to_template(
        db, fs.id, "Photo: any out-of-range unit", "photo",
        instructions="Only required if a temp was out of range",
        requires_photo=False, is_required=False, sort_order=4,
    )

    # Manager walkthrough with score
    walk = create_template(
        db,
        name="Manager Walkthrough",
        description="Cleanliness & brand standards score",
        role_codes="manager,owner",
        location_id=location_id,
        list_type="walkthrough",
        schedule_hint="daily",
    )
    add_task_to_template(db, walk.id, "Dining room cleanliness (1–5)", "score", sort_order=1)
    add_task_to_template(db, walk.id, "Restrooms stocked & clean (1–5)", "score", sort_order=2)
    add_task_to_template(
        db, walk.id, "Photo: dining room overview", "photo",
        requires_photo=True, sort_order=3,
    )
    add_task_to_template(db, walk.id, "Exterior / entrance clear", "checkbox", sort_order=4)
