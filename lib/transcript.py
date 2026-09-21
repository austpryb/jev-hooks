"""Read a Claude Code transcript (JSONL) into judgeable segments.

Record shapes observed 2026-09-19: {"type": "user"|"assistant", "message": {"role", "content"}}
where content is a string (a typed prompt) or a list of blocks of type
text | thinking | tool_use | tool_result. Everything else (attachment, system,
file-history-*, ...) is skipped. Thinking blocks are never read.
"""
import glob, json, os, re

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
WRITE_TOOLS = ("Write", "Edit", "NotebookEdit", "MultiEdit")
AUTHORED_CHARS = 400      # how much of what a write-like tool put into a file travels with its call line


def _authored(name, inp):
    """What a write-like tool put INTO the file. Its result line is "File created
    successfully" - it names no content - so a judge told to weigh the record
    cannot see what was written and reads any description of it as a claim from
    nowhere (2026-09-21: three stop-check blocks in one session on messages that
    described a file the assistant had just written). The call is the evidence;
    let it carry the text."""
    if name not in WRITE_TOOLS:
        return ""
    if name == "MultiEdit":
        body = "\n".join((e.get("new_string") or "") for e in (inp.get("edits") or []) if isinstance(e, dict))
    else:
        body = inp.get("content") or inp.get("new_string") or inp.get("new_source") or ""
    body = body.strip() if isinstance(body, str) else ""
    return _clip(body, AUTHORED_CHARS) if body else ""


def _clip(s, n):
    s = s if isinstance(s, str) else json.dumps(s)
    return s if len(s) <= n else s[:n] + " …[clipped]"


def _clip_ends(s, n):
    """Keep a result's head AND tail. A test run says what it did first and
    how it ended last - "ok" and "FAIL" are the final lines, so a head-only
    clip removes the verdict the judge is looking for."""
    s = s if isinstance(s, str) else json.dumps(s)
    if len(s) <= n:
        return s
    head = int(n * 0.6)
    return s[:head] + " …[clipped]… " + s[-(n - head):]


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
                    name = b.get("name")
                    summary = inp.get("command") or inp.get("file_path") or inp.get("prompt") or inp.get("description") or json.dumps(inp)
                    body = _authored(name, inp)
                    if body:
                        summary = f"{summary}\n{body}"
                    yield {"i": i, "role": "assistant", "kind": "tool_use", "id": b.get("id"),
                           "text": f"{name}: {_clip(summary, 300 + (AUTHORED_CHARS if body else 0))}"}; i += 1
                elif bt == "tool_result":
                    txt = _result_text(b).strip()
                    if txt:
                        yield {"i": i, "role": "tool", "kind": "tool_result", "id": b.get("tool_use_id"),
                               "text": _clip_ends(txt, TOOL_RESULT_CHARS)}; i += 1


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


def last_model(path):
    """The model id of the most recent assistant turn, e.g.
    'claude-opus-5'. A hook is told the session id and the transcript path but
    never the model, and the transcript is the only place it is written down."""
    found = ""
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "assistant":
                    m = (r.get("message") or {}).get("model")
                    if m:
                        found = m
    except Exception:
        pass
    return found


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


def is_subagent_transcript(path, lines=40):
    """Whether this file is a subagent's own transcript rather than the parent
    session's. A subagent's records carry agentId / isSidechain / parentSessionId
    (a fork opens with a fork-context-ref record), and a fork's first prompt is
    the harness's fork boilerplate. A parent transcript has none of these."""
    try:
        with open(path) as f:
            for n, line in enumerate(f):
                if n >= lines:
                    break
                if '"agentId"' in line or '"isSidechain": true' in line or '"isSidechain":true' in line \
                        or '"parentSessionId"' in line or '"fork-context-ref"' in line \
                        or any(b in line for b in BOILERPLATE):
                    return True
    except Exception:
        pass
    return False


def _summary_first(path):
    for s in segments(path):
        if s["kind"] == "prompt":
            return _is_summary(s["text"])
    return False


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip()[:400]


def _parent_shaped(path):
    """A PARENT session's transcript: somebody typed a prompt, and only later
    did an Agent call spawn a subagent. A subagent's own transcript opens with
    the spawning Agent call before any typed prompt (a fork), or never spawned
    anything at all (a plain worker). Carries no markers, so the shape decides."""
    typed = False
    for s in segments(path):
        if s["kind"] == "prompt" and not any(b in s["text"] for b in BOILERPLATE):
            typed = True
        elif s["kind"] == "tool_use" and s["text"].startswith("Agent:"):
            return typed
    return False


def subagent_path(given, agent_path=None, agent_id=None, report=""):
    """Which file is the SUBAGENT's transcript. Returns (path, how).

    On SubagentStop the harness sends TWO paths: `transcript_path` is the
    PARENT session's transcript and `agent_transcript_path` is the subagent's
    own, under `<session>/subagents/agent-<id>.jsonl` (hooks reference,
    2026-09-21). This hook read the parent's for its first twelve versions.
    Measured 2026-09-21: four forks spawned in one turn were every one judged
    against the criteria of the fork spawned LAST - the parent's last Agent
    call - and against the parent's tool record, so their own work read as
    invented (p 0.88-0.94). That is most of a 93% block rate.

    Order: the documented field; the given path if it is already a subagent's;
    the sibling file named by `agent_id`; else the sibling whose final message
    IS the report being judged. None of those -> (None, why): a judgment
    against the wrong record is worse than no judgment."""
    if agent_path and os.path.exists(agent_path):
        return agent_path, "agent_transcript_path"
    if not given:
        return None, "no-path"
    # A subagent's own file, marked or by shape: judge it. Only a file in which a
    # typed prompt precedes its first spawn, with no subagent markers, is a parent.
    if is_subagent_transcript(given) or not _parent_shaped(given):
        return given, "given"
    d = os.path.join(os.path.dirname(given), os.path.splitext(os.path.basename(given))[0], "subagents")
    if agent_id:
        p = os.path.join(d, f"agent-{agent_id}.jsonl")
        if os.path.exists(p):
            return p, "agent_id"
    if report and os.path.isdir(d):
        want = _norm(report)
        for p in sorted(glob.glob(os.path.join(d, "agent-*.jsonl")), key=os.path.getmtime, reverse=True):
            if want and _norm(last_assistant_text(p)) == want:
                return p, "report-match"
    return None, "parent-only"


LAST_STRATEGY = "none"


def task_prompt(path):
    """What a subagent was asked to do.

    A forked agent's own transcript starts with the parent's Agent tool call,
    whose input.prompt is the task, followed by fork boilerplate. But the
    harness has handed this hook a transcript whose first prompt was a
    compaction summary (observed 2026-09-19: the verifier judged a fork against
    ten criteria from a previous session). So: an Agent call before the first
    prompt wins; otherwise the LAST Agent call anywhere in the file - but only
    when the file is a subagent's own or opens with a compaction summary. In a
    PARENT transcript the last Agent call is whichever subagent was spawned
    most recently, which under parallel forks is somebody else's task
    (2026-09-21), so a parent-shaped file yields no task at all and the caller
    fails open. Otherwise the first prompt that is not a compaction summary.
    The strategy is logged so the next surprise is diagnosable."""
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
        if is_subagent_transcript(path) or _summary_first(path):
            _log_strategy("last-agent-call-in-file"); return last_call
        _log_strategy("parent-transcript"); return ""
    prompts = [s["text"] for s in segments(path) if s["kind"] == "prompt" and not _is_summary(s["text"])]
    prompts = [p for p in prompts if not any(b in p for b in BOILERPLATE)] or prompts
    for p in prompts:
        if any(l.strip()[:2] in ("- ", "* ") or l.strip()[:1].isdigit() for l in p.splitlines()):
            _log_strategy("first-listed-prompt"); return p
    _log_strategy("first-prompt" if prompts else "none")
    return prompts[0] if prompts else ""


def _log_strategy(name):
    global LAST_STRATEGY
    LAST_STRATEGY = name
    import time
    p = os.environ.get("JEV_HOOKS_LOG")
    if not p:
        return
    try:
        with open(p, "a") as f:
            f.write(json.dumps({"t": time.time(), "task_prompt": name}) + "\n")
    except Exception:
        pass


def tool_results(path, chars=16_000, result_chars=400):
    """What an agent ACTUALLY did: each call PAIRED with what it printed, oldest
    call first, as "Bash: go test ./...\n-> ok  pkg  1.2s".

    A report is a claim; these are the commands that ran and their output.
    Shared by the stop check and the subagent verifier so the two cannot drift:
    both judge a claim against this, never against the claim's own prose.

    Two things this must get right, both learned from live misjudgments
    (measured 2026-09-21 over 184 subagent verdicts, 94% of them blocks):

      Pairing. Results alone are unattributable - "File created successfully"
      names no file the criterion could match, and the judge, told to weigh the
      record, honestly answered that the record showed nothing. The call is
      what carries the path, the command and the test name.

      Reach. A flat newest-first byte budget spent itself on the last few
      results and dropped the early work entirely: for a 108-call agent the
      judge saw 15 calls, so any criterion finished early read as undone. Work
      that does not fit keeps its CALL line and drops its output - a header
      costs ~60 chars and still says the file was written - and only when even
      that will not fit does the window close, with a line saying how many
      earlier calls it left out.
    """
    pending, entries = {}, []
    for s in segments(path):
        if s["kind"] == "tool_use":
            pending[s.get("id")] = len(entries)
            entries.append([s["text"], ""])
        elif s["kind"] == "tool_result":
            idx = pending.pop(s.get("id"), None)
            if idx is None:
                entries.append(["", s["text"]])       # result with no call in view
            else:
                entries[idx][1] = s["text"]
    # Reserve the call lines FIRST, then spend what is left on output, newest
    # first. Spending in one newest-first pass looks fine until a long agent:
    # the newest ~35 results eat the whole budget and every earlier call
    # vanishes, which is exactly how work finished early came to read as never
    # done. A call line is ~60 chars and still says the file was written.
    # A write-like call line carries what it wrote (see _authored); give it the room.
    heads = [_clip(call, 300 + (AUTHORED_CHARS if call.startswith(WRITE_TOOLS) else 0)) if call else "" for call, _ in entries]
    reserve = sum(len(h) for h in heads)
    dropped = 0
    if reserve > chars // 2:      # too many calls to name them in full: shorten,
        heads = [_clip(h, 140) if h else "" for h in heads]   # then keep the newest
        reserve = sum(len(h) for h in heads)
    if reserve > chars // 2:
        keep, size = 0, 0
        for h in reversed(heads):
            if size + len(h) > chars // 2:
                break
            keep += 1; size += len(h)
        dropped = len(entries) - keep
        entries, heads = entries[-keep:], heads[-keep:]
        reserve = size
    budget = chars - reserve
    bodies = [""] * len(entries)
    for i in range(len(entries) - 1, -1, -1):
        res = entries[i][1]
        if not res:
            continue
        body = _clip_ends(res, result_chars)
        if len(body) + 4 <= budget:
            bodies[i] = body; budget -= len(body) + 4
    out = []
    for i, (call, res) in enumerate(entries):
        head = heads[i]
        out.append(f"{head}\n-> {bodies[i]}" if head and bodies[i] else (head or bodies[i] or res[:140]))
    if dropped:
        out.insert(0, f"…{dropped} earlier tool calls omitted (window full)")
    return out


def tool_call_count(path, exclude=("Agent",)):
    """How many tools the agent invoked ITSELF. Zero means it did nothing,
    whatever its report says.

    A fork's transcript opens with the PARENT's `Agent` call — the spawn, not
    the child's work — so counting it made a do-nothing agent look busy and the
    zero-work gate never fired."""
    n = 0
    for s in segments(path):
        if s["kind"] != "tool_use":
            continue
        name = s["text"].split(":", 1)[0].strip()
        if name not in exclude:
            n += 1
    return n
