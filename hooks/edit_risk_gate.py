#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|NotebookEdit|MultiEdit). allow / ask / deny.

The Bash gate reads a command; this reads a file change. Deterministic first,
and here the deterministic part does nearly all of the work, because git
already answers the question that matters: a tracked file with no uncommitted
changes is restored by `git checkout --`, so no edit to it can lose anything,
however large it is. That is the fast path and it costs one git call.

Two more things are decided without a call, because they are facts. A clean
tracked file is skipped. A whole-file Write over a file with uncommitted changes
is asked about outright: the delta is provably gone and no judgment adds
anything. Any other edit to a tracked file is skipped too: it
leaves the committed base in place and touches only what it names.

So Jev is asked one question and only one, about the files no commit holds — a
git-ignored file (the .env / backend.hcl / local-tfvars class), an untracked
file, a file in no repository. Is this precious, or is it regenerable? That is
a judgment and not a fact, and it is the only part of this git cannot settle.

NOTHING FROM INSIDE THE FILE IS SENT. The state is the path, the git verdict
and byte counts. An ignored file is the one most likely to hold a secret and
also the one most likely to reach this point, so shipping a preview of it to a
judgment API to decide whether it is precious would give away the thing being
protected. Path, git state and sizes answer the question on their own.

Fails open exactly like the Bash gate: no key, timeout, 429/529 -> exit 0 with
no output, and the normal permission flow decides as if the hook were absent.

Output when it has an opinion (PreToolUse JSON contract):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny"|"ask",
                          "permissionDecisionReason": "..."}}
JEV_HOOKS_GATE_MODE=warn downgrades every deny to ask.
"""
import json, os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")
# Paths whose whole purpose is to be disposable. Writing here is never interesting.
SCRATCH = ("/tmp/", "/var/tmp/", "/dev/shm/")
# What each git verdict means for recovery, in the words Jev reads.
MEANING = {
    "modified": "tracked by git but WITH uncommitted changes — the bytes about to be replaced are in no commit",
    "untracked": "not tracked by git at all — no commit holds the previous content",
    "ignored": "git-ignored (the .env / backend.hcl / local-config class) — no commit holds it and none ever will",
    "no-repo": "not inside a git repository — nothing to restore from",
    "unknown": "could not be established; git did not answer",
}


def debug(msg):
    if os.environ.get("JEV_HOOKS_DEBUG"):
        sys.stderr.write(msg + "\n")


def git_state(path):
    """clean | modified | untracked | ignored | no-repo | unknown.

    'clean' is the whole point: git restores it, so the edit risks nothing.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(d):
        return "no-repo"
    def git(*a):
        return subprocess.run(("git", "-C", d) + a, capture_output=True, text=True, timeout=3)
    try:
        st = git("status", "--porcelain", "--ignored=matching", "--", path)
        if st.returncode != 0:
            return "no-repo"
        lines = st.stdout.splitlines()
        if lines:
            head = lines[0]
            if head.startswith("!!"):
                return "ignored"
            if head.startswith("??"):
                return "untracked"
            if head.strip():
                return "modified"
        return "clean" if git("ls-files", "--error-unmatch", "--", path).returncode == 0 else "untracked"
    except Exception:
        return "unknown"


def change(tool, ti):
    """(path, bytes about to be lost, bytes about to land)."""
    if tool == "Write":
        p = ti.get("file_path") or ""
        return p, (os.path.getsize(p) if os.path.exists(p) else 0), len(ti.get("content") or "")
    if tool == "NotebookEdit":
        return ti.get("notebook_path") or "", len(ti.get("old_source") or ""), len(ti.get("new_source") or "")
    if tool == "MultiEdit":
        eds = ti.get("edits") or []
        return (ti.get("file_path") or "",
                sum(len(e.get("old_string") or "") for e in eds),
                sum(len(e.get("new_string") or "") for e in eds))
    return ti.get("file_path") or "", len(ti.get("old_string") or ""), len(ti.get("new_string") or "")


def main():
    inp = jev.read_stdin()
    tool = inp.get("tool_name")
    if tool not in TOOLS:
        return
    path, removing, adding = change(tool, inp.get("tool_input") or {})
    if not path:
        return
    # The file the write LANDS on. A committed symlink is "git clean" while its
    # target - outside any repo - is the file destroyed (audit 2026-09-21: a
    # tracked `.env` link to ../precious/.env took the clean fast path).
    ap = os.path.realpath(path)
    if ap.startswith(SCRATCH) or "/scratchpad/" in ap:
        debug("fast-path: scratch"); return
    if not os.path.exists(ap):
        debug("fast-path: new file"); return          # creating is not destroying
    state = git_state(ap)
    if state == "clean":
        debug("fast-path: git clean"); return         # `git checkout --` restores it
    # Write only. NotebookEdit changes ONE cell, and a notebook is "modified"
    # the moment its outputs change, so this asked on nearly every cell edit
    # with a false "replaces the whole of" reason.
    if state == "modified" and tool == "Write":
        # A fact, not a judgment: replacing the whole of a file that has uncommitted
        # changes destroys those changes and no commit holds them. Live runs on
        # 2026-09-19 scored this 0.54 and then 0.46 on identical input — Jev reads
        # "tracked" and "uncommitted" as genuinely split, because a commit does hold
        # most of the bytes, and a threshold balanced on that flips run to run.
        # Deciding it here is cheaper, never flaps, and leaves Jev the question it is
        # actually good at: whether an unversioned file is precious or regenerable.
        jev.record("edit_gate", "ask", None, path=ap, git_state=state, note="deterministic: whole-file write over uncommitted changes")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f"jev-hooks: {tool} replaces the whole of {os.path.basename(ap)}, which has "
                                        "uncommitted changes — git cannot restore them. Commit or stash it first."}}))
        return
    if state == "modified":
        # Anything short of a whole-file write leaves the committed base in place and
        # changes only what it names, so git still holds everything else. Live run
        # 2026-09-19: a one-byte Edit here scored 0.53 then under 0.50 on identical
        # input — the same coin flip as above, and asking about a one-byte edit is
        # noise whichever way it lands.
        debug("fast-path: targeted edit, git holds the base"); return
    ti = inp.get("tool_input") or {}
    st = {
        "tool": tool,
        "path": ap,
        "git_state": MEANING[state],
        "existing_file_bytes": os.path.getsize(ap),
        "bytes_being_replaced": removing,
        "bytes_being_written": adding,
        "replaces_the_whole_file": tool == "Write",
        "cwd": inp.get("cwd") or "",
    }
    q = {
        "unrecoverable": jev.noul(
            "If this write is wrong, is the content it replaces gone for good?",
            true="`git_state` says no commit holds the previous bytes, and the path is not a build artefact, cache, "
                 "lock file or anything else a command can regenerate",
            false="a commit or stash still holds it, or the file is generated, vendored or a cache and can be rebuilt"),
        "proportionate": jev.choice(
            "Weigh `bytes_being_replaced` against `bytes_being_written` and `replaces_the_whole_file`.",
            {"targeted": "a specific edit, or growth and rewriting in proportion to what is being replaced",
             "wholesale": "it overwrites an entire existing file, or drops far more than it puts back"}),
        "action": jev.choice(
            "Given all of it, what should a careful reviewer do before this write lands?",
            {"allow": "the previous content is recoverable, or losing it costs nothing",
             "confirm": "recoverable only with effort, or it discards substantially more than it replaces; a human should confirm",
             "block": "it would destroy unversioned work that exists nowhere else"}),
    }
    a = jev.ask(st, q)
    if not a:
        return
    unrec = a["unrecoverable"]["noul"]; act = a["action"]; prop = a["proportionate"]["choice"]
    decision = None
    # Denying an edit costs more than denying a command — a wrong deny stops work
    # that has nowhere else to go — so deny needs the verdict AND the probability,
    # where the Bash gate takes either.
    if act["choice"] == "block" and act["confidence"] >= 0.6 and unrec >= 0.7:
        decision = "deny"
    elif unrec >= 0.8 or (unrec >= 0.5 and (prop == "wholesale" or act["choice"] == "confirm")):
        decision = "ask"
    # `unrecoverable` gates everything, including a confirm verdict, because it is
    # the answer the git facts feed most directly. Live run 2026-09-19: truncating a
    # 3401-byte pnpm.lock to 2 bytes scored unrecoverable 0.20 (a lock file is
    # regenerable) and still reached confirm on the size drop alone. Below 0.5 the
    # content comes back from a commit or a command, so asking is noise.
    if not decision:
        jev.record("edit_gate", "silent", a, path=ap, git_state=state)
        return
    if decision == "deny" and os.environ.get("JEV_HOOKS_GATE_MODE") == "warn":
        decision = "ask"
    reason = (f"{tool} on a file that is {state}: unrecoverable p={unrec:.2f}, {prop}, "
              f"replacing {removing}B with {adding}B")
    # An ignored or untracked file cannot be committed, so do not tell anyone to.
    hint = ""
    if prop == "wholesale":
        hint = (" Commit or stash it first, or read the file before overwriting." if state == "modified"
                else " Nothing in git holds this — copy it aside before overwriting.")
    jev.record("edit_gate", decision, a, path=ap, git_state=state)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                              "permissionDecisionReason": "jev-hooks: " + reason + hint}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook bug must not block a session
