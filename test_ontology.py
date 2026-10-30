import pytest

import ai
import demo
from ontology import LockedRecord, Ontology, RuleViolation


def make(o, cid, ctype, status, parent=None):
    o.put("Contract", cid, contract_type=ctype, parent=parent, line_terms={}, status=status)
    if parent:
        o.link("governed_by", cid, parent)


def test_sow_blocked_until_msa_signed():
    o = Ontology()
    make(o, "MSA", "MSA", "IN_REVIEW")
    make(o, "SOW", "SOW", "SIGNED", parent="MSA")
    with pytest.raises(RuleViolation):
        o.act("submit", "SOW", demo.submit)


def test_cannot_skip_states():
    o = Ontology()
    make(o, "C", "NDA", "DRAFT")
    with pytest.raises(RuleViolation):
        o.act("lock_and_bill", "C", demo.lock_and_bill)


def test_locked_contract_rejects_edits():
    o = Ontology()
    make(o, "C", "NDA", "SUBMITTED")
    o.act("lock_and_bill", "C", demo.lock_and_bill)
    with pytest.raises(LockedRecord):
        o.put("Contract", "C", end_date="2030-01-01")


def test_unknown_property_rejected():
    with pytest.raises(ValueError):
        Ontology().put("Partner", "P", revenue=1)


def test_extraction_is_typed_by_the_ontology():
    doc, _ = ai.guard(ai.stand_in_extract(demo.signed_text("CT-2")))
    assert doc["payment_terms"] == "Net 45" and doc["end_date"] == "2029-02-01"


# ---------------------------------------------------------------- AI guardrails


def test_guard_types_and_rejects():
    rec, notes = ai.guard({"Expiration Date": "October 01, 2027", "total_value": "$98,000.00",
                           "end_date_guess": "soon", "payment_terms": "Net 45",
                           "effective_date": "not a date", "governing_law": "Delaware"})
    assert rec["end_date"] == "2027-10-01" and rec["total_value"] == 98000.0
    assert any("end_date_guess" in n for n in notes)
    assert any(n.startswith("rejected effective_date") for n in notes)


def test_agent_tools_come_from_ontology():
    names = {t["name"] for t in ai.agent_tools()}
    assert set(demo.EFFECTS) <= names and "delete_finding" not in names


def test_agent_cannot_sign_or_edit_locked():
    o = demo.load()
    o.get("Contract", "CT-1")["status"] = "IN_REVIEW"
    assert ai.run_tool(o, demo.EFFECTS, "execute", {"contract_id": "CT-1"}).startswith("BLOCKED")
    o.get("Contract", "CT-5")["status"] = "SUBMITTED"
    o.act("lock_and_bill", "CT-5", demo.lock_and_bill)
    res = ai.run_tool(o, demo.EFFECTS, "update_contract",
                      {"contract_id": "CT-5", "field": "payment_terms", "value": "Net 90"})
    assert res.startswith("BLOCKED") and o.get("Contract", "CT-5")["payment_terms"] == "Net 30"
    assert any(a["actor"] == ai.AGENT and a["event"] == "blocked_edit" for a in o.audit)
