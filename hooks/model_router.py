#!/usr/bin/env python3
"""PreToolUse hook (matcher: Agent). Picks the model a subagent runs on.

The session's own model cannot be changed by a hook — no hook event carries a
model field, and `/model` is the only mid-session switch. A SUBAGENT's model
can: the Agent tool takes `model` as an input, a per-invocation model beats
both the definition's `model:` and CLAUDE_CODE_SUBAGENT_MODEL, and PreToolUse
`updatedInput` rewrites a tool's input before it runs. Verified end to end
2026-09-21: a spawn asking for `opus`, rewritten here, ran on
claude-haiku-4-5 (the subagent transcript's own `message.model`).

So: one Choice over the subagent's task, and the fork that greps for a file
runs on haiku while the one that has to make a test pass keeps opus.

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
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

PROMPT_CHARS = 4_000
DEFAULT_MODELS = ("haiku", "sonnet", "opus", "fable")

# What each model is FOR, in the terms the task itself is written in. These are
# read by the judge, so they describe the work, never the model's marketing.
CRITERIA = {
    "opus": "implementation and debugging in a real repository: write or change code, make a failing test pass, "
            "work through a multi-step change where a wrong step is expensive to unwind",
    "fable": "planning, design and judgment about a system: compare approaches, weigh tradeoffs, review a decision, "
             "write a spec, a plan or prose explaining how something should work",
    "sonnet": "mechanical work on this machine: gather context, search a codebase, summarise what exists, run known "
              "commands, edit config, follow a procedure that is already decided",
    "haiku": "a small lookup or transformation where a mistake is obvious and cheap: find a file, list values, "
             "extract a field, reformat something short",
}


# Declining is not neutral. A spawn this hook leaves alone inherits the PARENT's
# model, which is the expensive one — so "unsure" must not mean "use Opus".
# Measured against the live judge 2026-09-21: "list the files in this directory"
# split haiku 0.52 / sonnet 0.48. The judge was not unsure whether the task was
# cheap; it was unsure WHICH cheap model. That is still an answer.
CHEAP = ("haiku", "sonnet")


def choose(probs, models, floor, tier):
    """The model to route to, or None to leave the spawn alone."""
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


def allowed():
    raw = os.environ.get("JEV_HOOKS_ROUTER_MODELS")
    names = [m.strip() for m in raw.split(",")] if raw else list(DEFAULT_MODELS)
    return [m for m in names if m in CRITERIA]


def envfloat(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def debug(msg):
    if os.environ.get("JEV_HOOKS_DEBUG"):
        sys.stderr.write(msg + "\n")


def main():
    if os.environ.get("JEV_HOOKS_ROUTER", "").lower() in ("off", "0", "false"):
        return
    inp = jev.read_stdin()
    if inp.get("tool_name") != "Agent":
        return
    ti = inp.get("tool_input") or {}
    task = (ti.get("prompt") or "").strip()
    if not task:
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
    ans = a.get("model") or {}
    probs = ans.get("probabilities") or ({ans.get("choice"): 1.0} if ans.get("choice") else {})
    floor, tier = envfloat("JEV_HOOKS_ROUTER_MIN", 0.5), envfloat("JEV_HOOKS_ROUTER_TIER", 0.7)
    pick = choose(probs, models, floor, tier)
    conf = float(probs.get(pick) or 0)
    if not pick:
        debug(f"no route: probs={probs} floor={floor} tier={tier}")
        return
    if pick == chosen:
        return                       # already going there; say nothing
    out = dict(ti)
    out["model"] = pick
    jev.record("model_router", pick, a, note=f"confidence={conf:.2f}", agent_type=state["agent_type"])
    print(json.dumps({
        "systemMessage": f"jev-hooks: routing this subagent to {pick} ({conf:.2f}).",
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": out}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook bug must never trap a spawn
