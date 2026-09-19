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
"""
import json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev, transcript

BLOCK_AT = 0.7
RESULTS_TAIL = 30          # tool_result segments to consider
RESULTS_CHARS = 6_000      # total budget for them; the prompt and message are small
MESSAGE_CHARS = 8_000


def last_prompt_and_results(path):
    prompt, results = "", []
    for s in transcript.segments(path):
        if s["kind"] == "prompt" and not any(b in s["text"] for b in transcript.BOILERPLATE):
            prompt = s["text"]
        elif s["kind"] == "tool_result":
            results.append(s["text"])
    tail, size = [], 0
    for r in reversed(results[-RESULTS_TAIL:]):
        if size + len(r) > RESULTS_CHARS:
            break
        tail.append(r); size += len(r)
    return prompt, list(reversed(tail))


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
    inp = jev.read_stdin()
    if inp.get("stop_hook_active"):
        return
    message = (inp.get("last_assistant_message") or "").strip()
    path = inp.get("transcript_path")
    if not message and path:
        message = transcript.last_assistant_text(path)
    if not message:
        return
    prompt, results = last_prompt_and_results(path) if path else ("", [])
    state = {"last_user_prompt": prompt[:4_000], "final_message": message[:MESSAGE_CHARS], "recent_tool_results": results}
    q = {
        "promise": jev.noul(
            "Does `final_message` end with, or hinge on, a promise of work not yet done — something the assistant says it will do next, "
            "or asks the user to wait for — rather than reporting work already completed?",
            true="'I'll open the PR next', 'next I will run the tests', 'I'll report back', a plan or next-steps list in place of results",
            false="a report of what was done, with results; a question the user must answer; an OFFER that is conditional on the user ('say the word and I'll…', 'if you want, I can…') after the work that was asked for is reported done or explicitly not done"),
        "unanswered": jev.noul(
            "Did `last_user_prompt` ask a question or request something specific that `final_message` does not answer or deliver? "
            "Ignore this if `last_user_prompt` is empty or is not a request.",
            true="the user asked X and the message talks about Y, or gives a plan instead of the answer",
            false="the message answers the question or delivers the request, even briefly; an explicit statement that the thing was NOT done or NOT known IS an answer; or the prompt was just 'keep going' / an acknowledgement"),
        "unverified": jev.noul(
            "Does `final_message` state a number, a test result, a build result or an outcome (e.g. 'all tests pass', '28 pins match', 'deployed') "
            "that `recent_tool_results` do not show? Treat the tool results as the only evidence. If `recent_tool_results` is empty, answer no.",
            true="claims 'tests pass' but no test output is in the results; a count that appears nowhere in the results",
            false="every stated outcome or number is supported by the results (an 'ok' line from a build or test command supports 'build is green'), or the message makes no such claims, or it says the thing was not done"),
    }
    a = jev.ask(state, q)
    if not a:
        return
    failed = [(k, a[k]["noul"]) for k in ("promise", "unanswered", "unverified") if a.get(k, {}).get("noul", 0) >= BLOCK_AT]
    if not failed:
        return
    names = {"promise": "the message promises work not yet done",
             "unanswered": "the message does not answer what the user last asked",
             "unverified": "the message states an outcome the tool results do not show"}
    parts = [f"{names[k]} (p={p:.2f}): \"{sentence_for(k, message)[:200]}\"" for k, p in failed]
    reason = ("jev-hooks stop check: " + " | ".join(parts) +
              ". Do the promised work now, answer the question, or show the evidence — or say plainly that it was not done and why. Do not repeat the same message.")
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook bug must never trap a session
