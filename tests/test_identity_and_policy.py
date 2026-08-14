from construction_ai.domain.models import Person, Company
from construction_ai.resolution.identity import resolve_person, resolve_company
from construction_ai.policy.engine import decide


def test_identity_exact_first():
    p=[Person("P1","O","Bob",emails=["bob@abc.ca"])]
    assert resolve_person("BOB@ABC.CA",p).entity_id=="P1"
    c=[Company("C1","O","ABC Electric",aliases=["ABC Electrical"])]
    assert resolve_company("ABC Electrical",c).entity_id=="C1"

def test_unknown_action_fails_closed():
    d=decide("DELETE_FINANCIAL_RECORD",evidence_valid=True,confidence_satisfied=True)
    assert not d.allowed
