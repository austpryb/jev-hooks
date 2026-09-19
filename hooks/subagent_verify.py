#!/usr/bin/env python3
"""SubagentStop hook. Judges the subagent's final report against the task it was
given, so the orchestrator stops trusting a self-graded "done".

Input (contract): transcript_path is the SUBAGENT's transcript; last_assistant_message
is its final text; stop_hook_active is true when this hook already sent it back once.
Output: {"decision": "block", "reason": "..."} keeps the subagent working with the
reason as feedback. Blocks at most once per agent (stop_hook_active), and never
when Jev is unavailable.

Criteria come from the prompt: bulleted or numbered lines, else sentences with
"must", else the whole prompt as one criterion. Jev reads each against the report.
"""
import json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev, transcript

MAX_CRITERIA = 14
BLOCK_BELOW = 0.35
EVIDENCE_BELOW = 0.4


def criteria_from(prompt):
    lines = [l.strip() for l in prompt.splitlines()]
    items = [re.sub(r"^(\d+[.)]|[-*•])\s+", "", l) for l in lines if re.match(r"^(\d+[.)]|[-*•])\s+\S", l)]
    items = [i for i in items if len(i) > 25]
    if not items:
        items = [s.strip() for s in re.split(r"(?<=[.!?])\s+", prompt) if " must " in f" {s} " or s.lower().startswith("must ")]
    if not items:
        items = [prompt.strip()[:1500] or "the task as described was completed"]
    return items[:MAX_CRITERIA]


def main():
    inp = jev.read_stdin()
    if inp.get("stop_hook_active"):
        return
    report = (inp.get("last_assistant_message") or "").strip()
    path = inp.get("transcript_path")
    if not report and path:
        report = transcript.last_assistant_text(path)
    prompt = transcript.first_prompt(path) if path else ""
    if not report or not prompt:
        return
    crit = criteria_from(prompt)
    state = {"task": prompt[:20_000], "criteria": crit, "report": report[:20_000]}
    q = {f"c{i}": jev.noul(
            f"Does `report` show that `criteria[{i}]` was actually done, with concrete evidence such as file paths, commands run, "
            f"test output, or URLs, rather than a bare assertion?",
            true="the report names what was produced and how it was checked", false="the criterion is not mentioned, is only asserted, or is reported as not done")
         for i in range(len(crit))}
    q["evidence"] = jev.noul("Does `report` cite verifiable evidence for its main claims (test output, a PR or commit, file paths, measured numbers)?")
    q["honest"] = jev.noul("Does `report` explicitly say what was not done, skipped, or could not be verified, if anything?")
    a = jev.ask(state, q)
    if not a:
        return
    weak = [(crit[i], a[f"c{i}"]["noul"]) for i in range(len(crit)) if a[f"c{i}"]["noul"] < BLOCK_BELOW]
    ev = a["evidence"]["noul"]
    if not weak and ev >= EVIDENCE_BELOW:
        return
    parts = []
    if weak:
        parts.append("these criteria from your task are not shown as done with evidence: " + "; ".join(f"[{p:.2f}] {c[:160]}" for c, p in weak))
    if ev < EVIDENCE_BELOW:
        parts.append(f"the report asserts outcomes without verifiable evidence (p={ev:.2f})")
    reason = ("jev-hooks verification: " + " | ".join(parts) +
              ". Either do the missing work and report the evidence, or state plainly that it was not done and why. Do not restate the same report.")
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
