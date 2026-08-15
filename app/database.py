from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .env import load_dotenv


load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./cc98_watch.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_existing_tables()


def _migrate_existing_tables() -> None:
    inspector = inspect(engine)
    if "notification_channels" not in inspector.get_table_names():
        return
    channel_columns = {column["name"] for column in inspector.get_columns("notification_channels")}
    if "last_sent_at" not in channel_columns:
        column_type = "DATETIME" if engine.dialect.name == "sqlite" else "TIMESTAMP WITH TIME ZONE"
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE notification_channels ADD COLUMN last_sent_at {column_type}"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
