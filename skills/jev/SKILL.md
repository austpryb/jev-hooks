---
name: jev
description: Use TypeSafe's Jev (System One) to make a bounded judgment — is this report true, are these two facts the same, how risky is this change — from a hook, a service or the shell. Use when asked to judge, verify, classify, dedupe, rank or gate something on meaning rather than on a rule, or when working on the jev-hooks plugin or enforcer-graph's judgment features.
---

# Jev: a judge, not a writer

Jev answers bounded questions about content you give it, in about 300 ms, for a
fraction of a cent. It returns calibrated probabilities. It does **not** generate
text, summarise, extract or write code. If the task needs prose, Jev is the wrong
tool; if the task is "decide something about this content", it is the right one.

Three question types, and nothing else:

| Type | Returns | Use for |
|---|---|---|
| `noul` | one probability, 0 to 1 | a yes/no judgment: is this criterion met, do these contradict |
| `choice` | one option of up to 255, with probabilities | one-of-N: which kind of change is this, which skill fits |
| `score` | a level 0..N-1 (2-10 levels) with probabilities | a graded judgment: how risky, how much effort |

## How to call it

From the shell, which is how an agent should reach for it:

```bash
bin/jev noul "Does the report show that every acceptance criterion was met, with evidence?" --state-file report.md
bin/jev score "How risky is this change if it is wrong?" --level "contained" --level "spreads" --level "structural" --state-file diff.txt
bin/jev ask questions.json --state-file state.txt     # several questions, ONE call
```

Exit 3 means the judge could not be reached. That is not an error to retry into
the ground; it means "no opinion", and the caller carries on.

From Python, inside this plugin: `sys.path.insert(0, .../lib)` then
`jev.ask(state, {"id": jev.noul(...)})`. It returns `None` on any failure.

## The rules that are not obvious

**Ask several questions in one call.** Questions in a request are independent and
evaluated in parallel; adding one costs its own tokens and almost no latency. Four
questions in one call beats four calls every time.

**Keep the state small and structured.** Accuracy falls as unrelated content grows
— pass the report and the criteria, not the whole transcript. Name a field in the
question with backticks (`` `segments[3].text` ``) when the state is an object.

**Code decides; Jev only judges.** Put thresholds, ordering and control flow in
code. In enforcer-graph the blast radius is a SQL count and the priority formula
is arithmetic; only risk and effort are judged. Never ask Jev what to do next.

**Fail open, always.** No key, a timeout, a 429 or a 529 must leave the caller
behaving exactly as if the judge were absent. Every hook here does this and it is
not negotiable: a judgment service must never be why a session stalls.

**Write criteria for the false positives, not the true ones.** This is where the
work is. Every question needs a `true` and a `false` list naming the cases that
look like the answer but are not. Five rounds of tuning in one day were all of
this shape:
- a stop check flagged "the PR needs your merge" as a broken promise, until the
  criterion excepted *a next step blocked on something only the user can do*;
- it flagged "the roll will show 200 instead of 404" as an unevidenced claim,
  until the criterion excepted *an outcome expected from a check not yet run*;
- a verifier judged an agent against the wrong criteria because it read a
  compaction summary as the task.

Assume your first wording is wrong. Test both directions: the case that must
fire and the case that must not.

**Record the decision.** Call `jev.record(hook, decision, answers, **extra)` on
every path, including the silent one. Without the probabilities behind a verdict
there is no way to move a threshold except by whoever complains loudest.
`bin/stats.py` reports the 0.60-0.80 band where verdicts flip on wording;
`bin/wrong.py` marks one wrong, which is the only labelled data there is.

**Pin the model.** `jev-latest` moves. A pinned version keeps tuned thresholds
meaningful; moving the pin is a deliberate act that re-checks them.

## What must never be built

**No pass-through.** The licence permits embedding Jev in a product for its users
and forbids offering it as a standalone service. Every call must be a verb of
ours — verify this report, dedupe this observation, rank this frontier — never a
generic "ask Jev" tool exposed to a caller. The CLI above is a developer tool in
a private repo, not a product surface.

**No published comparisons.** The licence forbids publishing benchmarks or
performance comparisons against other models. Our own latency and cost numbers
are fine; "Jev is N times faster than model X" is not, in docs, demos or READMEs.

**No content you would not send to a third party.** Inputs are stored by default,
US-hosted, not used for training, with no stated retention period. The edit gate
sends a path, a git verdict and byte counts and never file content, precisely
because the file most likely to reach it is the one most likely to hold a secret.
Judgment is off per tenant by default in enforcer-graph for the same reason.

## Where Jev already runs here

Server-side in enforcer-graph, on a platform key with per-tenant opt-in and
metering: `verify` a run's report against a node's acceptance criteria, `align`
a new observation against recent ones for duplicates and contradictions, `rank`
the frontier by risk and effort, and `rejudge` runs left unverified.

Client-side in this plugin, on the developer's own key: a Bash gate, an edit gate,
output narrowing, compaction triage, subagent verification, a stop self-check,
loop detection and prompt routing. `README.md` has the table and the live numbers.
