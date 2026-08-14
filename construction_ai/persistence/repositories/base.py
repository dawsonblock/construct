from __future__ import annotations

from typing import Any
from uuid import UUID

from construction_ai.persistence.db import Database, Scope, row_to_dict, rows_to_dicts


class Repository:
    """Base for every tenant-owned repository.

    There is intentionally no `get(id)` here. Scope is a required keyword on
    every method in every subclass, so an unscoped read is not something a caller
    can reach by omission — it does not exist to be reached.
    """

    table: str = ""
    id_column: str = ""
    #: False for tables with no project_id column (directory records like
    #: companies belong to the tenant, not to a project). Narrowing by project
    #: is then not merely unnecessary — it is a SQL error waiting to happen.
    project_scoped: bool = True

    def __init__(self, db: Database):
        self.db = db

    # -- scoping helpers ----------------------------------------------------

    def _tenant_clause(self, scope: Scope, *, alias: str = "") -> tuple[str, list[Any]]:
        """`organization_id` always; `project_id` too when both the table and the
        scope have one."""
        prefix = f"{alias}." if alias else ""
        sql = f"{prefix}organization_id = %s"
        params: list[Any] = [scope.organization_id]
        if self.project_scoped and scope.project_id is not None:
            sql += f" AND {prefix}project_id = %s"
            params.append(scope.project_id)
        return sql, params

    def _fetch_one(self, scope: Scope, sql: str, params: list[Any]) -> dict[str, Any] | None:
        with self.db.scoped(scope) as cur:
            cur.execute(sql, params)
            return row_to_dict(cur)

    def _fetch_all(self, scope: Scope, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        with self.db.scoped(scope) as cur:
            cur.execute(sql, params)
            return rows_to_dicts(cur)

    # -- generic reads ------------------------------------------------------

    def get_row(self, *, scope: Scope, record_id: UUID, columns: str = "*") -> dict[str, Any] | None:
        clause, params = self._tenant_clause(scope)
        return self._fetch_one(
            scope,
            f"SELECT {columns} FROM {self.table} WHERE {clause} AND {self.id_column} = %s",  # noqa: S608 - table/id are class constants
            [*params, record_id],
        )

    def exists(self, *, scope: Scope, record_id: UUID) -> bool:
        return self.get_row(scope=scope, record_id=record_id, columns=self.id_column) is not None

    def delete(self, *, scope: Scope, record_id: UUID) -> bool:
        clause, params = self._tenant_clause(scope)
        with self.db.scoped(scope) as cur:
            cur.execute(
                f"DELETE FROM {self.table} WHERE {clause} AND {self.id_column} = %s RETURNING {self.id_column}",  # noqa: S608
                [*params, record_id],
            )
            return cur.fetchone() is not None
