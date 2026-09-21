#!/usr/bin/env python3
"""Mark the most recent decision of a hook as wrong.

    python3 bin/wrong.py <hook> [why]
    python3 bin/wrong.py stop_check "the next step was blocked on the user"

A block that was wrong is the only labelled data a threshold can be tuned
against; without it, tuning is whoever complains loudest. This appends a
dispute record naming the decision it refers to. It never edits history. It refers to the hook's most recent decision where it
spoke, or its most recent decision of any kind if it has only been quiet.
Hook names: bash_gate, stop_check, subagent_verify, loop_detect, triage,
prompt_routing, narrow.
"""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev


def log_path():
    return jev.find_log()


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip()); sys.exit(2)
    hook, why = sys.argv[1], " ".join(sys.argv[2:])[:300]
    path = log_path()
    # Prefer the most recent decision where the hook SPOKE: "that block was
    # wrong" is what a dispute almost always means. A silent pass can be
    # disputed too (a miss), so fall back to the latest of any kind.
    QUIET = ("pass", "silent", "counting")
    last = last_quiet = None
    try:
        for line in open(path):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") == "decision" and r.get("hook") == hook:
                last_quiet = r
                if r.get("decision") not in QUIET:
                    last = r
    except FileNotFoundError:
        print(f"no decision log at {path}"); sys.exit(1)
    last = last or last_quiet
    if not last:
        print(f"no {hook} decision recorded yet in {path}"); sys.exit(1)
    with open(path, "a") as f:
        f.write(json.dumps({"t": time.time(), "kind": "dispute", "hook": hook,
                            "of": {"t": last["t"], "decision": last["decision"], "probs": last.get("probs")},
                            "why": why}) + "\n")
    print(f"marked wrong: {hook} {last['decision']} {last.get('probs')}")


if __name__ == "__main__":
    main()
