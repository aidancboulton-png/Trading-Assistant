"""Rules engine + memory store.

Rules are simple JSON predicates evaluated against the prediction context.
After a loss, the post-mortem agent can call `add_rule(...)` to install
a hard block so the same mistake doesn't repeat.

Rule shape:
{
  "scope":     "global" | "domain:prediction_market" | "market:fed-rate-...",
  "condition": {"feature": "geopolitical_flag", "op": "==", "value": true},
  "action":    "block" | "shrink:0.5" | "warn",
  "reason":    "Geopolitical flag was triggered 2 days prior to last loss."
}
"""
from __future__ import annotations
from typing import Any
from . import storage


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">":  lambda a, b: a is not None and b is not None and a > b,
    "<":  lambda a, b: a is not None and b is not None and a < b,
    ">=": lambda a, b: a is not None and b is not None and a >= b,
    "<=": lambda a, b: a is not None and b is not None and a <= b,
    "in": lambda a, b: a in b,
}


def _scope_matches(scope: str, ctx: dict) -> bool:
    if scope == "global":
        return True
    if scope.startswith("domain:"):
        return ctx.get("domain") == scope.split(":", 1)[1]
    if scope.startswith("market:"):
        return ctx.get("market") == scope.split(":", 1)[1]
    return False


def evaluate(ctx: dict) -> dict:
    """Return {action: 'allow'|'block'|'shrink'|'warn', factor, reasons[]}."""
    decisions = {"action": "allow", "factor": 1.0, "reasons": []}
    for r in storage.active_rules():
        rule = r["rule"]
        if not _scope_matches(rule.get("scope", "global"), ctx):
            continue
        cond = rule.get("condition", {})
        feat = cond.get("feature"); op = cond.get("op"); val = cond.get("value")
        actual = ctx.get(feat)
        if op not in _OPS:
            continue
        if _OPS[op](actual, val):
            action = rule.get("action", "warn")
            reason = rule.get("reason", "")
            decisions["reasons"].append(f"[{r['rule_key']}] {reason}")
            if action == "block":
                decisions["action"] = "block"
                return decisions
            if action.startswith("shrink:"):
                try:
                    f = float(action.split(":", 1)[1])
                    decisions["action"] = "shrink"
                    decisions["factor"] = min(decisions["factor"], f)
                except ValueError:
                    pass
            elif action == "warn" and decisions["action"] == "allow":
                decisions["action"] = "warn"
    return decisions


def add_rule(key: str, scope: str, condition: dict, action: str,
             reason: str, source_incident_id: int | None = None) -> None:
    storage.add_rule(
        key,
        {"scope": scope, "condition": condition, "action": action, "reason": reason},
        source_incident_id,
    )
