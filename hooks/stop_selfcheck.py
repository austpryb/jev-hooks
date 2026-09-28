#!/usr/bin/env python3
"""Stop hook. A judge that reads only the final message.

The harness already has the rule ("do that work now, don't hand back a
promise"); this enforces it with three yes/no questions over a small state:
the last user prompt, the final message, and the tail of recent tool results.

  promise     the message ends with, or hinges on, work not yet done
  unanswered  the last user prompt asked something the message does not answer
  unverified  the message states a number, test result or outcome the recent
              tool results do not show

Any answer >= 0.7 blocks once with {"decision":"block","reason":...}; the
reason names the check and quotes the offending sentence. stop_hook_active
means this hook already sent the session back: exit 0, never loop. Fails open.

In-flight work is not a promise. The Stop input carries `background_tasks` and
`session_crons` (hooks reference, 2026-09-21) precisely so a hook can tell
"the session is done" from "the session is paused until background work wakes
it". Measured 2026-09-21 in one session: eight of eight `promise` blocks were
honest status on a running subagent, a background command or a scheduled
wake-up, each rephrased a turn later to get past the check. So when the
harness says something is still running and the message says so too, `promise`
alone cannot block; the other two checks still can, and a plain promise about
something else ("I'll open the PR next") still blocks with work in flight.
"""
import json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev, transcript

BLOCK_AT = 0.7
IN_FLIGHT_BLOCK_AT = float(os.environ.get("JEV_HOOKS_STOP_IN_FLIGHT_AT", "0.9"))
RESULTS_CHARS = 8_000      # budget for the call+output record; prompt and message are small
MESSAGE_CHARS = 8_000

# For a harness that does not send background_tasks: what in-flight work looks
# like in the tool record the judge already sees.
IN_FLIGHT = re.compile(r"running in background|Monitor started|Async agent launched|Resuming agent|"
                       r"Next wakeup scheduled|·\s+running\s+·", re.I)
# The assistant was refused, so the next step is the user's to run - a gate, not a promise.
DENIED = re.compile(r"denied by the Claude Code auto mode classifier|Permission for this action was denied|"
                    r"permission[^.]{0,40}denied", re.I)
# The message itself says the work is not finished. Only with something in
# flight does this matter; a bare "still waiting" with nothing running is a stall.
PAUSED = re.compile(r"\b(not (yet )?(done|finished|complete|completed)|still (running|building|active|in progress|"
                    r"waiting|compiling|executing)|in progress|waiting (on|for)|hasn'?t (finished|landed|completed|"
                    r"returned)|no (new )?(change|results?|update)( yet| since)?|until (it|they|that|the \w+) "
                    r"(finish|land|complete|return)|when (it|they|that|the \w+) (finish|land|complete|return))", re.I)


def in_flight(inp, results):
    """Work the harness says is still running. `background_tasks` and
    `session_crons` are the authority when present (documented Stop input);
    the regex reads the same tool record the judge sees, for a harness that
    does not send them."""
    tasks, crons = inp.get("background_tasks"), inp.get("session_crons")
    if tasks is not None or crons is not None:
        out = [f"{t.get('type', 'task')}: {t.get('description') or t.get('command') or t.get('id')} ({t.get('status', 'running')})"
               for t in (tasks or []) if isinstance(t, dict)]
        out += [f"scheduled wakeup: {c.get('description') or c.get('id')}" for c in (crons or []) if isinstance(c, dict)]
        return out
    return [r[:120].replace("\n", " ") for r in results if IN_FLIGHT.search(r)]


NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)*")


def numbers_in_record(path, message):
    """Which numbers the message states that appear VERBATIM somewhere in the
    full tool output or the user's `!` output - not the clipped window the
    judge sees. Measured 2026-09-27: a report whose every count came from a
    stats table was blocked as unverified (0.81, 5 of 5 replays) because the
    400-char head-and-tail clip had dropped the table's middle rows. Whether a
    string occurs in the output is a fact grep settles, so code settles it and
    the judge is told; it still decides whether the claim is supported.
    Commas are dropped ("8,449" is "8449"); numbers under 3 characters are left
    out, since they match almost any output."""
    said = {n.replace(",", "") for n in NUMBER.findall(message or "")}
    said = {n for n in said if len(n) >= 3}
    if not said or not path:
        return [], []
    # Read the raw transcript: transcript.segments() has already clipped each
    # result to TOOL_RESULT_CHARS, which is the very loss this corrects for.
    seen = set()
    try:
        with open(path) as f:
            for line in f:
                if "tool_result" not in line and "bash-stdout" not in line:
                    continue
                try:
                    content = (json.loads(line).get("message") or {}).get("content")
                except ValueError:
                    continue
                if isinstance(content, str):
                    texts = [content] if "bash-stdout" in content else []
                else:
                    texts = [transcript._result_text(b) for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_result"]
                for t in texts:
                    seen |= {n.replace(",", "") for n in NUMBER.findall(t or "")}
    except OSError:
        return [], []
    return sorted(said & seen), sorted(said - seen)


def last_prompt_and_results(path):
    """The prompt being answered, and the record of what ran since. The record
    comes from transcript.tool_results so this check and the subagent verifier
    weigh the same evidence: each call paired with its output, not loose
    results that name nothing."""
    prompt, at, ran, replies = "", -1, [], []
    for s in transcript.segments(path):
        if s["kind"] == "prompt" and not any(b in s["text"] for b in transcript.BOILERPLATE) \
                and not transcript.is_harness_prompt(s["text"]) and not transcript._is_summary(s["text"]):
            prompt, at, ran, replies = s["text"], s["i"], [], []   # a compaction summary is not the user's question
        elif s["kind"] == "assistant" and at >= 0:
            # Replies already sent to this prompt. A later turn - after a task
            # notice or an agent's hand-back - is a follow-up, and judged alone
            # against the prompt it read as never answering it (2026-09-25).
            replies.append(s["text"])
        elif s["kind"] == "shell" and s["text"].startswith("$ "):
            # The user ran a `!` command after asking: their latest turn is that
            # command, not a new request, and "this turn" starts there.
            ran.append(s["text"][2:][:200]); at = s["i"]
    return (prompt, transcript.tool_results(path, chars=RESULTS_CHARS),
            transcript.tool_results(path, chars=RESULTS_CHARS, after=at), ran, replies)


def sentence_for(check, message):
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", message) if s.strip()]
    if not sents:
        return ""
    if check == "promise":
        for s in reversed(sents):
            if re.search(r"\b(I'?ll|I will|next I|let me know|once you|when you|after that I)\b", s, re.I):
                return s
        return sents[-1]
    if check == "unverified":
        for s in reversed(sents):
            if re.search(r"\d|pass|fail|green|succeed|works|verified", s, re.I):
                return s
    return sents[-1]


def main():
    if jev.disabled("stop_check"):
        return                       # switched off for this session (JEV_HOOKS_DISABLE)
    inp = jev.read_stdin()
    if inp.get("stop_hook_active"):
        return
    message = (inp.get("last_assistant_message") or "").strip()
    path = inp.get("transcript_path")
    if not message and path:
        message = transcript.last_assistant_text(path)
    if not message:
        return
    prompt, results, this_turn, ran, replies = last_prompt_and_results(path) if path else ("", [], [], [], [])
    # The final message is usually the transcript's last reply too; only EARLIER
    # ones count. Compared on a shared prefix, not equality: the transcript copy
    # is clipped and the Stop input's copy need not match it byte for byte.
    if replies:
        last, head = replies[-1].strip(), message.strip()[:80]
        if head and last.startswith(head[:len(last)] if len(last) < 80 else head):
            replies = replies[:-1]
    # "Still running" and "was denied" are facts about THIS turn. Read over the
    # whole session, a background build that finished turns ago set in_flight
    # and excused a fresh, real promise (audit 2026-09-21). An unverified claim
    # may rest on earlier evidence, so `results` stays the whole record.
    bg = in_flight(inp, this_turn)
    denied = any(DENIED.search(r) for r in this_turn)
    state = {"last_user_prompt": prompt[:4_000], "final_message": message[:MESSAGE_CHARS],
             "recent_tool_results": results, "in_flight": bg, "permission_denied": denied,
             "user_ran_commands": ran[-5:],
             "earlier_replies_to_prompt": [r[:1_500] for r in replies[-3:]]}
    found, missing = numbers_in_record(path, message)
    if found:
        state["numbers_found_in_full_output"] = found[:60]
        state["numbers_not_found_in_output"] = missing[:30]
    q = {
        "promise": jev.noul(
            "Does `final_message` end with, or hinge on, a promise of work not yet done — something the assistant says it will do next, "
            "or asks the user to wait for — rather than reporting work already completed?",
            true="'I'll open the PR next', 'next I will run the tests', 'I'll report back', a plan or next-steps list in place of results",
            false="a report of what was done, with results; a question the user must answer; an OFFER that is conditional on the user "
                  "('say the word and I'll…', 'if you want, I can…') after the work that was asked for is reported done or explicitly not done; "
                  "a NEXT STEP that is explicitly blocked on something only the user can do — merging a pull request, restarting the session, "
                  "supplying a key, approving a deploy — named once alongside what was already finished, since naming the gate is a report of "
                  "where the work stands and not a promise the assistant is free to keep; a STATUS REPORT on work the assistant has ALREADY "
                  "STARTED and cannot finish faster — a background command, a subagent, a scheduled wake-up, a deployment being polled "
                  "(`in_flight` lists what the harness says is still running) — that says plainly what is and is not done yet; a command "
                  "handed to the user to run themselves because the assistant was refused permission to run it (`permission_denied`); "
                  "a reply while the USER is running commands themselves (`user_ran_commands` lists the `!` commands they ran since "
                  "`last_user_prompt`) that reports on what the last one printed and names the next command for THEM to run, or asks "
                  "them to paste output of a command they will run so it can be reviewed"),
        "unanswered": jev.noul(
            "Did `last_user_prompt` ask a question or request something specific that `final_message` does not answer or deliver? "
            "Ignore this if `last_user_prompt` is empty or is not a request.",
            true="the user asked X and the message talks about Y, or gives a plan instead of the answer",
            false="a question or offer the ASSISTANT puts to the user in `final_message` ('Want me to…?', 'Should I…?') — that is the "
                  "assistant asking, not the user; judge only whether `last_user_prompt` itself was answered; "
                  "a FOLLOW-UP after the prompt was already answered by a reply in `earlier_replies_to_prompt` (e.g. a status line once a "
                  "background task finishes, or a note after a subagent reports) — the prompt was answered there, not left open; "
                  "the message answers the question or delivers the request, even briefly; an explicit statement that the thing was NOT done or NOT known IS an answer; "
                  "a status answer ('not yet', 'still running', 'no change since the last check') to a status question ('done?', 'check it now') IS an answer; "
                  "or the prompt was just 'keep going' / an acknowledgement / pasted command output"),
        "unverified": jev.noul(
            "Does `final_message` state a number, a test result, a build result or an outcome (e.g. 'all tests pass', '28 pins match', 'deployed') "
            "that `recent_tool_results` do not show? Treat the tool results as the only evidence. If `recent_tool_results` is empty, answer no.",
            true="claims 'tests pass' but no test output is in the results; a count that appears nowhere in the results",
            false="every stated outcome or number is supported by the results (an 'ok' line from a build or test command supports 'build is green'); "
                  "or the message makes no such claims; or it says the thing was not done, or names what was NOT verified or checked "
                  "(an admission of a gap is the opposite of a claim); or it tells the user what to run or look for next; or the outcome is EXPECTED FROM A CHECK NOT YET RUN "
                  "— what a pending test will look for, what a roll should show, the value that would confirm a fix — which is a statement of "
                  "intent, not a claim that the check already passed; or the outcome is a DESCRIPTION OF CONTENT THE ASSISTANT ITSELF WROTE "
                  "this turn — the Write or Edit call in `recent_tool_results` quotes what was written and is the evidence for it; "
                  "or the message's numbers are listed in `numbers_found_in_full_output`, which means they occur verbatim in the "
                  "FULL tool output even where `recent_tool_results` shows it clipped — a number found there is supported, and only "
                  "the numbers in `numbers_not_found_in_output` (or a claim with no number) still need evidence"),
    }
    a = jev.ask(state, q)
    if not a:
        return
    failed = [(k, a[k]["noul"]) for k in ("promise", "unanswered", "unverified") if a.get(k, {}).get("noul", 0) >= BLOCK_AT]
    # With work IN FLIGHT, a message that is waiting on it has usually neither
    # kept its promise nor fully answered yet, legitimately. From the decision log
    # (2026-09-25): 74 of 199 blocks fired with background work running; promise
    # and unanswered there had median 0.83, i.e. just over the line. So with work in
    # flight those two need IN_FLIGHT_BLOCK_AT; unverified is unchanged, because a
    # claim without evidence is not excused by something else running.
    if bg:
        failed = [(k, p) for k, p in failed if k == "unverified" or p >= IN_FLIGHT_BLOCK_AT]
    # The user's latest turn was a `!` command they ran, not a request, so there
    # is nothing for `unanswered` to measure: `last_user_prompt` is the request
    # from BEFORE they started, which earlier replies already answered. Judged
    # against it anyway, each short report on a command read as ignoring it
    # (2026-09-25: 4 of 5 blocks in a user-driven terraform roll). Deterministic,
    # not a judge instruction. `unverified` still runs - their output is now
    # evidence - and so does `promise`.
    if ran:
        failed = [(k, p) for k, p in failed if k != "unanswered"]
    # The harness says work is in flight AND the message says it is not finished:
    # that is a paused session reporting itself, so `promise` alone cannot block.
    paused = bool(bg) and bool(PAUSED.search(message))
    if paused and any(k == "promise" for k, _ in failed):
        failed = [(k, p) for k, p in failed if k != "promise"]
        gated = "paused"
    else:
        gated = None
    if not failed:
        jev.record("stop_check", "pass", a, note=gated or ("user_shell" if ran else None), in_flight=len(bg) or None)
        return
    names = {"promise": "the message promises work not yet done",
             "unanswered": "the message does not answer what the user last asked",
             "unverified": "the message states an outcome the tool results do not show"}
    parts = [f"{names[k]} (p={p:.2f}): \"{sentence_for(k, message)[:200]}\"" for k, p in failed]
    reason = ("jev-hooks stop check: " + " | ".join(parts) +
              ". Do the promised work now, answer the question, or show the evidence — or say plainly that it was not done and why. Do not repeat the same message.")
    jev.record("stop_check", "block", a, note=", ".join(k for k, _ in failed), in_flight=len(bg) or None)
    # The dispute command goes to the USER (systemMessage), never into `reason`,
    # which the model reads: a model told how to dispute its own blocks would
    # label them, and a label from the party being judged is not a label. Zero
    # disputes had ever been recorded (2026-09-22), largely because the command
    # was only in the README.
    wrong = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "wrong.py"))
    print(json.dumps({"decision": "block", "reason": reason,
                      "systemMessage": f"jev-hooks blocked ({', '.join(k for k, _ in failed)}). "
                                       f"If that was wrong: python3 {wrong} stop_check \"why\""}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook bug must never trap a session
