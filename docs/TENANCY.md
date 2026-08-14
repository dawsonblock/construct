# Tenancy, identity and provenance invariants

This is the trust boundary of the application. Every table, repository method and
endpoint is checked against the rules below. If tenancy, identity or provenance
are wrong here, better models later only produce faster, more convincing errors.

## 1. Tenant identity lives in relational keys

Not in a JSON payload, not in an application convention, not in a filter the
caller is trusted to remember.

Forbidden:

```sql
SELECT payload FROM ai_objects WHERE payload->>'organization_id' = $1;
```

Required:

```sql
SELECT ... FROM invoices WHERE organization_id = $1 AND project_id = $2;
```

Every tenant-owned table has `organization_id uuid NOT NULL` as the **leading
column of its primary key**. There is no table where a row's owner is derivable
only by reading JSON.

## 2. Identifiers are UUIDs; human references are columns

Primary keys are UUIDs. `PRJ-0042`, `INV-8831` and `APR-1041` are `reference`
columns, unique **within an organization**:

```sql
UNIQUE (organization_id, reference)
```

Two organizations may both have a `PRJ-0042`. They are different projects and the
schema says so.

## 3. Children reference the whole parent scope

A globally unique UUID is not, on its own, proof that a child belongs where it
claims to. Children carry the parent's full scope and the foreign key enforces it:

```
invoices                          invoice_lines
├─ organization_id                ├─ organization_id
├─ project_id     (nullable)      ├─ project_id
└─ invoice_id                     ├─ invoice_id
                                  └─ line_id
```

Two foreign keys, deliberately:

- `(organization_id, invoice_id) → invoices` — always enforced, including while a
  project is still unresolved.
- `(organization_id, project_id, invoice_id) → invoices` with `ON UPDATE CASCADE`
  — preserves project scope, and re-files every child automatically when an
  invoice moves from one project to another.

The second key is not enforced while `project_id IS NULL` (SQL skips FK checks on
NULL components). That is why the first one exists — and it is also why the
cascade cannot do the *first* filing: there is no matched key to cascade from
when the value is NULL on both sides. `assign_project` therefore updates the
invoice and its lines in one transaction. The database guarantees children follow
a re-filing; the repository guarantees they follow the initial filing. Relying on
the cascade alone would leave lines stranded on no project at all.

`project_id` is nullable on ingestion-facing tables **on purpose**. An invoice
whose project has not been resolved is a first-class state, not an error to be
papered over with a default project.

## 4. Repository methods cannot be called without scope

There is no `get_invoice(invoice_id)`. There is:

```python
invoices.get(scope=Scope(organization_id=..., project_id=...), invoice_id=...)
```

`Scope` is required, keyword-only, and typed. The repetition is the point: it
makes an unscoped lookup something you have to deliberately construct rather than
something you can forget to prevent. A future endpoint, worker, tool or agent
cannot reach an unscoped read by accident, because the unscoped read does not
exist.

`organization_id` always constrains the query. `project_id` narrows it further
whenever the scope carries one, and operations that are inherently project-scoped
(`list_for_project`, reconstruction) call `Scope.require_project()` and raise
rather than silently widening.

`project_id` is optional on the scope rather than mandatory because §3 makes it
nullable on the record: an invoice that has not been filed yet belongs to the
tenant but to no project, and a scope that could not express that would force
callers to invent a placeholder project — which is the failure this whole
boundary exists to prevent. The tenant wall is never optional; the project wall
is as tight as the record allows.

## 5. Tenant identity is never client-supplied

`organization_id` is not accepted from a request body, query parameter, header or
job payload. The API resolves it server-side from a bearer token
(`organization_api_keys`, SHA-256 hashed). Anything a caller sends that looks like
a tenant selector is ignored.

Job payloads carry no `organization_id` either — a job's scope comes from the
`jobs` row, which was written under the enqueuing request's authenticated scope.

## 6. Row-level security is the second wall, never the first

Every tenant table has RLS enabled **and forced**, with a policy keyed on
`current_setting('app.organization_id')`. The repository layer sets that GUC
per transaction; a connection that never set it sees nothing rather than
everything.

RLS is defense in depth. It does not excuse a single missing `WHERE
organization_id = $1`, and the isolation gate tests both walls independently.

The application connects as a **non-superuser** role (`construction_app`).
Superusers bypass RLS even when it is forced, so a stack where the app connects
as the owner has RLS in name only. Migrations run as the owner; the app does not.

Two tables are deliberately **not** under RLS, and both are on the path that
*establishes* tenancy rather than operating within it:

- `organization_api_keys` — consulted to discover which organization a caller
  belongs to, before any scope exists. Reachable only through the authentication
  path, which returns an organization id and nothing else.
- `organizations` — the tenant registry itself. Creating a tenant cannot happen
  under that tenant's own scope. No API endpoint reads it, and it holds no
  operational data; if that changes, it needs a policy.

Every other tenant-owned table is covered, and `test_a_connection_that_never_set_a_scope_sees_nothing`
asserts the list stays complete.

## 7. Three classes of information, chosen per field

Not an ideological SQL-versus-JSON choice:

| Class | Storage | Examples |
|---|---|---|
| Authoritative state | relational columns | tenancy, identity, relationships, state transitions, financial amounts, timestamps — anything queried, indexed, joined or reconciled |
| Source artifacts | immutable object storage, content-addressed | the original PDF, the original email body |
| Derived annotations | `jsonb` column on the owning row | model-derived labels, extraction diagnostics, scores that benefit from schema evolution |

An amount that a human will be asked to approve is a `numeric` column. A model's
opinion about that amount is an annotation. They do not share a representation.

## 8. Provenance is recorded, not reconstructed

Records whose provenance matters carry `source_id`, `source_version_id`,
`content_hash`, `observed_at` and `created_by`. Evidence points at a specific
**document version**, not a document — a quote that was revised must not silently
re-point existing evidence at the new revision.

Evidence also carries `(subject_type, subject_id)`: what the fact is *about*.
Without it, `total = 4760` is a claim about a project rather than about one
invoice, and any comparison across evidence has to guess. The pair is deliberately
not a foreign key — evidence is written during extraction, before the subject row
is necessarily verified enough to exist.

## 9. Audit is chained per organization

`audit_events` is hash-chained, and the chain is **per organization**: a shared
global chain lets one tenant infer another's activity rate from sequence gaps.

```
H_i = H(H_{i-1} ‖ event_type ‖ actor ‖ organization ‖ project ‖ object ‖ canonical_payload ‖ t_i)
```

The canonical serialization is `construction_ai.persistence.serialization.dumps`
— the same function that writes the payload column. Separate serializers for
hashing and storage caused a false-tamper bug once already; there is one encoder.

## 10. Deterministic reconstruction (Phase 3)

```
same database state ⇒ same reconstructed project state
```

`ProjectState` reconstruction reads persistent state only. No model call is
required to rebuild a project, and no model call may change what rebuilding
produces. Model output enters as a *proposed* relationship with evidence and a
confidence, and is promoted to authoritative only by deterministic rule or human
approval — so `G_observed`, `G_derived` and `G_approved` stay distinguishable.

## What Phase 2 deliberately did not create

Tables with no writer and no immediate consumer are not schema, they are
speculation. Deferred with their phase:

- `contracts`, `change_orders` — Phase 13/45
- `document_chunks`, `document_pages` — Phase 11 (extraction), when a chunker exists
- `cost_codes`, `time_entries` — Phase 48/52
- `conflicts` — Phase 33, once source authority scoring lands

`entities`, `relationships` and `decisions` **are** created here: they carry the
same tenancy pattern, and adding them later would mean a second migration
re-deriving every RLS policy.
