from __future__ import annotations
from dataclasses import dataclass
from construction_ai.domain.models import Project
from construction_ai.resolution.project import resolve_projects, classification_band

@dataclass(frozen=True)
class ResolutionCase:
    name: str
    expected_project: str | None
    signals: dict
    expect_auto: bool

def run_cases(projects: list[Project], cases: list[ResolutionCase]) -> dict:
    total=len(cases); correct=0; auto=0; auto_correct=0; failures=[]
    for case in cases:
        ranked=resolve_projects(projects,case.signals)
        top=ranked[0] if ranked else None
        predicted=top.project_id if top and top.score>0 else None
        band=classification_band(top.confidence) if top else "unresolved"
        is_correct=predicted==case.expected_project
        correct += int(is_correct)
        if band=="automatic":
            auto+=1; auto_correct+=int(is_correct)
        if not is_correct or ((band=="automatic") != case.expect_auto):
            failures.append({"case":case.name,"predicted":predicted,"expected":case.expected_project,"band":band,"confidence":getattr(top,"confidence",0)})
    return {"total":total,"top1_accuracy":correct/total if total else 0,"auto_count":auto,
            "auto_precision":auto_correct/auto if auto else 1.0,"failures":failures}
