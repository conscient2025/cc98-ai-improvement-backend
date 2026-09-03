from __future__ import annotations

import json
import os
import re
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
    if engine.dialect.name == "sqlite" and "subscriptions" in table_names:
        subscription_columns = {column["name"] for column in inspector.get_columns("subscriptions")}
        if "expression" not in subscription_columns:
            _migrate_sqlite_subscriptions()

    if "notification_channels" in table_names:
        channel_columns = {column["name"] for column in inspector.get_columns("notification_channels")}
        column_type = "DATETIME" if engine.dialect.name == "sqlite" else "TIMESTAMP WITH TIME ZONE"
        with engine.begin() as connection:
            if "last_sent_at" not in channel_columns:
                connection.execute(text(f"ALTER TABLE notification_channels ADD COLUMN last_sent_at {column_type}"))
            if "last_attempted_at" not in channel_columns:
                connection.execute(text(f"ALTER TABLE notification_channels ADD COLUMN last_attempted_at {column_type}"))
            if "last_dispatch_status" not in channel_columns:
                connection.execute(text("ALTER TABLE notification_channels ADD COLUMN last_dispatch_status VARCHAR(64)"))
            if "last_dispatch_error" not in channel_columns:
                connection.execute(text("ALTER TABLE notification_channels ADD COLUMN last_dispatch_error TEXT"))
            connection.execute(
                text(
                    "UPDATE notification_channels SET last_attempted_at = last_sent_at "
                    "WHERE last_attempted_at IS NULL AND last_sent_at IS NOT NULL"
                )
            )
            if "last_test_status" in channel_columns:
                connection.execute(
                    text(
                        "UPDATE notification_channels SET last_dispatch_status = last_test_status "
                        "WHERE last_dispatch_status IS NULL"
                    )
                )
            if "last_error" in channel_columns:
                connection.execute(
                    text(
                        "UPDATE notification_channels SET last_dispatch_error = last_error "
                        "WHERE last_dispatch_error IS NULL"
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
        if (
            "dispatch_pending" not in notification_columns
            or "subscription_id" in notification_columns
            or "is_read" in notification_columns
        ):
            _migrate_sqlite_notifications(notification_columns)

    _migrate_notification_list_rate_limit_state(table_names)
    _migrate_notification_preferences()


def _migrate_sqlite_subscriptions() -> None:
    """Replace legacy name/description subscriptions with one validated expression."""
    from .matcher import ExpressionSyntaxError, normalize_search_expression

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, user_id, name, description, status, created_at, updated_at "
                "FROM subscriptions ORDER BY id"
            )
        ).mappings().all()

    migrated: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    invalid_ids: list[int] = []
    duplicate_ids: list[int] = []
    for row in rows:
        candidate = str(row["description"] or "").strip() or str(row["name"] or "").strip()
        candidate = re.sub(r"[,，、;；\n]+", " ", candidate)
        try:
            expression = normalize_search_expression(candidate)
        except ExpressionSyntaxError:
            invalid_ids.append(int(row["id"]))
            continue
        key = (str(row["user_id"]), expression)
        if key in seen:
            duplicate_ids.append(int(row["id"]))
            continue
        seen.add(key)
        migrated.append(
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "expression": expression,
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    if invalid_ids or duplicate_ids:
        details: list[str] = []
        if invalid_ids:
            details.append(f"invalid ids={invalid_ids}")
        if duplicate_ids:
            details.append(f"duplicate ids={duplicate_ids}")
        raise RuntimeError("subscription migration requires manual correction: " + "; ".join(details))

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS subscriptions_next"))
        connection.execute(
            text(
                """
                CREATE TABLE subscriptions_next (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    expression VARCHAR(255) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT uq_subscription_user_expression UNIQUE (user_id, expression)
                )
                """
            )
        )
        if migrated:
            connection.execute(
                text(
                    "INSERT INTO subscriptions_next "
                    "(id, user_id, expression, status, created_at, updated_at) "
                    "VALUES (:id, :user_id, :expression, :status, :created_at, :updated_at)"
                ),
                migrated,
            )
        connection.execute(text("DROP TABLE subscriptions"))
        connection.execute(text("ALTER TABLE subscriptions_next RENAME TO subscriptions"))
        connection.execute(text("CREATE INDEX ix_subscriptions_id ON subscriptions (id)"))
        connection.execute(text("CREATE INDEX ix_subscriptions_user_id ON subscriptions (user_id)"))


def _migrate_sqlite_notifications(notification_columns: set[str]) -> None:
    """Rebuild the legacy SQLite notification table and safely retire old deliveries."""
    legacy_delivery = "dispatch_pending" not in notification_columns or "subscription_id" in notification_columns
    dispatch_pending = "0" if legacy_delivery else "n.dispatch_pending"
    dispatch_processed_at = "CURRENT_TIMESTAMP" if legacy_delivery else "n.dispatch_processed_at"
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS notifications_next"))
        connection.execute(
            text(
                """
                CREATE TABLE notifications_next (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    topic_id VARCHAR(128) NOT NULL,
                    topic_title VARCHAR(500) NOT NULL,
                    topic_url TEXT NOT NULL,
                    matched_reason TEXT,
                    dispatch_pending BOOLEAN NOT NULL DEFAULT 0,
                    dispatch_processed_at DATETIME,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_notification_user_topic UNIQUE (user_id, topic_id)
                )
                """
            )
        )
        connection.execute(
            text(
                f"""
                INSERT INTO notifications_next (
                    id, user_id, topic_id, topic_title, topic_url, matched_reason,
                    dispatch_pending, dispatch_processed_at, created_at
                )
                SELECT n.id, n.user_id, n.topic_id, n.topic_title, n.topic_url,
                       n.matched_reason, {dispatch_pending}, {dispatch_processed_at}, n.created_at
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
        connection.execute(text("ALTER TABLE notifications_next RENAME TO notifications"))
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


def _migrate_notification_list_rate_limit_state(table_names: set[str]) -> None:
    old_table = "notification_read_states"
    if old_table not in table_names:
        return
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO notification_list_rate_limit_states (user_id, last_success_at) "
                    "SELECT user_id, last_success_at FROM notification_read_states"
                )
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO notification_list_rate_limit_states (user_id, last_success_at) "
                    "SELECT old.user_id, old.last_success_at FROM notification_read_states AS old "
                    "WHERE NOT EXISTS (SELECT 1 FROM notification_list_rate_limit_states AS new "
                    "WHERE new.user_id = old.user_id)"
                )
            )
        connection.execute(text(f"DROP TABLE {old_table}"))


def _migrate_notification_preferences() -> None:
    """Migrate user-level dispatch timing and seed preferences from channels."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "notification_preferences" not in table_names or "notification_channels" not in table_names:
        return
    preference_columns = {column["name"] for column in inspector.get_columns("notification_preferences")}
    needs_dispatch_backfill = "last_dispatch_started_at" not in preference_columns
    column_type = "DATETIME" if engine.dialect.name == "sqlite" else "TIMESTAMP WITH TIME ZONE"
    with engine.begin() as connection:
        if needs_dispatch_backfill:
            connection.execute(
                text(f"ALTER TABLE notification_preferences ADD COLUMN last_dispatch_started_at {column_type}")
            )
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
        if needs_dispatch_backfill:
            connection.execute(
                text(
                    "UPDATE notification_preferences SET last_dispatch_started_at = ("
                    "SELECT MAX(notification_channels.last_attempted_at) FROM notification_channels "
                    "WHERE notification_channels.user_id = notification_preferences.user_id"
                    ") WHERE last_dispatch_started_at IS NULL"
                )
            )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
