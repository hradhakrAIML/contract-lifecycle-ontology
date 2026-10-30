"""Contract lifecycle ontology.

Four parts make this an ontology rather than a set of tables:
  1. Object types with typed properties   (what exists)
  2. Link types                            (how things relate)
  3. Action types                          (who may change what, from which state)
  4. Document binding                      (printed labels mapped to typed properties)
"""
from datetime import datetime

# 1. Object types ---------------------------------------------------------------
OBJECTS = {
    "Partner":     {"name": "string", "partner_type": "enum"},
    "Contract":    {"partner": "ref:Partner", "contract_type": "enum", "parent": "ref:Contract",
                    "effective_date": "date", "end_date": "date", "governing_law": "string",
                    "payment_terms": "string", "total_value": "money", "line_terms": "json",
                    "status": "enum"},
    "BillingLine": {"contract": "ref:Contract", "item": "string", "amount": "money"},
    "Document":    {"contract": "ref:Contract", "version": "enum", "text": "string"},
    "Finding":     {"contract": "ref:Contract", "field": "string", "system": "string",
                    "document": "string", "severity": "enum"},
    "LedgerEntry": {"contract": "ref:Contract", "status": "enum", "issues": "string",
                    "total_value": "money", "ai_cost_usd": "money"},
}
LOCKABLE = {"Contract", "BillingLine"}

# 2. Link types -----------------------------------------------------------------
LINKS = {
    "signs":        ("Partner", "Contract"),
    "governed_by":  ("Contract", "Contract"),      # SOW / Order Form -> MSA
    "evidenced_by": ("Contract", "Document"),
    "bills":        ("Contract", "BillingLine"),
    "has_finding":  ("Contract", "Finding"),
    "recorded_in":  ("Contract", "LedgerEntry"),
}

# 3. Action types: (actor, allowed from-states, to-state or None if the action decides)
ACTIONS = {
    "start_review":  ("Legal and BD",       ["DRAFT"],     "IN_REVIEW"),
    "execute":       ("Partner",            ["IN_REVIEW"], "SIGNED"),
    "submit":        ("BD",                 ["SIGNED"],    "SUBMITTED"),
    "lock_and_bill": ("Billing Automation", ["SUBMITTED"], "LOCKED"),
    "validate":      ("AI Validator",       ["LOCKED"],    None),   # PENDING_FINANCE | REVIEW_NEEDED
}
SIGNED_OR_LATER = {"SIGNED", "SUBMITTED", "LOCKED", "PENDING_FINANCE", "REVIEW_NEEDED"}
# Actions the owners delegated to the AI agent. Signing and negotiating stay with people.
DELEGATED_TO_AGENT = {"submit", "lock_and_bill", "validate"}

# 4. Document binding: label in the signed PDF -> Contract property ---------------
DOC_BINDING = {"Effective Date": "effective_date", "Expiration Date": "end_date",
               "Governing Law": "governing_law", "Payment Terms": "payment_terms",
               "Total Contract Value": "total_value"}
SEVERITY = {"total_value": "HIGH", "billing_lines": "HIGH", "payment_terms": "HIGH",
            "end_date": "MEDIUM", "effective_date": "MEDIUM", "governing_law": "MEDIUM"}


def cast(dtype: str, text: str):
    """Turn a printed value into its ontology type."""
    text = text.strip()
    if text in ("", "-"):
        return None
    if dtype == "date":
        return datetime.strptime(text, "%B %d, %Y").date().isoformat()
    if dtype == "money":
        return float(text.replace("$", "").replace(",", ""))
    return text


def show(dtype: str, value) -> str:
    """Print a value the way a contract does."""
    if value is None:
        return "-"
    if dtype == "date":
        return datetime.fromisoformat(value).strftime("%B %d, %Y")
    if dtype == "money":
        return f"${value:,.2f}"
    return str(value)


# Store -------------------------------------------------------------------------
class RuleViolation(Exception): ...
class LockedRecord(Exception): ...


class Ontology:
    def __init__(self):
        self.objects = {t: {} for t in OBJECTS}
        self.links: set[tuple] = set()
        self.audit: list[dict] = []

    def put(self, otype: str, key: str, actor: str = "system", **props) -> dict:
        unknown = set(props) - set(OBJECTS[otype])
        if unknown:
            self._log(actor, "blocked_edit", f"{otype}:{key}", {"unknown": sorted(unknown)})
            raise ValueError(f"{otype} has no properties {unknown}")
        rec = self.objects[otype].get(key)
        if rec and rec.get("_locked"):
            self._log(actor, "blocked_edit", f"{otype}:{key}", props)
            raise LockedRecord(f"{otype} {key} is locked")
        if rec and "status" in props:
            self._log(actor, "blocked_edit", f"{otype}:{key}", props)
            raise RuleViolation(f"{otype} status changes only through actions")
        if rec is None:
            rec = self.objects[otype][key] = {"id": key}
        rec.update(props)                                  # update in place: references stay valid
        self._log(actor, "update" if len(rec) > len(props) + 1 else "create", f"{otype}:{key}", props)
        return rec

    def get(self, otype: str, key: str) -> dict:
        return self.objects[otype][key]

    def link(self, name: str, src: str, dst: str) -> None:
        self.links.add((name, src, dst))

    def out(self, name: str, src: str) -> list[dict]:
        target = LINKS[name][1]
        return [self.objects[target][d] for n, s, d in sorted(self.links) if n == name and s == src]

    def lock(self, otype: str, key: str, actor: str) -> None:
        assert otype in LOCKABLE
        self.objects[otype][key]["_locked"] = True
        self._log(actor, "lock", f"{otype}:{key}", {})

    def act(self, action: str, key: str, effect=None, by: str | None = None) -> str:
        owner, allowed, to = ACTIONS[action]
        actor = by or owner
        if by and action not in DELEGATED_TO_AGENT:
            self._log(by, "blocked_action", f"Contract:{key}", {"action": action, "owner": owner})
            raise RuleViolation(f"{by} may not perform {action}; it belongs to {owner}")
        c = self.objects["Contract"][key]
        if c["status"] not in allowed:
            self._log(actor, "blocked_action", f"Contract:{key}", {"action": action, "status": c["status"]})
            raise RuleViolation(f"{action} not allowed from {c['status']}")
        try:
            result = effect(self, c) if effect else None  # effect may raise RuleViolation
        except RuleViolation as e:
            self._log(actor, "blocked_action", f"Contract:{key}", {"action": action, "rule": str(e)})
            raise
        new = to or result
        c["status"] = new                                 # status moves even on locked records
        self._log(actor, action, f"Contract:{key}", {"to": new})
        return new

    def _log(self, actor, event, obj, detail):
        self.audit.append({"actor": actor, "event": event, "object": obj, "detail": detail})
