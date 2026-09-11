"""
DB connection setup.

On Railway, a Postgres DATABASE_URL is provided as an env var once a
Postgres service is attached to this project. Locally (or if no DB is
attached yet), we fall back to a SQLite file so the app still runs.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

RAW_DATABASE_URL = os.environ.get("DATABASE_URL", "")

if RAW_DATABASE_URL:
    # Railway/Heroku-style URLs sometimes start with postgres:// — SQLAlchemy
    # needs the postgresql:// scheme, and we use the psycopg (v3) driver.
    url = RAW_DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    DATABASE_URL = url
    connect_args = {}
else:
    DATABASE_URL = "sqlite:///./qa_tool.db"
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    # Imported here to avoid circular imports at module load time.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
