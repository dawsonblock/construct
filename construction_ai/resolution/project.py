from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable
from construction_ai.domain.models import Project

WEIGHTS = {
    "project_id": 1.00,
    "po": 0.95,
    "address": 0.95,
    "thread": 0.90,
    "document_link": 0.85,
    "person": 0.50,
    "vendor": 0.30,
    "semantic": 0.15,
}

@dataclass(frozen=True)
class ProjectCandidate:
    project_id: str
    score: float
    confidence: float
    signals: tuple[str, ...]


def _norm(v: str | None) -> str:
    return " ".join((v or "").lower().strip().replace(",", "").split())


def resolve_projects(projects: Iterable[Project], signals: dict) -> list[ProjectCandidate]:
    raw: list[tuple[Project, float, list[str]]] = []
    for p in projects:
        score = 0.0
        hits: list[str] = []
        if signals.get("project_id") == p.project_id:
            score += WEIGHTS["project_id"]; hits.append("project_id")
        po = signals.get("po_number")
        if po and po in p.identifiers.get("po", []):
            score += WEIGHTS["po"]; hits.append("po")
        if signals.get("address") and _norm(signals["address"]) == _norm(p.address):
            score += WEIGHTS["address"]; hits.append("address")
        thread = signals.get("thread_id")
        if thread and thread in p.identifiers.get("thread", []):
            score += WEIGHTS["thread"]; hits.append("thread")
        vendor = signals.get("vendor_company_id")
        if vendor and vendor in p.company_ids:
            score += WEIGHTS["vendor"]; hits.append("vendor")
        person_project_ids = set(signals.get("person_active_projects", []))
        if p.project_id in person_project_ids:
            score += WEIGHTS["person"]; hits.append("person")
        semantic_scores = signals.get("semantic_scores", {})
        if p.project_id in semantic_scores:
            score += WEIGHTS["semantic"] * max(0.0, min(1.0, semantic_scores[p.project_id])); hits.append("semantic")
        raw.append((p, score, hits))
    raw.sort(key=lambda x: (-x[1], x[0].project_id))
    if not raw:
        return []
    top = raw[0][1]
    second = raw[1][1] if len(raw) > 1 else 0.0
    out: list[ProjectCandidate] = []
    for i, (p, score, hits) in enumerate(raw):
        quality = min(1.0, sum(WEIGHTS[h] for h in hits if h != "semantic") / 1.9)
        margin = max(0.0, top - second) if i == 0 else max(0.0, score - (raw[i+1][1] if i+1 < len(raw) else 0.0))
        base = min(1.0, score / 1.9)
        confidence = min(0.999, 0.55 * base + 0.30 * min(1.0, margin) + 0.15 * quality)
        hard_auto = "project_id" in hits or ("po" in hits and "address" in hits)
        if hard_auto:
            confidence = max(confidence, 0.995)
        else:
            # Ranking confidence may be strong, but automatic filing is reserved for
            # corroborated hard identifiers in v0.3.0. Keep all softer combinations
            # below the automatic threshold so they enter review rather than silently mutate state.
            confidence = min(confidence, 0.979)
        out.append(ProjectCandidate(p.project_id, round(score, 6), round(confidence, 6), tuple(hits)))
    return out


def classification_band(confidence: float) -> str:
    if confidence >= 0.98:
        return "automatic"
    if confidence >= 0.90:
        return "provisional_review"
    if confidence >= 0.70:
        return "human_confirmation"
    return "unresolved"
