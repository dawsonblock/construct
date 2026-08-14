"""Database access and the scope that every tenant-owned query must carry.

Two ways to reach the database, and only two:

- `Database.scoped(scope)` — arms RLS by setting `app.organization_id` for the
  transaction and hands back a cursor. Everything tenant-owned goes through here.
- `Database.unscoped_auth()` — reserved for `organization_api_keys`, the one
  table that must be readable before a scope exists. Named to be conspicuous in
  review; using it for anything else is a bug.

`Database.transaction()` shares one physical connection and one transaction
across every `scoped()` call inside the block, so a multi-step pipeline is
atomic: either every write commits or none does.

See docs/TENANCY.md.
"""
from __future__ import annotations

import contextvars
import os
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class ScopeError(RuntimeError):
    """A query was attempted without the scope it requires."""


@dataclass(frozen=True)
class Scope:
    """Who is asking, and about which project.

    `organization_id` is mandatory and always constrains the query.
    `project_id` is optional because an unresolved project is a real state — an
    invoice that has not been filed yet belongs to the tenant but to no project.
    When a scope does carry a project, every read narrows to it; operations that
    are inherently project-scoped call `require_project()` and fail loudly
    rather than silently widening.
    """

    organization_id: UUID
    project_id: UUID | None = None

    def __post_init__(self):
        if not isinstance(self.organization_id, UUID):
            raise ScopeError(f"organization_id must be a UUID, got {type(self.organization_id).__name__}")
        if self.project_id is not None and not isinstance(self.project_id, UUID):
            raise ScopeError(f"project_id must be a UUID or None, got {type(self.project_id).__name__}")

    def require_project(self) -> UUID:
        if self.project_id is None:
            raise ScopeError("this operation is project-scoped; Scope.project_id is required")
        return self.project_id

    def for_project(self, project_id: UUID | None) -> Scope:
        return Scope(self.organization_id, project_id)

    @property
    def organization_only(self) -> Scope:
        return Scope(self.organization_id)


class _ConnectionPool:
    """Minimal thread-safe connection pool."""

    def __init__(self, dsn: str, max_size: int = 10):
        self._dsn = dsn
        self._max_size = max_size
        self._idle: queue.Queue = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()

    def getconn(self):
        import psycopg

        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._max_size:
                self._created += 1
                return psycopg.connect(self._dsn)
        return self._idle.get()

    def putconn(self, conn):
        self._idle.put(conn)

    def closeall(self):
        while True:
            try:
                conn = self._idle.get_nowait()
                conn.close()
            except queue.Empty:
                break
        with self._lock:
            self._created = 0


_current_tx_conn = contextvars.ContextVar("_current_tx_conn", default=None)


class Database:
    def __init__(self, dsn: str):
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("the scoped repositories require PostgreSQL; see docs/TENANCY.md")
        self.dsn = dsn
        self._pool = _ConnectionPool(dsn)

    @classmethod
    def from_env(cls) -> Database:
        # APP_DATABASE_URL connects as the non-superuser app role, which is what
        # makes RLS enforceable. DATABASE_URL is the owner and is for migrations.
        dsn = (os.getenv("APP_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
        if not dsn:
            raise RuntimeError("APP_DATABASE_URL or DATABASE_URL must be set")
        return cls(dsn)

    @contextmanager
    def _connection(self):
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    @contextmanager
    def transaction(self):
        """Share one connection and one transaction across all scoped() calls within.

        Without this, each repository call commits independently, so a failure
        mid-pipeline leaves partial state. With it, either every write commits
        or none does.
        """
        conn = self._pool.getconn()
        token = _current_tx_conn.set(conn)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            _current_tx_conn.reset(token)
            self._pool.putconn(conn)

    @contextmanager
    def scoped(self, scope: Scope):
        """Cursor inside a transaction with the RLS tenant GUC set."""
        if not isinstance(scope, Scope):
            raise ScopeError(f"a Scope is required, got {type(scope).__name__}")
        conn = _current_tx_conn.get()
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
                yield cur
        else:
            with self._connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT set_config('app.organization_id', %s, true)", (str(scope.organization_id),))
                yield cur

    @contextmanager
    def unscoped_auth(self):
        """Only for resolving a bearer token to an organization. Nothing else."""
        with self._connection() as conn, conn.cursor() as cur:
            yield cur

    def healthy(self) -> bool:
        try:
            with self._connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)
        except Exception:
            return False


def rows_to_dicts(cursor) -> list[dict[str, Any]]:
    columns = [c.name for c in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def row_to_dict(cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [c.name for c in cursor.description]
    return dict(zip(columns, row, strict=True))
