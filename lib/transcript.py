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

# Text the HARNESS puts in the user's turn: a Stop hook's own block reason, the
# nudge after an empty reply, a subagent's hand-back. None of it is the user
# asking anything. Measured 2026-09-22: at 6 of 16 stop-check blocks in one
# session, the check's "last user prompt" was one of these - three times its
# own previous complaint - so `unanswered` judged a reply against the hook.
HARNESS_PROMPTS = ("Stop hook feedback:", "[Your previous response had no visible output",
                   "Another Claude session sent a message:",
                   # A background child's completion notice arrives as a user
                   # record. It is not the user, and it is not a task.
                   "[SYSTEM NOTIFICATION", "<task-notification>",
                   # A model-invoked skill's body, injected as a user record.
                   "Base directory for this skill:")


def is_harness_prompt(text):
    t = (text or "").lstrip()
    return any(t.startswith(h) for h in HARNESS_PROMPTS)


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


def _head_records(path, lines):
    """The first `lines` records, parsed. Unparseable lines are skipped."""
    out = []
    try:
        with open(path) as f:
            for n, line in enumerate(f):
                if n >= lines:
                    break
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except Exception:
        pass
    return out


def _typed_text(r):
    """What a user record says in its own voice - never a tool result, which
    can quote anything, including another transcript's markers."""
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_subagent_transcript(path, lines=40):
    """Whether this file is a subagent's own transcript rather than the parent
    session's. Decided on a record's TOP-LEVEL keys: a subagent's records say
    `isSidechain: true` and carry `agentId` (and `parentSessionId`), a fork
    opens with a fork-context-ref record, and a fork's first prompt is the
    harness's fork boilerplate.

    Never by substring. Measured 2026-09-21: 11 of 595 real MAIN transcripts
    matched `"agentId"` - it is written into a main session's own Agent tool
    RESULTS (toolUseResult.agentId), on records that also say
    `isSidechain: false` - and the verifier then judged each main session
    against the last task it had delegated."""
    for r in _head_records(path, lines):
        if r.get("isSidechain") is True or r.get("agentId") or r.get("parentSessionId") \
                or r.get("type") == "fork-context-ref":
            return True
        if r.get("type") == "user" and any(b in _typed_text(r) for b in BOILERPLATE):
            return True
    return False


def is_main_session_transcript(path, lines=40):
    """A MAIN session's transcript: its records say `"isSidechain": false` and
    none carries a subagent marker. Measured 2026-09-21: SubagentStop fired with
    no agent_transcript_path and no agent_type, handing over a main session's
    transcript (no Agent calls, ten typed prompts), and the verifier judged
    that session's own twelve-point prompt as the subagent's task. Test
    fixtures and older harnesses write no isSidechain key at all, so only an
    explicit `false` counts."""
    if is_subagent_transcript(path, lines):
        return False
    return any(r.get("isSidechain") is False for r in _head_records(path, lines))


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
    if is_subagent_transcript(given):
        return given, "given"
    main = is_main_session_transcript(given)
    # Unmarked (a fixture, an older harness) and not shaped like a parent: judge it.
    # Only a file in which a typed prompt precedes its first spawn is a parent.
    if not main and not _parent_shaped(given):
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
    return None, "main-session" if main else "parent-only"


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
    prompts = [s["text"] for s in segments(path) if s["kind"] == "prompt" and not _is_summary(s["text"])
               and not is_harness_prompt(s["text"])]
    prompts = [p for p in prompts if not any(b in p for b in BOILERPLATE)] or prompts
    if last_call:
        # A subagent that typed-in with its own brief and then delegated is
        # judged on its OWN brief. Its last Agent call is its child's task:
        # measured 2026-09-21, 19 real subagents took that path, one briefed on
        # six tickets and judged against the one it handed down. The last call
        # stands in only when there is no brief of its own to read - a
        # compacted transcript that opens with a summary.
        if is_subagent_transcript(path) and (_summary_first(path) or not prompts):
            _log_strategy("last-agent-call-in-file"); return last_call
        if not is_subagent_transcript(path):
            if _summary_first(path):
                _log_strategy("last-agent-call-in-file"); return last_call
            _log_strategy("parent-transcript"); return ""
    if prompts and is_subagent_transcript(path):
        # A subagent's first prompt of its own IS its brief. Preferring a later
        # prompt because it happens to contain a list picked a child's
        # completion notice over a six-ticket brief (2026-09-21).
        _log_strategy("subagent-brief"); return prompts[0]
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


def parent_spawn_ids(path):
    """The ids of the PARENT's spawning Agent call(s): the ones a fork's
    transcript opens with, before its first user record. Everything after that
    is the agent's own doing - including Agent calls it made itself, which are
    delegation, not idleness (an orchestrator with 11 delegations and one read
    used to count as ONE call of work)."""
    ids = set()
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "user":
                    break
                c = (r.get("message") or {}).get("content")
                if r.get("type") == "assistant" and isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
                            ids.add(b.get("id"))
    except Exception:
        pass
    return ids


def tool_results(path, chars=16_000, result_chars=400, after=-1):
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
    spawn = parent_spawn_ids(path)       # the parent's call, not this agent's work
    for s in segments(path):
        if s.get("id") in spawn and s["kind"] in ("tool_use", "tool_result"):
            continue
        if s["i"] <= after:
            continue                     # before the caller's cut-off (e.g. the prompt being answered)
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


def tool_call_count(path):
    """How many tools the agent invoked ITSELF. Zero means it did nothing,
    whatever its report says.

    A fork's transcript opens with the PARENT's `Agent` call - the spawn, not
    the child's work - and counting it made a do-nothing agent look busy. But
    excluding every Agent call by name went too far the other way: an agent
    that delegated was counted as idle, and the zero-work gate blocked an
    orchestrator for doing its job. Only the parent's spawn is excluded."""
    spawn = parent_spawn_ids(path)
    return sum(1 for s in segments(path) if s["kind"] == "tool_use" and s.get("id") not in spawn)
