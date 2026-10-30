# Contract Lifecycle Ontology: Making AI Safe on Contracts

A short demo of why AI on contracts needs an ontology. NDA, MSA, SOW, and Order Form
move from draft to Finance. The AI reads signed contracts and operates the lifecycle,
and the ontology decides what it may read, write, and do. All data is fictional.

## The idea in one table

| AI without an ontology | AI with this ontology |
|---|---|
| "Extract the key terms" returns free-form text and invented fields | The extraction prompt is generated from the schema; a guard keeps only known properties with valid types |
| Validation compares strings: `2029-02-01` vs `February 01, 2029` is a mismatch | Values are typed, so dates and money compare by meaning |
| An agent with write access can edit any record, sign for a partner, or delete the evidence | The agent's tools are the ontology's action types; state, link, lock, and delegation rules decide what happens |
| Nobody can say what the AI tried | Every attempt, allowed or blocked, is in the audit log with the actor |

## The ontology

| Part | In this demo |
|---|---|
| Object types | Partner, Contract, BillingLine, Document, Finding, LedgerEntry, with typed properties |
| Link types | `signs`, `governed_by` (SOW / Order Form → MSA), `evidenced_by`, `bills`, `has_finding`, `recorded_in` |
| Action types | `start_review` (Legal and BD) → `execute` (Partner) → `submit` (BD) → `lock_and_bill` (Billing) → `validate` (AI) |
| Rules | `submit` follows `governed_by` and holds a SOW until its MSA is signed; status changes only through actions |
| Locks | `lock_and_bill` locks the Contract and its BillingLines |
| Delegation | The AI agent may run `submit`, `lock_and_bill`, `validate`. Signing and negotiating stay with people. |
| Document binding | Printed labels on the signed contract map to typed Contract properties |

```
Partner ──signs──▶ Contract ──governed_by──▶ Contract (MSA)
                      │
     ┌────────────────┼──────────────┬──────────────┐
evidenced_by        bills       has_finding     recorded_in
     ▼                ▼              ▼              ▼
  Document       BillingLine      Finding       LedgerEntry
```

## Run

```bash
python demo.py          # standard library only, no API calls
python -m pytest -q
```

Output (abridged):

```
PART 1  Lifecycle
  CT-6 held at SIGNED: parent CT-5 is DRAFT, not signed
  CT-3 held at SIGNED: parent CT-2 is DRAFT, not signed
  ...
    CT-2 payment_terms: system Net 30 | signed Net 45
    CT-6 end_date: system 2027-04-01 | signed 2027-10-01
    CT-3 billing_lines: system M2=51000.0 | signed M2=63000.0

PART 2  AI extraction into the ontology schema
  CT-1 guard: ignored 'Partner': not a Contract property; ignored 'Renewal': not a Contract property
  Malformed model output example:
    rejected end_date='sometime in late 2027': not a valid date value
    rejected total_value='about 98k': not a valid money value
    ignored 'auto_renew': not a Contract property
    rejected billing_lines.M3='TBD': not a valid money value

PART 3  AI agent limited to ontology actions
  submit(CT-3) -> BLOCKED: parent CT-2 is IN_REVIEW, not signed
  execute(CT-2) -> BLOCKED: AI Agent may not perform execute; it belongs to Partner
  submit(CT-6) -> OK: CT-6 is now SUBMITTED
  lock_and_bill(CT-6) -> OK: CT-6 is now LOCKED
  validate(CT-6) -> OK: CT-6 is now REVIEW_NEEDED
  update_contract(CT-6, end_date) -> BLOCKED: Contract CT-6 is locked
  update_contract(CT-1, approved_by) -> BLOCKED: Contract has no properties {'approved_by'}
  delete_finding(CT-6) -> BLOCKED: delete_finding is not an action in the ontology
  ...

PART 4  Eval and cost
  without ontology (string match)    precision 0.15  recall 0.75
  with ontology (typed, guarded)     precision 1.00  recall 1.00
  AI cost (projected): extraction $0.02206 over 8 calls ($2.76 per 1,000 contracts)

Why the ontology matters for AI
  Agent       13 tool calls, 5 blocked by the ontology, 5 blocked attempts in the audit log
```

The agent was told to "get every contract to Finance and resolve issues." Its most dangerous
move was overwriting a locked end date so the contract would pass validation. The lock stopped it,
the finding stayed, and the attempt is in `out/agent_audit.json`.

## No API calls

The demo needs no API key and makes no network calls. The point is what the ontology does
around the model, so the model is simulated:

| Part | What runs |
|---|---|
| Extraction | A simulated model returns every printed value as untyped text. The ontology guard maps, types, and rejects. |
| Agent | A simulated plan replays typical risky tool calls through the same generated tools a model would get. |
| Cost | Projected from prompt size at example prices in `ai.py` (`PRICE_IN`, `PRICE_OUT`). |

To use a real model later, replace `stand_in_extract()` and `run_agent()` in `ai.py`. The guard,
tools, rules, and audit stay the same.

## Files

| File | Contents |
|---|---|
| `ontology.py` | Object, link, and action types, delegation, document binding, store with locks and audit |
| `ai.py` | Schema-generated extraction prompt, simulated model, output guard, agent tools generated from actions, cost meter |
| `demo.py` | Sample contracts, action effects, validation, agent scenario, eval |
| `showcase.html` | Visual walkthrough |

Signed contracts are generated as text to keep the demo short. In production, pass PDF text
to `ai.extract()` and keep the rest unchanged.
