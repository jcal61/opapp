"""
Checklist API for iOS (and other clients).

Push completions including live-camera photo evidence.
"""

from __future__ import annotations
import tempfile
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.database import SessionLocal
from app.models import (
    ChecklistTemplate, ChecklistTaskTemplate, ChecklistRun,
    ChecklistTaskCompletion, Location, User, SOP,
)
from app.services.checklists import (
    start_checklist_run, complete_task, finish_run,
    list_templates, list_open_runs, get_run_progress, list_sops,
)

router = APIRouter(prefix="/api/checklists", tags=["checklists"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Schemas ----------

class TaskOut(BaseModel):
    id: int
    title: str
    task_type: str
    requires_photo: bool
    sort_order: int
    is_required: bool
    instructions: Optional[str] = None

    class Config:
        from_attributes = True


class TemplateOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    role_codes: Optional[str] = None
    tasks: List[TaskOut] = []

    class Config:
        from_attributes = True


class RunOut(BaseModel):
    id: int
    template_id: int
    template_name: Optional[str] = None
    location_id: int
    status: str
    started_at: str
    completed_at: Optional[str] = None


class CompletionOut(BaseModel):
    id: int
    task_template_id: int
    completion_type: str
    photo_path: Optional[str] = None
    photo_url: Optional[str] = None
    text_value: Optional[str] = None
    number_value: Optional[float] = None
    employee_user_id: Optional[int] = None
    completed_at: str


class CompleteBody(BaseModel):
    task_template_id: int
    employee_user_id: Optional[int] = None
    completion_type: str = "checkbox"
    text_value: Optional[str] = None
    number_value: Optional[float] = None
    notes: Optional[str] = None
    score_value: Optional[int] = None
    corrective_action_taken: Optional[str] = None
    photo_url: Optional[str] = None


class StartRunBody(BaseModel):
    template_id: int
    location_id: int
    user_id: Optional[int] = None
    notes: Optional[str] = None


class SOPOut(BaseModel):
    id: int
    title: str
    category: Optional[str] = None
    body: Optional[str] = None
    role_codes: Optional[str] = None
    version: Optional[str] = None


# ---------- Endpoints ----------

@router.get("/templates", response_model=List[TemplateOut])
def api_list_templates(location_id: Optional[int] = None, db: Session = Depends(get_db)):
    templates = list_templates(db, location_id=location_id)
    out = []
    for t in templates:
        out.append(TemplateOut(
            id=t.id,
            name=t.name,
            description=t.description,
            role_codes=t.role_codes,
            tasks=[
                TaskOut(
                    id=task.id,
                    title=task.title,
                    task_type=task.task_type,
                    requires_photo=task.requires_photo,
                    sort_order=task.sort_order,
                    is_required=task.is_required,
                    instructions=task.instructions,
                )
                for task in sorted(t.tasks, key=lambda x: x.sort_order)
            ],
        ))
    return out


@router.get("/sops", response_model=List[SOPOut])
def api_list_sops(location_id: Optional[int] = None, db: Session = Depends(get_db)):
    return [
        SOPOut(
            id=s.id, title=s.title, category=s.category,
            body=s.body, role_codes=s.role_codes, version=s.version,
        )
        for s in list_sops(db, location_id=location_id)
    ]


@router.post("/runs", response_model=RunOut)
def api_start_run(body: StartRunBody, db: Session = Depends(get_db)):
    tmpl = db.get(ChecklistTemplate, body.template_id)
    if not tmpl:
        raise HTTPException(404, "Template not found")
    loc = db.get(Location, body.location_id)
    if not loc:
        raise HTTPException(404, "Location not found")
    run = start_checklist_run(db, body.template_id, body.location_id, user_id=body.user_id, notes=body.notes)
    return RunOut(
        id=run.id,
        template_id=run.template_id,
        template_name=tmpl.name,
        location_id=run.location_id,
        status=run.status,
        started_at=run.started_at.isoformat() if run.started_at else "",
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
    )


@router.get("/runs/open", response_model=List[RunOut])
def api_open_runs(location_id: Optional[int] = None, db: Session = Depends(get_db)):
    runs = list_open_runs(db, location_id=location_id)
    return [
        RunOut(
            id=r.id,
            template_id=r.template_id,
            template_name=r.template.name if r.template else None,
            location_id=r.location_id,
            status=r.status,
            started_at=r.started_at.isoformat() if r.started_at else "",
            completed_at=None,
        )
        for r in runs
    ]


@router.get("/runs/{run_id}")
def api_run_progress(run_id: int, db: Session = Depends(get_db)):
    progress = get_run_progress(db, run_id)
    if not progress:
        raise HTTPException(404, "Run not found")
    # Serialize completions lightly
    tasks = []
    for t in progress["tasks"]:
        item = {k: v for k, v in t.items() if k != "completion"}
        c = t.get("completion")
        if c:
            item["completion"] = {
                "id": c.id,
                "completion_type": c.completion_type,
                "photo_path": c.photo_path,
                "photo_url": c.photo_url,
                "text_value": c.text_value,
                "number_value": c.number_value,
                "employee_user_id": c.employee_user_id,
                "completed_at": c.completed_at.isoformat() if c.completed_at else None,
            }
        else:
            item["completion"] = None
        tasks.append(item)
    progress["tasks"] = tasks
    return progress


@router.post("/runs/{run_id}/complete", response_model=CompletionOut)
def api_complete_task(run_id: int, body: CompleteBody, db: Session = Depends(get_db)):
    """Complete a non-photo task, or attach a remote photo_url."""
    try:
        if body.completion_type == "photo" and not body.photo_url:
            raise HTTPException(
                400,
                "For photo tasks use POST /runs/{id}/complete-photo with multipart file, "
                "or provide photo_url here.",
            )
        # If remote URL only, still need a dummy local path path for service — handle here
        if body.photo_url and body.completion_type == "photo":
            # Store URL-only completion without local file
            run = db.get(ChecklistRun, run_id)
            if not run or run.status != "open":
                raise HTTPException(400, "Run not open")
            existing = (
                db.query(ChecklistTaskCompletion)
                .filter(
                    ChecklistTaskCompletion.run_id == run_id,
                    ChecklistTaskCompletion.task_template_id == body.task_template_id,
                )
                .first()
            )
            if existing:
                existing.completion_type = "photo"
                existing.photo_url = body.photo_url
                existing.employee_user_id = body.employee_user_id
                existing.notes = body.notes
                db.commit()
                db.refresh(existing)
                comp = existing
            else:
                from datetime import datetime, timezone
                comp = ChecklistTaskCompletion(
                    run_id=run_id,
                    task_template_id=body.task_template_id,
                    completion_type="photo",
                    photo_url=body.photo_url,
                    employee_user_id=body.employee_user_id,
                    notes=body.notes,
                )
                db.add(comp)
                db.commit()
                db.refresh(comp)
        else:
            comp = complete_task(
                db,
                run_id,
                body.task_template_id,
                body.employee_user_id,
                completion_type=body.completion_type,
                text_value=body.text_value,
                number_value=body.number_value,
                score_value=body.score_value,
                notes=body.notes,
                corrective_action_taken=body.corrective_action_taken,
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return CompletionOut(
        id=comp.id,
        task_template_id=comp.task_template_id,
        completion_type=comp.completion_type,
        photo_path=comp.photo_path,
        photo_url=comp.photo_url,
        text_value=comp.text_value,
        number_value=comp.number_value,
        employee_user_id=comp.employee_user_id,
        completed_at=comp.completed_at.isoformat() if comp.completed_at else "",
    )


@router.post("/runs/{run_id}/complete-photo", response_model=CompletionOut)
async def api_complete_photo(
    run_id: int,
    task_template_id: int = Form(...),
    employee_user_id: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
    photo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Push live-camera photo from iOS.
    Multipart form: task_template_id, employee_user_id?, notes?, photo (file).
    """
    suffix = Path(photo.filename or "capture.jpg").suffix or ".jpg"
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await photo.read()
            tmp.write(content)
            tmp_path = tmp.name
        comp = complete_task(
            db,
            run_id,
            task_template_id,
            employee_user_id,
            completion_type="photo",
            photo_source_path=tmp_path,
            notes=notes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {e}")
    return CompletionOut(
        id=comp.id,
        task_template_id=comp.task_template_id,
        completion_type=comp.completion_type,
        photo_path=comp.photo_path,
        photo_url=comp.photo_url,
        text_value=comp.text_value,
        number_value=comp.number_value,
        employee_user_id=comp.employee_user_id,
        completed_at=comp.completed_at.isoformat() if comp.completed_at else "",
    )


@router.post("/runs/{run_id}/finish", response_model=RunOut)
def api_finish_run(run_id: int, db: Session = Depends(get_db)):
    try:
        run = finish_run(db, run_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RunOut(
        id=run.id,
        template_id=run.template_id,
        template_name=run.template.name if run.template else None,
        location_id=run.location_id,
        status=run.status,
        started_at=run.started_at.isoformat() if run.started_at else "",
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
    )
