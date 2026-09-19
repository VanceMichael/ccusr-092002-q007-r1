
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


def connect(database_path: str | None = None) -> sqlite3.Connection:
    path = database_path or os.getenv("DATABASE_PATH", "data/app.sqlite3")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def session(database_path: str | None = None) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
