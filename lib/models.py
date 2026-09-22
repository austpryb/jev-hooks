"""What each model is for, and how to pick one. Shared by the two hooks that
choose a model so they cannot drift: `model_router` rewrites a subagent's model
outright, `prompt_routing` can only suggest one for the session — a hook cannot
change the session's own model, and `/model` is the only mid-session switch.

The criteria are read by the judge, so they describe the WORK, never the
model's marketing.
"""
import json, os

def _criteria():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry.json")) as f:
            return json.load(f)["criteria"]
    except Exception:
        return {}


# Defined once in registry.json, which the claude-loadout binary also reads.
CRITERIA = _criteria()

# Declining is not neutral where a subagent is concerned: a spawn left alone
# inherits the PARENT's model, which is the expensive one. Measured against the
# live judge 2026-09-21, "list the files in this directory" split haiku 0.52 /
# sonnet 0.48 — not doubt that the task is cheap, only which cheap model.
CHEAP = ("haiku", "sonnet")


def alias_of(model_id):
    """'claude-haiku-4-5-20251001' -> 'haiku'. None for anything unrecognised:
    a model this plugin has never heard of is not one it should reason about."""
    m = (model_id or "").lower()
    for name in CRITERIA:
        if name in m:
            return name
    return None


def envfloat(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def choose(probs, models, floor, tier):
    """The model to route to, or None to leave the choice alone."""
    ranked = sorted(((m, float(p)) for m, p in (probs or {}).items() if m in models), key=lambda kv: -kv[1])
    if not ranked:
        return None
    top, top_p = ranked[0]
    if top_p >= floor:
        return top
    cheap = [(m, p) for m, p in ranked if m in CHEAP]
    if sum(p for _, p in cheap) >= tier:
        return cheap[0][0]        # sure it is cheap, unsure which: take the likelier
    return None


def probabilities(answer):
    """A choice answer's distribution, falling back to its single pick."""
    a = answer or {}
    return a.get("probabilities") or ({a["choice"]: 1.0} if a.get("choice") else {})
