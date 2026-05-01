from __future__ import annotations

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.app_config import load_app_config

APP_CONFIG = load_app_config()
DATABASE_URL = APP_CONFIG.sqlalchemy_url

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    # Allow more concurrent users/jobs without blocking on DB pool exhaustion.
    pool_size=int(os.getenv("PB_DB_POOL_SIZE", "20")),
    max_overflow=int(os.getenv("PB_DB_MAX_OVERFLOW", "40")),
    pool_timeout=float(os.getenv("PB_DB_POOL_TIMEOUT", "30")),
    pool_recycle=int(os.getenv("PB_DB_POOL_RECYCLE", "1800")),
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

