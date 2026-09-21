#!/usr/bin/env python3
"""What the hooks have been deciding, and where the thresholds should move.

    python3 bin/stats.py [path-to-decisions.jsonl]

Without a record of decisions there is no way to measure a false positive rate,
so every threshold moves on annoyance instead of evidence. This reads the log
the hooks write and prints three things: how often each hook speaks, what it
costs, and — the one that matters for tuning — how many judgments landed in the
band just either side of a threshold, where a verdict flips on wording rather
than substance. A decision marked wrong with bin/wrong.py shows as disputed.
"""
import json, os, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

BANDS = (0.60, 0.80)


def main():
    # --v=0.15.0 counts only decisions that version of the code made. Sessions
    # keep running the version they started with, so a log always mixes them.
    args = [a for a in sys.argv[1:] if not a.startswith("--v=")]
    only = next((a[4:] for a in sys.argv[1:] if a.startswith("--v=")), None)
    path = args[0] if args else jev.find_log()
    if not os.path.exists(path):
        print(f"no decision log at {path}"); return
    by_hook = collections.defaultdict(collections.Counter)
    probs = collections.defaultdict(list)
    disputed = collections.Counter()
    versions = collections.Counter()
    calls = tokens = 0
    secs = []
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "decision":
            versions[r.get("v", "unversioned")] += 1
            if only and r.get("v") != only:
                continue
        if r.get("kind") == "call":
            calls += 1
            tokens += (r.get("usage") or {}).get("input_tokens", 0)
            if r.get("secs"):
                secs.append(r["secs"])
        elif r.get("kind") == "decision":
            by_hook[r["hook"]][r["decision"]] += 1
            for k, v in (r.get("probs") or {}).items():
                p = v if isinstance(v, (int, float)) else (v[1] if isinstance(v, list) and len(v) > 1 else None)
                if isinstance(p, (int, float)):
                    probs[f'{r["hook"]}.{k}'].append(p)
        elif r.get("kind") == "dispute":
            disputed[r.get("hook", "?")] += 1

    print(f"log: {path}" + (f"  (decisions from v{only} only)" if only else ""))
    print("decisions by plugin version: " + ", ".join(f"{v} {n}" for v, n in sorted(versions.items())))
    print(f"calls {calls}  input tokens {tokens:,}  cost ${tokens * 0.042 / 1_000_000:.4f}"
          + (f"  median {sorted(secs)[len(secs)//2]:.2f}s" if secs else ""))
    print()
    for hook in sorted(by_hook):
        total = sum(by_hook[hook].values())
        spoke = sum(n for d, n in by_hook[hook].items() if d not in ("pass", "silent", "counting"))
        d = f"  disputed {disputed[hook]}" if disputed[hook] else ""
        print(f"{hook:<18} {total:>4} decisions, spoke {spoke} ({100*spoke//total if total else 0}%){d}")
        for dec, n in by_hook[hook].most_common():
            print(f"    {dec:<16} {n}")
    if not probs:
        return
    print("\njudgments near a threshold (where a verdict flips on wording):")
    for key in sorted(probs):
        vals = probs[key]
        near = [v for v in vals if BANDS[0] <= v <= BANDS[1]]
        if near:
            print(f"  {key:<28} {len(near)}/{len(vals)} in {BANDS[0]}-{BANDS[1]}"
                  f"  (median {sorted(vals)[len(vals)//2]:.2f})")


if __name__ == "__main__":
    main()
