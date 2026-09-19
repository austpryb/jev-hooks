"""Read a Claude Code transcript (JSONL) into judgeable segments.

Record shapes observed 2026-09-19: {"type": "user"|"assistant", "message": {"role", "content"}}
where content is a string (a typed prompt) or a list of blocks of type
text | thinking | tool_use | tool_result. Everything else (attachment, system,
file-history-*, ...) is skipped. Thinking blocks are never read.
"""
import json

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
                txt = c.strip()
                if txt and not txt.startswith("<") :
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
                    txt = b.get("text", "").strip()
                    if txt and not txt.startswith("<"):
                        yield {"i": i, "role": "user", "kind": "prompt", "text": _clip(txt, TEXT_CHARS)}; i += 1
                elif bt == "tool_use":
                    inp = b.get("input") or {}
                    summary = inp.get("command") or inp.get("file_path") or inp.get("prompt") or inp.get("description") or json.dumps(inp)
                    yield {"i": i, "role": "assistant", "kind": "tool_use", "text": f"{b.get('name')}: {_clip(summary, 300)}"}; i += 1
                elif bt == "tool_result":
                    txt = _result_text(b).strip()
                    if txt:
                        yield {"i": i, "role": "tool", "kind": "tool_result", "text": _clip(txt, TOOL_RESULT_CHARS)}; i += 1


def first_prompt(path):
    for s in segments(path):
        if s["kind"] == "prompt":
            return s["text"]
    return ""


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
