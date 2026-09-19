"""SQLite 连接与迁移执行。数据文件位置由 DATABASE_PATH 决定。"""

import os
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
DEFAULT_PATH = Path("data/app.sqlite3")


def database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", str(DEFAULT_PATH)))


def connect(path=None) -> sqlite3.Connection:
    target = Path(path) if path else database_path()
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(path=None) -> Path:
    """按文件名顺序应用 migrations/ 下尚未执行的迁移，返回数据文件路径。"""
    target = Path(path) if path else database_path()
    with connect(target) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {
            row["version"] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.stem in applied:
                continue
            connection.executescript(sql_file.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (sql_file.stem,),
            )
    return target
