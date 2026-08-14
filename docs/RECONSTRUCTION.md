# The project graph and deterministic reconstruction

## The invariant

```
same database state  ⇒  same reconstructed project state
```

`reconstruct()` reads persistent state and nothing else: no model call, no
network, no clock, no randomness. It writes nothing, so calling it cannot change
what a later call returns. Every collection is totally ordered, so two rebuilds
are equal byte for byte and not merely equivalent.

`ProjectState.fingerprint()` is a SHA-256 over the canonical serialization of the
whole aggregate — the same encoder the persistence layer writes with, so a value
that round-trips through the database fingerprints identically on both sides.
That turns the invariant into an assertion instead of an aspiration, and both the
test suite and the acceptance gate check it.

An AI may read a `ProjectState`. Nothing an AI does may change what rebuilding one
returns.

## Three graphs, kept distinguishable

```
G_observed  ∪  G_derived  ∪  G_approved
```

| | `origin` | `status` | Written by |
|---|---|---|---|
| Structural | `observed` | `approved` | `projection.project_graph()` |
| Inferred | `derived` | `proposed` | `relationships.propose()` |
| Promoted | `derived` | `approved` | a human, or a named rule |

The distinction is enforced, not documented:

- `relationships.observe()` is reserved for edges that are true because an
  authoritative column says so. It is the only path that produces an approved
  edge with no decider, and the schema permits that **only** for
  `origin='observed'`.
- `relationships.propose()` is what inference gets. It always writes
  `status='proposed'`.
- Promotion goes through `decide(decided_by=<person>)` or
  `promote_by_rule(rule=<name>)`, recorded as `rule:<name>` so an audit can tell
  a rule's decision from a person's. A rule may promote because it is
  reproducible from state; a model may not, which is why there is no equivalent
  path for `ai`.
- A database `CHECK` blocks `status='approved' AND origin='derived' AND
  decided_by='ai'`. A hand-written `INSERT` cannot get around the repository.
- Re-proposing an already-decided edge updates its score but never its status:
  re-running inference must not un-approve or un-reject what a person settled.

## Nodes and edges

Node types come from the schema's `entity_type` CHECK: `PROJECT`, `DOCUMENT`,
`DRAWING`, `SPECIFICATION`, `CONTRACT`, `VENDOR`, `INVOICE`, `PURCHASE_ORDER`,
`CHANGE_ORDER`, `CLAIM`, `EVIDENCE`, `APPROVAL`, `PERSON`, `LOCATION`,
`COST_CODE`.

Relations likewise: `BELONGS_TO`, `REFERENCES`, `SUPERSEDES`, `SUPPORTS`,
`CONTRADICTS`, `BILLED_BY`, `ORDERED_FROM`, `MATCHES`, `APPROVES`, `REQUIRES`,
`DERIVED_FROM`, `AFFECTS`.

A node carries `(record_table, record_id)` — the row it stands for — so the graph
is a projection of authoritative state rather than a parallel copy of it.

## Projection is defined once and used twice

`structural_graph(rows)` is a pure function from a project's rows to the nodes and
edges those rows imply. Two things consume it:

- `project_graph()` **writes** them.
- the consistency check **compares** them against what is stored.

So a stale graph surfaces as a `GRAPH_INCOMPLETE` conflict and a
`provenance.structural_edges_missing` count, rather than as a quietly incomplete
answer. Projection is idempotent — a partial unique index on the currently-valid
edges (migration 004) makes re-projection a no-op instead of a multiplier.

## Conflicts are derived, never stored

A conflicts table would be a second copy of a fact that can drift from the state
it describes. Everything in `reconstruction/conflicts.py` is a pure function of
rows already read, computed fresh on every reconstruction.

| Conflict | Severity | Means |
|---|---|---|
| `PO_REFERENCE_UNRESOLVED` | high | invoice cites a PO that is not on this project |
| `AMOUNT_MISMATCH` | high | invoice total disagrees with its PO beyond $0.02 |
| `VENDOR_MISMATCH` | high | invoice billed by a different company than the PO was ordered from |
| `UNAPPROVED_QUOTE` | high | invoice billed against a quote nobody approved |
| `CONTRADICTORY_MATCH` | high | one source approved as `MATCHES` against several targets |
| `EVIDENCE_CONTRADICTION` | high | equally authoritative sources give different values for the same field of the same subject |
| `QUOTE_REFERENCE_UNRESOLVED` | medium | invoice cites a quote that is not on this project |
| `GRAPH_INCOMPLETE` | medium | the records imply an edge the graph does not have |
| `DANGLING_ENTITY` | medium | a node points at a row that is not on this project |
| `EDGE_LEAVES_PROJECT` | medium | an edge has an endpoint outside the project's node set |
| `EVIDENCE_FROM_SUPERSEDED_VERSION` | medium | evidence extracted from a document revision since superseded |
| `CONTRADICTORY_PROPOSAL` | low | a proposal disagrees with an already-approved match |

Evidence carries a `(subject_type, subject_id)` pair, and contradictions are
grouped by subject rather than by field name. Two invoices each carrying a
`total` are two invoices, not a disagreement; grouping by field alone would have
made every second invoice look like a conflict.

**No check picks a winner.** A conflict records that two things disagree and what
would settle it. Source-authority scoring and resolution are Phase 32–33; until
then, disagreement is surfaced to a person rather than averaged away.

Conflicts are ordered by `(severity, type, subject, detail)` so the list itself is
part of the deterministic output.

## Using it

```python
state = repos.projects.reconstruct(scope=Scope(organization_id, project_id))
state.fingerprint()
state.conflicts_of_severity("high")
```

Over HTTP:

```
GET  /projects/{id}/state          full aggregate plus fingerprint
GET  /projects/{id}/conflicts      ?severity=high
GET  /projects/{id}/graph          ?status=proposed
POST /projects/{id}/graph/project  re-derive structural edges (idempotent)
POST /relationships/{id}/decide    promote or reject a candidate
```

All of them are tenant-scoped like everything else — see
[TENANCY.md](TENANCY.md). The acceptance gate probes each with the wrong
organization's key and requires a 404.

## What is not here yet

- `decisions` has a table and no writer; the executive controller still returns
  actions without persisting them, so `ProjectState.decisions` is empty and says
  so rather than pretending.
- Nothing calls `propose()` in production yet. The candidate path is built,
  tested and enforced, but the first real producer is entity deduplication
  (Phase 7) and invoice→PO matching (Phase 15).
- `SUPERSEDES` edges between document versions are detected for the evidence
  conflict check but not projected as graph edges; that belongs with document
  family detection (Phase 12).
