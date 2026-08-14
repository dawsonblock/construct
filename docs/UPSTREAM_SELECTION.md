# Upstream selection

Included source snapshots:

- `frappe-develop.zip`: required runtime framework for ERPNext/HRMS. MIT upstream license.
- `erpnext-develop.zip`: accounting, purchasing, projects, invoices, payments. GPLv3 upstream license.
- `hrms-develop.zip`: HR/payroll/employee records. GPLv3 upstream license.
- `langgraph-main.zip`: durable execution/HITL reference and optional dependency. MIT upstream license.

Not embedded:

- `n8n-master.zip`: useful connector/workflow platform, but kept external to avoid coupling its Sustainable Use/Enterprise licensing and large monorepo to the core product.
- `langchain-mongodb-main.zip`: unnecessary for PostgreSQL-first V1.
- `openwork-main.zip`, `studio-develop.zip`, `openwiki-main.zip`, `toolbox-develop.zip`: not required for invoice vertical slice.

This distribution preserves original upstream archives unchanged under `vendor/upstream_sources/`.
