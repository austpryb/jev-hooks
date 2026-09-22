#!/usr/bin/env python3
"""UserPromptSubmit hook. One line of context about what kind of prompt this is.

The rule the harness already has ("a question is answered, not acted on; an
approval means proceed") is one models still miss. One Choice over the prompt
alone — never the transcript — says which it is, and a second Choice, when the
machine lists skills, says which skill is the right tool. Both are hints
printed to stdout, which the harness adds to the model's context.

Skips the call (exit 0, nothing printed) for: prompts under MIN_CHARS, slash
commands, prompts that are entirely <tag>...</tag> blocks (bash-input etc.).
Fails open on everything else: no key, timeout, bad JSON -> silence.
"""
import json, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev, models, transcript

MIN_CHARS = 12
CONFIDENCE = 0.7
MAX_SKILLS = 100
PROMPT_CHARS = 4_000

# The session's own model is the one thing a hook cannot change: no hook event
# carries a model field, and `/model` is the only mid-session switch. So this
# says so and stops. It rides on the call this hook already makes — one more
# question, no extra round trip.
#
# haiku is not offered by default. Driving a whole session on it is a decision
# worth typing, not one worth being nudged into; the router already sends the
# cheap FORKS there without anyone typing anything.
HINT_MODELS = ("opus", "fable", "sonnet")
HINT_MIN = 0.7           # advice interrupts a human: be surer than the router is

HINTS = {
    "question": "jev-hooks: this reads as a question — answer and report; do not change files unless asked.",
    "approval": "jev-hooks: this reads as approval of the pending proposal — proceed with it.",
    "thinking_aloud": "jev-hooks: this reads as thinking aloud — respond to the idea; do not start work.",
}

# A prompt made entirely of <tag>...</tag> blocks (bash-input and the like) is
# harness plumbing, not a message. This used to be one anchored regex,
# ^\s*(<[^>]+>[\s\S]*?</[^>]+>\s*)+$, which backtracks EXPONENTIALLY when tag
# blocks are followed by ordinary text - a pasted HTML list plus a question.
# Measured 2026-09-21: 20 tags 0.73s, 24 tags ~15s, far past the 5s the harness
# allows. Stripping matched pairs and checking what is left is linear enough.
TAG_BLOCK = re.compile(r"<([A-Za-z][\w-]*)[^>]*>[\s\S]*?</\1>")


def is_wrapped(p):
    return TAG_BLOCK.sub("", p[:2 * PROMPT_CHARS]).strip() == "" and p.lstrip().startswith("<")


def debug(msg):
    if os.environ.get("JEV_HOOKS_DEBUG"):
        sys.stderr.write(msg + "\n")


def skill_names(cwd):
    """Directory names under ~/.claude/skills and the nearest <dir>/.claude/skills
    walking up from cwd. Names only. None when there are none or too many."""
    dirs = [os.path.expanduser("~/.claude/skills")]
    d = os.path.abspath(cwd or os.getcwd())
    while True:
        cand = os.path.join(d, ".claude", "skills")
        if os.path.isdir(cand):
            dirs.append(cand)
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    names = set()
    for sd in dirs:
        try:
            for n in os.listdir(sd):
                if os.path.isdir(os.path.join(sd, n)) and not n.startswith("."):
                    names.add(n)
        except Exception:
            pass
    names.discard("none")
    if not names or len(names) > MAX_SKILLS:
        return None
    return sorted(names)


def _hint_path(sid):
    d = os.path.join(os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/jev-hooks"), "model_hint")
    return os.path.join(d, f"{jev.safe_id(sid or 'unknown')}.json")


def forget_hint(sid):
    """The session is already on the model this hook would point at: whatever
    it was told before has been acted on, so the next mismatch is news again."""
    try:
        os.remove(_hint_path(sid))
    except OSError:
        pass


def repeated(sid, want, current):
    """True when this session was already told to switch to `want` WHILE ON
    `current`. Advice repeated every prompt is noise, and the second telling
    never persuades anyone the first did not. But the state is keyed on the
    model it was given on too: keyed on `want` alone, it never reset - a user
    who took the advice and later moved off it was never told again for the
    rest of the session. Any failure here counts as 'not repeated'."""
    d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/jev-hooks")
    d = os.path.join(d, "model_hint")
    try:
        os.makedirs(d, exist_ok=True)
        jev.prune(d)
        p = os.path.join(d, f"{jev.safe_id(sid or 'unknown')}.json")
        try:
            st = json.load(open(p))
        except Exception:
            st = {}
        if st.get("last") == want and st.get("on") == current:
            return True
        json.dump({"last": want, "on": current}, open(p, "w"))
    except Exception:
        return False
    return False


def main():
    t0 = time.time()
    inp = jev.read_stdin()
    prompt = inp.get("prompt")
    if not isinstance(prompt, str):
        return
    p = prompt.strip()
    if len(p) < MIN_CHARS or p.startswith("/") or is_wrapped(p):
        debug("skip")
        return
    state = {"prompt": p[:PROMPT_CHARS]}
    q = {
        "kind": jev.choice(
            "What kind of message is `prompt`, from a user to a coding assistant that can also change files, run commands and merge code?",
            {
                "question": "asks how, what, why or whether; wants an answer or explanation; requests no change to files, config or infrastructure",
                "change_request": "asks for code, files, configuration or infrastructure to be created, changed, fixed, run, rolled or merged — an instruction to act",
                "thinking_aloud": "describes an idea, a worry or a problem without asking for anything to be done; musing, reflecting, weighing options",
                "approval": "says yes, go, do it, ship it, merge it, sounds good — consent to a proposal that was already on the table",
                "other": "greetings, status pings like 'keep going', pasted logs, anything that fits none of the above",
            }),
    }
    # Strip BEFORE keeping: "opus, sonnet" kept " sonnet", CRITERIA[" sonnet"]
    # raised, and the outer except swallowed every hint - kind and skill too.
    hint_models = [m.strip() for m in os.environ.get("JEV_HOOKS_HINT_MODELS", ",".join(HINT_MODELS)).split(",")
                   if m.strip() in models.CRITERIA]
    current = models.alias_of(transcript.last_model(inp.get("transcript_path") or ""))
    if current and len(hint_models) > 1 and os.environ.get("JEV_HOOKS_HINT", "").lower() not in ("off", "0", "false"):
        q["model"] = jev.choice(
            "Which model should answer `prompt`? Judge the WORK it asks for, not how long the text is: a short "
            "sentence can ask for a hard change, and a long brief can ask for a file listing.",
            {m: models.CRITERIA[m] for m in hint_models})
    skills = skill_names(inp.get("cwd"))
    if skills:
        crit = {n: None for n in skills}
        crit["none"] = "no listed skill is the right tool for this prompt, or the prompt is not about a task a skill covers"
        q["skill"] = jev.choice("Which listed skill, if any, is the right tool for `prompt`? Choose by the skill's name; choose none unless a name clearly matches what the prompt asks for.", crit)
    # The harness kills this hook at 5s (hooks.json); the library default is 6s,
    # so a slow answer arrived after the kill. Leave room for startup.
    a = jev.ask(state, q, retries=0, timeout=3.5)
    if not a:
        return
    parts = []
    k = a.get("kind") or {}
    if k.get("choice") in HINTS and float(k.get("confidence") or 0) >= CONFIDENCE:
        parts.append(HINTS[k["choice"]])
    s = a.get("skill") or {}
    if s and s.get("choice") and s["choice"] != "none" and float(s.get("confidence") or 0) >= CONFIDENCE:
        parts.append(f"Relevant skill: {s['choice']}.")
    if a.get("model"):
        probs = models.probabilities(a["model"])
        want = models.choose(probs, hint_models, models.envfloat("JEV_HOOKS_HINT_MIN", HINT_MIN), 1.1)
        if want and want == current:
            forget_hint(inp.get("session_id"))
        if want and want != current and not repeated(inp.get("session_id"), want, current):
            parts.append(f"This reads as {models.CRITERIA[want].split(':')[0]} — {current} is running it; "
                         f"`/model {want}` fits better (p={float(probs.get(want) or 0):.2f}).")
    debug(f"ms={int((time.time() - t0) * 1000)} kind={k.get('choice')}:{k.get('confidence')} skill={s.get('choice')}:{s.get('confidence')}")
    if parts:
        jev.record("prompt_routing", "hint", a, note=" ".join(parts)[:160])
        print(" ".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
