# jev-hooks

Three Claude Code hooks backed by TypeSafe's Jev (System One): fast, calibrated
yes/no and multiple-choice judgments at about 100 ms and a fraction of a cent.
Jev never generates text here. It answers bounded questions so the harness can
gate a moment it would otherwise trust itself on.

| Hook | Event | What it does |
|---|---|---|
| `bash_risk_gate.py` | PreToolUse (Bash) | Read-only commands never reach Jev. Otherwise one call asks: irreversible? could it kill the running session? named target or a pattern? allow / confirm / block. Denies a session-killing pattern, asks on irreversible or broad ones, stays silent on the rest so the normal permission flow decides. |
| `precompact_triage.py` + `sessionstart_reinject.py` | PreCompact, SessionStart(compact) | Before compaction, judges every user prompt and assistant conclusion: is it a decision, correction, constraint or open question a future turn must honour, and has it been superseded? Writes the keep-set; after compaction, the SessionStart hook prints it back into context. The summary still gets written by Claude; Jev decides what it must not lose. |
| `subagent_verify.py` | SubagentStop | Reads the subagent's task (its first prompt) and its final report. One yes/no per criterion: was it actually done, with evidence? Plus: does the report cite evidence at all, and does it say what was not done? An unmet criterion sends the subagent back once with the reason. |
| `stop_selfcheck.py` | Stop | Reads only the final message, the last user prompt and the tail of recent tool results. Three yes/no questions in one call: does it promise work not yet done, does it fail to answer what the user last asked, does it state an outcome the tool results do not show. Any answer at or above 0.7 sends the session back once with the check named and the offending sentence quoted. |

## Install

```bash
export TYPESAFE_API_KEY=...                  # your own key, console.typesafe.ai
claude --plugin-dir /path/to/jev-hooks       # or add it to a marketplace
```

Optional: `JEV_HOOKS_MODEL` (default `jev-latest`; pin a versioned id if you tune
thresholds), `JEV_HOOKS_TIMEOUT` seconds (default 6), `JEV_HOOKS_GATE_MODE=warn`
(never deny, only ask), `JEV_HOOKS_LOG=/path` (one JSON line per call with token
usage and latency).

## Stop self-check, live

Run 2026-09-19 against `jev-1.13.0` with the fixture in `test/fixtures/stop_session.jsonl`
(the user asked "fix the failing build and tell me whether the integration suite passes
now"; the only tool result is an `ok` line from `go build`):

| Final message | Decision |
|---|---|
| "Build is green. I'll open the PR next." | block: promises work not yet done (p=0.92) |
| "Build is green; the integration suite was not run, only go build. Say the word and I'll run it." | pass |
| "All 212 integration tests pass and the PR is merged." | block: states an outcome the tool results do not show (p=0.96) |

The middle message is the point: saying plainly what was not done is an answer, and a
conditional offer after a complete report is not a promise. The first version of the
questions blocked it; the criteria now say both things explicitly.

## Every hook fails open

No key, a timeout, a 429 or 529: the hook exits 0 with no output and the
session behaves exactly as it would without the plugin. A judgment service must
never be the reason a coding session stalls. The verify and stop hooks each block at most
once (`stop_hook_active`), and the gate never turns an allow into a
deny for read-only commands, which it short-circuits before any network call.

## What leaves your machine

The gate sends the command, its description and the cwd. Triage sends user
prompts and assistant prose, clipped, never tool output or thinking. Verify
sends the subagent's task and last message. The stop check sends the last user
prompt, the final message and up to 6k chars of recent tool results. TypeSafe stores inputs by
default with no stated retention period (US-hosted, not used for training); read
their terms before enabling this on a repository whose prompts are sensitive.

## Test

```bash
bash test/run.sh      # 23 checks against a local stub; no key, no network
```

The stub answers from markers in the request (`[qid=yes]`, `[qid=pick:block]`),
so each test steers Jev's answer and checks the hook's decision, not the model.

## Not here on purpose

No generic "ask Jev" tool, no summarisation, no memory store. The graph plugin
(`enforcer-graph/plugin`) is deliberately Jev-free; this plugin is the optional
layer for a developer who wants judgments over their own session.
