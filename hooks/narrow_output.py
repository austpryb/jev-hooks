#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash|Read|Grep). One Jev call decides whether the
output of a read-only call is about to be mostly noise for the current goal,
and if so appends a limiter.

Every token in a session's context got there because of a decision made before
a tool ran, and tool results dominate that context. This is the judge at that
moment: bulky output that the goal does not need never enters the transcript,
and every turn after it is cheaper.

Deterministic first: a command that already limits its own output, a call with
no command or path, and anything that is not provably read-only never reach
Jev. Then one request over {tool, command_or_path, description, goal}.
Fails open: no key, timeout, 429/529, malformed stdin -> exit 0 with no output,
and the tool runs exactly as the model wrote it.

Output when it narrows (PreToolUse JSON contract):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "updatedInput": {...},        # REPLACES the arguments
                          "additionalContext": "..."},
   "systemMessage": "..."}

Rails, in order of how much they matter:
  1. Never narrow a command that is not provably read-only (bash_risk_gate's
     own is_read_only, imported so there is one copy).
  2. Only ever APPEND a limiter. Never a path, a pattern, a flag's value or the
     command's meaning.
  3. The narrowed call must say so in its OWN output, so a later reader is
     never misled into thinking they saw everything.
  4. A systemMessage always names what was narrowed and why.
  5. JEV_HOOKS_NARROW=off disables the hook entirely.
"""
import json, os, re, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))
sys.path.insert(0, _HERE)
import jev
import transcript
# The read-only decision lives in bash_risk_gate and is imported, never copied:
# two copies of one safety predicate drift, and the copy that drifts is the one
# that lets a writing command through.
from bash_risk_gate import is_read_only

TOOLS = ("Bash", "Read", "Grep")
GOAL_CHARS = 1500

# How many lines are worth keeping when the judge says the output is bulky.
# Small on purpose: the point is an answer, not a sample of the file.
N_LINES = {"Bash": 200, "Read": 200, "Grep": 100}
# Under this, a file is not bulky by definition and never reaches Jev.
SMALL_FILE_BYTES = 64 * 1024

# The command already bounds its own output, so there is nothing to decide and
# no call to pay for. `-n`/`-m` also catch `sed -n`, `grep -m`, `docker logs -n`.
ALREADY_LIMITED = re.compile(
    r"(?:^|[|;&]\s*)(?:head|tail)\b|\bwc\b|\s-n\s*\d|\s-m\s*\d|\s-\d+\b|\s-c\b|"
    r"--max-count|--count|--tail|--oneline\s+-\d", re.I)
# A bare `&` backgrounds the command, so a pipe appended after it would read
# nothing. Redirection forms that merge streams are not backgrounding.
_BG_NOISE = re.compile(r"2>&1|&>|>&")
BACKGROUNDED = re.compile(r"(?<!&)&(?!&)")


def debug(msg):
    if os.environ.get("JEV_HOOKS_DEBUG"):
        sys.stderr.write(msg + "\n")


def last_user_prompt(path):
    """The goal: the most recent thing the user actually typed. Clipped, because
    a judgment needs the ask, not the whole prompt."""
    if not path:
        return ""
    last = ""
    try:
        for s in transcript.segments(path):
            if s.get("kind") == "prompt":
                last = s.get("text") or last
    except Exception:
        return ""
    return last[:GOAL_CHARS]


def bash_already_limited(cmd):
    return bool(ALREADY_LIMITED.search(cmd))


def narrowable(inp):
    """(state_fragment, skip_reason). state_fragment is None when no Jev call
    should be made at all."""
    tool = inp.get("tool_name")
    ti = inp.get("tool_input") or {}
    if not isinstance(ti, dict):
        return None, "unparsable-input"
    if tool == "Bash":
        cmd = (ti.get("command") or "").strip()
        if not cmd:
            return None, "no-command"
        if not is_read_only(cmd):
            return None, "not-read-only"
        if BACKGROUNDED.search(_BG_NOISE.sub("", cmd)):
            return None, "backgrounded"
        if bash_already_limited(cmd):
            return None, "already-limited"
        return {"command_or_path": cmd[:2000], "description": ti.get("description") or ""}, None
    if tool == "Read":
        p = ti.get("file_path") or ""
        if not p:
            return None, "no-path"
        if ti.get("limit"):
            return None, "already-limited"
        # Almost no Read arrives with a limit, so without this the most common
        # call in a session pays ~300 ms and ~680 tokens every time. A file
        # under the threshold cannot be bulky, so there is nothing to judge.
        # A missing or unreadable path skips too: fail open, let Read report it.
        try:
            size = os.path.getsize(p)
        except Exception:
            return None, "unreadable-path"
        if size < SMALL_FILE_BYTES:
            return None, "small-file"
        return {"command_or_path": f"Read the file {p} ({size // 1024} KB)", "description": ""}, None
    if tool == "Grep":
        pat = ti.get("pattern")
        if not pat:
            return None, "no-pattern"
        if ti.get("head_limit"):
            return None, "already-limited"
        if ti.get("output_mode") == "count":
            return None, "already-limited"
        where = ti.get("path") or "."
        mode = ti.get("output_mode") or "files_with_matches"
        return {"command_or_path": f"Grep for {pat!r} under {where} (output_mode={mode})", "description": ""}, None
    return None, "unsupported-tool"


def questions():
    return {
        "bulky": jev.noul(
            "Will this call return far more output than `goal` needs — a whole log, a whole large file, an "
            "unfiltered listing or an unbounded recursive search — rather than the specific answer the goal asks for?",
            true="cat of a log or a large source file, ls -R or find over a tree, an unfiltered git log, "
                 "grep -r across a repo for a common token, reading a file when one section answers the goal",
            false="a command whose output is inherently short: a status line, one config value, a count, "
                  "a version, a small file, a search for a rare and specific symbol"),
        "needs_all": jev.noul(
            "Does `goal` require seeing the WHOLE output to be answered correctly, so that showing only part of "
            "it would give a WRONG answer?",
            true="counting every match or every row, checking the end or the total of a file, diffing two outputs, "
                 "auditing every occurrence, confirming something is absent everywhere",
            false="finding out what happened, reading the shape of a file, locating where something is defined, "
                  "getting an example, understanding an error"),
        "where": jev.choice(
            "If only part of this output can be kept, which part carries the answer to `goal`?",
            {"head": "the beginning: a file's declarations, a listing's first entries, an error at the top",
             "tail": "the end: the most recent log lines, a summary or total printed last, the newest entries",
             "either": "the answer is not tied to one end, or it is unclear"}),
    }


def narrow_bash(cmd, n, where):
    """Buffer stdout, show one end of it, then report the command's REAL status.

    Not `| head -n`. A pipe makes the tool's exit status that of the last
    command in it, so a failing read-only command would report success — a
    worse lie than the bulk it saves. So: stdout goes to a temp file, `$?` is
    captured immediately, one end is printed, and `(exit $rc)` in a subshell
    sets the status without exiting the caller's persistent shell. stderr is
    never redirected: errors are short, important, and must not be narrowed or
    delayed behind a buffer.

    Counting the whole output is what lets the marker state the TRUE total
    ("first 200 of 4812 lines"), which a pipe could never know, and it means
    the marker prints only when something was actually cut.

    The tradeoff: the producer now runs to completion instead of being killed
    early by SIGPIPE. That is the right trade here — the goal is keeping tokens
    out of context, not saving the command work — and a read-only command that
    produces gigabytes is pathological.

    Shell variables are `_jh_`-prefixed because the harness reuses one shell
    across calls and a bare `f`, `rc` or `n` would clobber the caller's own.
    The group closes on a NEWLINE, not `;`: a command ending in a trailing
    `# comment` would swallow a `;` and the rewrite would not parse.
    """
    end = "tail" if where == "tail" else "head"
    word = "last" if end == "tail" else "first"
    marker = (f"[jev-hooks: showing {word} {n} of $_jh_n lines; "
              f"re-run without narrowing for all]")
    return ('_jh_f=$(mktemp)\n'
            '{ ' + cmd.rstrip() + '\n'
            '} > "$_jh_f"\n'
            '_jh_rc=$?\n'
            f'{end} -n {n} "$_jh_f"\n'
            '_jh_n=$(wc -l < "$_jh_f")\n'
            f'[ "$_jh_n" -gt {n} ] && echo "{marker}"\n'
            'rm -f "$_jh_f"\n'
            '(exit $_jh_rc)')


def file_lines(path):
    try:
        if os.path.getsize(path) > 64 * 1024 * 1024:
            return 0
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def main():
    if jev.disabled("narrow"):
        return                       # switched off for this session (JEV_HOOKS_DISABLE)
    if os.environ.get("JEV_HOOKS_NARROW") == "off":
        debug("off")
        return
    inp = jev.read_stdin()
    tool = inp.get("tool_name")
    if tool not in TOOLS:
        return
    frag, skip = narrowable(inp)
    if frag is None:
        debug("fast-path: " + skip)
        return
    goal = last_user_prompt(inp.get("transcript_path"))
    if not goal:
        # Without the goal there is nothing to be bulky RELATIVE TO, and a
        # judge asked to guess one would narrow on the command's looks alone.
        debug("fast-path: no-goal")
        return

    state = dict(frag)
    state["tool"] = tool
    state["goal"] = goal
    a = jev.ask(state, questions())
    if not a:
        debug("fail-open")
        return
    try:
        bulky = float(a["bulky"]["noul"])
        needs_all = float(a["needs_all"]["noul"])
        where = a["where"]["choice"]
    except Exception:
        return
    target = frag["command_or_path"]
    if not (bulky >= 0.7 and needs_all < 0.3):
        jev.record("narrow", "silent", a, command=target[:200])
        debug(f"silent bulky={bulky:.2f} needs_all={needs_all:.2f}")
        return

    ti = dict(inp.get("tool_input") or {})
    n = N_LINES[tool]
    why = (f"bulky={bulky:.2f}, needs_all={needs_all:.2f}" +
           (f", answer in the {where}" if where in ("head", "tail") else ""))
    if tool == "Bash":
        ti["command"] = narrow_bash(ti.get("command") or "", n, where)
        what = f"the first {n} lines" if where != "tail" else f"the last {n} lines"
        context = (f"jev-hooks narrowed this command to {what} of its output; it prints a marker saying so. "
                   "What you see is not the whole output.")
    elif tool == "Read":
        ti["limit"] = n
        what = f"{n} lines"
        if where == "tail":
            total = file_lines(ti.get("file_path") or "")
            if total > n:
                ti["offset"] = total - n + 1
                what = f"the last {n} lines (from line {ti['offset']})"
        else:
            what = f"the first {n} lines"
        context = (f"jev-hooks narrowed this Read to {what} of the file. This is NOT the whole file; "
                   "read it again with an explicit offset/limit if you need the rest.")
    else:  # Grep
        ti["head_limit"] = n
        what = f"the first {n} matches"
        context = (f"jev-hooks narrowed this Grep to {what}. There may be more matches than you can see here; "
                   "re-run with output_mode=\"count\" if the total is what matters.")

    jev.record("narrow", "narrowed", a, command=target[:200], narrowed_to=n, where=where)
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": ti, "additionalContext": context},
        "systemMessage": (f"jev-hooks: narrowed this {tool} call to {what} ({why}). "
                          "It would otherwise have put far more into context than the current goal needs; "
                          "re-run it yourself unnarrowed if you need all of it."),
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook bug must not change what a tool does
