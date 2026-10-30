"""Where the ontology meets AI.

1. Extraction: the prompt is generated from the ontology, and every value the
   model returns passes a guard (known property, correct type) before it can
   touch a record.
2. Agent: the model's only tools are ontology actions. Guards on state,
   links, locks, and delegation decide what actually happens, and every
   attempt lands in the audit log.
3. Cost: every model call is metered by purpose and record.

No API calls. A simulated model stands in for the LLM so the ontology's
role is visible and the demo runs anywhere. Cost is projected from token counts.
"""
import json
import re

from ontology import (ACTIONS, DOC_BINDING, OBJECTS, LockedRecord, RuleViolation, cast)

PRICE_IN, PRICE_OUT = 3.00, 15.00        # example USD per million tokens; set your model's price
AGENT = "AI Agent"
COSTS: list[dict] = []



def meter(purpose: str, ref: str, tin: int, tout: int) -> None:
    COSTS.append({"purpose": purpose, "ref": ref, "tokens": tin + tout,
                  "usd": round(tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT, 6)})


# ------------------------------------------------------------------ 1. extraction
REQUIRED = {"effective_date", "end_date", "governing_law"}


def schema() -> dict:
    """The extraction target is the ontology itself, not a hand-written field list."""
    types = OBJECTS["Contract"]
    return {**{p: types[p] for p in DOC_BINDING.values()}, "billing_lines": "map[milestone_or_sku -> money]"}


def extraction_prompt(text: str) -> str:
    return ("Extract the signed contract into this ontology schema. Return JSON only.\n"
            "Types: date = YYYY-MM-DD, money = number, string = text, null when absent.\n"
            f"Schema: {json.dumps(schema())}\n<contract>\n{text}\n</contract>")


def coerce(dtype: str, value):
    if value in (None, "", "-"):
        return None
    if dtype == "date":
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        return cast("date", str(value))                   # raises ValueError if not a date
    if dtype == "money":
        return float(value) if isinstance(value, (int, float)) else cast("money", str(value))
    return str(value)


def guard(raw: dict) -> tuple[dict, list[str]]:
    """Ontology guard on model output: map labels to properties, enforce types, reject the rest."""
    types, record, notes = schema(), {"billing_lines": {}}, []
    for key, value in raw.items():
        prop = DOC_BINDING.get(key, key)
        if prop == "billing_lines":
            for k, v in (value or {}).items():
                try:
                    record["billing_lines"][k] = coerce("money", v)
                except ValueError:
                    notes.append(f"rejected billing_lines.{k}={v!r}: not a valid money value")
        elif prop not in types:
            notes.append(f"ignored {key!r}: not a Contract property")
        else:
            try:
                record[prop] = coerce(types[prop], value)
            except ValueError:
                notes.append(f"rejected {prop}={value!r}: not a valid {types[prop]} value")
    for p in types:
        record.setdefault(p, None)
        if p in REQUIRED and record[p] is None:
            notes.append(f"missing required {p}")
    return record, notes


def stand_in_extract(text: str) -> dict:
    """Simulated model: returns every 'Label: value' as printed, untyped."""
    raw, lines = {}, {}
    for row in text.splitlines():
        if row.startswith("BILL "):
            key, _, amount = row[5:].split(" | ")
            lines[key] = amount
        elif ": " in row:
            label, value = row.split(": ", 1)
            raw[label] = value
    raw["billing_lines"] = lines
    return raw


def extract(ref: str, text: str) -> tuple[dict, list[str]]:
    prompt = extraction_prompt(text)
    meter("extract", ref, len(prompt) // 4, 150)   # projected tokens
    raw = stand_in_extract(text)                   # simulated model answer
    return guard(raw)


# ------------------------------------------------------------------ 2. agent
def agent_tools() -> list[dict]:
    """Tools are generated from the ontology: one per action type, plus reads and an edit."""
    cid = {"type": "object", "properties": {"contract_id": {"type": "string"}}, "required": ["contract_id"]}
    tools = [{"name": a, "description": f"Ontology action. Owner: {owner}. Allowed from: {allowed}.",
              "input_schema": cid} for a, (owner, allowed, _) in ACTIONS.items()]
    tools += [
        {"name": "list_contracts", "description": "List contracts with type, status, and parent.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_contract", "description": "Read a contract with its findings and parent status.",
         "input_schema": cid},
        {"name": "update_contract", "description": "Set one Contract property.",
         "input_schema": {"type": "object", "properties": {"contract_id": {"type": "string"},
                          "field": {"type": "string"}, "value": {"type": "string"}},
                          "required": ["contract_id", "field", "value"]}},
    ]
    return tools


def run_tool(o, effects: dict, name: str, args: dict) -> str:
    try:
        if name == "list_contracts":
            return json.dumps([{"id": k, "type": c["contract_type"], "status": c["status"],
                                "parent": c.get("parent")} for k, c in o.objects["Contract"].items()])
        cid = args.get("contract_id", "")
        if name == "get_contract":
            c = o.get("Contract", cid)
            parent = o.out("governed_by", cid)
            return json.dumps({**{k: v for k, v in c.items() if not k.startswith("_")},
                               "locked": bool(c.get("_locked")),
                               "parent_status": parent[0]["status"] if parent else None,
                               "findings": o.out("has_finding", cid)}, default=str)
        if name == "update_contract":
            o.put("Contract", cid, AGENT, **{args["field"]: args["value"]})
            return f"OK: {cid}.{args['field']} set"
        if name in ACTIONS:
            return f"OK: {cid} is now {o.act(name, cid, effects.get(name), by=AGENT)}"
        o._log(AGENT, "blocked_action", "Ontology", {"tool": name})
        return f"BLOCKED: {name} is not an action in the ontology"
    except (RuleViolation, LockedRecord, ValueError, KeyError) as e:
        return f"BLOCKED: {e}"



def run_agent(o, effects: dict, plan: list) -> tuple[list, str]:
    """Simulated agent: replays a plan of tool calls through the same tools a model would get."""
    return [(name, args, run_tool(o, effects, name, args)) for name, args in plan], "simulated agent plan"
