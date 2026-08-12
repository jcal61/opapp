"""
Training courses and quizzes — lessons followed by a graded multiple-choice
quiz, with a full per-employee attempt/answer trail so managers can see who
passed what (and what they actually answered), not just a checkbox.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session, joinedload

from app.models import (
    TrainingCourse, TrainingLesson, TrainingQuizQuestion,
    TrainingCompletion, TrainingQuizAnswer, User,
)


# ---------- Courses / lessons / questions (admin) ----------

def create_course(
    db: Session,
    title: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
    role_codes: Optional[str] = None,
    location_id: Optional[int] = None,
    passing_score: int = 80,
) -> TrainingCourse:
    course = TrainingCourse(
        title=title.strip(),
        description=(description or "").strip() or None,
        category=category,
        role_codes=(role_codes or "").strip() or None,
        location_id=location_id,
        passing_score=passing_score or 80,
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def set_course_active(db: Session, course_id: int, active: bool) -> TrainingCourse:
    course = db.get(TrainingCourse, course_id)
    if not course:
        raise ValueError("Course not found")
    course.is_active = active
    db.commit()
    return course


def list_courses(db: Session, active_only: bool = True, location_id: Optional[int] = None) -> List[TrainingCourse]:
    q = db.query(TrainingCourse).order_by(TrainingCourse.category, TrainingCourse.title)
    if active_only:
        q = q.filter(TrainingCourse.is_active == True)
    if location_id is not None:
        q = q.filter((TrainingCourse.location_id == location_id) | (TrainingCourse.location_id.is_(None)))
    return q.all()


def visible_courses_for_user(db: Session, user: User, location_id: Optional[int] = None) -> List[TrainingCourse]:
    """Active courses whose role_codes (if any) include this user's role."""
    courses = list_courses(db, active_only=True, location_id=location_id)
    out = []
    for c in courses:
        if c.role_codes:
            allowed = {r.strip() for r in c.role_codes.split(",") if r.strip()}
            if not user.role or user.role.code not in allowed:
                continue
        out.append(c)
    return out


def add_lesson(
    db: Session,
    course_id: int,
    title: str,
    content: Optional[str] = None,
    video_url: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> TrainingLesson:
    course = db.get(TrainingCourse, course_id)
    if not course:
        raise ValueError("Course not found")
    if not title or not title.strip():
        raise ValueError("Lesson title is required.")
    if sort_order is None:
        sort_order = len(course.lessons)
    lesson = TrainingLesson(
        course_id=course_id, title=title.strip(),
        content=(content or "").strip() or None,
        video_url=(video_url or "").strip() or None,
        sort_order=sort_order,
    )
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return lesson


def delete_lesson(db: Session, lesson_id: int) -> None:
    lesson = db.get(TrainingLesson, lesson_id)
    if lesson:
        db.delete(lesson)
        db.commit()


def add_quiz_question(
    db: Session,
    course_id: int,
    question: str,
    choices: List[str],
    correct_choice: str,
    sort_order: Optional[int] = None,
) -> TrainingQuizQuestion:
    """
    choices: the raw option strings (blanks allowed — trailing optional
    choices are simply dropped). correct_choice: the *text* of the right
    answer, matched after blanks are filtered out, so the caller never has
    to reason about index shifts.
    """
    course = db.get(TrainingCourse, course_id)
    if not course:
        raise ValueError("Course not found")
    if not question or not question.strip():
        raise ValueError("Question text is required.")

    cleaned = [c.strip() for c in choices if c and c.strip()]
    if len(cleaned) < 2:
        raise ValueError("Provide at least two answer choices.")

    correct_choice = (correct_choice or "").strip()
    if correct_choice not in cleaned:
        raise ValueError("Correct answer must match one of the provided (non-blank) choices.")

    if sort_order is None:
        sort_order = len(course.questions)

    q = TrainingQuizQuestion(
        course_id=course_id,
        question=question.strip(),
        choices="||".join(cleaned),
        correct_index=cleaned.index(correct_choice),
        sort_order=sort_order,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def delete_question(db: Session, question_id: int) -> None:
    q = db.get(TrainingQuizQuestion, question_id)
    if q:
        db.delete(q)
        db.commit()


# ---------- Taking a course ----------

def start_course(db: Session, course_id: int, user_id: int) -> TrainingCompletion:
    course = db.get(TrainingCourse, course_id)
    if not course:
        raise ValueError("Course not found")

    prior = (
        db.query(TrainingCompletion)
        .filter(TrainingCompletion.course_id == course_id, TrainingCompletion.user_id == user_id)
        .order_by(TrainingCompletion.attempt_number.desc())
        .first()
    )
    if prior and prior.completed_at is None:
        return prior  # resume the in-progress attempt rather than starting a duplicate

    attempt_number = (prior.attempt_number + 1) if prior else 1
    completion = TrainingCompletion(course_id=course_id, user_id=user_id, attempt_number=attempt_number)
    db.add(completion)
    db.commit()
    db.refresh(completion)
    return completion


def submit_quiz(db: Session, completion_id: int, answers: Dict[int, int]) -> TrainingCompletion:
    """answers = {question_id: selected_index}. Grades, stores the answer
    trail, and marks the attempt passed/failed against the course's
    passing_score."""
    completion = db.get(TrainingCompletion, completion_id)
    if not completion:
        raise ValueError("Attempt not found")
    if completion.completed_at is not None:
        raise ValueError("This attempt has already been submitted.")

    course = completion.course
    questions = course.questions
    if not questions:
        raise ValueError("This course has no quiz questions yet.")

    correct_count = 0
    for q in questions:
        if q.id not in answers or answers[q.id] is None:
            raise ValueError("Answer every question before submitting.")
        selected = int(answers[q.id])
        is_correct = selected == q.correct_index
        if is_correct:
            correct_count += 1
        db.add(TrainingQuizAnswer(
            completion_id=completion.id, question_id=q.id,
            selected_index=selected, is_correct=is_correct,
        ))

    score = round(correct_count / len(questions) * 100, 1)
    completion.score_percent = score
    completion.passed = score >= (course.passing_score or 80)
    completion.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(completion)
    return completion


def list_completions_for_user(db: Session, user_id: int, course_id: Optional[int] = None) -> List[TrainingCompletion]:
    q = (
        db.query(TrainingCompletion)
        .options(joinedload(TrainingCompletion.course))
        .filter(TrainingCompletion.user_id == user_id, TrainingCompletion.completed_at.isnot(None))
    )
    if course_id is not None:
        q = q.filter(TrainingCompletion.course_id == course_id)
    return q.order_by(TrainingCompletion.completed_at.desc()).all()


def best_completion(db: Session, course_id: int, user_id: int) -> Optional[TrainingCompletion]:
    completions = list_completions_for_user(db, user_id, course_id=course_id)
    if not completions:
        return None
    return max(completions, key=lambda c: c.score_percent or 0)


# ---------- Manager reporting ----------

def training_report(db: Session, location_id: Optional[int] = None) -> Dict[str, Any]:
    """Completion matrix across active users x active (role-eligible) courses."""
    courses = list_courses(db, active_only=True, location_id=location_id)
    users = (
        db.query(User)
        .options(joinedload(User.role))
        .filter(User.is_active == True)
        .order_by(User.name)
        .all()
    )

    rows = []
    for u in users:
        for c in courses:
            if c.role_codes:
                allowed = {r.strip() for r in c.role_codes.split(",") if r.strip()}
                if not u.role or u.role.code not in allowed:
                    continue
            latest = best_completion(db, c.id, u.id)
            status = "not started"
            if latest:
                status = "passed" if latest.passed else "failed"
            rows.append({
                "user_id": u.id,
                "user_name": u.name,
                "course_id": c.id,
                "course_title": c.title,
                "status": status,
                "score_percent": latest.score_percent if latest else None,
                "completed_at": latest.completed_at if latest else None,
            })

    total = len(rows)
    passed = sum(1 for r in rows if r["status"] == "passed")
    return {
        "rows": rows,
        "total_assignments": total,
        "passed": passed,
        "completion_rate": round(passed / total * 100, 1) if total else 0.0,
    }
