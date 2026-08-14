from __future__ import annotations
import random
from dataclasses import dataclass
from construction_ai.domain.models import Project
from construction_ai.resolution.project import resolve_projects, classification_band

@dataclass(frozen=True)
class BenchmarkResult:
    cases: int
    top1_accuracy: float
    auto_precision: float
    auto_coverage: float
    unsafe_auto: int


def generate(seed: int=42, n_projects: int=20, n_cases: int=300):
    rng=random.Random(seed)
    projects=[]
    for i in range(n_projects):
        pid=f'PRJ-{i:04d}'; vendor=f'COMP-{i%5}'
        projects.append(Project(pid,'ORG',f'Project {i}',f'{100+i} Test St',company_ids=[vendor],identifiers={'po':[f'PO-{i:04d}'],'thread':[f'THR-{i:04d}']}))
    cases=[]
    for j in range(n_cases):
        p=projects[rng.randrange(n_projects)]; kind=j%6
        s={'vendor_company_id':p.company_ids[0]}
        should_auto=False
        if kind==0: s['po_number']=p.identifiers['po'][0]; s['address']=p.address; should_auto=True
        elif kind==1: s['thread_id']=p.identifiers['thread'][0]; s['address']=p.address
        elif kind==2: s['person_active_projects']=[p.project_id]
        elif kind==3: s['semantic_scores']={p.project_id:0.95}
        elif kind==4: s['address']=p.address
        elif kind==5: s['po_number']='TYPO-'+p.identifiers['po'][0]; s['person_active_projects']=[p.project_id]
        cases.append((p.project_id,s,should_auto))
    return projects,cases


def run(seed=42,n_cases=300):
    projects,cases=generate(seed=seed,n_cases=n_cases); correct=auto=auto_correct=unsafe=0
    for expected,signals,should_auto in cases:
        r=resolve_projects(projects,signals); pred=r[0].project_id if r else None; band=classification_band(r[0].confidence) if r else 'unresolved'
        correct += pred==expected
        if band=='automatic':
            auto += 1; auto_correct += pred==expected
            if pred!=expected or not should_auto: unsafe += 1
    return BenchmarkResult(len(cases),correct/len(cases),auto_correct/auto if auto else 1.0,auto/len(cases),unsafe)
