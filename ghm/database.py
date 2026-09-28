from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.engine import make_url
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    # Additive Studio migration: use SQLite's online backup, including WAL contents.
    database = make_url(database_url).database
    if database_url.startswith("sqlite") and database and database != ":memory:" and Path(database).is_file():
        with sqlite3.connect(database) as source:
            exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='studio_projects'").fetchone()
            if not exists:
                backup_dir = Path(database).resolve().parent / "backups"
                backup_dir.mkdir(exist_ok=True)
                name = Path(database).stem + "-before-studio-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".db"
                with sqlite3.connect(backup_dir / name) as target:
                    source.backup(target)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_setup(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
