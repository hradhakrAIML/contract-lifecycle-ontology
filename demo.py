"""Contract lifecycle demo: how an ontology makes AI usable on contracts.

  Part 1  Lifecycle      draft -> review -> signed -> submit -> lock and bill -> validate
  Part 2  AI extraction  the model reads the signed contract into the ontology schema; a guard checks it
  Part 3  AI agent       the model operates the lifecycle, using only ontology actions as tools
  Part 4  Eval and cost

    python demo.py      # no API calls; a simulated model and agent plan
"""
import copy
import csv
import json
from collections import Counter
from pathlib import Path

import ai
from ontology import DOC_BINDING, OBJECTS, SEVERITY, SIGNED_OR_LATER, LockedRecord, Ontology, RuleViolation, show

# ---------------------------------------------------------------- sample data (fictional)
PARTNERS = {"P1": ("Acme Analytics Inc", "CUSTOMER"), "P2": ("Bluefin Software Ltd", "VENDOR")}
DRAFTS = {
    "CT-1": dict(partner="P1", contract_type="NDA", parent=None, effective_date="2026-01-01",
                 end_date="2028-01-01", governing_law="California", payment_terms=None, line_terms={}),
    "CT-2": dict(partner="P1", contract_type="MSA", parent=None, effective_date="2026-02-01",
                 end_date="2029-02-01", governing_law="California", payment_terms="Net 30", line_terms={}),
    "CT-3": dict(partner="P1", contract_type="SOW", parent="CT-2", effective_date="2026-03-01",
                 end_date="2027-03-01", governing_law="California", payment_terms="Net 30",
                 line_terms={"M1": ["Discovery and design", 20000.0], "M2": ["Build and integration", 51000.0],
                             "M3": ["Go-live and hypercare", 15000.0]}),
    "CT-4": dict(partner="P1", contract_type="ORDER_FORM", parent="CT-2", effective_date="2026-03-01",
                 end_date="2027-03-01", governing_law="California", payment_terms="Net 30",
                 line_terms={"SKU-PLAT": ["Platform subscription", 48000.0],
                             "SKU-SUP": ["Premium support", 12000.0]}),
    "CT-5": dict(partner="P2", contract_type="MSA", parent=None, effective_date="2026-02-15",
                 end_date="2029-02-15", governing_law="Delaware", payment_terms="Net 30", line_terms={}),
    "CT-6": dict(partner="P2", contract_type="SOW", parent="CT-5", effective_date="2026-04-01",
                 end_date="2027-04-01", governing_law="Delaware", payment_terms="Net 30",
                 line_terms={"M1": ["Data migration", 30000.0], "M2": ["Admin training", 8000.0]}),
}
REVIEW_EDITS = {"CT-1": {"end_date": "2029-01-01"},                      # keyed correctly in review
                "CT-4": {"line_terms": {"SKU-PLAT": ["Platform subscription", 45600.0],
                                        "SKU-SUP": ["Premium support", 12000.0]}}}
NOT_KEYED = {"CT-2": {"payment_terms": "Net 45"},                        # in the signed copy only
             "CT-3": {"line_terms": {**DRAFTS["CT-3"]["line_terms"], "M2": ["Build and integration", 63000.0]}},
             "CT-6": {"end_date": "2027-10-01"}}
GROUND_TRUTH = {"CT-2": {"payment_terms"}, "CT-3": {"billing_lines", "total_value"}, "CT-6": {"end_date"}}

# what an agent tends to try when told to "get everything to Finance" (offline plan)
AGENT_PLAN = [
    ("list_contracts", {}),
    ("submit", {"contract_id": "CT-3"}),                                  # SOW before its MSA is signed
    ("execute", {"contract_id": "CT-2"}),                                 # sign on the partner's behalf
    ("submit", {"contract_id": "CT-6"}),
    ("lock_and_bill", {"contract_id": "CT-6"}),
    ("validate", {"contract_id": "CT-6"}),
    ("get_contract", {"contract_id": "CT-6"}),
    ("update_contract", {"contract_id": "CT-6", "field": "end_date", "value": "2027-10-01"}),  # "fix" a locked record
    ("update_contract", {"contract_id": "CT-1", "field": "approved_by", "value": "AI Agent"}),  # invent a property
    ("delete_finding", {"contract_id": "CT-6"}),                          # make the issue disappear
    ("submit", {"contract_id": "CT-1"}),
    ("lock_and_bill", {"contract_id": "CT-1"}),
    ("validate", {"contract_id": "CT-1"}),
]
# a malformed model response, to show what the guard does with it
MALFORMED = {"Payment Terms": "Net 45", "end_date": "sometime in late 2027", "total_value": "about 98k",
             "auto_renew": True, "billing_lines": {"M2": "$63,000.00", "M3": "TBD"}}


def total(lines: dict):
    return round(sum(a for _, a in lines.values()), 2) if lines else None


def signed_text(cid: str) -> str:
    """The executed contract as extracted text (stand-in for PDF text extraction)."""
    t = {**copy.deepcopy(DRAFTS[cid]), **REVIEW_EDITS.get(cid, {}), **NOT_KEYED.get(cid, {})}
    t["total_value"] = total(t["line_terms"])
    types = OBJECTS["Contract"]
    rows = [f"COMMERCIAL AGREEMENT {cid} ({t['contract_type']})", f"Partner: {PARTNERS[t['partner']][0]}"]
    rows += [f"{label}: {show(types[prop], t.get(prop))}" for label, prop in DOC_BINDING.items()]
    rows += [f"BILL {k} | {item} | {show('money', amt)}" for k, (item, amt) in t["line_terms"].items()]
    rows += ["Renewal: Automatic for successive 12-month terms", "Signed by both parties."]
    return "\n".join(rows)


# ---------------------------------------------------------------- action effects
def review(o, c):
    edits = REVIEW_EDITS.get(c["id"], {})
    if "line_terms" in edits:
        edits = {**edits, "total_value": total(edits["line_terms"])}
    if edits:
        o.put("Contract", c["id"], "Legal and BD", **edits)


def execute(o, c):
    doc = f"{c['id']}-signed"
    o.put("Document", doc, "Partner", contract=c["id"], version="signed", text=signed_text(c["id"]))
    o.link("evidenced_by", c["id"], doc)


def submit(o, c):
    """Rule that reads a link: SOW and Order Form need a signed MSA."""
    if c["contract_type"] in ("SOW", "ORDER_FORM"):
        msa = o.out("governed_by", c["id"])[0]
        if msa["status"] not in SIGNED_OR_LATER:
            raise RuleViolation(f"parent {msa['id']} is {msa['status']}, not signed")


def lock_and_bill(o, c):
    for k, (item, amount) in (c["line_terms"] or {}).items():
        lid = f"{c['id']}|{k}"
        o.put("BillingLine", lid, "Billing Automation", contract=c["id"], item=item, amount=amount)
        o.link("bills", c["id"], lid)
        o.lock("BillingLine", lid, "Billing Automation")
    o.lock("Contract", c["id"], "Billing Automation")


EXTRACTIONS: dict[str, list[str]] = {}


def system_view(o, cid: str) -> dict:
    c = o.get("Contract", cid)
    view = {p: c.get(p) for p in DOC_BINDING.values()}
    view["billing_lines"] = {l["id"].split("|")[1]: l["amount"] for l in o.out("bills", cid)}
    return view


def compare(sys: dict, doc: dict) -> list[dict]:
    out = []
    for f, s in sys.items():
        d = doc.get(f)
        if s == d:
            continue
        if isinstance(s, dict):
            keys = sorted(k for k in set(s) | set(d or {}) if s.get(k) != (d or {}).get(k))
            s, d = ", ".join(f"{k}={s.get(k)}" for k in keys), ", ".join(f"{k}={(d or {}).get(k)}" for k in keys)
        out.append({"field": f, "system": str(s), "document": str(d), "severity": SEVERITY[f]})
    return out


def validate(o, c):
    """AI reads the signed contract into the ontology schema; the guarded record is compared."""
    text = o.out("evidenced_by", c["id"])[0]["text"]
    doc, notes = ai.extract(c["id"], text)
    EXTRACTIONS[c["id"]] = notes
    findings = compare(system_view(o, c["id"]), doc)
    for i, f in enumerate(findings, 1):
        o.put("Finding", f"{c['id']}-F{i}", "AI Validator", contract=c["id"], **f)
        o.link("has_finding", c["id"], f"{c['id']}-F{i}")
    return "REVIEW_NEEDED" if findings else "PENDING_FINANCE"


EFFECTS = {"start_review": review, "execute": execute, "submit": submit,
           "lock_and_bill": lock_and_bill, "validate": validate}
NEXT = {"DRAFT": "start_review", "IN_REVIEW": "execute", "SIGNED": "submit",
        "SUBMITTED": "lock_and_bill", "LOCKED": "validate"}


def load() -> Ontology:
    o = Ontology()
    for pid, (name, ptype) in PARTNERS.items():
        o.put("Partner", pid, "Ingestion", name=name, partner_type=ptype)
    for cid, d in DRAFTS.items():
        o.put("Contract", cid, "Ingestion", **d, total_value=total(d["line_terms"]), status="DRAFT")
        o.link("signs", d["partner"], cid)
        if d["parent"]:
            o.link("governed_by", cid, d["parent"])
    return o


def naive_fields(sys: dict, text: str) -> set:
    """Without an ontology: compare stored values to printed strings."""
    raw = ai.stand_in_extract(text)
    return {f for f, v in sys.items() if f != "billing_lines" and str(v) != raw.get(
        next(l for l, p in DOC_BINDING.items() if p == f))}


# ---------------------------------------------------------------- run
def main():
    o = load()
    print("PART 1  Lifecycle")
    queue = ["CT-6", "CT-3", "CT-1", "CT-2", "CT-4", "CT-5"]             # SOWs arrive before their MSAs
    for _ in range(3 * len(queue)):
        if not queue:
            break
        cid = queue.pop(0)
        c = o.get("Contract", cid)
        try:
            while c["status"] in NEXT:
                o.act(NEXT[c["status"]], cid, EFFECTS[NEXT[c["status"]]])
            print(f"  {cid} {c['contract_type']:<10} -> {c['status']}")
        except RuleViolation as e:
            print(f"  {cid} held at {c['status']}: {e}")
            queue.append(cid)
    for f in o.objects["Finding"].values():
        print(f"    {f['contract']} {f['field']}: system {f['system']} | signed {f['document']}")

    print(f"\nPART 2  AI extraction into the ontology schema (simulated model)")
    for cid, notes in EXTRACTIONS.items():
        print(f"  {cid} guard: {'; '.join(notes) or 'clean'}")
    _, notes = ai.guard(MALFORMED)
    print("  Malformed model output example:")
    for n in notes:
        print(f"    {n}")

    print("\nPART 3  AI agent limited to ontology actions")
    agent_o = load()
    for cid in DRAFTS:                                   # people review and sign; CT-2 still in review
        agent_o.act("start_review", cid, review)
        if cid != "CT-2":
            agent_o.act("execute", cid, execute)
    log, mode = ai.run_agent(agent_o, EFFECTS, AGENT_PLAN)
    print(f"  mode: {mode}")
    for name, args, res in log:
        print(f"  {name}({args.get('contract_id', '')}{', ' + args['field'] if 'field' in args else ''}) -> {res[:90]}")
    blocked = [r for _, _, r in log if r.startswith("BLOCKED")]
    audited = [a for a in agent_o.audit if a["actor"] == ai.AGENT and a["event"].startswith("blocked")]

    print("\nPART 4  Eval and cost")
    for name, pred in [("without ontology (string match)",
                        lambda cid: naive_fields(system_view(o, cid), o.out("evidenced_by", cid)[0]["text"])),
                       ("with ontology (typed, guarded)", lambda cid: {f["field"] for f in o.out("has_finding", cid)})]:
        tp = fp = fn = 0
        for cid in DRAFTS:
            got, exp = pred(cid), GROUND_TRUTH.get(cid, set())
            tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)
        print(f"  {name:<34} precision {tp / (tp + fp):.2f}  recall {tp / (tp + fn):.2f}")
    by = Counter()
    for c in ai.COSTS:
        by[c["purpose"]] += c["usd"]
    n_extract = sum(1 for c in ai.COSTS if c["purpose"] == "extract")
    print(f"  AI cost (projected): extraction ${by['extract']:.5f} over {n_extract} calls "
          f"(${by['extract'] / n_extract * 1000:.2f} per 1,000 contracts)")

    print("\nWhy the ontology matters for AI")
    notes = [n for ns in EXTRACTIONS.values() for n in ns]
    print(f"  Extraction  prompt built from the schema; guard ignored {sum(n.startswith('ignored') for n in notes)} "
          f"out-of-schema labels and rejected {sum(n.startswith('rejected') for n in notes)} bad values "
          f"(malformed example: {sum(n.startswith('rejected') for n in ai.guard(MALFORMED)[1])} rejected)")
    print(f"  Validation  findings are typed fields, so eval scores them against ground truth")
    print(f"  Agent       {len(log)} tool calls, {len(blocked)} blocked by the ontology, {len(audited)} blocked attempts in the audit log")

    ledger = []
    for cid, c in o.objects["Contract"].items():
        cost = sum(x["usd"] for x in ai.COSTS if x["ref"] == cid)
        issues = "; ".join(f"{f['field']}: {f['system']} vs {f['document']}" for f in o.out("has_finding", cid))
        o.put("LedgerEntry", f"LE-{cid}", "Billing Automation", contract=cid, status=c["status"],
              issues=issues, total_value=c["total_value"], ai_cost_usd=cost)
        o.link("recorded_in", cid, f"LE-{cid}")
        ledger.append({"contract": cid, "type": c["contract_type"], "partner": PARTNERS[c["partner"]][0],
                       "status": c["status"], "total_value": c["total_value"], "issues": issues,
                       "ai_cost_usd": cost})
    out = Path("out"); out.mkdir(exist_ok=True)
    with open(out / "ledger.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ledger[0])); w.writeheader(); w.writerows(ledger)
    (out / "agent_audit.json").write_text(json.dumps(agent_o.audit, indent=2, default=str))
    (out / "ontology.json").write_text(json.dumps(
        {"objects": o.objects, "links": sorted(o.links), "audit": o.audit}, indent=2, default=str))


if __name__ == "__main__":
    main()
