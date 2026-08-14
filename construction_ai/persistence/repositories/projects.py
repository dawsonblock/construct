from __future__ import annotations

from typing import Any
from uuid import UUID

from construction_ai.domain.models import Company, Project
from construction_ai.persistence.db import Scope
from construction_ai.persistence.repositories.base import Repository

PROJECT_COLUMNS = "organization_id, project_id, reference, name, address, status, client_company_id, project_manager_person_id, identifiers, annotations"


def _to_project(row: dict[str, Any], company_ids: list[str] | None = None) -> Project:
    return Project(
        project_id=str(row["project_id"]),
        organization_id=str(row["organization_id"]),
        name=row["name"],
        address=row["address"],
        status=row["status"],
        client_id=str(row["client_company_id"]) if row.get("client_company_id") else None,
        project_manager_id=str(row["project_manager_person_id"]) if row.get("project_manager_person_id") else None,
        company_ids=company_ids or [],
        identifiers=row.get("identifiers") or {},
        reference=row["reference"],
    )


class ProjectRepository(Repository):
    table = "projects"
    id_column = "project_id"

    def create(
        self,
        *,
        scope: Scope,
        reference: str,
        name: str,
        address: str | None = None,
        status: str = "active",
        identifiers: dict[str, list[str]] | None = None,
        created_by: str = "system",
    ) -> Project:
        from psycopg.types.json import Jsonb

        with self.db.scoped(scope.organization_only) as cur:
            cur.execute(
                f"""INSERT INTO projects(organization_id, reference, name, address, status, identifiers, created_by)
                    VALUES(%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (organization_id, reference) DO UPDATE
                      SET name = EXCLUDED.name, address = EXCLUDED.address,
                          status = EXCLUDED.status, identifiers = EXCLUDED.identifiers
                    RETURNING {PROJECT_COLUMNS}""",
                (scope.organization_id, reference, name, address, status, Jsonb(identifiers or {}), created_by),
            )
            columns = [c.name for c in cur.description]
            row = dict(zip(columns, cur.fetchone(), strict=True))
        return _to_project(row)

    def get(self, *, scope: Scope, project_id: UUID) -> Project | None:
        row = self.get_row(scope=scope, record_id=project_id, columns=PROJECT_COLUMNS)
        return _to_project(row, self._company_ids(scope, project_id)) if row else None

    def get_by_reference(self, *, scope: Scope, reference: str) -> Project | None:
        clause, params = self._tenant_clause(scope)
        row = self._fetch_one(
            scope,
            f"SELECT {PROJECT_COLUMNS} FROM projects WHERE {clause} AND reference = %s",
            [*params, reference],
        )
        if row is None:
            return None
        return _to_project(row, self._company_ids(scope, row["project_id"]))

    def list(self, *, scope: Scope, status: str | None = None) -> list[Project]:
        clause, params = self._tenant_clause(scope)
        sql = f"SELECT {PROJECT_COLUMNS} FROM projects WHERE {clause}"
        if status:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY reference"
        rows = self._fetch_all(scope, sql, params)
        memberships = self._company_ids_by_project(scope)
        return [_to_project(row, memberships.get(row["project_id"], [])) for row in rows]

    # -- project ↔ company membership --------------------------------------

    def add_company(self, *, scope: Scope, project_id: UUID, company_id: UUID, role: str = "participant") -> None:
        with self.db.scoped(scope.organization_only) as cur:
            cur.execute(
                """INSERT INTO project_memberships(organization_id, project_id, company_id, role)
                   VALUES(%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (scope.organization_id, project_id, company_id, role),
            )

    def companies_for_project(self, *, scope: Scope) -> list[str]:
        """Company ids on this project, in a stable order. Reconstruction reads it."""
        project_id = scope.require_project()
        rows = self._fetch_all(
            scope,
            """SELECT DISTINCT company_id FROM project_memberships
               WHERE organization_id = %s AND project_id = %s AND company_id IS NOT NULL
               ORDER BY company_id""",
            [scope.organization_id, project_id],
        )
        return [str(r["company_id"]) for r in rows]

    def _company_ids(self, scope: Scope, project_id: UUID) -> list[str]:
        rows = self._fetch_all(
            scope,
            "SELECT company_id FROM project_memberships WHERE organization_id = %s AND project_id = %s AND company_id IS NOT NULL",
            [scope.organization_id, project_id],
        )
        return [str(r["company_id"]) for r in rows]

    def _company_ids_by_project(self, scope: Scope) -> dict[UUID, list[str]]:
        rows = self._fetch_all(
            scope,
            "SELECT project_id, company_id FROM project_memberships WHERE organization_id = %s AND company_id IS NOT NULL",
            [scope.organization_id],
        )
        grouped: dict[UUID, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["project_id"], []).append(str(row["company_id"]))
        return grouped


COMPANY_COLUMNS = "organization_id, company_id, reference, name, company_type, aliases, erp_supplier_id, tax_id"


def _to_company(row: dict[str, Any]) -> Company:
    return Company(
        company_id=str(row["company_id"]),
        organization_id=str(row["organization_id"]),
        name=row["name"],
        aliases=list(row.get("aliases") or []),
        type=row["company_type"],
        erp_supplier_id=row.get("erp_supplier_id"),
        reference=row["reference"],
        tax_id=row.get("tax_id"),
    )


class CompanyRepository(Repository):
    table = "companies"
    id_column = "company_id"
    project_scoped = False  # a vendor belongs to the tenant, not to one project

    def create(
        self,
        *,
        scope: Scope,
        reference: str,
        name: str,
        company_type: str = "vendor",
        aliases: list[str] | None = None,
        erp_supplier_id: str | None = None,
        tax_id: str | None = None,
        created_by: str = "system",
    ) -> Company:
        with self.db.scoped(scope.organization_only) as cur:
            cur.execute(
                f"""INSERT INTO companies(organization_id, reference, name, company_type, aliases, erp_supplier_id, tax_id, created_by)
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (organization_id, reference) DO UPDATE
                      SET name = EXCLUDED.name, company_type = EXCLUDED.company_type,
                          aliases = EXCLUDED.aliases, erp_supplier_id = EXCLUDED.erp_supplier_id,
                          tax_id = EXCLUDED.tax_id
                    RETURNING {COMPANY_COLUMNS}""",
                (scope.organization_id, reference, name, company_type, aliases or [], erp_supplier_id, tax_id, created_by),
            )
            columns = [c.name for c in cur.description]
            return _to_company(dict(zip(columns, cur.fetchone(), strict=True)))

    def get(self, *, scope: Scope, company_id: UUID) -> Company | None:
        row = self.get_row(scope=scope, record_id=company_id, columns=COMPANY_COLUMNS)
        return _to_company(row) if row else None

    def list(self, *, scope: Scope) -> list[Company]:
        rows = self._fetch_all(
            scope,
            f"SELECT {COMPANY_COLUMNS} FROM companies WHERE organization_id = %s ORDER BY reference",
            [scope.organization_id],
        )
        return [_to_company(row) for row in rows]

    def find_by_erp_supplier(self, *, scope: Scope, erp_supplier_id: str) -> Company | None:
        row = self._fetch_one(
            scope,
            f"SELECT {COMPANY_COLUMNS} FROM companies WHERE organization_id = %s AND erp_supplier_id = %s",
            [scope.organization_id, erp_supplier_id],
        )
        return _to_company(row) if row else None
