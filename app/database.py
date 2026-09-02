from __future__ import annotations

import json
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
    table_names = set(inspector.get_table_names())
    if "notification_channels" in table_names:
        channel_columns = {column["name"] for column in inspector.get_columns("notification_channels")}
        column_type = "DATETIME" if engine.dialect.name == "sqlite" else "TIMESTAMP WITH TIME ZONE"
        with engine.begin() as connection:
            if "last_sent_at" not in channel_columns:
                connection.execute(text(f"ALTER TABLE notification_channels ADD COLUMN last_sent_at {column_type}"))
            if "last_attempted_at" not in channel_columns:
                connection.execute(text(f"ALTER TABLE notification_channels ADD COLUMN last_attempted_at {column_type}"))
            connection.execute(
                text(
                    "UPDATE notification_channels SET last_attempted_at = last_sent_at "
                    "WHERE last_attempted_at IS NULL AND last_sent_at IS NOT NULL"
                )
            )
            # Old code normally kept one row, but make the new database guarantee safe
            # even if manual edits or concurrent saves created duplicates.
            connection.execute(
                text(
                    "DELETE FROM notification_channels WHERE id NOT IN "
                    "(SELECT MIN(id) FROM notification_channels GROUP BY user_id, provider)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_channel_user_provider "
                    "ON notification_channels (user_id, provider)"
                )
            )

    if engine.dialect.name == "sqlite" and "notifications" in table_names:
        notification_columns = {column["name"] for column in inspector.get_columns("notifications")}
        if "dispatch_pending" not in notification_columns or "subscription_id" in notification_columns:
            _migrate_sqlite_notifications()

    _migrate_notification_preferences()


def _migrate_sqlite_notifications() -> None:
    """Rebuild the legacy SQLite notification table and safely retire old deliveries."""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE notifications_v2 (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    topic_id VARCHAR(128) NOT NULL,
                    topic_title VARCHAR(500) NOT NULL,
                    topic_url TEXT NOT NULL,
                    matched_reason TEXT,
                    dispatch_pending BOOLEAN NOT NULL DEFAULT 0,
                    dispatch_processed_at DATETIME,
                    is_read BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_notification_user_topic UNIQUE (user_id, topic_id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notifications_v2 (
                    id, user_id, topic_id, topic_title, topic_url, matched_reason,
                    dispatch_pending, dispatch_processed_at, is_read, created_at
                )
                SELECT n.id, n.user_id, n.topic_id, n.topic_title, n.topic_url,
                       n.matched_reason, 0, CURRENT_TIMESTAMP, n.is_read, n.created_at
                FROM notifications AS n
                INNER JOIN (
                    SELECT user_id, topic_id, MIN(id) AS kept_id
                    FROM notifications
                    GROUP BY user_id, topic_id
                ) AS kept ON kept.kept_id = n.id
                """
            )
        )
        connection.execute(text("DROP TABLE notifications"))
        connection.execute(text("ALTER TABLE notifications_v2 RENAME TO notifications"))
        connection.execute(text("CREATE INDEX ix_notifications_id ON notifications (id)"))
        connection.execute(text("CREATE INDEX ix_notifications_user_id ON notifications (user_id)"))
        connection.execute(text("CREATE INDEX ix_notifications_topic_id ON notifications (topic_id)"))
        connection.execute(text("CREATE INDEX ix_notifications_user_id_id ON notifications (user_id, id)"))
        connection.execute(
            text(
                "CREATE INDEX ix_notifications_user_dispatch_pending_id "
                "ON notifications (user_id, dispatch_pending, id)"
            )
        )


def _migrate_notification_preferences() -> None:
    """Seed the user-level interval from existing channel configs once."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "notification_preferences" not in table_names or "notification_channels" not in table_names:
        return
    with engine.begin() as connection:
        existing_users = {
            str(row[0])
            for row in connection.execute(text("SELECT user_id FROM notification_preferences"))
        }
        rows = connection.execute(
            text("SELECT user_id, config_json FROM notification_channels ORDER BY id ASC")
        )
        now_default = max(1, int(os.getenv("SCAN_INTERVAL_MINUTES", "10")))
        for user_id, config_json in rows:
            user_id = str(user_id)
            if user_id in existing_users:
                continue
            try:
                config = json.loads(config_json or "{}")
                requested = int(config.get("notify_interval_minutes") or now_default)
            except (TypeError, ValueError, json.JSONDecodeError):
                requested = now_default
            interval = max(now_default, requested)
            connection.execute(
                text(
                    "INSERT INTO notification_preferences "
                    "(user_id, notify_interval_minutes, created_at, updated_at) "
                    "VALUES (:user_id, :interval, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"user_id": user_id, "interval": interval},
            )
            existing_users.add(user_id)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
