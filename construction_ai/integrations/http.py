from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class ERPNextHTTPTransport:
    """Read transport for ERPNext's REST API.

    Satisfies the `ReadTransport` protocol used by `ERPNextEvidenceResolver`.
    Identical against the in-repo stub and a live Frappe instance; only
    `base_url` and credentials differ.
    """

    base_url: str
    api_key: str | None = None
    api_secret: str | None = None
    timeout: float = 30.0
    client: httpx.Client | None = field(default=None, repr=False)

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout)

    def _headers(self) -> dict[str, str]:
        if self.api_key and self.api_secret:
            return {"Authorization": f"token {self.api_key}:{self.api_secret}"}
        return {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self.client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
        r.raise_for_status()
        return r.json()


def create_erpnext_resolver(organization_id: str):
    """Build a resolver from the environment, or None when ERPNext is unconfigured.

    Returning None is deliberate: with no ERP the invoice verifier finds no PO or
    quote and holds the transaction. Absent integration must not read as a pass.
    """
    base_url = os.getenv("ERP_NEXT_URL", "").strip()
    if not base_url:
        return None
    from construction_ai.integrations.erpnext import ERPNextEvidenceResolver

    transport = ERPNextHTTPTransport(
        base_url,
        api_key=os.getenv("ERP_NEXT_API_KEY") or None,
        api_secret=os.getenv("ERP_NEXT_API_SECRET") or None,
    )
    return ERPNextEvidenceResolver(transport, organization_id)
