from __future__ import annotations
from dataclasses import dataclass
from construction_ai.domain.models import Person, Company

@dataclass(frozen=True)
class IdentityMatch:
    entity_id: str | None
    confidence: float
    evidence: tuple[str, ...]


def resolve_person(sender: str, people: list[Person]) -> IdentityMatch:
    s = sender.lower().strip()
    for p in people:
        if any(s == e.lower().strip() for e in p.emails):
            return IdentityMatch(p.person_id, 0.999, ("exact_email_match",))
    return IdentityMatch(None, 0.0, ())


def resolve_company(name: str, companies: list[Company]) -> IdentityMatch:
    n = " ".join(name.lower().replace(".", "").split())
    for c in companies:
        names = [c.name, *c.aliases]
        if any(n == " ".join(x.lower().replace(".", "").split()) for x in names):
            return IdentityMatch(c.company_id, 0.995, ("exact_or_alias_name_match",))
    return IdentityMatch(None, 0.0, ())
