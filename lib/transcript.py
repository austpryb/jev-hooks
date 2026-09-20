"""Read a Claude Code transcript (JSONL) into judgeable segments.

Record shapes observed 2026-09-19: {"type": "user"|"assistant", "message": {"role", "content"}}
where content is a string (a typed prompt) or a list of blocks of type
text | thinking | tool_use | tool_result. Everything else (attachment, system,
file-history-*, ...) is skipped. Thinking blocks are never read.
"""
import json, re

# Leading <tag>...</tag> blocks the harness wraps around a prompt (system-reminder,
# fork-boilerplate, bash-input/stdout/stderr). They are stripped, not dropped: the
# user's own text after them is the prompt.
_LEAD_TAGS = re.compile(r"^\s*(?:<([A-Za-z][\w-]*)(?:\s[^>]*)?>.*?</\1>\s*)+", re.S)


def prompt_text(txt):
    txt = (txt or "").strip()
    if txt.startswith("<"):
        txt = _LEAD_TAGS.sub("", txt, count=1).strip()
        if txt.startswith("<"):        # an unclosed or unknown tag: not a prompt
            return ""
    return txt

TOOL_RESULT_CHARS = 700   # a result's first lines say what happened; the rest is bulk
TEXT_CHARS = 2500


def _clip(s, n):
    s = s if isinstance(s, str) else json.dumps(s)
    return s if len(s) <= n else s[:n] + " …[clipped]"


def _result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return c or ""


def segments(path):
    """Yield dicts {i, role, kind, text} in transcript order.
    kinds: prompt (user typed), assistant (final prose), tool_use (name + input summary), tool_result.
    Fork/subagent boilerplate and system-reminder wrappers are dropped from prompts."""
    i = 0
    try:
        f = open(path)
    except Exception:
        return
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("type")
            if t not in ("user", "assistant"):
                continue
            c = (r.get("message") or {}).get("content")
            if isinstance(c, str):
                txt = prompt_text(c)
                if txt:
                    yield {"i": i, "role": "user", "kind": "prompt", "text": _clip(txt, TEXT_CHARS)}; i += 1
                continue
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and t == "assistant":
                    txt = b.get("text", "").strip()
                    if txt:
                        yield {"i": i, "role": "assistant", "kind": "assistant", "text": _clip(txt, TEXT_CHARS)}; i += 1
                elif bt == "text" and t == "user":
                    txt = prompt_text(b.get("text", ""))
                    if txt:
                        yield {"i": i, "role": "user", "kind": "prompt", "text": _clip(txt, TEXT_CHARS)}; i += 1
                elif bt == "tool_use":
                    inp = b.get("input") or {}
                    summary = inp.get("command") or inp.get("file_path") or inp.get("prompt") or inp.get("description") or json.dumps(inp)
                    yield {"i": i, "role": "assistant", "kind": "tool_use", "text": f"{b.get('name')}: {_clip(summary, 300)}"}; i += 1
                elif bt == "tool_result":
                    txt = _result_text(b).strip()
                    if txt:
                        yield {"i": i, "role": "tool", "kind": "tool_result", "text": _clip(txt, TOOL_RESULT_CHARS)}; i += 1


BOILERPLATE = ("You've inherited the conversation context", "<fork-boilerplate>", "You are a worker fork")


def first_prompt(path):
    """The task a subagent was given. A forked agent's first prompt is harness
    boilerplate, and the real task is the next one; prefer the first prompt that
    carries a list (bullets or numbers), else the first non-boilerplate prompt."""
    prompts = [s["text"] for s in segments(path) if s["kind"] == "prompt"]
    prompts = [p for p in prompts if not any(b in p for b in BOILERPLATE)] or prompts
    for p in prompts:
        if any(l.strip()[:2] in ("- ", "* ") or l.strip()[:1].isdigit() for l in p.splitlines()):
            return p
    return prompts[0] if prompts else ""


def last_assistant_text(path):
    last = ""
    for s in segments(path):
        if s["kind"] == "assistant":
            last = s["text"]
    return last


def batches(segs, max_chars):
    """Group segments so each batch's text fits one Jev request's state budget."""
    cur, size = [], 0
    for s in segs:
        n = len(s["text"]) + 40
        if cur and size + n > max_chars:
            yield cur; cur, size = [], 0
        cur.append(s); size += n
    if cur:
        yield cur


SUMMARY_START = ("This session is being continued", "Summary:", "# Session summary")


def _is_summary(text):
    t = text.lstrip()
    return any(t.startswith(m) for m in SUMMARY_START)


def task_prompt(path):
    """What a subagent was asked to do.

    A forked agent's own transcript starts with the parent's Agent tool call,
    whose input.prompt is the task, followed by fork boilerplate. But the
    harness has handed this hook a transcript whose first prompt was a
    compaction summary (observed 2026-09-19: the verifier judged a fork against
    ten criteria from a previous session). So: an Agent call before the first
    prompt wins; otherwise the LAST Agent call anywhere in the file (the most
    recent spawn is the likeliest task for a subagent that just stopped);
    otherwise the first prompt that is not a compaction summary. The strategy
    is logged so the next surprise is diagnosable."""
    first_call, last_call, seen_user = None, None, False
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                t = r.get("type")
                c = (r.get("message") or {}).get("content")
                if t == "user":
                    seen_user = True
                if t == "assistant" and isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
                            p = (b.get("input") or {}).get("prompt")
                            if p:
                                last_call = p
                                if first_call is None and not seen_user:
                                    first_call = p
    except Exception:
        pass
    if first_call:
        _log_strategy("agent-call-before-first-prompt"); return first_call
    if last_call:
        _log_strategy("last-agent-call-in-file"); return last_call
    prompts = [s["text"] for s in segments(path) if s["kind"] == "prompt" and not _is_summary(s["text"])]
    prompts = [p for p in prompts if not any(b in p for b in BOILERPLATE)] or prompts
    for p in prompts:
        if any(l.strip()[:2] in ("- ", "* ") or l.strip()[:1].isdigit() for l in p.splitlines()):
            _log_strategy("first-listed-prompt"); return p
    _log_strategy("first-prompt" if prompts else "none")
    return prompts[0] if prompts else ""


def _log_strategy(name):
    import os, time
    p = os.environ.get("JEV_HOOKS_LOG")
    if not p:
        return
    try:
        with open(p, "a") as f:
            f.write(json.dumps({"t": time.time(), "task_prompt": name}) + "\n")
    except Exception:
        pass
