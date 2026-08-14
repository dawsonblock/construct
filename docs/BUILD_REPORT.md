# v0.3.0 Qualification Report

## Qualification status

- Unit/integration tests: **17/17 PASS**
- Python bytecode compilation: **PASS**
- API import/health check: **PASS**
- Project-resolution benchmark: **1,000 cases**
  - Top-1 accuracy: **1.000**
  - Automatic precision: **1.000**
  - Automatic coverage: **0.167**
  - Unsafe automatic assignments: **0**

Automatic coverage is intentionally conservative. v0.3 permits automatic assignment only when the candidate carries a hard project identifier or corroborated PO + exact address. Strong softer evidence remains reviewable rather than silently mutating state.

## Added in v0.3.0

1. PostgreSQL production repository and migration 002.
2. Gmail REST message/attachment fetch adapter.
3. Microsoft Graph message/attachment fetch adapter.
4. OAuth refresh-token and client-credentials token-provider primitives.
5. Content-addressed attachment ingestion and local object-store implementation.
6. PDF/DOCX/XLSX/CSV extraction with table preservation and scanned-PDF fail-closed warning.
7. Deterministic invoice extractor schema.
8. ERPNext exact supplier, Purchase Order and Supplier Quotation resolution.
9. Evidence-backed approval packet persistence/API/UI.
10. End-to-end invoice pipeline coordinator.
11. 300-case checked-in construction resolution corpus plus scalable 1,000-case qualification generator.
12. Harder project auto-classification gate discovered during benchmark qualification.

## Known deployment work still required

- Configure actual OAuth applications/redirect flows and encrypted secret storage.
- Configure ERPNext API authentication/tenant mapping.
- Replace local object storage with production object storage if required.
- Add a document-vision/OCR service for scanned PDFs; the current extractor explicitly routes them rather than fabricating text.
- Run PostgreSQL migration and integration tests against the deployment database.
- Add organization-specific tax, holdback, approval and accounting policies before live financial use.
- Run shadow mode on real historical/live projects before enabling any external write path.
