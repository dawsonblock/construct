"""Authority-aware graph views.

The failure this file exists to prevent is not a crash. It is a prompt builder
writing:

    for edge in state.graph.edges: ...

and a model consuming a 0.61-confidence proposal as established project state.
Type checking will never catch that, so the flat attribute simply does not
exist. Edges are reachable only through a view that names the authority level
being asked for:

    state.graph.authoritative()   approved edges — the default
    state.graph.proposed()        candidates, never facts
    state.graph.all()             everything, explicitly

Iterating the container itself raises. Choosing a view is the whole point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GraphNode:
    entity_id: str
    entity_type: str
    label: str
    record_table: str | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class GraphEdge:
    relationship_id: str
    relation: str
    source: str
    target: str
    source_entity_id: str
    target_entity_id: str
    origin: str
    status: str
    confidence: float | None = None
    evidence_ids: tuple[str, ...] = ()
    decided_by: str | None = None
    producer: str | None = None

    @property
    def is_authoritative(self) -> bool:
        return self.status == "approved"

    def describe(self) -> str:
        """One line a person or a prompt can read without losing the authority."""
        if self.is_authoritative:
            basis = "established" if self.origin == "observed" else f"promoted by {self.decided_by}"
        else:
            basis = f"{self.status}, confidence {self.confidence if self.confidence is not None else 'unknown'}"
        return f"{self.source} -{self.relation}-> {self.target} ({basis})"


@dataclass(frozen=True)
class ProjectGraph:
    """Edges grouped by authority. There is deliberately no flat `edges`."""

    nodes: tuple[GraphNode, ...] = ()
    approved: tuple[GraphEdge, ...] = ()
    proposed: tuple[GraphEdge, ...] = ()
    rejected: tuple[GraphEdge, ...] = ()
    superseded: tuple[GraphEdge, ...] = ()
    #: Set by callers that filtered the graph, so a consumer can tell a genuinely
    #: empty section from one that was never loaded.
    filtered_to: str | None = field(default=None)

    def authoritative(self) -> tuple[GraphEdge, ...]:
        """Edges the system is entitled to state as fact. The default view."""
        return self.approved

    def proposed_only(self) -> tuple[GraphEdge, ...]:
        """Candidates. Anything reading these must carry the uncertainty forward."""
        return self.proposed

    def all(self) -> tuple[GraphEdge, ...]:
        """Everything, in one list, because the caller said so explicitly."""
        return (*self.approved, *self.proposed, *self.rejected, *self.superseded)

    def by_relation(self, relation: str, *, view: str = "authoritative") -> tuple[GraphEdge, ...]:
        source = {"authoritative": self.authoritative, "proposed": self.proposed_only, "all": self.all}
        if view not in source:
            raise ValueError(f"unknown graph view {view!r}; choose authoritative, proposed or all")
        return tuple(edge for edge in source[view]() if edge.relation == relation)

    def __iter__(self):
        raise TypeError(
            "ProjectGraph is not iterable on purpose: iterating it would flatten proposals "
            "into facts. Choose a view — .authoritative(), .proposed_only() or .all()."
        )

    def __len__(self) -> int:
        return len(self.all())

    def counts(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "approved": len(self.approved),
            "proposed": len(self.proposed),
            "rejected": len(self.rejected),
            "superseded": len(self.superseded),
        }


def edge_from_relationship(relationship, labels: dict[Any, str]) -> GraphEdge:
    return GraphEdge(
        relationship_id=str(relationship.relationship_id),
        relation=relationship.relation,
        source=labels.get(relationship.source_entity_id, str(relationship.source_entity_id)),
        target=labels.get(relationship.target_entity_id, str(relationship.target_entity_id)),
        source_entity_id=str(relationship.source_entity_id),
        target_entity_id=str(relationship.target_entity_id),
        origin=relationship.origin,
        status=relationship.status,
        confidence=relationship.confidence,
        evidence_ids=tuple(sorted(str(e) for e in relationship.evidence_ids)),
        decided_by=relationship.decided_by,
        producer=relationship.producer,
    )
