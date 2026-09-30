import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    # Additive Studio migration: use SQLite's online backup, including WAL contents.
    database = make_url(database_url).database
    if database_url.startswith("sqlite") and database and database != ":memory:" and Path(database).is_file():
        with sqlite3.connect(database) as source:
            exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='studio_projects'").fetchone()
            event_columns = {row[1] for row in source.execute('PRAGMA table_info(studio_job_events)')}
            migrate_events = bool(event_columns) and 'context' not in event_columns
            if not exists or migrate_events:
                backup_dir = Path(database).resolve().parent / "backups"
                backup_dir.mkdir(exist_ok=True)
                name = Path(database).stem + "-before-studio-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".db"
                with sqlite3.connect(backup_dir / name) as target:
                    source.backup(target)
            if migrate_events:
                source.execute('ALTER TABLE studio_job_events ADD COLUMN context JSON')
                source.commit()
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_setup(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            # WAL lets the UI read while the worker writes; far fewer "database is locked" errors.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
