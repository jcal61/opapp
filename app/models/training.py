"""
Employee training courses and quizzes — Connecteam-style HR & Skills Hub.
A course is a short stack of lessons followed by a graded quiz; each attempt
(TrainingCompletion) keeps a full accountability trail of what was answered,
mirroring the pattern already used for checklist runs/completions.
"""

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Integer, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class TrainingCourse(Base):
    __tablename__ = "training_courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(80))  # onboarding, food_safety, service, compliance...
    role_codes: Mapped[str | None] = mapped_column(String(120))  # comma-separated; blank/None = all roles
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    passing_score: Mapped[int] = mapped_column(Integer, default=80)  # % required on the quiz to pass
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    lessons = relationship(
        "TrainingLesson", back_populates="course",
        cascade="all, delete-orphan", order_by="TrainingLesson.sort_order",
    )
    questions = relationship(
        "TrainingQuizQuestion", back_populates="course",
        cascade="all, delete-orphan", order_by="TrainingQuizQuestion.sort_order",
    )
    completions = relationship("TrainingCompletion", back_populates="course", cascade="all, delete-orphan")


class TrainingLesson(Base):
    """One screen of content within a course — text/markdown and/or a video link."""
    __tablename__ = "training_lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("training_courses.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    video_url: Mapped[str | None] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    course = relationship("TrainingCourse", back_populates="lessons")


class TrainingQuizQuestion(Base):
    """Multiple-choice question. choices stored as a '||'-delimited string."""
    __tablename__ = "training_quiz_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("training_courses.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    choices: Mapped[str] = mapped_column(Text, nullable=False)
    correct_index: Mapped[int] = mapped_column(Integer, default=0)  # 0-based index into choice_list()
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    course = relationship("TrainingCourse", back_populates="questions")

    def choice_list(self) -> list[str]:
        return [c for c in (self.choices or "").split("||") if c != ""]


class TrainingCompletion(Base):
    """One attempt at a course: lessons opened + quiz score, pass/fail."""
    __tablename__ = "training_completions"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("training_courses.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    score_percent: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)

    course = relationship("TrainingCourse", back_populates="completions")
    user = relationship("User")
    answers = relationship("TrainingQuizAnswer", back_populates="completion", cascade="all, delete-orphan")


class TrainingQuizAnswer(Base):
    """Accountability trail: exactly what the employee answered, right or wrong."""
    __tablename__ = "training_quiz_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    completion_id: Mapped[int] = mapped_column(ForeignKey("training_completions.id"), nullable=False)
    question_id: Mapped[int] = mapped_column(ForeignKey("training_quiz_questions.id"), nullable=False)
    selected_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)

    completion = relationship("TrainingCompletion", back_populates="answers")
    question = relationship("TrainingQuizQuestion")
