from dataclasses import dataclass
from typing import Any, Protocol

class Transport(Protocol):
    def post(self, path: str, json: dict[str, Any]) -> Any: ...

@dataclass
class FrappeHRAdapter:
    transport: Transport

    def prepare_payroll_inputs(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "status": "prepared", "requires_human_approval": True}

    def run_payroll(self, payload: dict[str, Any], *, approved: bool = False):
        if not approved:
            raise PermissionError("human payroll approval required")
        return self.transport.post("/api/method/hrms.payroll.doctype.payroll_entry.payroll_entry.submit_salary_slips_for_employees", json=payload)
