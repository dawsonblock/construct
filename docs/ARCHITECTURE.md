# Architecture — v0.5.0-rc5.dev0

Construction AI Ops separates authoritative transactional systems from an evidence-first AI control plane.

- **Frappe + ERPNext:** authoritative customers, suppliers, projects, POs, quotations, invoices, payments and accounting.
- **Frappe HR:** authoritative employee/payroll transaction system. Payroll execution remains outside the v0.3 AI gate.
- **LangGraph:** retained upstream reference/dependency for durable state and human-in-the-loop orchestration.
- **Construction AI Ops:** immutable ingestion, document extraction, evidence, identity/project resolution, verification, policy, approval packets, audit and integration adapters.
- **n8n:** external integration option only; not embedded in the runtime.

## v0.3 invoice path

```text
Gmail / Microsoft Graph
        ↓
message fetch + attachment fetch
        ↓
immutable communication + content-addressed document
        ↓
text/table extraction + document classification
        ↓
conservative invoice extraction
        ↓
exact-first vendor/project resolution
        ↓
ERPNext PO + supplier quotation evidence
        ↓
deterministic verification
        ↓
evidence-backed approval packet
        ↓
human approve / hold / reject
        ↓
ERPNext submit policy gate
        ↓
tamper-evident audit
```

## Authority boundary

No model confidence can authorize a financial transaction. `SUBMIT_ERP_TRANSACTION` requires explicit approval status plus approver identity. Unknown/ambiguous project evidence is capped below the automatic classification threshold unless a hard identifier gate is satisfied.

## Persistence

`DATABASE_URL=postgresql://...` selects `PostgresStore` for production. SQLite is retained only for local development and deterministic unit tests.
