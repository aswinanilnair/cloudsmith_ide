"""PostgreSQL helpers on top of a small connection pool (autocommit, rows as dicts)."""
import os
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cloudsmith:cloudsmith@localhost:5432/cloudsmith")

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=int(os.getenv("DB_POOL_SIZE", "5")),
    kwargs={"autocommit": True, "row_factory": dict_row},
    open=False,
)


# Opens the connection pool and creates or upgrades the tables.
def init() -> None:
    pool.open(wait=True, timeout=60)
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with pool.connection() as conn:
        # Several Cloud Run instances may start at once; run the migration one at a time.
        conn.execute("SELECT pg_advisory_lock(5253)")
        try:
            conn.execute(sql)
        finally:
            conn.execute("SELECT pg_advisory_unlock(5253)")


# Runs a query and returns the first row.
def fetchone(sql: str, params=None):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()


# Runs a query and returns all rows.
def fetchall(sql: str, params=None) -> list:
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


# Runs a statement and returns the number of rows changed.
def execute(sql: str, params=None) -> int:
    with pool.connection() as conn:
        return conn.execute(sql, params).rowcount


# A connection whose statements commit together, or not at all.
@contextmanager
def transaction():
    with pool.connection() as conn:
        with conn.transaction():
            yield conn
