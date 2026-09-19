#!/usr/bin/env python3
"""SessionStart hook (matcher: compact). Prints the keep-set precompact_triage.py
wrote for this session; on exit 0 plain stdout becomes context. Nothing to print
when there is no keep file, which is the case when Jev was unavailable."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

inp = jev.read_stdin()
sid = inp.get("session_id") or "unknown"
d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/jev-hooks")
p = os.path.join(d, "keep", f"{sid}.md")
try:
    sys.stdout.write(open(p).read())
except Exception:
    pass
