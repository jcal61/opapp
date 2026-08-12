from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False}  # needed for SQLite
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added to a table that already existed in earlier versions of the
# app. `Base.metadata.create_all()` only creates *missing* tables — it never
# ALTERs an existing one to add a new column — so on any database created
# before a given column existed, that column has to be patched in by hand.
# Safe to run on every startup: a database that's missing the column gets it
# added once; a database that already has it (including a brand-new one,
# where create_all() included it from the start) is left untouched.
_ADDED_COLUMNS = {
    "users": [("hourly_rate", "FLOAT DEFAULT 0.0")],
    "checklist_task_templates": [("is_active", "BOOLEAN DEFAULT 1")],
}


def ensure_schema() -> None:
    """Create any missing tables, then patch in any columns that were added
    to a pre-existing table after that table was first created."""
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # brand-new database — create_all() already has it right
            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_def in columns:
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))
