#!/usr/bin/env python3
"""PreCompact hook. Jev cannot write the summary; it decides what the summary must not lose.

Reads the transcript, judges every user prompt and assistant conclusion (tool
results are skipped: decisions live in what people said and what the model
concluded, not in build output), and writes the keep-set to
  ${CLAUDE_PLUGIN_DATA}/keep/<session_id>.md
which sessionstart_reinject.py prints back into context after the compaction.
Side-effect only, as the PreCompact contract requires. Fails open: no key or a
dead Jev means no keep file and an ordinary compaction.
"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev, transcript

# Small batches on purpose. Measured live on a 923-segment session (2026-09-19):
# 120 segments x 2500 chars per request kept 5 lines, mostly narration; 20
# segments x 600 chars kept 23 and the top of the list was the real decisions.
# Jev's own docs say accuracy falls as unrelated state grows, and it shows.
BATCH_CHARS = 14_000
MAX_PER_BATCH = 20
SEGMENT_CHARS = 600       # a decision is short; the rest of a long message is context rot
MIN_SEGMENT_CHARS = 25    # "keep going", "ok", "yes" carry nothing to preserve
KEEP_MIN = 0.75
KEEP_MAX = 40
NARRATION_START = __import__("re").compile(r"^(Let me|Now |Next,? |Running |Checking |Looking |Reading |Starting |I'll |Merged|Pushed|Done\.)", __import__("re").I)


def keep_dir():
    d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/jev-hooks")
    d = os.path.join(d, "keep"); os.makedirs(d, exist_ok=True); return d


def judge(batch):
    state = {"segments": [{"id": s["i"], "role": s["role"], "text": s["text"]} for s in batch]}
    q = {}
    for n, s in enumerate(batch):
        q[f"k{s['i']}"] = jev.noul(
            f"Is `segments[{n}].text` something a future turn must still honour: a decision that constrains later work, "
            f"a user correction or stated preference, a constraint or rule, or a still-open question the user asked?",
            true="'use X not Y', 'never do Z', 'the plan is A then B', a correction of a mistake, a question not yet answered, a fact only the user could know",
            false="a progress narration, a greeting, a tool call description, an explanation the user did not act on, a superseded plan")
        q[f"n{s['i']}"] = jev.noul(
            f"Is `segments[{n}].text` narration of what the assistant is about to do or is doing right now — a progress update, a transition, or an announcement of the next step — rather than a conclusion or decision?",
            true="'let me check', 'now the test', 'running the build', 'next I will', 'merged, moving on'",
            false="a stated decision, a rule, a correction, a finding with its reason, a question the user asked")
        q[f"x{s['i']}"] = jev.noul(
            f"Has `segments[{n}].text` been resolved, superseded or completed later in the same list, so keeping it would mislead?",
            true="an error that was fixed later, a plan replaced by a later decision, a question answered later",
            false="a standing rule, a decision that still holds, an open question")
    a = jev.ask(state, q)
    if not a:
        return []
    out = []
    for s in batch:
        k = a.get(f"k{s['i']}", {}).get("noul", 0); x = a.get(f"x{s['i']}", {}).get("noul", 0)
        nar = a.get(f"n{s['i']}", {}).get("noul", 0)
        if k >= KEEP_MIN and x < 0.6 and nar < 0.6:
            out.append((k - x - nar, s))
    return out


def main():
    inp = jev.read_stdin()
    path = inp.get("transcript_path"); sid = inp.get("session_id") or "unknown"
    if not path or not os.environ.get("TYPESAFE_API_KEY"):
        return
    segs = [s for s in transcript.segments(path) if s["kind"] in ("prompt", "assistant") and len(s["text"]) >= MIN_SEGMENT_CHARS
            and not (s["kind"] == "assistant" and NARRATION_START.match(s["text"]))]
    for s in segs:
        s["text"] = s["text"][:SEGMENT_CHARS]
    if not segs:
        return
    batches = []
    for b in transcript.batches(segs, BATCH_CHARS):
        for i in range(0, len(b), MAX_PER_BATCH):
            batches.append(b[i:i + MAX_PER_BATCH])
    with ThreadPoolExecutor(max_workers=4) as ex:
        kept = [item for res in ex.map(judge, batches) for item in res]
    if not kept:
        return
    kept.sort(key=lambda t: -t[0])
    kept = sorted(kept[:KEEP_MAX], key=lambda t: t[1]["i"])   # top N, then back in transcript order
    lines = ["## Preserved across compaction (jev-hooks triage)",
             "These are decisions, corrections, constraints and open questions judged still binding. Honour them; do not re-ask.", ""]
    for _, s in kept:
        who = "user" if s["role"] == "user" else "assistant"
        lines.append(f"- ({who}) " + s["text"].replace("\n", " ")[:400])
    with open(os.path.join(keep_dir(), f"{sid}.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
