#!/usr/bin/env python3
"""PreToolUse hook (matcher: Agent). Picks the model a subagent runs on.

The session's own model cannot be changed by a hook — no hook event carries a
model field, and `/model` is the only mid-session switch. A SUBAGENT's model
can: the Agent tool takes `model` as an input, a per-invocation model beats
both the definition's `model:` and CLAUDE_CODE_SUBAGENT_MODEL, and PreToolUse
`updatedInput` rewrites a tool's input before it runs. Verified end to end
2026-09-21: a spawn asking for `opus`, rewritten here, ran on
claude-haiku-4-5 (the subagent transcript's own `message.model`).

So: one Choice over the subagent's task, and the worker that greps for a file
runs on haiku while the one that has to make a test pass keeps opus. (Not a
fork: a fork runs on its parent's model whatever its input says.)

Routes only when the caller left `model` unset or `inherit` — an explicit
model is a deliberate choice and is left alone unless JEV_HOOKS_ROUTER_FORCE=1.
Writes only `updatedInput`, never a permissionDecision: this hook rewrites an
input, it does not decide what is allowed to run. Fails open everywhere — no
key, a timeout, a low-confidence answer, an unknown model name all leave the
spawn exactly as it was.

  JEV_HOOKS_ROUTER=off       disable
  JEV_HOOKS_ROUTER_MODELS    allowed picks (default haiku,sonnet,opus,fable)
  JEV_HOOKS_ROUTER_MIN       probability the top pick needs to route (default 0.5)
  JEV_HOOKS_ROUTER_TIER      probability mass on haiku+sonnet that routes to the
                             likelier of the two when no single pick clears MIN
                             (default 0.7) — declining sends the spawn to the
                             parent's model, which is the expensive one
  JEV_HOOKS_ROUTER_FORCE=1   route even when the caller named a model
  JEV_HOOKS_ROUTER_TYPES     subagent types to route (default general-purpose).
                             A typed agent can define its own model, and a
                             route beats that definition; a fork is never
                             routed - it runs on its parent's model regardless
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

PROMPT_CHARS = 4_000
DEFAULT_MODELS = ("haiku", "sonnet", "opus", "fable")
DEFAULT_TYPES = ("general-purpose",)     # an unset subagent_type IS general-purpose

# One definition of what each model is for, shared with the session-model hint
# in prompt_routing so the two cannot drift.
from models import CRITERIA, CHEAP, choose, envfloat, probabilities   # noqa: E402


def routed_types():
    raw = os.environ.get("JEV_HOOKS_ROUTER_TYPES")
    names = [t.strip() for t in raw.split(",") if t.strip()] if raw else list(DEFAULT_TYPES)
    return [t for t in names if t != "fork"]


def allowed():
    raw = os.environ.get("JEV_HOOKS_ROUTER_MODELS")
    names = [m.strip() for m in raw.split(",")] if raw else list(DEFAULT_MODELS)
    return [m for m in names if m in CRITERIA]


def debug(msg):
    if os.environ.get("JEV_HOOKS_DEBUG"):
        sys.stderr.write(msg + "\n")


def main():
    if jev.disabled("model_router"):
        return                       # switched off for this session (JEV_HOOKS_DISABLE)
    if os.environ.get("JEV_HOOKS_ROUTER", "").lower() in ("off", "0", "false"):
        return
    inp = jev.read_stdin()
    if inp.get("tool_name") != "Agent":
        return
    ti = inp.get("tool_input") or {}
    task = (ti.get("prompt") or "").strip()
    if not task:
        return
    kind = (ti.get("subagent_type") or "general-purpose").strip()
    # A fork runs on its parent's model whatever its input says. Observed
    # 2026-09-21: rewritten to haiku here, the fork ran on claude-opus-5 and its
    # own meta.json recorded "model": "inherit" — a fork shares the parent's
    # context, and that pins its model. Routing one changed nothing except the
    # decision log, which then recorded a route that never happened. Not even
    # FORCE applies: there is nothing to force.
    if kind == "fork":
        debug("fork: runs on the parent's model regardless; not routed")
        return
    # A typed agent may carry its own `model:` - and a per-invocation model
    # beats it. Observed the same day: claude-code-guide runs on haiku by
    # definition, and this hook "routed" it UP to sonnet. Built-in definitions
    # are not on disk to read, so only the untyped worker is routed unless told
    # otherwise.
    if kind not in routed_types():
        debug(f"{kind}: may define its own model; not routed")
        return
    chosen = (ti.get("model") or "").strip().lower()
    if chosen and chosen != "inherit" and os.environ.get("JEV_HOOKS_ROUTER_FORCE") != "1":
        debug(f"caller asked for {chosen}: leaving it alone")
        return
    models = allowed()
    if len(models) < 2:
        return
    state = {"task": task[:PROMPT_CHARS], "description": ti.get("description") or "",
             "agent_type": ti.get("subagent_type") or ""}
    a = jev.ask(state, {"model": jev.choice(
        "Which model should do `task`? Judge the WORK the task asks for, not how long the text is: a short "
        "sentence can ask for a hard change, and a long brief can ask for a file listing.",
        {m: CRITERIA[m] for m in models})}, retries=0)
    if not a:
        return                       # judge unavailable: the spawn is untouched
    probs = probabilities(a.get("model"))
    floor, tier = envfloat("JEV_HOOKS_ROUTER_MIN", 0.5), envfloat("JEV_HOOKS_ROUTER_TIER", 0.7)
    pick = choose(probs, models, floor, tier)
    conf = float(probs.get(pick) or 0)
    # Every path the judge answered on is recorded, the silent ones too: under
    # 0.23.0 only a reroute was, and without the task nothing could be audited.
    # The DESCRIPTION goes on the record, never the prompt - a task brief quotes
    # private code, paths and incident details; the log is local, but it is read
    # and pasted into reviews, and a 120-char label is enough to find the spawn.
    def rec(decision):
        jev.record("model_router", decision, a, note=f"confidence={conf:.2f}",
                   agent_type=state["agent_type"], description=(state["description"] or "")[:120] or None,
                   pick=pick, top=max(probs, key=lambda m: float(probs[m])) if probs else None,
                   dist={m: round(float(p), 3) for m, p in probs.items()})
    if not pick:
        debug(f"no route: probs={probs} floor={floor} tier={tier}")
        rec("no_route")              # the spawn inherits the parent's model
        return
    if pick == chosen:
        rec("already")               # already going there; say nothing
        return
    out = dict(ti)
    out["model"] = pick
    rec(pick)
    print(json.dumps({
        "systemMessage": f"jev-hooks: routing this subagent to {pick} ({conf:.2f}).",
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": out}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook bug must never trap a spawn
