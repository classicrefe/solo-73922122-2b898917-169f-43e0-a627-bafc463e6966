"""SQLAlchemy 引擎与会话管理。"""
from __future__ import annotations

import json
import os
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _ensure_data_dir() -> None:
    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


_ensure_data_dir()

engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# 旧版本 submissions 表缺少复核功能所需列；这里做幂等的轻量补列迁移，
# 避免已在运行的环境（如持久化 Docker 卷中的旧库）升级后缺列报错。
_ADDED_COLUMNS: dict[str, str] = {
    "kind": "ALTER TABLE submissions ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'SUBMISSION'",
    "payload_json": "ALTER TABLE submissions ADD COLUMN payload_json TEXT",
    "source_batch_id": "ALTER TABLE submissions ADD COLUMN source_batch_id VARCHAR(128)",
    "exempted_indexes_json": "ALTER TABLE submissions ADD COLUMN exempted_indexes_json TEXT",
}


def _migrate_submissions() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "submissions" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("submissions")}
    with engine.begin() as conn:
        for column, ddl in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(text(ddl))


def init_db() -> None:
    """建表（幂等）并对旧库执行补列迁移。"""
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)
    _migrate_submissions()

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def canonical_json(payload: object) -> str:
    """用于幂等比对的规范化 JSON 字符串。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
