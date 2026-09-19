#!/usr/bin/env python3
"""PostToolUse hook (matcher: .*). Notices when an agent is repeating itself.

Deterministic first: the last six (tool, input, result) pairs per session are
kept in ${CLAUDE_PLUGIN_DATA}/loops/<session_id>.json. Unless the last THREE
tool names are identical, nothing else happens — no file beyond the append, no
network — and that path is well under 150 ms.

On a repeat, ONE Score over exactly those three pairs:
  0 = the same failure or the same output again
  1 = a different attempt whose effect is unclear
  2 = clear progress
A score under 0.5 is a strike; a score of 1 or more resets the strikes. Three
consecutive strikes emit a systemMessage naming the repeated tool and the last
error, plus additionalContext telling the model to change approach. This hook
NEVER blocks the tool and fails open on any error or an unreachable judge.
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

KEEP = 6
REPEAT = 3
STRIKES = 3
STRIKE_BELOW = 0.5
INPUT_CHARS = 300
RESULT_CHARS = 400


def state_path(sid):
    d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/jev-hooks")
    d = os.path.join(d, "loops"); os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{sid}.json")


def clip(v, n):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


def summarise_input(tool, inp):
    inp = inp or {}
    for k in ("command", "file_path", "pattern", "prompt", "description"):
        if inp.get(k):
            return clip(inp[k], INPUT_CHARS)
    return clip(inp, INPUT_CHARS)


def summarise_response(resp):
    if isinstance(resp, dict):
        for k in ("stdout", "stderr", "error", "output", "content", "result", "text"):
            if resp.get(k):
                return clip(resp[k], RESULT_CHARS)
        return clip(resp, RESULT_CHARS)
    return clip(resp if resp is not None else "", RESULT_CHARS)


def looks_like_error(resp, text):
    if isinstance(resp, dict) and (resp.get("is_error") or resp.get("error") or (resp.get("exit_code") not in (None, 0))):
        return True
    t = text.lower()
    return any(m in t for m in ("error", "fail", "panic", "traceback", "exception", "not found", "denied"))


def main():
    inp = jev.read_stdin()
    sid = inp.get("session_id") or "unknown"
    tool = inp.get("tool_name") or ""
    if not tool:
        return
    p = state_path(sid)
    try:
        st = json.load(open(p))
    except Exception:
        st = {"pairs": [], "strikes": 0}
    rtext = summarise_response(inp.get("tool_response"))
    st["pairs"] = (st.get("pairs") or [])[-(KEEP - 1):] + [{
        "tool": tool, "input": summarise_input(tool, inp.get("tool_input")), "result": rtext,
        "error": looks_like_error(inp.get("tool_response"), rtext)}]
    last = st["pairs"][-REPEAT:]
    repeat = len(last) == REPEAT and len({x["tool"] for x in last}) == 1
    if not repeat:
        st["strikes"] = 0
        _save(p, st)
        return                       # fast path: no judgment, no output
    q = {"progress": jev.score(
        "Looking at `attempts`, three consecutive calls of the same tool, is the latest attempt making progress compared with the earlier ones?",
        ["No progress: the same failure, the same error text, or the same output again",
         "A different attempt whose effect is unclear",
         "Clear progress: the error changed to a later stage, a check now passes, or the output shows the intended change"])}
    a = jev.ask({"tool": tool, "attempts": last}, q)
    if not a:
        _save(p, st)
        return                       # judge unavailable: fail open
    s = a["progress"]["score"]
    st["strikes"] = st.get("strikes", 0) + 1 if s < STRIKE_BELOW else 0
    st["last_score"] = s
    fire = st["strikes"] >= STRIKES
    if fire:
        st["strikes"] = 0            # say it once, then start counting again
    _save(p, st)
    if not fire:
        return
    err = next((x["result"] for x in reversed(last) if x["error"]), last[-1]["result"])
    msg = f"jev-hooks: the last three {tool} calls made no progress ({clip(err, 160)})"
    print(json.dumps({
        "systemMessage": msg,
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
            msg + ". Change approach: do not run the same command again. Say in one sentence what you will do differently "
            "(read the error's source, inspect state, or try a different tool), then do that."}}))


def _save(p, st):
    try:
        with open(p, "w") as f:
            json.dump(st, f)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
